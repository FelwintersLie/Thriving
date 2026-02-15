from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Tuple

from app.api_server import handle_generate
from app.auto_scheduler import (
    auto_reconfigure_schedule,
    explain_infeasibility,
    generate_three_week_schedule,
    validate_requirement,
)
from app.health_check import run_health_check
from app.persistence import (
    load_last_generated_schedule,
    load_last_profile,
    load_provider_catalog,
    load_provider_profiles,
    load_requirements_catalog,
    save_last_generated_schedule,
    save_last_profile,
    save_last_schedule,
    save_provider_catalog,
    save_provider_profiles,
    save_requirements_catalog,
)
from app.profile_io import load_profile, save_profile, validate_profile
from app.windows_program import save_json

DISCIPLINES = [
    "Primary Care",
    "Physical Therapy",
    "Speech-Language Pathology",
    "Athletic Trainer",
    "Dietician",
    "Neuropsychology",
    "Psychiatry",
    "Behavioral Health",
    "Moral Injury",
    "Reading Group",
    "Accupuncture",
    "Equine Therapy",
    "Pharmacology",
    "Art therapy group",
    "Sleep group",
    "Writing group",
    "Supplements group",
    "PT/Audiology Group",
    "Audiology",
    "Yoga",
]

PREDEFINED_ROOMS = [
    "Room 1",
    "Room 2",
    "Room 3",
    "Room 4",
    "Gym",
    "VNG Room",
    "Audiology Room",
    "Room 207",
    "Room 208",
    "Lounge",
    "Conference Room",
    "Jason's Office",
    "Brittany's Office",
    "Suite 1",
    "Suite 2",
    "Suite 3",
    "Off-site",
]

