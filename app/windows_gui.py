from __future__ import annotations

import copy
import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import threading
from pathlib import Path
from typing import Any, Dict, List, Tuple

from app.api_server import handle_generate
from app.auto_scheduler import (
    auto_reconfigure_schedule,
    explain_infeasibility,
    generate_three_week_schedule,
    generate_eval_schedule,
    validate_requirement,
)
from app.health_check import run_health_check
from app.persistence import (
    load_last_generated_schedule,
    load_last_profile,
    load_provider_catalog_entries,
    load_provider_profiles,
    load_requirements_catalog,
    load_room_rules,
    save_last_generated_schedule,
    save_last_profile,
    save_last_schedule,
    save_provider_catalog_entries,
    save_provider_profiles,
    save_requirements_catalog,
    save_room_rules,
)
from app.profile_io import load_profile, save_profile, validate_profile
from app.provider_catalog import normalize_provider_catalog, provider_is_available
from app.room_rules import room_is_available
from app.windows_program import save_json
from app.schedule_exports import (
    build_schedule_layout_model,
    draw_layout_on_tk_canvas,
    export_layout_to_pptx,
    render_layout_to_png,
)

DISCIPLINES = [
    "Accupuncture",
    "Art therapy group",
    "Athletic Trainer",
    "Audiology",
    "Behavioral Health",
    "Dietician",
    "Equine Therapy",
    "Evaluation Group",
    "Moral Injury",
    "Neuropsychology",
    "PT/Audiology Group",
    "Pharmacology",
    "Physical Therapy",
    "Primary Care",
    "Psychiatry",
    "Reading Group",
    "Sleep group",
    "Speech-Language Pathology",
    "Supplements group",
    "Writing group",
    "Yoga",
]

PREDEFINED_ROOMS = [
    "Audiology Room",
    "Brittany's Office",
    "Conference Room",
    "Gym",
    "Jason's Office",
    "Lounge",
    "Off-site",
    "Room 1",
    "Room 2",
    "Room 207",
    "Room 208",
    "Room 3",
    "Room 4",
    "Suite 1",
    "Suite 2",
    "Suite 3",
    "VNG Room",
]

DEFAULT_PROVIDER_NAMES = [
    "Ali Giacona",
    "Carter Smith",
    "Christine Flicek",
    "Dana Lebo",
    "Daniel Fenton",
    "Devon Weist",
    "Elizabeth Lewis",
    "Elizabeth Watt",
    "Equine",
    "Evan Vitello",
    "Heidi Greata",
    "Lisa Padgett",
    "Michelle Ward",
    "Robert Kanser",
    "Sarah Teague",
    "Shawn Kane",
    "Wesley Cole",
]

DISCIPLINE_COLORS = {
    "Primary Care": "#7cb5ec",
    "Physical Therapy": "#f6c85f",
    "Speech-Language Pathology": "#9fd356",
    "Athletic Trainer": "#8dd3c7",
    "Dietician": "#ffd166",
    "Neuropsychology": "#f4978e",
    "Psychiatry": "#a1c181",
    "Behavioral Health": "#f4a261",
    "Moral Injury": "#4895ef",
    "Reading Group": "#00b4d8",
    "Accupuncture": "#cdb4db",
    "Equine Therapy": "#d4a373",
    "Pharmacology": "#ff8fab",
    "Art therapy group": "#b8c0ff",
    "Sleep group": "#90e0ef",
    "Writing group": "#a3cef1",
    "Supplements group": "#d9ed92",
    "PT/Audiology Group": "#80ed99",
    "Audiology": "#94d2bd",
    "Yoga": "#f9c74f",
}

GRID_START_MINUTE = 7 * 60 + 30
GRID_END_MINUTE = 18 * 60
GRID_SLOT_MINUTES = 15
IOP_PATIENT_ID_CHOICES = [f"I{i}" for i in range(1, 31)]
EVAL_PATIENT_ID_CHOICES = [f"E{i}" for i in range(1, 11)]
PATIENT_ID_CHOICES = IOP_PATIENT_ID_CHOICES + EVAL_PATIENT_ID_CHOICES


@dataclass
class DashboardSummary:
    assignment_count: int
    room_count: int
    booked_slot_count: int


def summarize_schedule(result: Dict[str, Any]) -> DashboardSummary:
    assignments = result.get("assignments", {})
    room_timeline = result.get("room_timeline", {})
    booked_slot_count = sum(
        1 for slots in room_timeline.values() for slot in slots if slot.get("status") == "booked"
    )
    return DashboardSummary(len(assignments), len(room_timeline), booked_slot_count)


def _safe_tk_import():
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext, ttk

    return tk, ttk, messagebox, scrolledtext, filedialog


def _to_ampm(minute: int) -> str:
    hour_24 = minute // 60
    minute_of_hour = minute % 60
    period = "AM" if hour_24 < 12 else "PM"
    hour_12 = hour_24 % 12 or 12
    return f"{hour_12}:{minute_of_hour:02d} {period}"


def _grid_minutes() -> List[int]:
    return list(range(GRID_START_MINUTE, GRID_END_MINUTE, GRID_SLOT_MINUTES))


def date_to_key(d: date) -> str:
    return d.isoformat()


def parse_date_parts(year: str, month: str, day: str) -> date:
    return date(int(year), int(month), int(day))


def planning_dates(start: date, weeks: int = 3) -> List[str]:
    out: List[str] = []
    current = start
    while len(out) < weeks * 5:
        if current.weekday() < 5:
            out.append(date_to_key(current))
        current += timedelta(days=1)
    return out


def parse_time_input(raw: str) -> int:
    value = raw.strip()
    if not value:
        raise ValueError("Time is required. Use HHMM, e.g., 0730 or 1600")
    if not value.isdigit():
        raise ValueError(f"Invalid time '{raw}'. Use HHMM, e.g., 0730 or 1600")
    if len(value) not in (3, 4):
        raise ValueError(f"Invalid time '{raw}'. Use 3 or 4 digits in HHMM format")
    if len(value) == 3:
        value = "0" + value

    hour = int(value[:2])
    minute = int(value[2:])
    if hour > 23 or minute > 59:
        raise ValueError(f"Invalid military time '{raw}'. Hour must be 00-23 and minute 00-59")
    if minute % GRID_SLOT_MINUTES != 0:
        raise ValueError(f"Time '{raw}' must be on a 15-minute boundary (00, 15, 30, 45)")
    return hour * 60 + minute


def military_time_choices(start_minute: int = GRID_START_MINUTE, end_minute: int = GRID_END_MINUTE) -> List[str]:
    return [f"{m // 60:02d}{m % 60:02d}" for m in range(start_minute, end_minute + GRID_SLOT_MINUTES, GRID_SLOT_MINUTES)]


def is_overlap(start_a: int, end_a: int, start_b: int, end_b: int) -> bool:
    return not (end_a <= start_b or start_a >= end_b)


def build_provider_records(provider_names: List[str], day_start: int, day_end: int) -> List[Dict[str, Any]]:
    templates = [
        {"weekday": weekday, "windows": [{"start_minute": day_start, "end_minute": day_end}]}
        for weekday in range(5)
    ]
    return [
        {
            "id": name,
            "name": name,
            "disciplines": list(DISCIPLINES),
            "templates": templates,
            "exceptions": [],
            "allowed_rooms": list(PREDEFINED_ROOMS),
        }
        for name in provider_names
    ]



def build_provider_records_from_profiles(provider_profiles: List[Dict[str, Any]], day_start: int, day_end: int) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for profile in provider_profiles:
        pid = str(profile.get("provider_id") or profile.get("id") or profile.get("provider_name", "")).strip()
        name = str(profile.get("provider_name") or profile.get("name") or pid).strip()
        if not pid:
            continue

        raw_templates = profile.get("availability_templates") or profile.get("templates")
        templates: List[Dict[str, Any]] = []
        if isinstance(raw_templates, list) and raw_templates:
            for t in raw_templates:
                templates.append(
                    {
                        "weekday": int(t["weekday"]),
                        "windows": [
                            {"start_minute": int(w["start_minute"]), "end_minute": int(w["end_minute"])}
                            for w in t.get("windows", [])
                        ],
                    }
                )
        else:
            templates = [
                {"weekday": weekday, "windows": [{"start_minute": day_start, "end_minute": day_end}]}
                for weekday in range(5)
            ]

        raw_exceptions = profile.get("exceptions", [])
        exceptions = []
        for ex in raw_exceptions:
            date_key = ex.get("date") or ex.get("date_key")
            if not date_key:
                continue
            if ex.get("window"):
                start_val = int(ex["window"].get("start_minute", day_start))
                end_val = int(ex["window"].get("end_minute", day_end))
            else:
                start_val = int(ex.get("start_minute", day_start))
                end_val = int(ex.get("end_minute", day_end))
            kind = str(ex.get("kind") or "").strip().lower()
            available_override = bool(ex.get("available_override", False)) or kind == "added"
            exceptions.append(
                {
                    "date_key": str(date_key),
                    "window": {"start_minute": start_val, "end_minute": end_val},
                    "available_override": available_override,
                }
            )

        discipline_list = [str(d).strip() for d in (profile.get("disciplines") or []) if str(d).strip()]
        if not discipline_list:
            discipline = str(profile.get("discipline", "")).strip()
            discipline_list = [discipline] if discipline else []
        disciplines = sorted(dict.fromkeys(discipline_list), key=lambda x: x.split()[0].lower()) if discipline_list else list(DISCIPLINES)
        allowed_rooms = profile.get("allowed_rooms") or ["Any compatible room"]
        if "Any compatible room" in allowed_rooms:
            allowed_rooms = list(PREDEFINED_ROOMS)
        else:
            allowed_rooms = sorted(dict.fromkeys([r for r in allowed_rooms if r in PREDEFINED_ROOMS]), key=lambda x: x.split()[0].lower())

        records.append(
            {
                "id": pid,
                "name": name,
                "disciplines": disciplines,
                "templates": templates,
                "exceptions": exceptions,
                "allowed_rooms": allowed_rooms,
            }
        )

    return records

def build_room_records(room_rules: Dict[str, Any] | None = None) -> List[Dict[str, Any]]:
    rules = room_rules or {"rooms": {}}
    out: List[Dict[str, Any]] = []
    for room in PREDEFINED_ROOMS:
        rr = (rules.get("rooms", {}) or {}).get(room, {})
        out.append(
            {
                "id": room,
                "name": room,
                "capacity": 10,
                "allowed_disciplines": list(DISCIPLINES),
                "unavailable_weekly": rr.get("unavailable_weekly", {}),
                "unavailable_dates": _date_rule_list_to_map(rr.get("unavailable_dates", [])),
                "available_only_weekly": rr.get("available_only_weekly", {}),
                "available_only_dates": _date_rule_list_to_map(rr.get("available_only_dates", [])),
            }
        )
    return out




def _date_rule_list_to_map(items: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, int]]]:
    by_date: Dict[str, List[Dict[str, int]]] = {}
    for item in items or []:
        date_key = str(item.get("date") or "").strip()
        if not date_key:
            continue
        by_date.setdefault(date_key, []).append(
            {"start_minute": int(item.get("start_minute", 0)), "end_minute": int(item.get("end_minute", 0))}
        )
    return by_date

def build_patient_records(date_keys: List[str], day_start: int, day_end: int) -> List[Dict[str, Any]]:
    availability = {d: [{"start_minute": day_start, "end_minute": day_end}] for d in date_keys}
    return [{"id": pid, "name": f"Patient {pid}", "availability": availability} for pid in PATIENT_ID_CHOICES]




def build_provider_availability_preview_data(provider_profile: Dict[str, Any], day_start: int, day_end: int) -> Dict[int, List[Tuple[int, int]]]:
    preview: Dict[int, List[Tuple[int, int]]] = {i: [] for i in range(5)}
    templates = provider_profile.get("availability_templates") or provider_profile.get("templates") or []
    for t in templates:
        wd = int(t.get("weekday", -1))
        if wd < 0 or wd > 4:
            continue
        for w in t.get("windows", []):
            start = max(day_start, int(w.get("start_minute", day_start)))
            end = min(day_end, int(w.get("end_minute", day_end)))
            if end > start:
                preview[wd].append((start, end))
        preview[wd].sort()
    return preview

def build_live_result_from_profile(profile: Dict[str, Any]) -> Dict[str, Any]:
    assignments: Dict[str, Dict[str, Any]] = {}
    for req in profile.get("requests", []):
        req_id = req.get("id")
        window = req.get("preferred_window") or {}
        if not req_id:
            continue
        assignments[req_id] = {
            "request_id": req_id,
            "provider_id": req.get("provider_id", "unassigned_provider"),
            "room_id": req.get("room_id", "unassigned_room"),
            "start_minute": int(window.get("start_minute", GRID_START_MINUTE)),
            "end_minute": int(window.get("end_minute", GRID_START_MINUTE + GRID_SLOT_MINUTES)),
            "label": req.get("label") or req.get("discipline", "Session"),
            "mode": req.get("mode", "individual"),
            "date_key": req.get("date_key"),
            "program_type": req.get("program_type", "IOP"),
            "soft_locked": bool(req.get("soft_locked", False)),
            "appointment_id": req.get("appointment_id") or req_id,
        }
    return {"assignments": assignments, "room_timeline": {}}


def build_patient_grid_data(
    profile: Dict[str, Any],
    result: Dict[str, Any],
    view_date_key: str | None = None,
) -> Tuple[List[int], List[str], Dict[Tuple[int, str], Dict[str, str]]]:
    request_map = {r["id"]: r for r in profile.get("requests", []) if "id" in r}
    patient_ids = sorted(
        {pid for req in request_map.values() for pid in req.get("patient_ids", [])},
        key=lambda v: int(v) if str(v).isdigit() else str(v),
    )
    patient_labels = [str(pid) for pid in patient_ids]

    cell_map: Dict[Tuple[int, str], Dict[str, str]] = {}
    for assignment in result.get("assignments", {}).values():
        req = request_map.get(assignment.get("request_id", ""), {})
        if view_date_key and req.get("date_key") != view_date_key:
            continue
        discipline = req.get("discipline", "Other")
        label = assignment.get("label") or discipline
        for minute in range(int(assignment["start_minute"]), int(assignment["end_minute"]), GRID_SLOT_MINUTES):
            if minute < GRID_START_MINUTE or minute >= GRID_END_MINUTE:
                continue
            for patient_id in req.get("patient_ids", []):
                cell_map[(minute, str(patient_id))] = {"discipline": discipline, "label": label}

    return _grid_minutes(), patient_labels, cell_map


class SchedulerDesktopApp:
    def __init__(self) -> None:
        tk, ttk, messagebox, scrolledtext, filedialog = _safe_tk_import()
        self.tk = tk
        self.ttk = ttk
        self.messagebox = messagebox
        self.scrolledtext = scrolledtext
        self.filedialog = filedialog

        self.root = tk.Tk()
        self.root.title("Therapy Scheduler - Visual Planner")
        self.root.geometry("1380x900")

        saved_profiles_payload = load_provider_profiles().get("providers", [])
        provider_seed = saved_profiles_payload if saved_profiles_payload else load_provider_catalog_entries(DEFAULT_PROVIDER_NAMES)
        self.provider_profiles = normalize_provider_catalog(
            provider_seed,
            defaults=DEFAULT_PROVIDER_NAMES,
            all_rooms=PREDEFINED_ROOMS,
            disciplines=DISCIPLINES,
        )
        self.provider_catalog = [p["provider_name"] for p in self.provider_profiles]
        self._persist_provider_catalog()
        self.room_rules = load_room_rules(PREDEFINED_ROOMS)
        conf = self.room_rules.setdefault("rooms", {}).setdefault(
            "Conference Room",
            {"unavailable_weekly": {i: [] for i in range(5)}, "unavailable_dates": [], "available_only_weekly": {i: [] for i in range(5)}, "available_only_dates": []},
        )
        for wd in [0, 1]:
            windows = conf.setdefault("unavailable_weekly", {}).setdefault(wd, [])
            if not any(int(w.get("start_minute", -1)) == 8 * 60 + 30 and int(w.get("end_minute", -1)) == 11 * 60 for w in windows):
                windows.append({"start_minute": 8 * 60 + 30, "end_minute": 11 * 60})
        save_room_rules(self.room_rules, valid_rooms=PREDEFINED_ROOMS)
        self.last_generated_schedule = load_last_generated_schedule()
        self.last_result: Dict[str, Any] | None = None
        requirements_payload = load_requirements_catalog()
        self.auto_conditions: List[Dict[str, Any]] = []
        self.eval_conditions: List[Dict[str, Any]] = []
        for condition in requirements_payload.get("iop_requirements", []):
            try:
                self.auto_conditions.append(validate_requirement(condition))
            except Exception:
                continue
        for condition in requirements_payload.get("eval_requirements", []):
            try:
                self.eval_conditions.append(validate_requirement(condition))
            except Exception:
                continue
        self.auto_reconfigure_history: List[List[Dict[str, Any]]] = []
        self.loaded_profile: Dict[str, Any] | None = load_last_profile()
        self.loaded_profile_path: Path | None = None
        self.manual_undo_stack: List[Dict[str, Any]] = []
        self.selected_request_id: str | None = None

        self._build_layout()

        if self.loaded_profile:
            self._sync_profile_resources(self.loaded_profile)
            self.status_var.set("Status: Restored last profile")
            self.profile_var.set("Profile: restored from data/last_profile.json")
            self._refresh_profile_preview()

    def _build_layout(self) -> None:
        tk = self.tk
        ttk = self.ttk

        main = ttk.Frame(self.root, padding=10)
        main.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(main)
        header.pack(fill=tk.X)
        ttk.Label(header, text="Therapy Day Scheduler", font=("Segoe UI", 20, "bold")).pack(anchor="w")

        controls = ttk.Frame(main)
        controls.pack(fill=tk.X)
        buttons = [
            ("Health Check", self.run_health_check),
            ("New Blank Profile", self.new_blank_profile),
            ("Load Profile", self.load_profile_from_file),
            ("Generate Day", self.generate_from_loaded_profile),
            ("Export Schedule", self.export_schedule),
            ("Save Profile", self.save_current_profile),
        ]
        for idx, (label, handler) in enumerate(buttons):
            ttk.Button(controls, text=label, command=lambda h=handler: self._safe_action(h)).grid(row=0, column=idx, padx=3, pady=4, sticky="w")

        self.status_var = tk.StringVar(value="Status: Ready")
        ttk.Label(controls, textvariable=self.status_var).grid(row=1, column=0, columnspan=6, sticky="w", padx=6)
        self.profile_var = tk.StringVar(value="Profile: (none loaded)")
        ttk.Label(controls, textvariable=self.profile_var).grid(row=2, column=0, columnspan=6, sticky="w", padx=6)

        notebook = ttk.Notebook(main)
        notebook.pack(fill=tk.BOTH, expand=True, pady=(8, 8))
        self.main_notebook = notebook

        manual_tab = ttk.Frame(notebook)
        self.manual_tab = manual_tab
        notebook.add(manual_tab, text="Manual Scheduler")

        auto_tab = ttk.Frame(notebook)
        notebook.add(auto_tab, text="IOP Generator (3-week)")

        eval_tab = ttk.Frame(notebook)
        notebook.add(eval_tab, text="EVAL Generator")

        provider_tab = ttk.Frame(notebook)
        notebook.add(provider_tab, text="Provider Profiles")

        room_rules_tab = ttk.Frame(notebook)
        notebook.add(room_rules_tab, text="Room Rules")

        upper = ttk.Panedwindow(manual_tab, orient=tk.HORIZONTAL)
        upper.pack(fill=tk.BOTH, expand=True, pady=(0, 8))

        form_wrap = ttk.Labelframe(upper, text="Inputs", padding=8)
        upper.add(form_wrap, weight=2)
        self._build_form_panel(form_wrap)

        preview_wrap = ttk.Labelframe(upper, text="Profile Summary", padding=8)
        upper.add(preview_wrap, weight=1)
        self.summary_text = self.scrolledtext.ScrolledText(preview_wrap, height=22, wrap=tk.WORD, font=("Consolas", 10))
        self.summary_text.pack(fill=tk.BOTH, expand=True)
        self.summary_text.insert(tk.END, "Create or load a profile to begin.\n")
        self.summary_text.configure(state=tk.DISABLED)

        grid_wrap = ttk.Labelframe(manual_tab, text="Patient Schedule Grid", padding=8)
        grid_wrap.pack(fill=tk.BOTH, expand=True)

        grid_container = ttk.Frame(grid_wrap)
        grid_container.pack(fill=tk.BOTH, expand=True)

        self.grid_canvas = tk.Canvas(grid_container, bg="white", highlightthickness=0)
        self.grid_canvas.grid(row=0, column=0, rowspan=2, sticky="nsew")

        yscroll = ttk.Scrollbar(grid_container, orient=tk.VERTICAL, command=self.grid_canvas.yview)
        yscroll.grid(row=0, column=1, rowspan=2, sticky="ns")
        xscroll = ttk.Scrollbar(grid_container, orient=tk.HORIZONTAL, command=self.grid_canvas.xview)
        xscroll.grid(row=2, column=0, sticky="ew")
        self.grid_canvas.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.grid_canvas.bind("<Button-3>", self._on_grid_right_click)

        grid_container.rowconfigure(0, weight=1)
        grid_container.columnconfigure(0, weight=1)

        arrow_frame = ttk.Frame(grid_container)
        arrow_frame.grid(row=0, column=2, sticky="ns", padx=(4, 0))
        ttk.Button(arrow_frame, text="▲", width=3, command=lambda: self.grid_canvas.yview_scroll(-6, "units")).pack(pady=4)
        ttk.Button(arrow_frame, text="▼", width=3, command=lambda: self.grid_canvas.yview_scroll(6, "units")).pack(pady=4)

        self.legend_var = tk.StringVar(value="Legend: Add appointments to visualize schedule.")
        ttk.Label(manual_tab, textvariable=self.legend_var).pack(anchor="w", pady=(6, 0))

        view_controls = ttk.Frame(manual_tab)
        view_controls.pack(fill=tk.X, pady=(4, 0))
        self.grid_mode_var = tk.StringVar(value="Patient Grid")
        self.program_filter_var = tk.StringVar(value="Both")
        ttk.Label(view_controls, text="Grid Mode").pack(side="left")
        ttk.Combobox(view_controls, textvariable=self.grid_mode_var, values=["Patient Grid", "Room Grid", "Provider Grid"], state="readonly", width=16).pack(side="left", padx=4)
        ttk.Label(view_controls, text="Program Filter").pack(side="left", padx=(10, 0))
        ttk.Combobox(view_controls, textvariable=self.program_filter_var, values=["Both", "IOP", "EVAL"], state="readonly", width=10).pack(side="left", padx=4)
        ttk.Button(view_controls, text="Apply View", command=lambda: self._safe_action(self.refresh_current_grid_view)).pack(side="left", padx=8)
        ttk.Button(view_controls, text="Export View as PNG", command=lambda: self._safe_action(self.export_view_as_png)).pack(side="left", padx=6)
        ttk.Button(view_controls, text="Export as PowerPoint", command=lambda: self._safe_action(self.export_view_as_pptx)).pack(side="left", padx=6)

        self._build_auto_generator_tab(auto_tab)
        self._build_eval_generator_tab(eval_tab)
        self._build_provider_profiles_tab(provider_tab)
        self._build_room_rules_tab(room_rules_tab)

    def _build_auto_generator_tab(self, parent) -> None:
        ttk = self.ttk
        today = datetime.utcnow().date()
        time_choices = military_time_choices()
        weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

        frame = ttk.Labelframe(parent, text="3-Week Schedule Ingredients", padding=10)
        frame.pack(fill="x", padx=6, pady=6)

        self.auto_patients_var = self.tk.StringVar(value="6")
        self.auto_start_year_var = self.tk.StringVar(value=str(today.year))
        self.auto_start_month_var = self.tk.StringVar(value=f"{today.month:02d}")
        self.auto_start_day_var = self.tk.StringVar(value=f"{today.day:02d}")

        ttk.Label(frame, text="Patient count").grid(row=0, column=0, sticky="w")
        ttk.Combobox(frame, textvariable=self.auto_patients_var, values=[str(i) for i in range(1, 31)], state="readonly", width=8).grid(row=1, column=0, padx=2)

        ttk.Label(frame, text="Start Year").grid(row=0, column=1, sticky="w")
        ttk.Combobox(frame, textvariable=self.auto_start_year_var, values=[str(today.year + i) for i in range(0, 3)], state="readonly", width=8).grid(row=1, column=1, padx=2)
        ttk.Label(frame, text="Start Month").grid(row=0, column=2, sticky="w")
        ttk.Combobox(frame, textvariable=self.auto_start_month_var, values=[f"{m:02d}" for m in range(1, 13)], state="readonly", width=6).grid(row=1, column=2, padx=2)
        ttk.Label(frame, text="Start Day").grid(row=0, column=3, sticky="w")
        ttk.Combobox(frame, textvariable=self.auto_start_day_var, values=[f"{d:02d}" for d in range(1, 32)], state="readonly", width=6).grid(row=1, column=3, padx=2)

        ttk.Separator(parent, orient="horizontal").pack(fill="x", padx=6, pady=6)

        cond = ttk.Labelframe(parent, text="Requirements Builder", padding=10)
        cond.pack(fill="x", padx=6, pady=6)

        self.auto_req_id_var = self.tk.StringVar(value="req_1")
        self.auto_provider_var = self.tk.StringVar(value="Any provider")
        self.auto_provider_b_var = self.tk.StringVar(value="(none)")
        self.auto_provider_c_var = self.tk.StringVar(value="(none)")
        self.auto_room_var = self.tk.StringVar(value="Any compatible room")
        self.auto_discipline_var = self.tk.StringVar(value=DISCIPLINES[1])
        self.auto_mode_var = self.tk.StringVar(value="individual")
        self.auto_scope_var = self.tk.StringVar(value="all")
        self.auto_subset_patients_var = self.tk.StringVar(value="")
        self.auto_duration_var = self.tk.StringVar(value="60")
        self.auto_sessions_per_week_var = self.tk.StringVar(value="1")
        self.auto_weekday_1_var = self.tk.StringVar(value="Monday")
        self.auto_weekday_2_var = self.tk.StringVar(value="Wednesday")
        self.auto_weekday_3_var = self.tk.StringVar(value="(none)")
        self.auto_weekday_4_var = self.tk.StringVar(value="(none)")
        self.auto_weekday_5_var = self.tk.StringVar(value="(none)")
        self.auto_week_1_var = self.tk.BooleanVar(value=True)
        self.auto_week_2_var = self.tk.BooleanVar(value=True)
        self.auto_week_3_var = self.tk.BooleanVar(value=True)
        self.auto_window_start_var = self.tk.StringVar(value="0800")
        self.auto_window_end_var = self.tk.StringVar(value="1400")
        self.auto_windows_var = self.tk.StringVar(value="")
        self.auto_hard_var = self.tk.BooleanVar(value=True)
        self.auto_priority_var = self.tk.StringVar(value="100")
        self.auto_solver_effort_var = self.tk.StringVar(value="High")

        provider_values = ["Any provider"] + self.provider_catalog
        provider_optional_values = ["(none)"] + self.provider_catalog
        room_values = ["Any compatible room"] + PREDEFINED_ROOMS

        ttk.Label(cond, text="Requirement ID").grid(row=0, column=0, sticky="w")
        ttk.Entry(cond, textvariable=self.auto_req_id_var, width=12).grid(row=1, column=0, padx=2)

        ttk.Label(cond, text="Discipline").grid(row=0, column=1, sticky="w")
        ttk.Combobox(cond, textvariable=self.auto_discipline_var, values=DISCIPLINES, state="readonly", width=20).grid(row=1, column=1, padx=2)

        ttk.Label(cond, text="Provider A").grid(row=0, column=2, sticky="w")
        self.auto_provider_combo = ttk.Combobox(cond, textvariable=self.auto_provider_var, values=provider_values, state="readonly", width=18)
        self.auto_provider_combo.grid(row=1, column=2, padx=2)

        ttk.Label(cond, text="Provider B").grid(row=0, column=3, sticky="w")
        self.auto_provider_b_combo = ttk.Combobox(cond, textvariable=self.auto_provider_b_var, values=provider_optional_values, state="readonly", width=18)
        self.auto_provider_b_combo.grid(row=1, column=3, padx=2)

        ttk.Label(cond, text="Provider C").grid(row=0, column=4, sticky="w")
        self.auto_provider_c_combo = ttk.Combobox(cond, textvariable=self.auto_provider_c_var, values=provider_optional_values, state="readonly", width=18)
        self.auto_provider_c_combo.grid(row=1, column=4, padx=2)

        ttk.Label(cond, text="Room").grid(row=0, column=5, sticky="w")
        self.auto_room_combo = ttk.Combobox(cond, textvariable=self.auto_room_var, values=room_values, state="readonly", width=18)
        self.auto_room_combo.grid(row=1, column=5, padx=2)

        ttk.Label(cond, text="Mode").grid(row=0, column=6, sticky="w")
        ttk.Combobox(cond, textvariable=self.auto_mode_var, values=["individual", "group"], state="readonly", width=11).grid(row=1, column=6, padx=2)

        ttk.Label(cond, text="Duration").grid(row=0, column=7, sticky="w")
        ttk.Combobox(cond, textvariable=self.auto_duration_var, values=[str(i) for i in range(15, 241, 15)], state="readonly", width=8).grid(row=1, column=7, padx=2)
        ttk.Label(cond, text="Sessions / week").grid(row=0, column=8, sticky="w")
        ttk.Combobox(cond, textvariable=self.auto_sessions_per_week_var, values=[str(i) for i in range(1, 6)], state="readonly", width=10).grid(row=1, column=8, padx=2)

        ttk.Label(cond, text="Patient Scope").grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Combobox(cond, textvariable=self.auto_scope_var, values=["all", "single", "subset"], state="readonly", width=12).grid(row=3, column=0, padx=2)
        ttk.Label(cond, text="Subset/Single IDs (comma)").grid(row=2, column=1, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Entry(cond, textvariable=self.auto_subset_patients_var, width=28).grid(row=3, column=1, columnspan=2, padx=2, sticky="w")

        ttk.Label(cond, text="Weekday 1").grid(row=2, column=3, sticky="w", pady=(6, 0))
        ttk.Combobox(cond, textvariable=self.auto_weekday_1_var, values=weekdays, state="readonly", width=12).grid(row=3, column=3, padx=2)
        ttk.Label(cond, text="Weekday 2").grid(row=2, column=4, sticky="w", pady=(6, 0))
        ttk.Combobox(cond, textvariable=self.auto_weekday_2_var, values=["(none)"] + weekdays, state="readonly", width=12).grid(row=3, column=4, padx=2)
        ttk.Label(cond, text="Weekday 3").grid(row=2, column=5, sticky="w", pady=(6, 0))
        ttk.Combobox(cond, textvariable=self.auto_weekday_3_var, values=["(none)"] + weekdays, state="readonly", width=12).grid(row=3, column=5, padx=2)
        ttk.Label(cond, text="Weekday 4").grid(row=2, column=6, sticky="w", pady=(6, 0))
        ttk.Combobox(cond, textvariable=self.auto_weekday_4_var, values=["(none)"] + weekdays, state="readonly", width=12).grid(row=3, column=6, padx=2)
        ttk.Label(cond, text="Weekday 5").grid(row=2, column=7, sticky="w", pady=(6, 0))
        ttk.Combobox(cond, textvariable=self.auto_weekday_5_var, values=["(none)"] + weekdays, state="readonly", width=12).grid(row=3, column=7, padx=2)

        week_frame = ttk.Frame(cond)
        week_frame.grid(row=4, column=6, columnspan=2, sticky="w")
        ttk.Checkbutton(week_frame, text="W1", variable=self.auto_week_1_var).pack(side="left")
        ttk.Checkbutton(week_frame, text="W2", variable=self.auto_week_2_var).pack(side="left")
        ttk.Checkbutton(week_frame, text="W3", variable=self.auto_week_3_var).pack(side="left")

        ttk.Label(cond, text="Appointment Window Start").grid(row=5, column=0, sticky="w", pady=(6, 0))
        ttk.Combobox(cond, textvariable=self.auto_window_start_var, values=time_choices, state="readonly", width=10).grid(row=6, column=0, padx=2)
        ttk.Label(cond, text="Appointment Window End").grid(row=5, column=1, sticky="w", pady=(6, 0))
        ttk.Combobox(cond, textvariable=self.auto_window_end_var, values=time_choices, state="readonly", width=10).grid(row=6, column=1, padx=2)
        ttk.Button(cond, text="Add Appointment Window", command=lambda: self._safe_action(self.add_requirement_window)).grid(row=6, column=2, padx=6)

        ttk.Label(cond, text="Appointment Windows").grid(row=5, column=3, sticky="w", pady=(6, 0))
        ttk.Entry(cond, textvariable=self.auto_windows_var, width=36).grid(row=6, column=3, columnspan=2, padx=2, sticky="w")

        ttk.Checkbutton(cond, text="Hard constraint", variable=self.auto_hard_var).grid(row=6, column=5, sticky="w")
        ttk.Label(cond, text="Priority").grid(row=5, column=6, sticky="w", pady=(6, 0))
        ttk.Combobox(cond, textvariable=self.auto_priority_var, values=["25", "50", "75", "100"], state="readonly", width=8).grid(row=6, column=6, padx=2)
        ttk.Label(cond, text="Solver Effort").grid(row=5, column=7, sticky="w", pady=(6, 0))
        ttk.Combobox(cond, textvariable=self.auto_solver_effort_var, values=["Standard", "High", "Very High", "Maximum"], state="readonly", width=12).grid(row=6, column=7, padx=2)

        ttk.Label(cond, text="Provider availability windows are managed in Provider List Management below.", foreground="#495057").grid(row=7, column=0, columnspan=8, sticky="w", pady=(6, 0))
        action_row = ttk.Frame(cond)
        action_row.grid(row=8, column=0, columnspan=8, sticky="w", pady=(8, 0))
        self.show_eval_requirements_var = self.tk.BooleanVar(value=False)
        ttk.Checkbutton(action_row, text="Show EVAL Requirements", variable=self.show_eval_requirements_var, command=lambda: self._safe_action(self._refresh_auto_condition_list)).pack(side="left", padx=4)
        ttk.Button(action_row, text="Add Requirement", command=lambda: self._safe_action(self.add_auto_condition)).pack(side="left", padx=4)
        ttk.Button(action_row, text="Edit Selected", command=lambda: self._safe_action(self.edit_selected_condition)).pack(side="left", padx=4)
        ttk.Button(action_row, text="Duplicate Selected", command=lambda: self._safe_action(self.duplicate_selected_condition)).pack(side="left", padx=4)
        ttk.Button(action_row, text="Remove Selected Requirement", command=lambda: self._safe_action(self.remove_selected_condition)).pack(side="left", padx=4)
        ttk.Button(action_row, text="Clear Requirements", command=lambda: self._safe_action(self.clear_auto_conditions)).pack(side="left", padx=4)

        list_frame = ttk.Labelframe(parent, text="Requirement List", padding=10)
        list_frame.pack(fill="both", expand=True, padx=6, pady=6)

        list_scroll = ttk.Scrollbar(list_frame, orient=self.tk.VERTICAL)
        self.auto_condition_list = self.tk.Listbox(list_frame, height=10, yscrollcommand=list_scroll.set)
        list_scroll.config(command=self.auto_condition_list.yview)
        self.auto_condition_list.pack(side="left", fill="both", expand=True)
        list_scroll.pack(side="right", fill="y")
        self.auto_condition_list.bind("<<ListboxSelect>>", self._on_requirement_select)
        self.auto_condition_view_index: List[Tuple[str, int]] = []

        output_frame = ttk.Labelframe(parent, text="Generation Status / Bottleneck Report", padding=10)
        output_frame.pack(fill="both", expand=True, padx=6, pady=6)
        self.auto_report_text = self.scrolledtext.ScrolledText(output_frame, height=8, wrap=self.tk.WORD)
        self.auto_report_text.pack(fill="both", expand=True)
        self.auto_report_text.insert(self.tk.END, "No report yet.\n")
        self.auto_report_text.configure(state=self.tk.DISABLED)

        action_frame = ttk.Frame(parent)
        action_frame.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Button(action_frame, text="Generate 3-week Schedule", command=lambda: self._safe_action(self.generate_auto_schedule)).pack(side="left", padx=4)
        ttk.Button(action_frame, text="Auto reconfigure existing schedule", command=lambda: self._safe_action(self.auto_reconfigure_existing_schedule)).pack(side="left", padx=4)
        ttk.Button(action_frame, text="Explain bottleneck", command=lambda: self._safe_action(self.explain_auto_bottleneck)).pack(side="left", padx=4)

        self._refresh_auto_condition_list()
    def _build_eval_generator_tab(self, parent) -> None:
        ttk = self.ttk
        time_choices = military_time_choices()
        weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
        frame = ttk.Labelframe(parent, text="3-Day EVAL Generator", padding=10)
        frame.pack(fill="x", padx=6, pady=6)

        self.eval_start_year_var = self.tk.StringVar(value=self.auto_start_year_var.get() if hasattr(self, "auto_start_year_var") else str(datetime.utcnow().year))
        self.eval_start_month_var = self.tk.StringVar(value=self.auto_start_month_var.get() if hasattr(self, "auto_start_month_var") else f"{datetime.utcnow().month:02d}")
        self.eval_start_day_var = self.tk.StringVar(value=self.auto_start_day_var.get() if hasattr(self, "auto_start_day_var") else f"{datetime.utcnow().day:02d}")
        self.eval_cohort_var = self.tk.StringVar(value="Mon-Wed")
        self.eval_patient_count_var = self.tk.StringVar(value="3")
        self.eval_group_start_var = self.tk.StringVar(value="0830")
        self.eval_group_duration_var = self.tk.StringVar(value="60")

        ttk.Label(frame, text="Cohort Start Year").grid(row=0, column=0, sticky="w")
        ttk.Combobox(frame, textvariable=self.eval_start_year_var, values=self.year_choices if hasattr(self, "year_choices") else [str(datetime.utcnow().year)], state="readonly", width=8).grid(row=1, column=0, padx=2)
        ttk.Label(frame, text="Month").grid(row=0, column=1, sticky="w")
        ttk.Combobox(frame, textvariable=self.eval_start_month_var, values=[f"{m:02d}" for m in range(1, 13)], state="readonly", width=6).grid(row=1, column=1, padx=2)
        ttk.Label(frame, text="Day").grid(row=0, column=2, sticky="w")
        ttk.Combobox(frame, textvariable=self.eval_start_day_var, values=[f"{d:02d}" for d in range(1, 32)], state="readonly", width=6).grid(row=1, column=2, padx=2)

        ttk.Label(frame, text="Cohort Type").grid(row=0, column=3, sticky="w")
        ttk.Combobox(frame, textvariable=self.eval_cohort_var, values=["Mon-Wed", "Tue-Thu"], state="readonly", width=10).grid(row=1, column=3, padx=2)
        ttk.Label(frame, text="EVAL Patients").grid(row=0, column=4, sticky="w")
        ttk.Combobox(frame, textvariable=self.eval_patient_count_var, values=[str(i) for i in range(1, 11)], state="readonly", width=8).grid(row=1, column=4, padx=2)

        ttk.Label(frame, text="Day-1 Group Start").grid(row=0, column=5, sticky="w")
        ttk.Combobox(frame, textvariable=self.eval_group_start_var, values=["0830", "0930", "1000"], state="readonly", width=8).grid(row=1, column=5, padx=2)
        ttk.Label(frame, text="Group Duration").grid(row=0, column=6, sticky="w")
        ttk.Combobox(frame, textvariable=self.eval_group_duration_var, values=[str(i) for i in range(30, 241, 15)], state="readonly", width=8).grid(row=1, column=6, padx=2)

        req = ttk.Labelframe(parent, text="EVAL Requirements Builder", padding=10)
        req.pack(fill="x", padx=6, pady=(0, 6))
        self.eval_req_id_var = self.tk.StringVar(value=f"eval_req_{len(self.eval_conditions)+1}")
        self.eval_discipline_var = self.tk.StringVar(value=DISCIPLINES[0])
        self.eval_provider_var = self.tk.StringVar(value="Any provider")
        self.eval_room_var = self.tk.StringVar(value="Any compatible room")
        self.eval_mode_var = self.tk.StringVar(value="individual")
        self.eval_duration_var = self.tk.StringVar(value="60")
        self.eval_scope_var = self.tk.StringVar(value="all")
        self.eval_subset_patients_var = self.tk.StringVar(value="")
        self.eval_weekday_1_var = self.tk.StringVar(value="Monday")
        self.eval_weekday_2_var = self.tk.StringVar(value="(none)")
        self.eval_weekday_3_var = self.tk.StringVar(value="(none)")
        self.eval_window_start_var = self.tk.StringVar(value="0830")
        self.eval_window_end_var = self.tk.StringVar(value="1700")
        self.eval_windows_var = self.tk.StringVar(value="")
        self.eval_hard_var = self.tk.BooleanVar(value=True)
        self.eval_priority_var = self.tk.StringVar(value="100")

        provider_values = ["Any provider"] + self.provider_catalog
        room_values = ["Any compatible room"] + PREDEFINED_ROOMS
        ttk.Label(req, text="Requirement ID").grid(row=0, column=0, sticky="w")
        ttk.Entry(req, textvariable=self.eval_req_id_var, width=14).grid(row=1, column=0, padx=2)
        ttk.Label(req, text="Discipline").grid(row=0, column=1, sticky="w")
        ttk.Combobox(req, textvariable=self.eval_discipline_var, values=DISCIPLINES, state="readonly", width=20).grid(row=1, column=1, padx=2)
        ttk.Label(req, text="Provider").grid(row=0, column=2, sticky="w")
        ttk.Combobox(req, textvariable=self.eval_provider_var, values=provider_values, state="readonly", width=18).grid(row=1, column=2, padx=2)
        ttk.Label(req, text="Room").grid(row=0, column=3, sticky="w")
        ttk.Combobox(req, textvariable=self.eval_room_var, values=room_values, state="readonly", width=18).grid(row=1, column=3, padx=2)
        ttk.Label(req, text="Mode").grid(row=0, column=4, sticky="w")
        ttk.Combobox(req, textvariable=self.eval_mode_var, values=["individual", "group"], state="readonly", width=10).grid(row=1, column=4, padx=2)
        ttk.Label(req, text="Duration").grid(row=0, column=5, sticky="w")
        ttk.Combobox(req, textvariable=self.eval_duration_var, values=[str(i) for i in range(15, 241, 15)], state="readonly", width=8).grid(row=1, column=5, padx=2)

        ttk.Label(req, text="Patient Scope").grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Combobox(req, textvariable=self.eval_scope_var, values=["all", "single", "subset"], state="readonly", width=12).grid(row=3, column=0, padx=2)
        ttk.Label(req, text="Subset/Single IDs").grid(row=2, column=1, sticky="w", pady=(6, 0))
        ttk.Entry(req, textvariable=self.eval_subset_patients_var, width=20).grid(row=3, column=1, padx=2)
        ttk.Label(req, text="Weekday 1").grid(row=2, column=2, sticky="w", pady=(6, 0))
        ttk.Combobox(req, textvariable=self.eval_weekday_1_var, values=weekdays, state="readonly", width=12).grid(row=3, column=2, padx=2)
        ttk.Label(req, text="Weekday 2").grid(row=2, column=3, sticky="w", pady=(6, 0))
        ttk.Combobox(req, textvariable=self.eval_weekday_2_var, values=["(none)"] + weekdays, state="readonly", width=12).grid(row=3, column=3, padx=2)
        ttk.Label(req, text="Weekday 3").grid(row=2, column=4, sticky="w", pady=(6, 0))
        ttk.Combobox(req, textvariable=self.eval_weekday_3_var, values=["(none)"] + weekdays, state="readonly", width=12).grid(row=3, column=4, padx=2)

        ttk.Label(req, text="Window Start").grid(row=4, column=0, sticky="w", pady=(6, 0))
        ttk.Combobox(req, textvariable=self.eval_window_start_var, values=time_choices, state="readonly", width=10).grid(row=5, column=0, padx=2)
        ttk.Label(req, text="Window End").grid(row=4, column=1, sticky="w", pady=(6, 0))
        ttk.Combobox(req, textvariable=self.eval_window_end_var, values=time_choices, state="readonly", width=10).grid(row=5, column=1, padx=2)
        ttk.Button(req, text="Add Appointment Window", command=lambda: self._safe_action(self.add_eval_requirement_window)).grid(row=5, column=2, padx=4)
        ttk.Label(req, text="Appointment Windows").grid(row=4, column=3, sticky="w", pady=(6, 0))
        ttk.Entry(req, textvariable=self.eval_windows_var, width=30).grid(row=5, column=3, columnspan=2, padx=2, sticky="w")
        ttk.Checkbutton(req, text="Hard constraint", variable=self.eval_hard_var).grid(row=5, column=5, sticky="w")
        ttk.Combobox(req, textvariable=self.eval_priority_var, values=["25", "50", "75", "100"], state="readonly", width=8).grid(row=5, column=6, padx=2)
        ttk.Label(req, text="Priority").grid(row=4, column=6, sticky="w", pady=(6, 0))

        eval_row = ttk.Frame(req)
        eval_row.grid(row=6, column=0, columnspan=7, sticky="w", pady=(8, 0))
        ttk.Button(eval_row, text="Add Requirement", command=lambda: self._safe_action(self.add_eval_condition)).pack(side="left", padx=4)
        ttk.Button(eval_row, text="Edit Selected", command=lambda: self._safe_action(self.edit_selected_eval_condition)).pack(side="left", padx=4)
        ttk.Button(eval_row, text="Remove Selected Requirement", command=lambda: self._safe_action(self.remove_selected_eval_condition)).pack(side="left", padx=4)

        eval_list_frame = ttk.Labelframe(parent, text="EVAL Requirement List", padding=8)
        eval_list_frame.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        eval_scroll = ttk.Scrollbar(eval_list_frame, orient=self.tk.VERTICAL)
        self.eval_condition_list = self.tk.Listbox(eval_list_frame, height=7, yscrollcommand=eval_scroll.set)
        eval_scroll.config(command=self.eval_condition_list.yview)
        self.eval_condition_list.pack(side="left", fill="both", expand=True)
        eval_scroll.pack(side="right", fill="y")
        self.eval_condition_list.bind("<<ListboxSelect>>", self._on_eval_requirement_select)
        self._refresh_eval_condition_list()

        actions = ttk.Frame(parent)
        actions.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Button(actions, text="Generate EVAL Schedule", command=lambda: self._safe_action(self.generate_eval_schedule)).pack(side="left", padx=4)
        ttk.Button(actions, text="Generate Combined Schedule", command=lambda: self._safe_action(self.generate_combined_schedule)).pack(side="left", padx=4)
        ttk.Button(actions, text="Import Existing Schedule JSON", command=lambda: self._safe_action(self.import_existing_schedule_json)).pack(side="left", padx=4)

        report = ttk.Labelframe(parent, text="EVAL / Combined Report", padding=10)
        report.pack(fill="both", expand=True, padx=6, pady=6)
        self.eval_report_text = self.scrolledtext.ScrolledText(report, height=10, wrap=self.tk.WORD)
        self.eval_report_text.pack(fill="both", expand=True)
        self.eval_report_text.insert(self.tk.END, "No EVAL generation run yet.\n")
        self.eval_report_text.configure(state=self.tk.DISABLED)

    def _build_provider_profiles_tab(self, parent) -> None:
        ttk = self.ttk
        tk = self.tk
        time_choices = military_time_choices()
        weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

        wrapper = ttk.Frame(parent, padding=8)
        wrapper.pack(fill="both", expand=True)
        wrapper.columnconfigure(0, weight=1)
        wrapper.columnconfigure(1, weight=2)
        wrapper.rowconfigure(0, weight=1)

        left = ttk.Labelframe(wrapper, text="Providers", padding=8)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        right = ttk.Labelframe(wrapper, text="Provider Profile", padding=8)
        right.grid(row=0, column=1, sticky="nsew")

        self.provider_search_var = tk.StringVar(value="")
        ttk.Label(left, text="Search").pack(anchor="w")
        search_entry = ttk.Entry(left, textvariable=self.provider_search_var)
        search_entry.pack(fill="x", pady=(0, 6))
        search_entry.bind("<KeyRelease>", lambda _e: self.refresh_provider_profile_list())

        list_scroll = ttk.Scrollbar(left, orient=tk.VERTICAL)
        self.provider_search_list = tk.Listbox(left, height=24, yscrollcommand=list_scroll.set)
        list_scroll.config(command=self.provider_search_list.yview)
        self.provider_search_list.pack(side="left", fill="both", expand=True)
        list_scroll.pack(side="right", fill="y")
        self.provider_search_list.bind("<<ListboxSelect>>", self._on_provider_profile_select)

        self.provider_profile_id_var = tk.StringVar(value="")
        self.provider_profile_name_var = tk.StringVar(value="")
        self.provider_profile_disciplines_var = tk.StringVar(value="")

        row = 0
        ttk.Label(right, text="Provider ID").grid(row=row, column=0, sticky="w")
        ttk.Entry(right, textvariable=self.provider_profile_id_var, state="readonly", width=24).grid(row=row, column=1, sticky="w", padx=4)
        row += 1
        ttk.Label(right, text="Provider Name").grid(row=row, column=0, sticky="w")
        ttk.Entry(right, textvariable=self.provider_profile_name_var, width=30).grid(row=row, column=1, sticky="w", padx=4)
        row += 1
        ttk.Label(right, text="Disciplines (up to 5, comma-separated)").grid(row=row, column=0, sticky="w")
        ttk.Entry(right, textvariable=self.provider_profile_disciplines_var, width=42).grid(row=row, column=1, sticky="w", padx=4)
        row += 1

        ttk.Label(right, text="Allowed Rooms").grid(row=row, column=0, sticky="nw", pady=(6, 0))
        rooms_frame = ttk.Frame(right)
        rooms_frame.grid(row=row, column=1, sticky="w", pady=(6, 0))
        self.allowed_room_vars = {"Any compatible room": tk.BooleanVar(value=True)}
        ttk.Checkbutton(rooms_frame, text="Any compatible room", variable=self.allowed_room_vars["Any compatible room"], command=self._on_allowed_room_toggle).grid(row=0, column=0, sticky="w")
        for idx, room in enumerate(PREDEFINED_ROOMS, start=1):
            var = tk.BooleanVar(value=False)
            self.allowed_room_vars[room] = var
            ttk.Checkbutton(rooms_frame, text=room, variable=var, command=self._on_allowed_room_toggle).grid(row=idx, column=0, sticky="w")
        row += 1

        avail = ttk.Labelframe(right, text="General Availability (Mon-Fri)", padding=6)
        avail.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.profile_avail_weekday_var = tk.StringVar(value="Monday")
        self.profile_avail_start_var = tk.StringVar(value="0730")
        self.profile_avail_end_var = tk.StringVar(value="1800")
        ttk.Combobox(avail, textvariable=self.profile_avail_weekday_var, values=weekdays, state="readonly", width=12).grid(row=0, column=0, padx=2)
        ttk.Combobox(avail, textvariable=self.profile_avail_start_var, values=time_choices, state="readonly", width=10).grid(row=0, column=1, padx=2)
        ttk.Combobox(avail, textvariable=self.profile_avail_end_var, values=time_choices, state="readonly", width=10).grid(row=0, column=2, padx=2)
        ttk.Button(avail, text="Add Window", command=lambda: self._safe_action(self.add_provider_profile_availability_window)).grid(row=0, column=3, padx=4)
        ttk.Button(avail, text="Remove Window", command=lambda: self._safe_action(self.remove_provider_profile_availability_window)).grid(row=0, column=4, padx=4)
        self.provider_availability_list = tk.Listbox(avail, height=6)
        self.provider_availability_list.grid(row=1, column=0, columnspan=5, sticky="ew", pady=(4, 0))
        row += 1

        ex = ttk.Labelframe(right, text="Exceptions", padding=6)
        ex.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.profile_exception_kind_var = tk.StringVar(value="unavailable")
        self.profile_exception_date_var = tk.StringVar(value=date_to_key(datetime.utcnow().date()))
        self.profile_exception_start_var = tk.StringVar(value="1200")
        self.profile_exception_end_var = tk.StringVar(value="1300")
        ttk.Combobox(ex, textvariable=self.profile_exception_kind_var, values=["unavailable", "added"], state="readonly", width=12).grid(row=0, column=0, padx=2)
        ttk.Entry(ex, textvariable=self.profile_exception_date_var, width=12).grid(row=0, column=1, padx=2)
        ttk.Combobox(ex, textvariable=self.profile_exception_start_var, values=time_choices, state="readonly", width=10).grid(row=0, column=2, padx=2)
        ttk.Combobox(ex, textvariable=self.profile_exception_end_var, values=time_choices, state="readonly", width=10).grid(row=0, column=3, padx=2)
        ttk.Button(ex, text="Add Exception", command=lambda: self._safe_action(self.add_provider_profile_exception)).grid(row=0, column=4, padx=4)
        ttk.Button(ex, text="Remove Exception", command=lambda: self._safe_action(self.remove_provider_profile_exception)).grid(row=0, column=5, padx=4)
        self.provider_exception_list = tk.Listbox(ex, height=5)
        self.provider_exception_list.grid(row=1, column=0, columnspan=6, sticky="ew", pady=(4, 0))
        row += 1

        self.provider_profile_preview_canvas = tk.Canvas(right, width=620, height=180, bg="white", highlightthickness=1, highlightbackground="#ced4da")
        self.provider_profile_preview_canvas.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        row += 1

        btns = ttk.Frame(right)
        btns.grid(row=row, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(btns, text="Save/Update", command=lambda: self._safe_action(self.save_selected_provider_profile)).pack(side="left", padx=4)
        ttk.Button(btns, text="Revert Changes", command=lambda: self._safe_action(self.revert_selected_provider_profile)).pack(side="left", padx=4)
        ttk.Button(btns, text="New Provider", command=lambda: self._safe_action(self.new_provider_profile)).pack(side="left", padx=4)
        ttk.Button(btns, text="Remove Provider", command=lambda: self._safe_action(self.remove_selected_provider_profile)).pack(side="left", padx=4)

        self.selected_provider_profile_id = ""
        self.refresh_provider_profile_list()

    def _build_room_rules_tab(self, parent) -> None:
        ttk = self.ttk
        tk = self.tk
        weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
        time_choices = military_time_choices()

        wrap = ttk.Frame(parent, padding=8)
        wrap.pack(fill="both", expand=True)
        wrap.columnconfigure(0, weight=1)
        wrap.columnconfigure(1, weight=2)
        wrap.rowconfigure(0, weight=1)

        left = ttk.Labelframe(wrap, text="Rooms", padding=8)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        right = ttk.Labelframe(wrap, text="Room Availability Restrictions", padding=8)
        right.grid(row=0, column=1, sticky="nsew")

        self.room_rule_list = tk.Listbox(left, height=24)
        self.room_rule_list.pack(fill="both", expand=True)
        self.room_rule_list.bind("<<ListboxSelect>>", self._on_room_rule_select)
        for room in PREDEFINED_ROOMS:
            self.room_rule_list.insert(tk.END, room)
        if PREDEFINED_ROOMS:
            self.room_rule_list.selection_set(0)
            self.room_rule_selected_var = tk.StringVar(value=PREDEFINED_ROOMS[0])

        ttk.Label(right, text="Room").grid(row=0, column=0, sticky="w")
        ttk.Entry(right, textvariable=self.room_rule_selected_var, state="readonly", width=24).grid(row=0, column=1, sticky="w", padx=4)

        ttk.Label(right, text="Discipline compatibility").grid(row=1, column=0, sticky="w")
        ttk.Label(right, text="All configured disciplines", foreground="#6c757d").grid(row=1, column=1, sticky="w", padx=4)

        self.room_rule_weekday_var = tk.StringVar(value="Monday")
        self.room_rule_start_var = tk.StringVar(value="1100")
        self.room_rule_end_var = tk.StringVar(value="1300")
        self.room_rule_type_var = tk.StringVar(value="unavailable")

        weekly = ttk.Labelframe(right, text="Weekly rules", padding=6)
        weekly.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Combobox(weekly, textvariable=self.room_rule_weekday_var, values=weekdays, state="readonly", width=12).grid(row=0, column=0, padx=2)
        ttk.Combobox(weekly, textvariable=self.room_rule_start_var, values=time_choices, state="readonly", width=10).grid(row=0, column=1, padx=2)
        ttk.Combobox(weekly, textvariable=self.room_rule_end_var, values=time_choices, state="readonly", width=10).grid(row=0, column=2, padx=2)
        ttk.Combobox(weekly, textvariable=self.room_rule_type_var, values=["unavailable", "available-only"], state="readonly", width=14).grid(row=0, column=3, padx=2)
        ttk.Button(weekly, text="Add Window", command=lambda: self._safe_action(self.add_room_weekly_rule)).grid(row=0, column=4, padx=4)
        ttk.Button(weekly, text="Remove Window", command=lambda: self._safe_action(self.remove_room_weekly_rule)).grid(row=0, column=5, padx=4)
        self.room_weekly_rules_list = tk.Listbox(weekly, height=6)
        self.room_weekly_rules_list.grid(row=1, column=0, columnspan=6, sticky="ew", pady=(4, 0))

        self.room_rule_date_var = tk.StringVar(value=date_to_key(datetime.utcnow().date()))
        self.room_rule_date_start_var = tk.StringVar(value="1100")
        self.room_rule_date_end_var = tk.StringVar(value="1300")
        self.room_rule_date_type_var = tk.StringVar(value="unavailable")

        dates = ttk.Labelframe(right, text="Date-specific exceptions", padding=6)
        dates.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Entry(dates, textvariable=self.room_rule_date_var, width=12).grid(row=0, column=0, padx=2)
        ttk.Combobox(dates, textvariable=self.room_rule_date_start_var, values=time_choices, state="readonly", width=10).grid(row=0, column=1, padx=2)
        ttk.Combobox(dates, textvariable=self.room_rule_date_end_var, values=time_choices, state="readonly", width=10).grid(row=0, column=2, padx=2)
        ttk.Combobox(dates, textvariable=self.room_rule_date_type_var, values=["unavailable", "available-only"], state="readonly", width=14).grid(row=0, column=3, padx=2)
        ttk.Button(dates, text="Add Exception", command=lambda: self._safe_action(self.add_room_date_rule)).grid(row=0, column=4, padx=4)
        ttk.Button(dates, text="Remove Exception", command=lambda: self._safe_action(self.remove_room_date_rule)).grid(row=0, column=5, padx=4)
        self.room_date_rules_list = tk.Listbox(dates, height=6)
        self.room_date_rules_list.grid(row=1, column=0, columnspan=6, sticky="ew", pady=(4, 0))

        self.room_rules_preview_canvas = tk.Canvas(right, width=620, height=180, bg="white", highlightthickness=1, highlightbackground="#ced4da")
        self.room_rules_preview_canvas.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(8, 0))

        btns = ttk.Frame(right)
        btns.grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(btns, text="Save/Update", command=lambda: self._safe_action(self.save_room_rules)).pack(side="left", padx=4)
        ttk.Button(btns, text="Revert", command=lambda: self._safe_action(self.revert_room_rules_editor)).pack(side="left", padx=4)
        ttk.Button(btns, text="Clear rules for room", command=lambda: self._safe_action(self.clear_selected_room_rules)).pack(side="left", padx=4)

        self.revert_room_rules_editor()

    def _build_form_panel(self, parent) -> None:
        ttk = self.ttk

        current = datetime.utcnow().date()
        self.year_choices = [str(current.year + i) for i in range(0, 3)]
        self.month_choices = [f"{m:02d}" for m in range(1, 13)]
        self.day_choices = [f"{d:02d}" for d in range(1, 32)]
        time_choices = military_time_choices()

        cal = ttk.Labelframe(parent, text="Calendar Settings", padding=6)
        cal.pack(fill="x", pady=(0, 6))

        self.cal_year_var = self.tk.StringVar(value=str(current.year))
        self.cal_month_var = self.tk.StringVar(value=f"{current.month:02d}")
        self.cal_day_var = self.tk.StringVar(value=f"{current.day:02d}")
        self.day_start_var = self.tk.StringVar(value="0730")
        self.day_end_var = self.tk.StringVar(value="1800")

        self.ttk.Label(cal, text="Year").grid(row=0, column=0, sticky="w")
        self.ttk.Combobox(cal, textvariable=self.cal_year_var, values=self.year_choices, state="readonly", width=8).grid(row=1, column=0, padx=2)
        self.ttk.Label(cal, text="Month").grid(row=0, column=1, sticky="w")
        self.ttk.Combobox(cal, textvariable=self.cal_month_var, values=self.month_choices, state="readonly", width=6).grid(row=1, column=1, padx=2)
        self.ttk.Label(cal, text="Day").grid(row=0, column=2, sticky="w")
        self.ttk.Combobox(cal, textvariable=self.cal_day_var, values=self.day_choices, state="readonly", width=6).grid(row=1, column=2, padx=2)

        self.ttk.Label(cal, text="Day Start (HHMM)").grid(row=0, column=3, sticky="w")
        self.ttk.Combobox(cal, textvariable=self.day_start_var, values=time_choices, state="readonly", width=10).grid(row=1, column=3, padx=2)
        self.ttk.Label(cal, text="Day End (HHMM)").grid(row=0, column=4, sticky="w")
        self.ttk.Combobox(cal, textvariable=self.day_end_var, values=time_choices, state="readonly", width=10).grid(row=1, column=4, padx=2)

        ttk.Button(cal, text="Apply Calendar Settings", command=lambda: self._safe_action(self.apply_calendar_settings)).grid(row=1, column=5, padx=6)

        provider_frame = ttk.Labelframe(parent, text="Provider List Management", padding=6)
        provider_frame.pack(fill="x", pady=(0, 6))
        self.provider_selected_var = self.tk.StringVar(value=self.provider_catalog[0] if self.provider_catalog else "")
        self.provider_new_var = self.tk.StringVar()
        self.ttk.Label(provider_frame, text="Provider Name").grid(row=0, column=0, sticky="w")
        self.provider_manage_combo = self.ttk.Combobox(provider_frame, textvariable=self.provider_selected_var, values=self.provider_catalog, state="readonly", width=30)
        self.provider_manage_combo.grid(row=1, column=0, padx=2)
        self.ttk.Label(provider_frame, text="New Provider Name").grid(row=0, column=1, sticky="w")
        self.ttk.Entry(provider_frame, textvariable=self.provider_new_var, width=28).grid(row=1, column=1, padx=2)
        ttk.Button(provider_frame, text="Add New Provider", command=lambda: self._safe_action(self.add_new_provider)).grid(row=1, column=2, padx=4)
        ttk.Button(provider_frame, text="Remove Provider", command=lambda: self._safe_action(self.remove_provider)).grid(row=1, column=3, padx=4)

        self.provider_avail_weekday_var = self.tk.StringVar(value="Monday")
        self.provider_avail_start_var = self.tk.StringVar(value="0730")
        self.provider_avail_end_var = self.tk.StringVar(value="1800")
        self.ttk.Label(provider_frame, text="Provider Availability Window").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.ttk.Combobox(provider_frame, textvariable=self.provider_avail_weekday_var, values=["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"], state="readonly", width=12).grid(row=3, column=0, padx=2, sticky="w")
        self.ttk.Combobox(provider_frame, textvariable=self.provider_avail_start_var, values=time_choices, state="readonly", width=10).grid(row=3, column=1, padx=2, sticky="w")
        self.ttk.Combobox(provider_frame, textvariable=self.provider_avail_end_var, values=time_choices, state="readonly", width=10).grid(row=3, column=2, padx=2, sticky="w")
        ttk.Button(provider_frame, text="Set Availability Window", command=lambda: self._safe_action(self.set_provider_availability_window)).grid(row=3, column=3, padx=4)
        self.provider_manage_combo.bind("<<ComboboxSelected>>", lambda _e: self._safe_action(self.render_provider_availability_preview))

        self.provider_preview_canvas = self.tk.Canvas(provider_frame, width=620, height=160, bg="white", highlightthickness=1, highlightbackground="#ced4da")
        self.provider_preview_canvas.grid(row=4, column=0, columnspan=4, pady=(8, 0), sticky="ew")

        appt = ttk.Labelframe(parent, text="Add Appointment", padding=6)
        appt.pack(fill="x", pady=(0, 6))

        self.appt_date_var = self.tk.StringVar(value=date_to_key(current))
        self.appt_patient_var = self.tk.StringVar(value=PATIENT_ID_CHOICES[0])
        self.appt_program_var = self.tk.StringVar(value="IOP")
        self.appt_provider_var = self.tk.StringVar(value=self.provider_catalog[0] if self.provider_catalog else "")
        self.appt_room_var = self.tk.StringVar(value=PREDEFINED_ROOMS[0])
        self.appt_mode_var = self.tk.StringVar(value="individual")
        self.appt_discipline_var = self.tk.StringVar(value=DISCIPLINES[1])
        self.appt_start_var = self.tk.StringVar(value="0730")
        self.appt_end_var = self.tk.StringVar(value="0800")

        self.ttk.Label(appt, text="Date").grid(row=0, column=0, sticky="w")
        self.appt_date_combo = self.ttk.Combobox(appt, textvariable=self.appt_date_var, values=[date_to_key(current)], state="readonly", width=14)
        self.appt_date_combo.grid(row=1, column=0, padx=2)

        self.ttk.Label(appt, text="Patient ID").grid(row=0, column=1, sticky="w")
        self.ttk.Combobox(appt, textvariable=self.appt_patient_var, values=PATIENT_ID_CHOICES, state="readonly", width=10).grid(row=1, column=1, padx=2)

        self.ttk.Label(appt, text="Program").grid(row=0, column=2, sticky="w")
        self.ttk.Combobox(appt, textvariable=self.appt_program_var, values=["IOP", "EVAL"], state="readonly", width=10).grid(row=1, column=2, padx=2)

        self.ttk.Label(appt, text="Provider Name").grid(row=0, column=3, sticky="w")
        self.appt_provider_combo = self.ttk.Combobox(appt, textvariable=self.appt_provider_var, values=self.provider_catalog, state="readonly", width=20)
        self.appt_provider_combo.grid(row=1, column=3, padx=2)
        self.appt_provider_combo.bind("<<ComboboxSelected>>", lambda _e: self._safe_action(self._on_manual_provider_selected))

        self.ttk.Label(appt, text="Room").grid(row=0, column=4, sticky="w")
        self.appt_room_combo = self.ttk.Combobox(appt, textvariable=self.appt_room_var, values=PREDEFINED_ROOMS, state="readonly", width=16)
        self.appt_room_combo.grid(row=1, column=4, padx=2)

        self.ttk.Label(appt, text="Type").grid(row=0, column=5, sticky="w")
        self.ttk.Combobox(appt, textvariable=self.appt_mode_var, values=["individual", "group"], state="readonly", width=12).grid(row=1, column=5, padx=2)

        self.ttk.Label(appt, text="Discipline").grid(row=0, column=6, sticky="w")
        self.ttk.Combobox(appt, textvariable=self.appt_discipline_var, values=DISCIPLINES, state="readonly", width=22).grid(row=1, column=6, padx=2)

        self.ttk.Label(appt, text="Begin").grid(row=0, column=7, sticky="w")
        self.ttk.Combobox(appt, textvariable=self.appt_start_var, values=time_choices, state="readonly", width=10).grid(row=1, column=7, padx=2)
        self.ttk.Label(appt, text="End").grid(row=0, column=8, sticky="w")
        self.ttk.Combobox(appt, textvariable=self.appt_end_var, values=time_choices, state="readonly", width=10).grid(row=1, column=8, padx=2)

        ttk.Button(appt, text="Add Appointment", command=lambda: self._safe_action(self.add_appointment)).grid(row=1, column=9, padx=6)
        ttk.Button(appt, text="Delete Appointment", command=lambda: self._safe_action(self.delete_selected_appointment)).grid(row=1, column=10, padx=6)
        ttk.Button(appt, text="Undo Manual Action", command=lambda: self._safe_action(self.undo_manual_action)).grid(row=1, column=11, padx=6)

    def _safe_action(self, fn) -> None:
        try:
            fn()
        except Exception as exc:
            self.status_var.set("Status: Error")
            self.messagebox.showerror("Action failed", str(exc))


    def _push_manual_undo_snapshot(self, action_label: str) -> None:
        snapshot = {
            "action": action_label,
            "profile": copy.deepcopy(self.loaded_profile) if self.loaded_profile else None,
            "provider_catalog": copy.deepcopy(self.provider_catalog),
            "provider_profiles": copy.deepcopy(self.provider_profiles),
            "last_result": copy.deepcopy(self.last_result),
        }
        self.manual_undo_stack.append(snapshot)
        if len(self.manual_undo_stack) > 10:
            self.manual_undo_stack = self.manual_undo_stack[-10:]

    def undo_manual_action(self) -> None:
        if not self.manual_undo_stack:
            raise ValueError("No manual actions to undo")
        snapshot = self.manual_undo_stack.pop()
        self.loaded_profile = snapshot.get("profile")
        self.provider_catalog = snapshot.get("provider_catalog", self.provider_catalog)
        self.provider_profiles = snapshot.get("provider_profiles", self.provider_profiles)
        self.last_result = snapshot.get("last_result")
        self.selected_request_id = None
        self._refresh_provider_dropdowns()
        self._refresh_profile_preview()
        if self.loaded_profile and self.last_result:
            self._render_patient_grid(self.loaded_profile, self.last_result)
        self.status_var.set(f"Status: Undid manual action: {snapshot.get('action', 'unknown')}")

    def _set_text(self, widget, text: str) -> None:
        widget.configure(state=self.tk.NORMAL)
        widget.delete("1.0", self.tk.END)
        widget.insert(self.tk.END, text)
        widget.configure(state=self.tk.DISABLED)

    def _require_profile(self) -> Dict[str, Any]:
        if not self.loaded_profile:
            raise ValueError("No profile loaded. Click 'New Blank Profile' first.")
        return self.loaded_profile

    def _sync_profile_resources(self, profile: Dict[str, Any]) -> None:
        day_window = profile.get("day_window", {"start_minute": GRID_START_MINUTE, "end_minute": GRID_END_MINUTE})
        day_start = int(day_window.get("start_minute", GRID_START_MINUTE))
        day_end = int(day_window.get("end_minute", GRID_END_MINUTE))
        date_keys = profile.get("planning_dates") or [profile.get("date_key", date_to_key(datetime.utcnow().date()))]

        profile["rooms"] = build_room_records(self.room_rules)
        provider_records = build_provider_records_from_profiles(self.provider_profiles, day_start, day_end)
        if not provider_records:
            provider_records = build_provider_records(self.provider_catalog, day_start, day_end)
        profile["providers"] = provider_records
        profile["patients"] = build_patient_records(date_keys, day_start, day_end)

    def _autosave_profile(self) -> None:
        if self.loaded_profile:
            save_last_profile(self.loaded_profile, str(self.loaded_profile_path) if self.loaded_profile_path else None)

    def _refresh_date_dropdowns(self) -> None:
        if not self.loaded_profile:
            return
        dates = self.loaded_profile.get("planning_dates", [])
        if not dates:
            return
        self.appt_date_combo["values"] = dates
        if self.appt_date_var.get() not in dates:
            self.appt_date_var.set(dates[0])

    def _persist_provider_catalog(self) -> None:
        self.provider_profiles = normalize_provider_catalog(
            self.provider_profiles,
            defaults=DEFAULT_PROVIDER_NAMES,
            all_rooms=PREDEFINED_ROOMS,
            disciplines=DISCIPLINES,
        )
        self.provider_catalog = [p["provider_name"] for p in self.provider_profiles]
        save_provider_catalog_entries(self.provider_profiles)
        save_provider_profiles(self.provider_profiles)

    def _persist_requirements_catalog(self) -> None:
        save_requirements_catalog(self.auto_conditions, self.eval_conditions)

    def _provider_profile_by_name(self, provider_name: str) -> Dict[str, Any] | None:
        key = provider_name.strip().lower()
        for profile in self.provider_profiles:
            if str(profile.get("provider_name", "")).strip().lower() == key:
                return profile
        return None

    def _provider_profile_by_id(self, provider_id: str) -> Dict[str, Any] | None:
        key = provider_id.strip()
        for profile in self.provider_profiles:
            if str(profile.get("provider_id", "")).strip() == key:
                return profile
        return None

    def _provider_display_name(self, provider_id: str) -> str:
        profile = self._provider_profile_by_id(provider_id)
        return str(profile.get("provider_name")) if profile else provider_id

    def _refresh_provider_dropdowns(self) -> None:
        if hasattr(self, "appt_provider_combo"):
            self.appt_provider_combo["values"] = self.provider_catalog
        if hasattr(self, "provider_manage_combo"):
            self.provider_manage_combo["values"] = self.provider_catalog
        if hasattr(self, "auto_provider_combo"):
            primary_values = ["Any provider"] + self.provider_catalog
            optional_values = ["(none)"] + self.provider_catalog
            self.auto_provider_combo["values"] = primary_values
            self.auto_provider_b_combo["values"] = optional_values
            self.auto_provider_c_combo["values"] = optional_values
            if self.auto_provider_var.get() not in primary_values:
                self.auto_provider_var.set("Any provider")
            if self.auto_provider_b_var.get() not in optional_values:
                self.auto_provider_b_var.set("(none)")
            if self.auto_provider_c_var.get() not in optional_values:
                self.auto_provider_c_var.set("(none)")
        if self.provider_catalog and hasattr(self, "appt_provider_var") and self.appt_provider_var.get() not in self.provider_catalog:
            self.appt_provider_var.set(self.provider_catalog[0])
        if self.provider_catalog and hasattr(self, "provider_selected_var") and self.provider_selected_var.get() not in self.provider_catalog:
            self.provider_selected_var.set(self.provider_catalog[0])
        if hasattr(self, "provider_search_list"):
            self.refresh_provider_profile_list()

    def refresh_provider_profile_list(self) -> None:
        if not hasattr(self, "provider_search_list"):
            return
        query = self.provider_search_var.get().strip().lower() if hasattr(self, "provider_search_var") else ""
        self.provider_search_list.delete(0, self.tk.END)
        for profile in self.provider_profiles:
            name = str(profile.get("provider_name", ""))
            if query and query not in name.lower():
                continue
            self.provider_search_list.insert(self.tk.END, f"{name} [{profile.get('provider_id', '')}]")

    def _selected_provider_profile(self) -> Dict[str, Any] | None:
        if not self.selected_provider_profile_id:
            return None
        return self._provider_profile_by_id(self.selected_provider_profile_id)

    def _on_provider_profile_select(self, _event=None) -> None:
        if not hasattr(self, "provider_search_list"):
            return
        selection = self.provider_search_list.curselection()
        if not selection:
            return
        text = self.provider_search_list.get(selection[0])
        if "[" in text and text.endswith("]"):
            provider_id = text.rsplit("[", 1)[1][:-1]
        else:
            provider_id = text.strip()
        self.selected_provider_profile_id = provider_id
        self._load_provider_profile_into_editor(provider_id)

    def _load_provider_profile_into_editor(self, provider_id: str) -> None:
        profile = self._provider_profile_by_id(provider_id)
        if not profile:
            return
        self.provider_profile_id_var.set(profile.get("provider_id", ""))
        self.provider_profile_name_var.set(profile.get("provider_name", ""))
        disciplines = profile.get("disciplines") or ([] if not profile.get("discipline") else [profile.get("discipline")])
        self.provider_profile_disciplines_var.set(", ".join([str(d).strip() for d in disciplines if str(d).strip()]))

        allowed = set(profile.get("allowed_rooms") or ["Any compatible room"])
        for room_name, var in self.allowed_room_vars.items():
            var.set(room_name in allowed)
        self._on_allowed_room_toggle()

        self.provider_availability_list.delete(0, self.tk.END)
        names = ["Mon", "Tue", "Wed", "Thu", "Fri"]
        for tmpl in sorted(profile.get("availability_templates", []), key=lambda x: int(x.get("weekday", 0))):
            wd = int(tmpl.get("weekday", 0))
            if wd > 4:
                continue
            for w in tmpl.get("windows", []):
                self.provider_availability_list.insert(self.tk.END, f"{names[wd]} {w['start_minute']//60:02d}:{w['start_minute']%60:02d}-{w['end_minute']//60:02d}:{w['end_minute']%60:02d}")

        self.provider_exception_list.delete(0, self.tk.END)
        for ex in profile.get("exceptions", []):
            date_key = ex.get("date") or ex.get("date_key")
            start = int(ex.get("start_minute") if "start_minute" in ex else ex.get("window", {}).get("start_minute", 0))
            end = int(ex.get("end_minute") if "end_minute" in ex else ex.get("window", {}).get("end_minute", 0))
            kind = ex.get("kind") or ("added" if ex.get("available_override") else "unavailable")
            self.provider_exception_list.insert(self.tk.END, f"{date_key} {kind} {start//60:02d}:{start%60:02d}-{end//60:02d}:{end%60:02d}")
        self._render_provider_preview_canvas(self.provider_profile_preview_canvas, profile)

    def _collect_editor_allowed_rooms(self) -> List[str]:
        selected = [room for room, var in self.allowed_room_vars.items() if var.get()]
        if not selected:
            return ["Any compatible room"]
        if "Any compatible room" in selected:
            return ["Any compatible room"]
        return selected

    def _on_allowed_room_toggle(self) -> None:
        any_selected = self.allowed_room_vars["Any compatible room"].get()
        for room in PREDEFINED_ROOMS:
            if any_selected:
                self.allowed_room_vars[room].set(False)

    def _next_provider_id(self) -> str:
        existing = {p.get("provider_id") for p in self.provider_profiles}
        idx = 1
        while True:
            candidate = f"provider_{idx:03d}"
            if candidate not in existing:
                return candidate
            idx += 1

    def _collect_editor_profile_payload(self, preserve_id: str | None = None) -> Dict[str, Any]:
        pid = preserve_id or self.provider_profile_id_var.get().strip() or self._next_provider_id()
        name = self.provider_profile_name_var.get().strip()
        if not name:
            raise ValueError("Provider name is required")
        raw_disciplines = [x.strip() for x in self.provider_profile_disciplines_var.get().split(",") if x.strip()]
        disciplines: List[str] = []
        for d in raw_disciplines:
            if d not in DISCIPLINES:
                raise ValueError(f"Invalid discipline: {d}")
            if d not in disciplines:
                disciplines.append(d)
        if len(disciplines) > 5:
            raise ValueError("A provider can have up to 5 disciplines")
        return {
            "provider_id": pid,
            "provider_name": name,
            "discipline": disciplines[0] if disciplines else "",
            "disciplines": disciplines,
            "allowed_rooms": self._collect_editor_allowed_rooms(),
            "availability_templates": self._selected_provider_profile().get("availability_templates", []) if self._selected_provider_profile() else [],
            "exceptions": self._selected_provider_profile().get("exceptions", []) if self._selected_provider_profile() else [],
        }

    def save_selected_provider_profile(self) -> None:
        selected = self._selected_provider_profile()
        preserve_id = selected.get("provider_id") if selected else None
        payload = self._collect_editor_profile_payload(preserve_id=preserve_id)
        if selected:
            for idx, profile in enumerate(self.provider_profiles):
                if profile.get("provider_id") == preserve_id:
                    payload["availability_templates"] = profile.get("availability_templates", [])
                    payload["exceptions"] = profile.get("exceptions", [])
                    self.provider_profiles[idx] = payload
                    break
        else:
            self.provider_profiles.append(payload)
            self.selected_provider_profile_id = payload["provider_id"]
        self._persist_provider_catalog()
        self._refresh_provider_dropdowns()
        if self.loaded_profile:
            self._sync_profile_resources(self.loaded_profile)
            self.last_result = build_live_result_from_profile(self.loaded_profile)
            self._render_patient_grid(self.loaded_profile, self.last_result)
        self._refresh_profile_preview()
        self.status_var.set(f"Status: Saved provider profile {payload['provider_name']}")

    def revert_selected_provider_profile(self) -> None:
        selected = self._selected_provider_profile()
        if not selected:
            raise ValueError("Select a provider profile first")
        self._load_provider_profile_into_editor(selected.get("provider_id", ""))

    def new_provider_profile(self) -> None:
        self.selected_provider_profile_id = ""
        self.provider_profile_id_var.set(self._next_provider_id())
        self.provider_profile_name_var.set("")
        self.provider_profile_disciplines_var.set("")
        self.allowed_room_vars["Any compatible room"].set(True)
        self._on_allowed_room_toggle()
        self.provider_availability_list.delete(0, self.tk.END)
        self.provider_exception_list.delete(0, self.tk.END)
        self.provider_profile_preview_canvas.delete("all")

    def remove_selected_provider_profile(self) -> None:
        selected = self._selected_provider_profile()
        if not selected:
            raise ValueError("Select a provider profile to remove")
        if not self.messagebox.askyesno("Remove Provider", f"Remove provider '{selected.get('provider_name')}'?"):
            return
        self.provider_profiles = [p for p in self.provider_profiles if p.get("provider_id") != selected.get("provider_id")]
        self.selected_provider_profile_id = ""
        self._persist_provider_catalog()
        self._refresh_provider_dropdowns()
        if self.loaded_profile:
            self._sync_profile_resources(self.loaded_profile)
            self.last_result = build_live_result_from_profile(self.loaded_profile)
            self._render_patient_grid(self.loaded_profile, self.last_result)
        self.status_var.set("Status: Provider removed")

    def add_provider_profile_availability_window(self) -> None:
        selected = self._selected_provider_profile()
        if not selected:
            raise ValueError("Select a provider profile first")
        weekday_map = {"Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3, "Friday": 4}
        wd = weekday_map[self.profile_avail_weekday_var.get()]
        start = parse_time_input(self.profile_avail_start_var.get())
        end = parse_time_input(self.profile_avail_end_var.get())
        if end <= start:
            raise ValueError("Availability end must be after start")
        templates = list(selected.get("availability_templates") or [])
        entry = next((t for t in templates if int(t.get("weekday", -1)) == wd), None)
        if not entry:
            entry = {"weekday": wd, "windows": []}
            templates.append(entry)
        entry["windows"].append({"start_minute": start, "end_minute": end})
        entry["windows"] = sorted(entry["windows"], key=lambda w: (w["start_minute"], w["end_minute"]))
        selected["availability_templates"] = templates
        self._persist_provider_catalog()
        self._load_provider_profile_into_editor(selected["provider_id"])

    def remove_provider_profile_availability_window(self) -> None:
        selected = self._selected_provider_profile()
        if not selected:
            raise ValueError("Select a provider profile first")
        idx = self.provider_availability_list.curselection()
        if not idx:
            raise ValueError("Select an availability window in the list")
        token = self.provider_availability_list.get(idx[0])
        day_abbr = token.split()[0]
        rng = token.split()[1]
        start_hm, end_hm = rng.split("-")
        start = int(start_hm.split(":")[0]) * 60 + int(start_hm.split(":")[1])
        end = int(end_hm.split(":")[0]) * 60 + int(end_hm.split(":")[1])
        day_map = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4}
        wd = day_map.get(day_abbr)
        templates = []
        for tmpl in selected.get("availability_templates", []):
            if int(tmpl.get("weekday", -1)) != wd:
                templates.append(tmpl)
                continue
            windows = [w for w in tmpl.get("windows", []) if not (int(w.get("start_minute", -1)) == start and int(w.get("end_minute", -1)) == end)]
            templates.append({"weekday": wd, "windows": windows})
        selected["availability_templates"] = templates
        self._persist_provider_catalog()
        self._load_provider_profile_into_editor(selected["provider_id"])

    def add_provider_profile_exception(self) -> None:
        selected = self._selected_provider_profile()
        if not selected:
            raise ValueError("Select a provider profile first")
        date_key = self.profile_exception_date_var.get().strip()
        date.fromisoformat(date_key)
        start = parse_time_input(self.profile_exception_start_var.get())
        end = parse_time_input(self.profile_exception_end_var.get())
        if end <= start:
            raise ValueError("Exception end must be after start")
        exceptions = list(selected.get("exceptions") or [])
        exceptions.append({
            "date": date_key,
            "kind": self.profile_exception_kind_var.get().strip(),
            "start_minute": start,
            "end_minute": end,
        })
        selected["exceptions"] = exceptions
        self._persist_provider_catalog()
        self._load_provider_profile_into_editor(selected["provider_id"])

    def remove_provider_profile_exception(self) -> None:
        selected = self._selected_provider_profile()
        if not selected:
            raise ValueError("Select a provider profile first")
        idx = self.provider_exception_list.curselection()
        if not idx:
            raise ValueError("Select an exception in the list")
        token = self.provider_exception_list.get(idx[0])
        selected["exceptions"] = [ex for ex in selected.get("exceptions", []) if f"{ex.get('date') or ex.get('date_key')} {ex.get('kind') or ('added' if ex.get('available_override') else 'unavailable')}" not in token]
        self._persist_provider_catalog()
        self._load_provider_profile_into_editor(selected["provider_id"])

    def _render_provider_preview_canvas(self, canvas, profile: Dict[str, Any]) -> None:
        canvas.delete("all")
        day_start = parse_time_input(self.day_start_var.get()) if hasattr(self, "day_start_var") else GRID_START_MINUTE
        day_end = parse_time_input(self.day_end_var.get()) if hasattr(self, "day_end_var") else GRID_END_MINUTE
        preview = build_provider_availability_preview_data(profile, day_start, day_end)
        weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri"]
        left_w, header_h, row_h, col_w = 52, 24, 10, 110
        slots = max(1, (day_end - day_start) // GRID_SLOT_MINUTES)
        total_w = left_w + len(weekdays) * col_w
        total_h = header_h + slots * row_h
        canvas.config(scrollregion=(0, 0, total_w, total_h))
        canvas.create_rectangle(0, 0, left_w, header_h, fill="#0b4f6c", outline="#0b4f6c")
        for idx, wd in enumerate(weekdays):
            x0 = left_w + idx * col_w
            x1 = x0 + col_w
            canvas.create_rectangle(x0, 0, x1, header_h, fill="#1d3557", outline="#f1faee")
            canvas.create_text((x0 + x1) // 2, header_h // 2, text=wd, fill="white", font=("Segoe UI", 8, "bold"))
        for slot in range(slots):
            minute = day_start + slot * GRID_SLOT_MINUTES
            y0 = header_h + slot * row_h
            y1 = y0 + row_h
            if slot % 4 == 0:
                canvas.create_text(left_w // 2, (y0 + y1) // 2, text=f"{minute//60:02d}:{minute%60:02d}", font=("Segoe UI", 6), fill="#495057")
            for idx in range(len(weekdays)):
                x0 = left_w + idx * col_w
                x1 = x0 + col_w
                canvas.create_rectangle(x0, y0, x1, y1, fill="#ffffff", outline="#e9ecef")
        for wd, windows in preview.items():
            for start, end in windows:
                start_slot = max(0, (start - day_start) // GRID_SLOT_MINUTES)
                end_slot = min(slots, (end - day_start) // GRID_SLOT_MINUTES)
                if end_slot <= start_slot:
                    continue
                x0 = left_w + wd * col_w + 2
                x1 = x0 + col_w - 4
                y0 = header_h + start_slot * row_h + 1
                y1 = header_h + end_slot * row_h - 1
                canvas.create_rectangle(x0, y0, x1, y1, fill="#90e0ef", outline="#0077b6", width=2)

    def _selected_room_name(self) -> str:
        name = self.room_rule_selected_var.get().strip() if hasattr(self, "room_rule_selected_var") else ""
        if not name:
            raise ValueError("Select a room first")
        return name

    def _room_rule_bucket(self, room_name: str) -> Dict[str, Any]:
        rooms = self.room_rules.setdefault("rooms", {})
        if room_name not in rooms:
            rooms[room_name] = {
                "unavailable_weekly": {i: [] for i in range(5)},
                "unavailable_dates": [],
                "available_only_weekly": {i: [] for i in range(5)},
                "available_only_dates": [],
            }
        return rooms[room_name]

    def _on_room_rule_select(self, _event=None) -> None:
        if not hasattr(self, "room_rule_list"):
            return
        selection = self.room_rule_list.curselection()
        if not selection:
            return
        room_name = self.room_rule_list.get(selection[0])
        self.room_rule_selected_var.set(room_name)
        self.revert_room_rules_editor()

    def _render_room_rules_preview(self, room_name: str) -> None:
        canvas = self.room_rules_preview_canvas
        canvas.delete("all")
        room_bucket = self._room_rule_bucket(room_name)
        day_start = parse_time_input(self.day_start_var.get()) if hasattr(self, "day_start_var") else GRID_START_MINUTE
        day_end = parse_time_input(self.day_end_var.get()) if hasattr(self, "day_end_var") else GRID_END_MINUTE
        preview = room_bucket.get("unavailable_weekly", {})

        weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri"]
        left_w, header_h, row_h, col_w = 52, 24, 10, 110
        slots = max(1, (day_end - day_start) // GRID_SLOT_MINUTES)
        total_w = left_w + len(weekdays) * col_w
        total_h = header_h + slots * row_h
        canvas.config(scrollregion=(0, 0, total_w, total_h))

        canvas.create_rectangle(0, 0, left_w, header_h, fill="#0b4f6c", outline="#0b4f6c")
        for idx, wd in enumerate(weekdays):
            x0 = left_w + idx * col_w
            x1 = x0 + col_w
            canvas.create_rectangle(x0, 0, x1, header_h, fill="#1d3557", outline="#f1faee")
            canvas.create_text((x0 + x1) // 2, header_h // 2, text=wd, fill="white", font=("Segoe UI", 8, "bold"))

        for slot in range(slots):
            minute = day_start + slot * GRID_SLOT_MINUTES
            y0 = header_h + slot * row_h
            y1 = y0 + row_h
            if slot % 4 == 0:
                canvas.create_text(left_w // 2, (y0 + y1) // 2, text=f"{minute//60:02d}:{minute%60:02d}", font=("Segoe UI", 6), fill="#495057")
            for idx in range(len(weekdays)):
                x0 = left_w + idx * col_w
                x1 = x0 + col_w
                canvas.create_rectangle(x0, y0, x1, y1, fill="#ffffff", outline="#e9ecef")

        for wd in range(5):
            windows = preview.get(wd) or preview.get(str(wd), [])
            for w in windows:
                start = max(day_start, int(w.get("start_minute", day_start)))
                end = min(day_end, int(w.get("end_minute", day_end)))
                start_slot = max(0, (start - day_start) // GRID_SLOT_MINUTES)
                end_slot = min(slots, (end - day_start) // GRID_SLOT_MINUTES)
                if end_slot <= start_slot:
                    continue
                x0 = left_w + wd * col_w + 2
                x1 = x0 + col_w - 4
                y0 = header_h + start_slot * row_h + 1
                y1 = header_h + end_slot * row_h - 1
                canvas.create_rectangle(x0, y0, x1, y1, fill="#ffadad", outline="#d90429", width=2)

    def revert_room_rules_editor(self) -> None:
        room_name = self._selected_room_name()
        bucket = self._room_rule_bucket(room_name)
        self.room_weekly_rules_list.delete(0, self.tk.END)
        weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri"]
        for wd in range(5):
            for w in bucket.get("unavailable_weekly", {}).get(wd, []):
                self.room_weekly_rules_list.insert(self.tk.END, f"{weekdays[wd]} unavailable {w['start_minute']//60:02d}:{w['start_minute']%60:02d}-{w['end_minute']//60:02d}:{w['end_minute']%60:02d}")
            for w in bucket.get("available_only_weekly", {}).get(wd, []):
                self.room_weekly_rules_list.insert(self.tk.END, f"{weekdays[wd]} available-only {w['start_minute']//60:02d}:{w['start_minute']%60:02d}-{w['end_minute']//60:02d}:{w['end_minute']%60:02d}")

        self.room_date_rules_list.delete(0, self.tk.END)
        for w in bucket.get("unavailable_dates", []):
            self.room_date_rules_list.insert(self.tk.END, f"{w['date']} unavailable {w['start_minute']//60:02d}:{w['start_minute']%60:02d}-{w['end_minute']//60:02d}:{w['end_minute']%60:02d}")
        for w in bucket.get("available_only_dates", []):
            self.room_date_rules_list.insert(self.tk.END, f"{w['date']} available-only {w['start_minute']//60:02d}:{w['start_minute']%60:02d}-{w['end_minute']//60:02d}:{w['end_minute']%60:02d}")
        self._render_room_rules_preview(room_name)

    def add_room_weekly_rule(self) -> None:
        room_name = self._selected_room_name()
        weekday_map = {"Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3, "Friday": 4}
        wd = weekday_map[self.room_rule_weekday_var.get()]
        start = parse_time_input(self.room_rule_start_var.get())
        end = parse_time_input(self.room_rule_end_var.get())
        if end <= start:
            raise ValueError("Window end must be after start")
        bucket = self._room_rule_bucket(room_name)
        key = "unavailable_weekly" if self.room_rule_type_var.get() == "unavailable" else "available_only_weekly"
        weekly = bucket.setdefault(key, {i: [] for i in range(5)})
        day_windows = weekly.setdefault(wd, [])
        day_windows.append({"start_minute": start, "end_minute": end})
        day_windows.sort(key=lambda w: (w["start_minute"], w["end_minute"]))
        self.revert_room_rules_editor()

    def remove_room_weekly_rule(self) -> None:
        room_name = self._selected_room_name()
        sel = self.room_weekly_rules_list.curselection()
        if not sel:
            raise ValueError("Select a weekly rule row")
        token = self.room_weekly_rules_list.get(sel[0])
        day = token.split()[0]
        rule_type = token.split()[1]
        hm = token.split()[2]
        start_hm, end_hm = hm.split("-")
        start = int(start_hm.split(":")[0]) * 60 + int(start_hm.split(":")[1])
        end = int(end_hm.split(":")[0]) * 60 + int(end_hm.split(":")[1])
        day_map = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4}
        wd = day_map[day]
        bucket = self._room_rule_bucket(room_name)
        key = "unavailable_weekly" if rule_type == "unavailable" else "available_only_weekly"
        weekly = bucket.setdefault(key, {i: [] for i in range(5)})
        weekly[wd] = [w for w in weekly.get(wd, []) if not (int(w.get("start_minute", -1)) == start and int(w.get("end_minute", -1)) == end)]
        self.revert_room_rules_editor()

    def add_room_date_rule(self) -> None:
        room_name = self._selected_room_name()
        date_key = self.room_rule_date_var.get().strip()
        date.fromisoformat(date_key)
        start = parse_time_input(self.room_rule_date_start_var.get())
        end = parse_time_input(self.room_rule_date_end_var.get())
        if end <= start:
            raise ValueError("Window end must be after start")
        key = "unavailable_dates" if self.room_rule_date_type_var.get() == "unavailable" else "available_only_dates"
        bucket = self._room_rule_bucket(room_name)
        bucket.setdefault(key, []).append({"date": date_key, "start_minute": start, "end_minute": end})
        bucket[key].sort(key=lambda x: (x["date"], x["start_minute"], x["end_minute"]))
        self.revert_room_rules_editor()

    def remove_room_date_rule(self) -> None:
        room_name = self._selected_room_name()
        sel = self.room_date_rules_list.curselection()
        if not sel:
            raise ValueError("Select a date rule row")
        token = self.room_date_rules_list.get(sel[0])
        parts = token.split()
        date_key, rule_type, hm = parts[0], parts[1], parts[2]
        start_hm, end_hm = hm.split("-")
        start = int(start_hm.split(":")[0]) * 60 + int(start_hm.split(":")[1])
        end = int(end_hm.split(":")[0]) * 60 + int(end_hm.split(":")[1])
        key = "unavailable_dates" if rule_type == "unavailable" else "available_only_dates"
        bucket = self._room_rule_bucket(room_name)
        bucket[key] = [w for w in bucket.get(key, []) if not (w.get("date") == date_key and int(w.get("start_minute", -1)) == start and int(w.get("end_minute", -1)) == end)]
        self.revert_room_rules_editor()

    def save_room_rules(self) -> None:
        self.room_rules = self.room_rules
        save_room_rules(self.room_rules, valid_rooms=PREDEFINED_ROOMS)
        if self.loaded_profile:
            self._sync_profile_resources(self.loaded_profile)
            self.last_result = build_live_result_from_profile(self.loaded_profile)
            self._render_patient_grid(self.loaded_profile, self.last_result)
        self.status_var.set("Status: Room availability rules saved")

    def clear_selected_room_rules(self) -> None:
        room_name = self._selected_room_name()
        self.room_rules.setdefault("rooms", {})[room_name] = {
            "unavailable_weekly": {i: [] for i in range(5)},
            "unavailable_dates": [],
            "available_only_weekly": {i: [] for i in range(5)},
            "available_only_dates": [],
        }
        self.revert_room_rules_editor()
        self.save_room_rules()

    def refresh_current_grid_view(self) -> None:
        if self.loaded_profile:
            self.last_result = self.last_result or build_live_result_from_profile(self.loaded_profile)
            self._render_patient_grid(self.loaded_profile, self.last_result)

    def _refresh_profile_preview(self) -> None:
        if not self.loaded_profile:
            return
        profile = self.loaded_profile
        lines = [
            "Profile Overview",
            f"Start date: {profile.get('date_key')}",
            f"Planning days: {len(profile.get('planning_dates', []))}",
            f"Providers in catalog: {len(self.provider_catalog)}",
            "Patient IDs available: I1-I30 and E1-E10",
            f"Rooms: {len(PREDEFINED_ROOMS)} (fixed)",
            f"Appointments: {len(profile.get('requests', []))}",
        ]
        self._set_text(self.summary_text, "\n".join(lines))
        self._refresh_date_dropdowns()
        self._refresh_provider_dropdowns()
        self._autosave_profile()
        if hasattr(self, "provider_preview_canvas"):
            self.render_provider_availability_preview()

    def _collect_appointments_for_grid(self, profile: Dict[str, Any], result: Dict[str, Any]) -> List[Dict[str, Any]]:
        request_map = {r["id"]: r for r in profile.get("requests", []) if "id" in r}
        appointments: List[Dict[str, Any]] = []
        for assignment in result.get("assignments", {}).values():
            req = request_map.get(assignment.get("request_id", ""), {})
            date_key = req.get("date_key") or assignment.get("date_key")
            if not date_key:
                continue
            pids = [str(pid) for pid in req.get("patient_ids", [])]
            if not pids:
                continue
            appointments.append(
                {
                    "request_id": req.get("id") or assignment.get("request_id"),
                    "date_key": date_key,
                    "start": int(assignment["start_minute"]),
                    "end": int(assignment["end_minute"]),
                    "discipline": req.get("discipline", "Other"),
                    "provider": self._provider_display_name(assignment.get("provider_id", "")),
                    "room": assignment.get("room_id", ""),
                    "patients": pids,
                    "program_type": req.get("program_type", assignment.get("program_type", "IOP")),
                }
            )
        return appointments

    def _build_current_grid_layout(self, profile: Dict[str, Any], result: Dict[str, Any], visible_dates: List[str] | None = None) -> Dict[str, Any]:
        appointments = self._collect_appointments_for_grid(profile, result)
        return build_schedule_layout_model(
            appointments,
            profile.get("planning_dates", []),
            DISCIPLINE_COLORS,
            grid_mode=self.grid_mode_var.get() if hasattr(self, "grid_mode_var") else "Patient Grid",
            program_filter=self.program_filter_var.get() if hasattr(self, "program_filter_var") else "Both",
            selected_request_id=self.selected_request_id,
            visible_dates=visible_dates,
        )

    def _render_patient_grid(self, profile: Dict[str, Any], result: Dict[str, Any]) -> None:
        layout = self._build_current_grid_layout(profile, result)
        draw_layout_on_tk_canvas(self.grid_canvas, layout)
        legends = layout.get("legend_disciplines", [])
        self.legend_var.set(
            "Legend: " + " | ".join(legends) if legends else "Legend: No assigned sessions"
        )

    def _run_in_background(self, worker, on_success, on_error) -> None:
        def wrapped() -> None:
            try:
                value = worker()
            except Exception as exc:  # noqa: BLE001
                self.root.after(0, lambda: on_error(exc))
                return
            self.root.after(0, lambda: on_success(value))

        threading.Thread(target=wrapped, daemon=True).start()

    def export_view_as_png(self) -> None:
        profile = self._require_profile()
        if not self.last_result:
            self.messagebox.showinfo("Export", "Add or generate a schedule first.")
            return
        selected_date = self.appt_date_var.get().strip() if hasattr(self, "appt_date_var") else ""
        date_part = selected_date or "current-view"
        mode_part = (self.grid_mode_var.get() if hasattr(self, "grid_mode_var") else "Patient Grid").replace(" ", "_").lower()
        path_raw = self.filedialog.asksaveasfilename(
            title="Export current schedule view as PNG",
            defaultextension=".png",
            initialfile=f"schedule_{date_part}_{mode_part}.png",
            filetypes=[("PNG files", "*.png")],
        )
        if not path_raw:
            return
        out_path = Path(path_raw)
        visible = [selected_date] if selected_date else None
        layout = self._build_current_grid_layout(profile, self.last_result, visible_dates=visible)
        self.status_var.set("Status: Exporting PNG...")

        def worker() -> Path:
            render_layout_to_png(layout, out_path)
            return out_path

        self._run_in_background(
            worker,
            lambda p: (self.status_var.set(f"Status: Exported PNG to {p}"), self.messagebox.showinfo("Export complete", f"PNG exported to:\n{p}")),
            lambda e: self.messagebox.showerror("PNG export failed", str(e)),
        )

    def export_view_as_pptx(self) -> None:
        profile = self._require_profile()
        if not self.last_result:
            self.messagebox.showinfo("Export", "Add or generate a schedule first.")
            return
        selected_date = self.appt_date_var.get().strip() if hasattr(self, "appt_date_var") else ""
        mode = self.grid_mode_var.get() if hasattr(self, "grid_mode_var") else "Patient Grid"
        program_filter = self.program_filter_var.get() if hasattr(self, "program_filter_var") else "Both"
        date_part = selected_date or "current-view"
        mode_part = mode.replace(" ", "_").lower()
        path_raw = self.filedialog.asksaveasfilename(
            title="Export current schedule view as PowerPoint",
            defaultextension=".pptx",
            initialfile=f"schedule_{date_part}_{mode_part}.pptx",
            filetypes=[("PowerPoint", "*.pptx")],
        )
        if not path_raw:
            return
        out_path = Path(path_raw)
        visible = [selected_date] if selected_date else None
        layout = self._build_current_grid_layout(profile, self.last_result, visible_dates=visible)
        title = f"Schedule {date_part} | {mode} | {program_filter}"
        self.status_var.set("Status: Exporting PowerPoint...")

        def worker() -> Path:
            export_layout_to_pptx(layout, out_path, title)
            return out_path

        self._run_in_background(
            worker,
            lambda p: (self.status_var.set(f"Status: Exported PowerPoint to {p}"), self.messagebox.showinfo("Export complete", f"PowerPoint exported to:\n{p}")),
            lambda e: self.messagebox.showerror("PowerPoint export failed", str(e)),
        )

    def _on_grid_right_click(self, event) -> None:
        if not self.loaded_profile or not self.last_result:
            return
        canvas = self.grid_canvas
        clicked = canvas.find_overlapping(canvas.canvasx(event.x), canvas.canvasy(event.y), canvas.canvasx(event.x), canvas.canvasy(event.y))
        req_id = None
        for item in reversed(clicked):
            for tag in canvas.gettags(item):
                if tag.startswith("req:"):
                    req_id = tag.split(":", 1)[1]
                    break
            if req_id:
                break
        if not req_id:
            return
        self.selected_request_id = req_id
        self.status_var.set(f"Status: Selected appointment {req_id}. Click Delete Appointment to remove it.")
        self._render_patient_grid(self.loaded_profile, self.last_result)
    def _payload_for_date(self, date_key: str) -> Dict[str, Any]:
        profile = self._require_profile()
        day_dt = date.fromisoformat(date_key)
        requests = [r for r in profile.get("requests", []) if r.get("date_key") == date_key]
        return {
            "date_key": date_key,
            "weekday": day_dt.weekday(),
            "day_window": profile["day_window"],
            "providers": profile["providers"],
            "patients": profile["patients"],
            "rooms": profile["rooms"],
            "requests": requests,
        }

    def _run_auto_reorganize_for_dates(self, date_keys: List[str]) -> None:
        profile = self._require_profile()
        by_id = {r["id"]: r for r in profile.get("requests", []) if "id" in r}
        for date_key in date_keys:
            payload = self._payload_for_date(date_key)
            if not payload["requests"]:
                continue
            try:
                result = handle_generate(payload)
            except Exception:
                continue
            for req_id, assignment in result.get("assignments", {}).items():
                req = by_id.get(req_id)
                if not req:
                    continue
                req["provider_id"] = assignment["provider_id"]
                req["room_id"] = assignment["room_id"]
                req["preferred_window"] = {
                    "start_minute": assignment["start_minute"],
                    "end_minute": assignment["end_minute"],
                }

    def run_health_check(self) -> None:
        result = run_health_check()
        self.status_var.set(f"Status: Health Check {'PASS' if result['all_ok'] else 'FAIL'}")
        self._set_text(self.summary_text, json.dumps(result, indent=2))

    def new_blank_profile(self) -> None:
        today = datetime.utcnow().date()
        date_keys = planning_dates(today)
        self.loaded_profile = {
            "date_key": date_keys[0],
            "planning_dates": date_keys,
            "weekday": today.weekday(),
            "day_window": {"start_minute": GRID_START_MINUTE, "end_minute": GRID_END_MINUTE},
            "providers": [],
            "patients": [],
            "rooms": [],
            "requests": [],
        }
        self._sync_profile_resources(self.loaded_profile)
        self.loaded_profile_path = None
        self.manual_undo_stack = []
        self.selected_request_id = None
        self.profile_var.set("Profile: unsaved")
        self.status_var.set("Status: Created blank profile")
        self._refresh_profile_preview()

    def load_profile_from_file(self) -> None:
        path_raw = self.filedialog.askopenfilename(title="Select profile JSON", filetypes=[("JSON files", "*.json")])
        if not path_raw:
            return
        path = Path(path_raw)
        self.loaded_profile = load_profile(path)
        if "planning_dates" not in self.loaded_profile:
            start = date.fromisoformat(self.loaded_profile.get("date_key", date_to_key(datetime.utcnow().date())))
            self.loaded_profile["planning_dates"] = planning_dates(start)
        self.loaded_profile_path = path
        self.manual_undo_stack = []
        self.selected_request_id = None
        self._sync_profile_resources(self.loaded_profile)
        self.profile_var.set(f"Profile: {path}")
        self.status_var.set("Status: Profile loaded")
        self._refresh_profile_preview()

    def apply_calendar_settings(self) -> None:
        profile = self._require_profile()
        self._push_manual_undo_snapshot("Apply calendar settings")
        start_date = parse_date_parts(self.cal_year_var.get(), self.cal_month_var.get(), self.cal_day_var.get())
        start_minute = parse_time_input(self.day_start_var.get())
        end_minute = parse_time_input(self.day_end_var.get())
        if end_minute <= start_minute:
            raise ValueError("Day end must be after day start")

        profile["date_key"] = date_to_key(start_date)
        profile["planning_dates"] = planning_dates(start_date)
        profile["weekday"] = start_date.weekday()
        profile["day_window"] = {"start_minute": start_minute, "end_minute": end_minute}
        self._sync_profile_resources(profile)
        validate_profile(profile)
        self.status_var.set("Status: Calendar settings applied")
        self._refresh_profile_preview()

    def add_new_provider(self) -> None:
        name = self.provider_new_var.get().strip()
        self._push_manual_undo_snapshot("Add provider")
        if not name:
            raise ValueError("Enter a provider name to add")
        if name in self.provider_catalog:
            raise ValueError("Provider already exists")
        self.provider_catalog = sorted(self.provider_catalog + [name])
        self.provider_profiles.append({"provider_id": name, "provider_name": name, "discipline": "", "availability_templates": [], "exceptions": []})
        self._persist_provider_catalog()
        if self.loaded_profile:
            self._sync_profile_resources(self.loaded_profile)
        self.provider_new_var.set("")
        self.status_var.set(f"Status: Added provider {name}")
        self._refresh_profile_preview()

    def remove_provider(self) -> None:
        name = self.provider_selected_var.get().strip()
        self._push_manual_undo_snapshot("Remove provider")
        if not name:
            raise ValueError("Select a provider to remove")
        if name not in self.provider_catalog:
            raise ValueError("Selected provider not found")
        if len(self.provider_catalog) <= 1:
            raise ValueError("Cannot remove the last provider")
        self.provider_catalog = [p for p in self.provider_catalog if p != name]
        self.provider_profiles = [p for p in self.provider_profiles if p.get("provider_id") != name and p.get("provider_name") != name and p.get("id") != name]
        self._persist_provider_catalog()
        if self.loaded_profile:
            self._sync_profile_resources(self.loaded_profile)
        self.status_var.set(f"Status: Removed provider {name}")
        self._refresh_profile_preview()

    def set_provider_availability_window(self) -> None:
        selected_name = self.provider_selected_var.get().strip()
        if not selected_name:
            raise ValueError("Select a provider first")

        weekday_map = {"Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3, "Friday": 4}
        weekday_name = self.provider_avail_weekday_var.get().strip()
        weekday = weekday_map.get(weekday_name)
        if weekday is None:
            raise ValueError("Weekday must be Monday-Friday")

        start = parse_time_input(self.provider_avail_start_var.get())
        end = parse_time_input(self.provider_avail_end_var.get())
        if end <= start:
            raise ValueError("Availability end must be after start")

        self._push_manual_undo_snapshot("Set provider availability window")
        updated = False
        for profile in self.provider_profiles:
            p_name = str(profile.get("provider_name") or "").strip()
            if p_name != selected_name:
                continue
            templates = profile.get("availability_templates")
            if not isinstance(templates, list):
                templates = []
            replaced = False
            for t in templates:
                if int(t.get("weekday", -1)) == weekday:
                    t["windows"] = [{"start_minute": start, "end_minute": end}]
                    replaced = True
                    break
            if not replaced:
                templates.append({"weekday": weekday, "windows": [{"start_minute": start, "end_minute": end}]})
            profile["availability_templates"] = templates
            updated = True
            break

        if not updated:
            self.provider_profiles.append(
                {
                    "provider_id": selected_name,
                    "provider_name": selected_name,
                    "discipline": "",
                    "availability_templates": [{"weekday": weekday, "windows": [{"start_minute": start, "end_minute": end}]}],
                    "exceptions": [],
            "allowed_rooms": list(PREDEFINED_ROOMS),
                }
            )

        self._persist_provider_catalog()
        if self.loaded_profile:
            self._sync_profile_resources(self.loaded_profile)
            self.last_result = build_live_result_from_profile(self.loaded_profile)
            self._render_patient_grid(self.loaded_profile, self.last_result)
        self._refresh_profile_preview()
        self.status_var.set(f"Status: Updated availability window for {selected_name} ({weekday_name})")

    def _allowed_rooms_for_provider(self, provider_profile: Dict[str, Any], discipline: str) -> List[str]:
        allowed = provider_profile.get("allowed_rooms") or ["Any compatible room"]
        if "Any compatible room" in allowed:
            return list(PREDEFINED_ROOMS)
        return [r for r in allowed if r in PREDEFINED_ROOMS]

    def _on_manual_provider_selected(self) -> None:
        provider_name = self.appt_provider_var.get().strip()
        profile = self._provider_profile_by_name(provider_name)
        if not profile:
            self.appt_room_var.set(PREDEFINED_ROOMS[0])
            return
        discipline = str(profile.get("discipline", "")).strip()
        if discipline:
            self.appt_discipline_var.set(discipline)
        rooms = self._allowed_rooms_for_provider(profile, self.appt_discipline_var.get().strip())
        if hasattr(self, "appt_room_combo"):
            self.appt_room_combo["values"] = rooms
        if self.appt_room_var.get() not in rooms and rooms:
            self.appt_room_var.set(rooms[0])

    def delete_selected_appointment(self) -> None:
        profile = self._require_profile()
        if not self.selected_request_id:
            raise ValueError("Right-click an appointment block first to select it")

        before = len(profile.get("requests", []))
        self._push_manual_undo_snapshot("Delete appointment")
        profile["requests"] = [r for r in profile.get("requests", []) if r.get("id") != self.selected_request_id]
        after = len(profile.get("requests", []))
        if after == before:
            raise ValueError("Selected appointment was not found")

        self.selected_request_id = None
        self.last_result = build_live_result_from_profile(profile)
        self._render_patient_grid(profile, self.last_result)
        self._refresh_profile_preview()
        self.status_var.set("Status: Deleted selected appointment")

    def add_appointment(self) -> None:
        profile = self._require_profile()
        self._push_manual_undo_snapshot("Add appointment")
        patient_id = self.appt_patient_var.get().strip()
        provider_name = self.appt_provider_var.get().strip()
        provider_profile = self._provider_profile_by_name(provider_name)
        if not provider_profile:
            raise ValueError("Selected provider profile not found")
        provider_id = str(provider_profile.get("provider_id", provider_name))
        room_id = self.appt_room_var.get().strip()
        mode = self.appt_mode_var.get().strip().lower()
        discipline = self.appt_discipline_var.get().strip()
        date_key = self.appt_date_var.get().strip()
        program_type = self.appt_program_var.get().strip() or ("EVAL" if patient_id.startswith("E") else "IOP")

        if date_key not in profile.get("planning_dates", []):
            raise ValueError("Appointment date must be within current planning dates")

        profile_discipline = str(provider_profile.get("discipline", "")).strip()
        if profile_discipline and discipline != profile_discipline:
            raise ValueError(f"Provider discipline is '{profile_discipline}'. Choose matching discipline.")

        allowed_rooms = self._allowed_rooms_for_provider(provider_profile, discipline)
        if room_id not in allowed_rooms:
            raise ValueError("Selected room is not allowed for this provider profile")

        start_minute = parse_time_input(self.appt_start_var.get())
        end_minute = parse_time_input(self.appt_end_var.get())
        if end_minute <= start_minute:
            raise ValueError("Appointment end time must be after begin time")

        weekday = date.fromisoformat(date_key).weekday()
        if not provider_is_available(provider_profile, date_key=date_key, weekday=weekday, start_minute=start_minute, end_minute=end_minute):
            raise ValueError("Provider is unavailable for the selected date/time based on profile availability/exceptions")
        if not room_is_available(self.room_rules, room_id=room_id, date_key=date_key, weekday=weekday, start_minute=start_minute, end_minute=end_minute):
            raise ValueError("Selected room is unavailable for the chosen date/time based on Room Rules")

        for existing in profile.get("requests", []):
            if existing.get("date_key") != date_key:
                continue
            ex_window = existing.get("preferred_window") or {}
            ex_start = int(ex_window.get("start_minute", 0))
            ex_end = int(ex_window.get("end_minute", 0))
            if not is_overlap(start_minute, end_minute, ex_start, ex_end):
                continue
            ex_mode = str(existing.get("mode", "individual")).lower()
            if mode == "individual" or ex_mode == "individual":
                if existing.get("room_id") == room_id:
                    raise ValueError("Conflict: individual appointment cannot overlap in the same room")
                if existing.get("provider_id") == provider_id:
                    raise ValueError("Conflict: individual appointment cannot overlap with the same provider")
                if patient_id in existing.get("patient_ids", []):
                    raise ValueError("Conflict: patient already has an overlapping appointment")

        req_id = f"appt_{len(profile.get('requests', [])) + 1}"
        appointment_id = f"appt_{uuid.uuid4().hex[:12]}"
        profile["requests"].append(
            {
                "id": req_id,
                "patient_ids": [patient_id],
                "discipline": discipline,
                "duration_minutes": end_minute - start_minute,
                "mode": mode,
                "date_key": date_key,
                "preferred_window": {"start_minute": start_minute, "end_minute": end_minute},
                "group_key": None,
                "label": f"{discipline} (Patient {patient_id})",
                "provider_id": provider_id,
                "provider_name": provider_name,
                "room_id": room_id,
                "program_type": program_type,
                "soft_locked": False,
                "appointment_id": appointment_id,
            }
        )

        self._run_auto_reorganize_for_dates([date_key])
        self.selected_request_id = None
        live_result = build_live_result_from_profile(profile)
        self.last_result = live_result
        self.status_var.set(f"Status: Added appointment {req_id}")
        self._render_patient_grid(profile, live_result)
        self._refresh_profile_preview()

    def add_requirement_window(self) -> None:
        start_minute = parse_time_input(self.auto_window_start_var.get())
        end_minute = parse_time_input(self.auto_window_end_var.get())
        if end_minute <= start_minute:
            raise ValueError("Window end must be after start")

        token = f"{self.auto_window_start_var.get()}-{self.auto_window_end_var.get()}"
        existing = [w.strip() for w in self.auto_windows_var.get().split(",") if w.strip()]
        if token not in existing:
            existing.append(token)
        self.auto_windows_var.set(", ".join(existing))

    def _parse_windows_from_ui(self) -> List[Dict[str, int]]:
        raw = [w.strip() for w in self.auto_windows_var.get().split(",") if w.strip()]
        if not raw:
            raw = [f"{self.auto_window_start_var.get()}-{self.auto_window_end_var.get()}"]

        windows: List[Dict[str, int]] = []
        for item in raw:
            if "-" not in item:
                raise ValueError(f"Invalid window '{item}'. Use HHMM-HHMM")
            start_raw, end_raw = [p.strip() for p in item.split("-", 1)]
            start = parse_time_input(start_raw)
            end = parse_time_input(end_raw)
            if end <= start:
                raise ValueError(f"Invalid window '{item}': end must be after start")
            windows.append({"start_minute": start, "end_minute": end})
        return windows

    def _parse_eval_windows_from_ui(self) -> List[Dict[str, int]]:
        raw = [w.strip() for w in self.eval_windows_var.get().split(",") if w.strip()]
        if not raw:
            raw = [f"{self.eval_window_start_var.get()}-{self.eval_window_end_var.get()}"]
        windows: List[Dict[str, int]] = []
        for item in raw:
            if "-" not in item:
                raise ValueError(f"Invalid window '{item}'. Use HHMM-HHMM")
            start_raw, end_raw = [p.strip() for p in item.split("-", 1)]
            start = parse_time_input(start_raw)
            end = parse_time_input(end_raw)
            if end <= start:
                raise ValueError(f"Invalid window '{item}': end must be after start")
            windows.append({"start_minute": start, "end_minute": end})
        return windows

    def _selected_weeks(self) -> List[int]:
        weeks: List[int] = []
        if self.auto_week_1_var.get():
            weeks.append(1)
        if self.auto_week_2_var.get():
            weeks.append(2)
        if self.auto_week_3_var.get():
            weeks.append(3)
        if not weeks:
            raise ValueError("Select at least one week")
        return weeks

    def _selected_patient_scope_ids(self) -> List[str]:
        raw = [p.strip() for p in self.auto_subset_patients_var.get().split(",") if p.strip()]
        for pid in raw:
            if pid not in PATIENT_ID_CHOICES:
                raise ValueError(f"Patient ID '{pid}' must be in set I1-I30 or E1-E10")
        return raw

    def _selected_eval_weekdays(self) -> List[int]:
        weekday_map = {"Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3, "Friday": 4}
        picks = [self.eval_weekday_1_var.get(), self.eval_weekday_2_var.get(), self.eval_weekday_3_var.get()]
        out = [weekday_map[w] for w in picks if w not in ("", "(none)")]
        if not out:
            raise ValueError("Select at least one weekday")
        return sorted(set(out))

    def _selected_eval_scope_ids(self) -> List[str]:
        raw = [p.strip() for p in self.eval_subset_patients_var.get().split(",") if p.strip()]
        for pid in raw:
            if pid not in PATIENT_ID_CHOICES:
                raise ValueError(f"Patient ID '{pid}' must be in set I1-I30 or E1-E10")
        return raw

    def add_eval_requirement_window(self) -> None:
        start_minute = parse_time_input(self.eval_window_start_var.get())
        end_minute = parse_time_input(self.eval_window_end_var.get())
        if end_minute <= start_minute:
            raise ValueError("Window end must be after start")
        token = f"{self.eval_window_start_var.get()}-{self.eval_window_end_var.get()}"
        existing = [w.strip() for w in self.eval_windows_var.get().split(",") if w.strip()]
        if token not in existing:
            existing.append(token)
        self.eval_windows_var.set(", ".join(existing))

    def add_eval_condition(self) -> None:
        provider_choice = self.eval_provider_var.get().strip()
        room_choice = self.eval_room_var.get().strip()
        provider_id = "any"
        if provider_choice not in ("", "Any provider"):
            profile = self._provider_profile_by_name(provider_choice)
            provider_id = profile.get("provider_id") if profile else provider_choice
        condition = {
            "id": self.eval_req_id_var.get().strip() or f"eval_req_{len(self.eval_conditions)+1}",
            "discipline": self.eval_discipline_var.get().strip(),
            "provider_id": provider_id,
            "provider_ids": [] if provider_id == "any" else [provider_id],
            "room_id": "any" if room_choice == "Any compatible room" else room_choice,
            "duration_minutes": int(self.eval_duration_var.get()),
            "sessions_per_week": 1,
            "session_mode": self.eval_mode_var.get().strip(),
            "patient_scope": self.eval_scope_var.get().strip(),
            "patient_ids": self._selected_eval_scope_ids(),
            "weekdays": self._selected_eval_weekdays(),
            "weeks": [1],
            "time_windows": self._parse_eval_windows_from_ui(),
            "hard_constraint": bool(self.eval_hard_var.get()),
            "priority": int(self.eval_priority_var.get()),
            "source_program": "EVAL",
        }
        condition = validate_requirement(condition)
        if any(c["id"] == condition["id"] for c in self.eval_conditions):
            raise ValueError(f"Requirement ID '{condition['id']}' already exists")
        self.eval_conditions.append(condition)
        self._persist_requirements_catalog()
        self.eval_req_id_var.set(f"eval_req_{len(self.eval_conditions)+1}")
        self.eval_windows_var.set("")
        self._refresh_eval_condition_list()
        self._refresh_auto_condition_list()

    def _on_eval_requirement_select(self, _event=None) -> None:
        if not self.eval_condition_list.curselection():
            return
        idx = int(self.eval_condition_list.curselection()[0])
        if idx >= len(self.eval_conditions):
            return
        selected = self.eval_conditions[idx]
        self.eval_req_id_var.set(selected["id"])
        self.eval_discipline_var.set(selected.get("discipline", self.eval_discipline_var.get()))
        self.eval_mode_var.set(selected.get("session_mode", self.eval_mode_var.get()))
        self.eval_duration_var.set(str(selected.get("duration_minutes", self.eval_duration_var.get())))

    def edit_selected_eval_condition(self) -> None:
        selection = self.eval_condition_list.curselection()
        if not selection:
            raise ValueError("Select an EVAL requirement first")
        idx = int(selection[0])
        existing = self.eval_conditions[idx]
        updated = {
            **existing,
            "discipline": self.eval_discipline_var.get().strip(),
            "duration_minutes": int(self.eval_duration_var.get()),
            "session_mode": self.eval_mode_var.get().strip(),
            "time_windows": self._parse_eval_windows_from_ui(),
            "hard_constraint": bool(self.eval_hard_var.get()),
            "priority": int(self.eval_priority_var.get()),
            "weekdays": self._selected_eval_weekdays(),
            "patient_scope": self.eval_scope_var.get().strip(),
            "patient_ids": self._selected_eval_scope_ids(),
            "weeks": [1],
            "source_program": "EVAL",
        }
        self.eval_conditions[idx] = validate_requirement(updated)
        self._persist_requirements_catalog()
        self._refresh_eval_condition_list()
        self._refresh_auto_condition_list()

    def remove_selected_eval_condition(self) -> None:
        selection = self.eval_condition_list.curselection()
        if not selection:
            raise ValueError("Select an EVAL requirement first")
        idx = int(selection[0])
        self.eval_conditions.pop(idx)
        self._persist_requirements_catalog()
        self._refresh_eval_condition_list()
        self._refresh_auto_condition_list()

    def _refresh_eval_condition_list(self) -> None:
        if not hasattr(self, "eval_condition_list"):
            return
        self.eval_condition_list.delete(0, self.tk.END)
        if not self.eval_conditions:
            self.eval_condition_list.insert(self.tk.END, "No EVAL requirements added yet.")
            return
        name_map = ["Mon", "Tue", "Wed", "Thu", "Fri"]
        for idx, c in enumerate(self.eval_conditions, start=1):
            days = ",".join(name_map[d] for d in c["weekdays"])
            windows = "; ".join(f"{_to_ampm(w['start_minute'])}-{_to_ampm(w['end_minute'])}" for w in c["time_windows"])
            line = f"{idx}) [EVAL] {c['id']} | {c['discipline']} | {c['session_mode']} {c['duration_minutes']}m | days={days} | windows={windows}"
            self.eval_condition_list.insert(self.tk.END, line)

    def add_auto_condition(self) -> None:
        weekday_map = {"Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3, "Friday": 4}

        weekday_selections = [
            self.auto_weekday_1_var.get(),
            self.auto_weekday_2_var.get(),
            self.auto_weekday_3_var.get(),
            self.auto_weekday_4_var.get(),
            self.auto_weekday_5_var.get(),
        ]
        weekdays = [weekday_map[w] for w in weekday_selections if w not in ("", "(none)")]
        if not weekdays:
            raise ValueError("Select at least one weekday")

        scope = self.auto_scope_var.get().strip()
        subset_ids = self._selected_patient_scope_ids()

        provider_choice = self.auto_provider_var.get().strip()
        provider_choice_b = self.auto_provider_b_var.get().strip()
        provider_choice_c = self.auto_provider_c_var.get().strip()
        room_choice = self.auto_room_var.get().strip()

        provider_names = [p for p in [provider_choice, provider_choice_b, provider_choice_c] if p not in ("", "Any provider", "(none)")]
        provider_ids = []
        for name in provider_names:
            profile = self._provider_profile_by_name(name)
            provider_ids.append(profile.get("provider_id") if profile else name)

        condition = {
            "id": self.auto_req_id_var.get().strip() or f"req_{len(self.auto_conditions)+1}",
            "discipline": self.auto_discipline_var.get().strip(),
            "provider_id": "any" if provider_choice == "Any provider" and not provider_ids else (provider_ids[0] if provider_ids else provider_choice),
            "provider_ids": provider_ids,
            "room_id": "any" if room_choice == "Any compatible room" else room_choice,
            "duration_minutes": int(self.auto_duration_var.get()),
            "sessions_per_week": int(self.auto_sessions_per_week_var.get()),
            "session_mode": self.auto_mode_var.get().strip(),
            "patient_scope": scope,
            "patient_ids": subset_ids,
            "weekdays": sorted(set(weekdays)),
            "weeks": self._selected_weeks(),
            "time_windows": self._parse_windows_from_ui(),
            "hard_constraint": bool(self.auto_hard_var.get()),
            "priority": int(self.auto_priority_var.get()),
            "source_program": "IOP",
        }
        condition = validate_requirement(condition)

        if any(c["id"] == condition["id"] for c in self.auto_conditions):
            raise ValueError(f"Requirement ID '{condition['id']}' already exists")

        self.auto_conditions.append(condition)
        self._persist_requirements_catalog()
        self.auto_req_id_var.set(f"req_{len(self.auto_conditions)+1}")
        self.auto_windows_var.set("")
        self._refresh_auto_condition_list()
        self.status_var.set(f"Status: Added requirement {condition['id']}")

    def _on_requirement_select(self, _event=None) -> None:
        if not self.auto_condition_list.curselection():
            return
        idx = int(self.auto_condition_list.curselection()[0])
        if idx >= len(self.auto_condition_view_index):
            return
        source, real_idx = self.auto_condition_view_index[idx]
        if source != "IOP":
            self.status_var.set("Status: EVAL requirements are read-only in this view")
            return
        selected = self.auto_conditions[real_idx]
        self.auto_req_id_var.set(selected["id"])
        self.auto_discipline_var.set(selected.get("discipline", self.auto_discipline_var.get()))
        self.auto_mode_var.set(selected.get("session_mode", self.auto_mode_var.get()))
        self.auto_duration_var.set(str(selected.get("duration_minutes", self.auto_duration_var.get())))
        self.auto_sessions_per_week_var.set(str(selected.get("sessions_per_week", 1)))

    def edit_selected_condition(self) -> None:
        selection = self.auto_condition_list.curselection()
        if not selection:
            raise ValueError("Select a requirement in the list first")
        idx = int(selection[0])
        if idx >= len(self.auto_condition_view_index):
            raise ValueError("Selected requirement is out of range")
        source, real_idx = self.auto_condition_view_index[idx]
        if source != "IOP":
            raise ValueError("EVAL requirements are read-only in this list")
        existing = self.auto_conditions[real_idx]
        updated = {
            **existing,
            "discipline": self.auto_discipline_var.get().strip(),
            "duration_minutes": int(self.auto_duration_var.get()),
            "sessions_per_week": int(self.auto_sessions_per_week_var.get()),
            "session_mode": self.auto_mode_var.get().strip(),
            "time_windows": self._parse_windows_from_ui(),
            "hard_constraint": bool(self.auto_hard_var.get()),
            "priority": int(self.auto_priority_var.get()),
        }
        self.auto_conditions[real_idx] = validate_requirement(updated)
        self._persist_requirements_catalog()
        self._refresh_auto_condition_list()
        self.status_var.set(f"Status: Updated requirement {updated['id']}")

    def duplicate_selected_condition(self) -> None:
        selection = self.auto_condition_list.curselection()
        if not selection:
            raise ValueError("Select a requirement in the list first")
        idx = int(selection[0])
        if idx >= len(self.auto_condition_view_index):
            raise ValueError("Selected requirement is out of range")
        source_program, real_idx = self.auto_condition_view_index[idx]
        if source_program != "IOP":
            raise ValueError("EVAL requirements are read-only in this list")
        source = dict(self.auto_conditions[real_idx])
        base = source.get("id", "req") + "_copy"
        existing_ids = {c.get("id") for c in self.auto_conditions}
        new_id = base
        n = 2
        while new_id in existing_ids:
            new_id = f"{base}{n}"
            n += 1
        source["id"] = new_id
        self.auto_conditions.append(source)
        self._persist_requirements_catalog()
        self._refresh_auto_condition_list()
        self.status_var.set(f"Status: Duplicated requirement as {new_id}")

    def render_provider_availability_preview(self) -> None:
        if not hasattr(self, "provider_preview_canvas"):
            return
        selected_name = self.provider_selected_var.get().strip()
        if not selected_name:
            self.provider_preview_canvas.delete("all")
            self.provider_preview_canvas.create_text(10, 10, anchor="nw", text="Select a provider to preview availability.", fill="#495057")
            return
        provider = self._provider_profile_by_name(selected_name)
        if not provider:
            self.provider_preview_canvas.delete("all")
            self.provider_preview_canvas.create_text(10, 10, anchor="nw", text="No availability data for selected provider.", fill="#495057")
            return
        self._render_provider_preview_canvas(self.provider_preview_canvas, provider)

    def remove_selected_condition(self) -> None:
        selection = self.auto_condition_list.curselection()
        if not selection:
            raise ValueError("Select a requirement in the list first")
        idx = int(selection[0])
        if idx >= len(self.auto_condition_view_index):
            raise ValueError("Selected requirement is out of range")
        source, real_idx = self.auto_condition_view_index[idx]
        if source != "IOP":
            raise ValueError("EVAL requirements are read-only in this list")
        rid = self.auto_conditions[real_idx]["id"]
        self.auto_conditions.pop(real_idx)
        self._persist_requirements_catalog()
        self._refresh_auto_condition_list()
        self.status_var.set(f"Status: Removed requirement {rid}")

    def clear_auto_conditions(self) -> None:
        self.auto_conditions = []
        self._persist_requirements_catalog()
        self._refresh_auto_condition_list()
        self.status_var.set("Status: Cleared all requirements")

    def _refresh_auto_condition_list(self) -> None:
        self.auto_condition_list.delete(0, self.tk.END)
        self.auto_condition_view_index = []
        if not self.auto_conditions and not (hasattr(self, "show_eval_requirements_var") and self.show_eval_requirements_var.get() and self.eval_conditions):
            self.auto_condition_list.insert(self.tk.END, "No conditions added yet.")
            return

        name_map = ["Mon", "Tue", "Wed", "Thu", "Fri"]
        display_rows: List[Tuple[str, Dict[str, Any], int]] = [("IOP", c, idx) for idx, c in enumerate(self.auto_conditions)]
        if hasattr(self, "show_eval_requirements_var") and self.show_eval_requirements_var.get():
            display_rows.extend(("EVAL", c, idx) for idx, c in enumerate(self.eval_conditions))

        for disp_idx, (source_program, c, original_idx) in enumerate(display_rows, start=1):
            days = ",".join(name_map[d] for d in c["weekdays"])
            windows = "; ".join(f"{_to_ampm(w['start_minute'])}-{_to_ampm(w['end_minute'])}" for w in c["time_windows"])
            hard_soft = "hard" if c.get("hard_constraint", True) else "soft"
            provider_keys = c.get("provider_ids") or ([c.get("provider_id", "any")] if c.get("provider_id", "any") != "any" else ["any"])
            providers = [self._provider_display_name(p) if p != "any" else "any" for p in provider_keys]
            line = (
                f"{disp_idx}) [{source_program}] {c['id']} | {c['discipline']} | providers={','.join(providers)} | room={c['room_id']} | "
                f"{c['session_mode']} {c['duration_minutes']}m x{c.get('sessions_per_week',1)}/wk | scope={c['patient_scope']} | days={days} | weeks={c['weeks']} | "
                f"windows={windows} | {hard_soft} p={c.get('priority', 100)}"
            )
            self.auto_condition_list.insert(self.tk.END, line)
            self.auto_condition_view_index.append((source_program, original_idx))
            if source_program == "IOP":
                self.auto_condition_list.itemconfig(self.auto_condition_list.size() - 1, foreground="#0b2d5c")
            else:
                self.auto_condition_list.itemconfig(self.auto_condition_list.size() - 1, foreground="#b00020")

    def _solver_limits_from_ui(self) -> Dict[str, int]:
        effort = self.auto_solver_effort_var.get().strip().lower()
        mapping = {
            "standard": {"max_backtrack_states": 250000, "max_candidates_per_request": 5000},
            "high": {"max_backtrack_states": 750000, "max_candidates_per_request": 10000},
            "very high": {"max_backtrack_states": 1500000, "max_candidates_per_request": 20000},
            "maximum": {"max_backtrack_states": 3000000, "max_candidates_per_request": 30000},
        }
        return mapping.get(effort, mapping["high"])

    def _build_auto_profile_template(self) -> Dict[str, Any]:
        start = parse_date_parts(self.auto_start_year_var.get(), self.auto_start_month_var.get(), self.auto_start_day_var.get())
        patient_count = int(self.auto_patients_var.get())
        if patient_count < 1:
            raise ValueError("Patient count must be at least 1")

        date_keys = planning_dates(start, weeks=3)
        day_start = parse_time_input(self.day_start_var.get())
        day_end = parse_time_input(self.day_end_var.get())

        return {
            "date_key": date_keys[0],
            "planning_dates": date_keys,
            "weekday": start.weekday(),
            "day_window": {"start_minute": day_start, "end_minute": day_end},
            "providers": build_provider_records_from_profiles(self.provider_profiles, day_start, day_end)
            or build_provider_records(self.provider_catalog, day_start, day_end),
            "patients": build_patient_records(date_keys, day_start, day_end)[:patient_count],
            "rooms": build_room_records(self.room_rules),
        }

    def _update_after_auto_generation(self, profile_template: Dict[str, Any], result: Dict[str, Any], default_program_type: str = "IOP") -> None:
        request_lookup = {r.get("id"): r for r in result.get("requests", [])}
        generated_requests: List[Dict[str, Any]] = []
        for rid, assignment in result.get("assignments", {}).items():
            src = request_lookup.get(rid, {})
            generated_requests.append(
                {
                    "id": rid,
                    "appointment_id": src.get("appointment_id") or f"appt_{uuid.uuid4().hex[:12]}",
                    "patient_ids": list(src.get("patient_ids", [])),
                    "discipline": src.get("discipline", assignment.get("label", "Session")),
                    "duration_minutes": int(assignment.get("end_minute", 0)) - int(assignment.get("start_minute", 0)),
                    "mode": src.get("mode", assignment.get("mode", "individual")),
                    "date_key": src.get("date_key", assignment.get("date_key")),
                    "preferred_window": {"start_minute": int(assignment.get("start_minute", 0)), "end_minute": int(assignment.get("end_minute", 0))},
                    "group_key": src.get("group_key"),
                    "label": assignment.get("label") or src.get("label") or src.get("discipline", "Session"),
                    "provider_id": assignment.get("provider_id"),
                    "room_id": assignment.get("room_id"),
                    "program_type": src.get("program_type", default_program_type),
                    "soft_locked": bool(src.get("soft_locked", True)),
                }
            )
        profile = {
            **profile_template,
            "requests": generated_requests,
        }
        self.loaded_profile = profile
        self.last_result = {"assignments": result.get("assignments", {}), "room_timeline": {}}
        self._refresh_date_dropdowns()
        self._render_patient_grid(profile, self.last_result)
        self._refresh_profile_preview()
        save_last_generated_schedule(result)
        if hasattr(self, "main_notebook") and hasattr(self, "manual_tab"):
            self.main_notebook.select(self.manual_tab)

    def generate_auto_schedule(self) -> None:
        if not self.auto_conditions:
            raise ValueError("Add at least one requirement before auto-generating")

        profile_template = self._build_auto_profile_template()
        result = generate_three_week_schedule(
            profile_template=profile_template,
            requirements=self.auto_conditions,
            previous_assignments=self._existing_assignment_map(),
            solver_limits=self._solver_limits_from_ui(),
            locked_request_ids=self._soft_locked_request_ids(),
        )
        self._push_manual_undo_snapshot("Generate IOP schedule")
        self._update_after_auto_generation(profile_template, result)

        if not result.get("ok"):
            issues = result.get("report", {}).get("issues", [])
            text = "Auto-generation failed.\n" + "\n".join(issues[:25] or ["No detailed bottlenecks available."])
            self._set_text(self.auto_report_text, text)
            self.status_var.set("Status: Auto-generation failed")
            return

        diff = result.get("diff", {})
        bottlenecks = result.get("bottlenecks", [])
        lines = [
            "Auto-generation completed.",
            f"Assigned requests: {len(result.get('assignments', {}))}",
            f"Unchanged: {diff.get('unchanged', 0)} | Moved: {diff.get('moved', 0)} | Added: {diff.get('added', 0)}",
        ]
        if bottlenecks:
            lines.append("Soft requirement bottlenecks:")
            lines.extend([f"- {b['date_key']} {b['requirement_id']}: {b['reason']}" for b in bottlenecks[:10]])
        self._set_text(self.auto_report_text, "\n".join(lines))
        self.status_var.set("Status: Auto-generation completed")

    def auto_reconfigure_existing_schedule(self) -> None:
        if not self.auto_conditions:
            raise ValueError("Add requirements before auto reconfigure")
        profile_template = self._build_auto_profile_template()
        existing = {}
        if self.last_result:
            existing = dict(self.last_result.get("assignments", {}))
        result = auto_reconfigure_schedule(
            profile_template=profile_template,
            requirements=self.auto_conditions,
            existing_assignments=existing,
            solver_limits=self._solver_limits_from_ui(),
        )
        self._push_manual_undo_snapshot("Auto reconfigure IOP")
        self._update_after_auto_generation(profile_template, result)
        diff = result.get("diff", {})
        self._set_text(
            self.auto_report_text,
            "Auto reconfigure finished.\n"
            f"Unchanged: {diff.get('unchanged', 0)}\n"
            f"Moved: {diff.get('moved', 0)}\n"
            f"Added: {diff.get('added', 0)}\n"
            f"Removed: {diff.get('removed', 0)}",
        )
        self.status_var.set("Status: Auto reconfigure completed")

    def explain_auto_bottleneck(self) -> None:
        profile_template = self._build_auto_profile_template()
        report = explain_infeasibility(
            self.auto_conditions,
            profile_template["planning_dates"],
            profile_template["providers"],
            profile_template["rooms"],
            [p["id"] for p in profile_template["patients"]],
        )
        if report.get("ok"):
            self._set_text(self.auto_report_text, "No obvious infeasibility detected in pre-check.")
            self.status_var.set("Status: Bottleneck pre-check found no hard conflicts")
            return

        lines = ["Top bottlenecks:"] + [f"- {issue}" for issue in report.get("issues", [])[:25]]
        self._set_text(self.auto_report_text, "\n".join(lines))
        self.status_var.set("Status: Bottleneck report generated")

    def _existing_assignment_map(self) -> Dict[str, Dict[str, Any]]:
        return dict((self.last_result or {}).get("assignments", {}))

    def _soft_locked_request_ids(self) -> List[str]:
        if not self.loaded_profile:
            return []
        return [r.get("id") for r in self.loaded_profile.get("requests", []) if r.get("soft_locked") and r.get("id")]

    def generate_eval_schedule(self) -> None:
        profile_template = self._build_auto_profile_template()
        start = parse_date_parts(self.eval_start_year_var.get(), self.eval_start_month_var.get(), self.eval_start_day_var.get())
        result = generate_eval_schedule(
            profile_template=profile_template,
            cohort_start=start,
            cohort_type=self.eval_cohort_var.get(),
            eval_patient_count=int(self.eval_patient_count_var.get()),
            group_duration_minutes=int(self.eval_group_duration_var.get()),
            group_start_time=parse_time_input(self.eval_group_start_var.get()),
            previous_assignments=self._existing_assignment_map(),
            solver_limits=self._solver_limits_from_ui(),
            locked_request_ids=self._soft_locked_request_ids(),
        )
        self._push_manual_undo_snapshot("Generate EVAL schedule")
        self._update_after_auto_generation(profile_template, result, default_program_type="EVAL")
        lines = [
            "EVAL generation completed." if result.get("ok") else "EVAL generation failed.",
            f"Assignments: {len(result.get('assignments', {}))}",
            f"Moved: {result.get('diff', {}).get('moved', 0)} | Added: {result.get('diff', {}).get('added', 0)}",
        ]
        if result.get("bottlenecks"):
            lines.append("Bottlenecks:")
            lines.extend(f"- {b['date_key']} {b['requirement_id']}: {b['reason']}" for b in result.get("bottlenecks", [])[:10])
        self._set_text(self.eval_report_text, "\n".join(lines))
        self.status_var.set("Status: EVAL generation complete" if result.get("ok") else "Status: EVAL generation failed")

    def generate_combined_schedule(self) -> None:
        if not self.auto_conditions:
            raise ValueError("Add at least one IOP requirement before combined generation")
        profile_template = self._build_auto_profile_template()
        existing = self._existing_assignment_map()
        soft_locked_ids = self._soft_locked_request_ids()

        iop_result = generate_three_week_schedule(
            profile_template=profile_template,
            requirements=self.auto_conditions,
            previous_assignments=existing,
            solver_limits=self._solver_limits_from_ui(),
            locked_request_ids=soft_locked_ids,
        )

        start = parse_date_parts(self.eval_start_year_var.get(), self.eval_start_month_var.get(), self.eval_start_day_var.get())
        eval_result = generate_eval_schedule(
            profile_template=profile_template,
            cohort_start=start,
            cohort_type=self.eval_cohort_var.get(),
            eval_patient_count=int(self.eval_patient_count_var.get()),
            group_duration_minutes=int(self.eval_group_duration_var.get()),
            group_start_time=parse_time_input(self.eval_group_start_var.get()),
            previous_assignments={**existing, **iop_result.get("assignments", {})},
            solver_limits=self._solver_limits_from_ui(),
            locked_request_ids=soft_locked_ids,
        )

        merged_requests = list(iop_result.get("requests", [])) + list(eval_result.get("requests", []))
        merged_assignments = {**iop_result.get("assignments", {}), **eval_result.get("assignments", {})}
        merged_result = {
            "ok": bool(iop_result.get("ok")) and bool(eval_result.get("ok")),
            "requests": merged_requests,
            "assignments": merged_assignments,
            "bottlenecks": list(iop_result.get("bottlenecks", [])) + list(eval_result.get("bottlenecks", [])),
            "diff": {
                "unchanged": iop_result.get("diff", {}).get("unchanged", 0) + eval_result.get("diff", {}).get("unchanged", 0),
                "moved": iop_result.get("diff", {}).get("moved", 0) + eval_result.get("diff", {}).get("moved", 0),
                "added": iop_result.get("diff", {}).get("added", 0) + eval_result.get("diff", {}).get("added", 0),
                "removed": iop_result.get("diff", {}).get("removed", 0) + eval_result.get("diff", {}).get("removed", 0),
                "by_date": {},
            },
        }
        self._push_manual_undo_snapshot("Generate combined schedule")
        self._update_after_auto_generation(profile_template, merged_result, default_program_type="IOP")
        self._set_text(
            self.eval_report_text,
            "Combined generation complete.\n"
            f"IOP ok={iop_result.get('ok')} EVAL ok={eval_result.get('ok')}\n"
            f"Moved={merged_result['diff']['moved']} Added={merged_result['diff']['added']}\n"
            f"Soft-locked considered: {len(soft_locked_ids)}",
        )
        self.status_var.set("Status: Combined generation completed")

    def import_existing_schedule_json(self) -> None:
        path_raw = self.filedialog.askopenfilename(title="Import schedule JSON", filetypes=[("JSON files", "*.json")])
        if not path_raw:
            return
        payload = json.loads(Path(path_raw).read_text(encoding="utf-8"))
        incoming = payload.get("requests") if isinstance(payload, dict) else []
        if not isinstance(incoming, list):
            raise ValueError("JSON must contain a top-level 'requests' list")
        profile = self._require_profile()
        imported = []
        for item in incoming:
            if not isinstance(item, dict):
                continue
            req = dict(item)
            req.setdefault("id", f"import_{uuid.uuid4().hex[:10]}")
            req.setdefault("appointment_id", req["id"])
            req.setdefault("program_type", "EVAL" if any(str(pid).startswith("E") for pid in req.get("patient_ids", [])) else "IOP")
            req["soft_locked"] = True
            imported.append(req)
        profile.setdefault("requests", []).extend(imported)
        self.last_result = build_live_result_from_profile(profile)
        self._render_patient_grid(profile, self.last_result)
        self._refresh_profile_preview()
        self._set_text(self.eval_report_text, f"Imported {len(imported)} appointments as soft-locked.")
        self.status_var.set("Status: Imported existing schedule")

    def save_current_profile(self) -> None:
        profile = self._require_profile()
        default = self.loaded_profile_path or (Path("profiles") / "profile.json")
        path_raw = self.filedialog.asksaveasfilename(
            title="Save current profile",
            defaultextension=".json",
            initialfile=default.name,
            initialdir=str(default.parent),
            filetypes=[("JSON files", "*.json")],
        )
        if not path_raw:
            return
        path = Path(path_raw)
        save_profile(path, profile)
        self.loaded_profile_path = path
        self.profile_var.set(f"Profile: {path}")
        self.status_var.set("Status: Profile saved")
        self._autosave_profile()
        if hasattr(self, "provider_preview_canvas"):
            self.render_provider_availability_preview()

    def generate_from_loaded_profile(self) -> None:
        profile = self._require_profile()
        self._run_auto_reorganize_for_dates(profile.get("planning_dates", []))
        self.last_result = build_live_result_from_profile(profile)
        save_last_schedule(self.last_result)
        summary = summarize_schedule(self.last_result)
        self.status_var.set(f"Status: Generated {summary.assignment_count} sessions across planning dates")
        self._render_patient_grid(profile, self.last_result)

    def export_schedule(self) -> None:
        if not self.last_result:
            self.messagebox.showinfo("Export", "Add or generate a schedule first.")
            return
        default = Path("output") / "generated_schedule.json"
        path_raw = self.filedialog.asksaveasfilename(
            title="Export generated schedule",
            defaultextension=".json",
            initialfile="generated_schedule.json",
            initialdir=str(default.parent),
            filetypes=[("JSON files", "*.json")],
        )
        if not path_raw:
            return
        out_path = Path(path_raw)
        save_json(out_path, self.last_result)
        self.status_var.set(f"Status: Exported to {out_path}")

    def run(self) -> None:
        self.root.mainloop()


def launch_gui() -> int:
    app = SchedulerDesktopApp()
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(launch_gui())