DEFAULT_PROVIDER_NAMES = [
    "Daniel Fenton",
    "Heidi Greata",
    "Elizabeth Watt",
    "Dana Lebo",
    "Shawn Kane",
    "Evan Vitello",
    "Robert Kanser",
    "Wesley Cole",
    "Ali Giacona",
    "Sarah Teague",
    "Carter Smith",
    "Michelle Ward",
    "Lisa Padgett",
    "Christine Flicek",
    "Equine",
    "Devon Weist",
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
PATIENT_ID_CHOICES = [str(i) for i in range(1, 100)]


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
        exceptions = [
            {
                "date_key": ex["date_key"],
                "window": {
                    "start_minute": int(ex["window"]["start_minute"]),
                    "end_minute": int(ex["window"]["end_minute"]),
                },
                "available_override": bool(ex.get("available_override", False)),
            }
            for ex in raw_exceptions
            if ex.get("date_key") and ex.get("window")
        ]

        discipline = str(profile.get("discipline", "")).strip()
        disciplines = [discipline] if discipline else list(DISCIPLINES)

        records.append(
            {
                "id": pid,
                "name": name,
                "disciplines": disciplines,
                "templates": templates,
                "exceptions": exceptions,
            }
        )

    return records

def build_room_records() -> List[Dict[str, Any]]:
    return [{"id": room, "name": room, "capacity": 10, "allowed_disciplines": list(DISCIPLINES)} for room in PREDEFINED_ROOMS]


def build_patient_records(date_keys: List[str], day_start: int, day_end: int) -> List[Dict[str, Any]]:
    availability = {d: [{"start_minute": day_start, "end_minute": day_end}] for d in date_keys}
    return [{"id": pid, "name": f"Patient {pid}", "availability": availability} for pid in PATIENT_ID_CHOICES]


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

        self.provider_catalog = load_provider_catalog(DEFAULT_PROVIDER_NAMES)
        self.provider_profiles = load_provider_profiles().get("providers", [])
        if not self.provider_profiles:
            self.provider_profiles = [
                {"provider_id": name, "provider_name": name, "discipline": "", "availability_templates": [], "exceptions": []}
                for name in self.provider_catalog
            ]
            save_provider_profiles(self.provider_profiles)
        self.last_generated_schedule = load_last_generated_schedule()
        self.last_result: Dict[str, Any] | None = None
        raw_conditions = load_requirements_catalog()
        self.auto_conditions: List[Dict[str, Any]] = []
        for condition in raw_conditions:
            try:
                self.auto_conditions.append(validate_requirement(condition))
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
        notebook.add(auto_tab, text="Auto Generator (3-week)")

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

        self._build_auto_generator_tab(auto_tab)

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
        ttk.Combobox(frame, textvariable=self.auto_patients_var, values=[str(i) for i in range(1, 100)], state="readonly", width=8).grid(row=1, column=0, padx=2)

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
        ttk.Combobox(cond, textvariable=self.auto_duration_var, values=["30", "45", "60", "75", "90"], state="readonly", width=8).grid(row=1, column=7, padx=2)

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
        ttk.Button(action_row, text="Add Requirement", command=lambda: self._safe_action(self.add_auto_condition)).pack(side="left", padx=4)
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

        appt = ttk.Labelframe(parent, text="Add Appointment", padding=6)
        appt.pack(fill="x", pady=(0, 6))

        self.appt_date_var = self.tk.StringVar(value=date_to_key(current))
        self.appt_patient_var = self.tk.StringVar(value=PATIENT_ID_CHOICES[0])
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

        self.ttk.Label(appt, text="Provider Name").grid(row=0, column=2, sticky="w")
        self.appt_provider_combo = self.ttk.Combobox(appt, textvariable=self.appt_provider_var, values=self.provider_catalog, state="readonly", width=20)
        self.appt_provider_combo.grid(row=1, column=2, padx=2)

        self.ttk.Label(appt, text="Room").grid(row=0, column=3, sticky="w")
        self.ttk.Combobox(appt, textvariable=self.appt_room_var, values=PREDEFINED_ROOMS, state="readonly", width=16).grid(row=1, column=3, padx=2)

        self.ttk.Label(appt, text="Type").grid(row=0, column=4, sticky="w")
        self.ttk.Combobox(appt, textvariable=self.appt_mode_var, values=["individual", "group"], state="readonly", width=12).grid(row=1, column=4, padx=2)

        self.ttk.Label(appt, text="Discipline").grid(row=0, column=5, sticky="w")
        self.ttk.Combobox(appt, textvariable=self.appt_discipline_var, values=DISCIPLINES, state="readonly", width=22).grid(row=1, column=5, padx=2)

        self.ttk.Label(appt, text="Begin").grid(row=0, column=6, sticky="w")
        self.ttk.Combobox(appt, textvariable=self.appt_start_var, values=time_choices, state="readonly", width=10).grid(row=1, column=6, padx=2)
        self.ttk.Label(appt, text="End").grid(row=0, column=7, sticky="w")
        self.ttk.Combobox(appt, textvariable=self.appt_end_var, values=time_choices, state="readonly", width=10).grid(row=1, column=7, padx=2)

        ttk.Button(appt, text="Add Appointment", command=lambda: self._safe_action(self.add_appointment)).grid(row=1, column=8, padx=6)
        ttk.Button(appt, text="Delete Appointment", command=lambda: self._safe_action(self.delete_selected_appointment)).grid(row=1, column=9, padx=6)
        ttk.Button(appt, text="Undo Manual Action", command=lambda: self._safe_action(self.undo_manual_action)).grid(row=1, column=10, padx=6)

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

        profile["rooms"] = build_room_records()
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

    def _refresh_provider_dropdowns(self) -> None:
        self.appt_provider_combo["values"] = self.provider_catalog
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
        if self.provider_catalog and self.appt_provider_var.get() not in self.provider_catalog:
            self.appt_provider_var.set(self.provider_catalog[0])
        if self.provider_catalog and self.provider_selected_var.get() not in self.provider_catalog:
            self.provider_selected_var.set(self.provider_catalog[0])

    def _refresh_profile_preview(self) -> None:
        if not self.loaded_profile:
            return
        profile = self.loaded_profile
        lines = [
            "Profile Overview",
            f"Start date: {profile.get('date_key')}",
            f"Planning days: {len(profile.get('planning_dates', []))}",
            f"Providers in catalog: {len(self.provider_catalog)}",
            "Patient IDs available: 1-99",
            f"Rooms: {len(PREDEFINED_ROOMS)} (fixed)",
            f"Appointments: {len(profile.get('requests', []))}",
        ]
        self._set_text(self.summary_text, "\n".join(lines))
        self._refresh_date_dropdowns()
        self._refresh_provider_dropdowns()
        self._autosave_profile()

    def _render_patient_grid(self, profile: Dict[str, Any], result: Dict[str, Any]) -> None:
        canvas = self.grid_canvas
        canvas.delete("all")
        minutes = _grid_minutes()

        request_map = {r["id"]: r for r in profile.get("requests", []) if "id" in r}
        appointments = []
        patient_ids = set()
        date_keys_in_use = set()

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
                    "provider": assignment.get("provider_id", ""),
                    "room": assignment.get("room_id", ""),
                    "patients": pids,
                }
            )
            patient_ids.update(pids)
            date_keys_in_use.add(date_key)

        planning_dates = profile.get("planning_dates", [])
        ordered_dates = [d for d in planning_dates if d in date_keys_in_use] + sorted(date_keys_in_use.difference(planning_dates))
        if not ordered_dates:
            ordered_dates = planning_dates[:]

        patient_labels = sorted(patient_ids, key=lambda x: int(x) if x.isdigit() else x)
        if not patient_labels:
            canvas.create_text(16, 20, anchor="w", text="No appointments scheduled yet.", fill="#003049", font=("Segoe UI", 11, "bold"))
            return

        time_col_w, header_h, row_h, patient_col_w = 85, 42, 22, 180
        col_pairs = [(d, p) for d in ordered_dates for p in patient_labels]
        total_w = time_col_w + len(col_pairs) * patient_col_w
        total_h = header_h + len(minutes) * row_h
        canvas.config(scrollregion=(0, 0, total_w, total_h))

        canvas.create_rectangle(0, 0, time_col_w, header_h, fill="#0b4f6c", outline="#0b4f6c")
        canvas.create_text(time_col_w // 2, header_h // 2, text="Time", fill="white", font=("Segoe UI", 10, "bold"))

        for idx, (date_key, pid) in enumerate(col_pairs):
            x0 = time_col_w + idx * patient_col_w
            x1 = x0 + patient_col_w
            canvas.create_rectangle(x0, 0, x1, header_h, fill="#1d3557", outline="#f1faee")
            canvas.create_text((x0 + x1) // 2, header_h // 2, text=f"{date_key}\nPatient {pid}", fill="white", font=("Segoe UI", 8, "bold"))

        for row_idx, minute in enumerate(minutes):
            y0 = header_h + row_idx * row_h
            y1 = y0 + row_h
            canvas.create_rectangle(0, y0, time_col_w, y1, fill="#f8f9fa" if row_idx % 2 == 0 else "#e9ecef", outline="#adb5bd")
            canvas.create_text(time_col_w // 2, (y0 + y1) // 2, text=_to_ampm(minute), fill="#1b263b", font=("Segoe UI", 8))
            for col_idx in range(len(col_pairs)):
                x0 = time_col_w + col_idx * patient_col_w
                x1 = x0 + patient_col_w
                canvas.create_rectangle(x0, y0, x1, y1, fill="#ffffff", outline="#dee2e6")

        pair_index = {pair: idx for idx, pair in enumerate(col_pairs)}
        used_disciplines = set()
        for appt in appointments:
            start_idx = max(0, (appt["start"] - GRID_START_MINUTE) // GRID_SLOT_MINUTES)
            end_idx = min(len(minutes), (appt["end"] - GRID_START_MINUTE) // GRID_SLOT_MINUTES)
            if end_idx <= start_idx:
                continue
            for pid in appt["patients"]:
                pair = (appt["date_key"], pid)
                if pair not in pair_index:
                    continue
                col_idx = pair_index[pair]
                x0 = time_col_w + col_idx * patient_col_w + 1
                x1 = x0 + patient_col_w - 2
                y0 = header_h + start_idx * row_h + 1
                y1 = header_h + end_idx * row_h - 1
                color = DISCIPLINE_COLORS.get(appt["discipline"], "#ffb3c1")
                width = 3 if self.selected_request_id and appt["request_id"] == self.selected_request_id else 2
                outline = "#d00000" if self.selected_request_id and appt["request_id"] == self.selected_request_id else "#495057"
                tags = ("appointment", f"req:{appt['request_id']}")
                canvas.create_rectangle(x0, y0, x1, y1, fill=color, outline=outline, width=width, tags=tags)
                text = f"{appt['discipline']}\n{appt['room']}\n{appt['provider']}"
                canvas.create_text((x0 + x1) // 2, (y0 + y1) // 2, text=text, fill="#1b263b", font=("Segoe UI", 8), justify="center", tags=tags)
                used_disciplines.add(appt["discipline"])

        self.legend_var.set(
            "Legend: " + " | ".join(sorted(used_disciplines)) if used_disciplines else "Legend: No assigned sessions"
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
        save_provider_catalog(self.provider_catalog)
        save_provider_profiles(self.provider_profiles)
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
        save_provider_catalog(self.provider_catalog)
        save_provider_profiles(self.provider_profiles)
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
            pid = str(profile.get("provider_id") or profile.get("provider_name") or profile.get("id", ""))
            if pid != selected_name:
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
                }
            )

        save_provider_profiles(self.provider_profiles)
        if self.loaded_profile:
            self._sync_profile_resources(self.loaded_profile)
            self.last_result = build_live_result_from_profile(self.loaded_profile)
            self._render_patient_grid(self.loaded_profile, self.last_result)
        self._refresh_profile_preview()
        self.status_var.set(f"Status: Updated availability window for {selected_name} ({weekday_name})")

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
        room_id = self.appt_room_var.get().strip()
        mode = self.appt_mode_var.get().strip().lower()
        discipline = self.appt_discipline_var.get().strip()
        date_key = self.appt_date_var.get().strip()

        if date_key not in profile.get("planning_dates", []):
            raise ValueError("Appointment date must be within current planning dates")

        start_minute = parse_time_input(self.appt_start_var.get())
        end_minute = parse_time_input(self.appt_end_var.get())
        if end_minute <= start_minute:
            raise ValueError("Appointment end time must be after begin time")

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
                if existing.get("provider_id") == provider_name:
                    raise ValueError("Conflict: individual appointment cannot overlap with the same provider")
                if patient_id in existing.get("patient_ids", []):
                    raise ValueError("Conflict: patient already has an overlapping appointment")

        req_id = f"appt_{len(profile.get('requests', [])) + 1}"
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
                "provider_id": provider_name,
                "room_id": room_id,
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
                raise ValueError(f"Patient ID '{pid}' must be in range 1-99")
        return raw

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

        provider_ids = [p for p in [provider_choice, provider_choice_b, provider_choice_c] if p not in ("", "Any provider", "(none)")]

        condition = {
            "id": self.auto_req_id_var.get().strip() or f"req_{len(self.auto_conditions)+1}",
            "discipline": self.auto_discipline_var.get().strip(),
            "provider_id": "any" if provider_choice == "Any provider" and not provider_ids else (provider_ids[0] if provider_ids else provider_choice),
            "provider_ids": provider_ids,
            "room_id": "any" if room_choice == "Any compatible room" else room_choice,
            "duration_minutes": int(self.auto_duration_var.get()),
            "session_mode": self.auto_mode_var.get().strip(),
            "patient_scope": scope,
            "patient_ids": subset_ids,
            "weekdays": sorted(set(weekdays)),
            "weeks": self._selected_weeks(),
            "time_windows": self._parse_windows_from_ui(),
            "hard_constraint": bool(self.auto_hard_var.get()),
            "priority": int(self.auto_priority_var.get()),
        }
        condition = validate_requirement(condition)

        if any(c["id"] == condition["id"] for c in self.auto_conditions):
            raise ValueError(f"Requirement ID '{condition['id']}' already exists")

        self.auto_conditions.append(condition)
        save_requirements_catalog(self.auto_conditions)
        self.auto_req_id_var.set(f"req_{len(self.auto_conditions)+1}")
        self.auto_windows_var.set("")
        self._refresh_auto_condition_list()
        self.status_var.set(f"Status: Added requirement {condition['id']}")

    def _on_requirement_select(self, _event=None) -> None:
        if not self.auto_condition_list.curselection():
            return
        idx = int(self.auto_condition_list.curselection()[0])
        if idx >= len(self.auto_conditions):
            return
        selected = self.auto_conditions[idx]
        self.auto_req_id_var.set(selected["id"])

    def remove_selected_condition(self) -> None:
        selection = self.auto_condition_list.curselection()
        if not selection:
            raise ValueError("Select a requirement in the list first")
        idx = int(selection[0])
        if idx >= len(self.auto_conditions):
            raise ValueError("Selected requirement is out of range")
        rid = self.auto_conditions[idx]["id"]
        self.auto_conditions.pop(idx)
        save_requirements_catalog(self.auto_conditions)
        self._refresh_auto_condition_list()
        self.status_var.set(f"Status: Removed requirement {rid}")

    def clear_auto_conditions(self) -> None:
        self.auto_conditions = []
        save_requirements_catalog(self.auto_conditions)
        self._refresh_auto_condition_list()
        self.status_var.set("Status: Cleared all requirements")

    def _refresh_auto_condition_list(self) -> None:
        self.auto_condition_list.delete(0, self.tk.END)
        if not self.auto_conditions:
            self.auto_condition_list.insert(self.tk.END, "No conditions added yet.")
            return

        name_map = ["Mon", "Tue", "Wed", "Thu", "Fri"]
        for idx, c in enumerate(self.auto_conditions, start=1):
            days = ",".join(name_map[d] for d in c["weekdays"])
            windows = "; ".join(f"{_to_ampm(w['start_minute'])}-{_to_ampm(w['end_minute'])}" for w in c["time_windows"])
            hard_soft = "hard" if c.get("hard_constraint", True) else "soft"
            providers = c.get("provider_ids") or ([c.get("provider_id", "any")] if c.get("provider_id", "any") != "any" else ["any"])
            line = (
                f"{idx}) {c['id']} | {c['discipline']} | providers={','.join(providers)} | room={c['room_id']} | "
                f"{c['session_mode']} {c['duration_minutes']}m | scope={c['patient_scope']} | days={days} | weeks={c['weeks']} | "
                f"windows={windows} | {hard_soft} p={c.get('priority', 100)}"
            )
            self.auto_condition_list.insert(self.tk.END, line)

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
            "rooms": build_room_records(),
        }

    def _update_after_auto_generation(self, profile_template: Dict[str, Any], result: Dict[str, Any]) -> None:
        profile = {
            **profile_template,
            "requests": result.get("requests", []),
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
            solver_limits=self._solver_limits_from_ui(),
        )
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
