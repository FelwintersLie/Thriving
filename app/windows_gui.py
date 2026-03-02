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
    validate_requirement,
)
from app.health_check import run_health_check
from app.persistence import (
    load_last_generated_schedule,
    load_last_profile,
    load_app_settings,
    load_provider_catalog_entries,
    load_provider_profiles,
    load_requirements_catalog,
    load_room_discipline_profile,
    load_room_rules,
    save_last_generated_schedule,
    save_last_profile,
    save_app_settings,
    save_last_schedule,
    save_provider_catalog_entries,
    save_provider_profiles,
    save_requirements_catalog,
    save_room_discipline_profile,
    save_room_rules,
)
from app.profile_io import load_profile, save_profile, validate_profile
from app.provider_catalog import normalize_provider_catalog, provider_is_available
from app.room_rules import is_room_discipline_compatible, room_is_available, room_preference_tier
from app.windows_program import save_json
from app.schedule_exports import (
    build_schedule_layout_model,
    draw_layout_on_tk_canvas,
    export_layout_to_xlsx,
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
    "LAUNCH",
    "Lunch",
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
    "Wes's Office",
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

ROOM_TIER_DEFAULTS = {
    "Suite 1": 1,
    "Suite 2": 1,
    "Suite 3": 1,
    "Brittany's Office": 2,
    "Conference Room": 2,
    "Jason's Office": 3,
}

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
                "enforce_lunch_break": bool(profile.get("enforce_lunch_break", False)),
                "lunch_earliest_start_minute": int(profile.get("lunch_earliest_start_minute", 11 * 60 + 30)),
                "lunch_latest_start_minute": int(profile.get("lunch_latest_start_minute", 13 * 60)),
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
                "allowed_disciplines": rr.get("allowed_disciplines") or list(DISCIPLINES),
                "room_preference_tier": int(rr.get("room_preference_tier", 0) or 0),
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
        self.app_settings = self._normalize_app_settings(load_app_settings())

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
        for room_name in PREDEFINED_ROOMS:
            bucket = self.room_rules.setdefault("rooms", {}).setdefault(
                room_name,
                {"unavailable_weekly": {i: [] for i in range(5)}, "unavailable_dates": [], "available_only_weekly": {i: [] for i in range(5)}, "available_only_dates": []},
            )
            bucket.setdefault("allowed_disciplines", list(DISCIPLINES))
            bucket.setdefault("room_preference_tier", ROOM_TIER_DEFAULTS.get(room_name, 0))

        latest_discipline_profile = load_room_discipline_profile(PREDEFINED_ROOMS)
        if latest_discipline_profile:
            latest_rooms = latest_discipline_profile.get("rooms", {})
            for room_name in PREDEFINED_ROOMS:
                if room_name not in latest_rooms:
                    continue
                source = latest_rooms.get(room_name, {})
                target = self.room_rules.setdefault("rooms", {}).setdefault(room_name, {})
                if "allowed_disciplines" in source:
                    target["allowed_disciplines"] = list(source.get("allowed_disciplines") or list(DISCIPLINES))
                if "room_preference_tier" in source:
                    target["room_preference_tier"] = int(source.get("room_preference_tier", ROOM_TIER_DEFAULTS.get(room_name, 0)) or 0)
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
        self.discipline_registry: List[Dict[str, Any]] = self._default_discipline_registry()
        self.active_clinic_config: Dict[str, Any] | None = None
        self.active_schedule_snapshot: Dict[str, Any] | None = None

        self._build_layout()

        if self.loaded_profile:
            if isinstance(self.loaded_profile.get("clinic_config"), dict):
                self.active_clinic_config = dict(self.loaded_profile.get("clinic_config") or {})
            self._sync_profile_resources(self.loaded_profile)
            self.status_var.set("Status: Restored last profile")
            self.profile_var.set("Profile: restored from data/last_profile.json")
            self._refresh_profile_preview()

    def _normalize_app_settings(self, raw: Dict[str, Any] | None) -> Dict[str, Any]:
        default = {"max_solve_seconds": 10}
        if not isinstance(raw, dict):
            return default
        try:
            value = int(raw.get("max_solve_seconds", 10))
        except Exception:
            value = 10
        if value < 1:
            value = 10
        return {"max_solve_seconds": value}

    def _save_app_settings_from_ui(self) -> None:
        value_raw = self.max_solve_seconds_var.get().strip() if hasattr(self, "max_solve_seconds_var") else str(self.app_settings.get("max_solve_seconds", 10))
        try:
            value = int(value_raw)
        except Exception:
            value = 10
        if value < 1:
            value = 10
        self.app_settings["max_solve_seconds"] = value
        if hasattr(self, "max_solve_seconds_var"):
            self.max_solve_seconds_var.set(str(value))
        save_app_settings(self.app_settings)

    def _update_loaded_artifact_status(self) -> None:
        clinic_name = "None Loaded"
        if isinstance(self.active_clinic_config, dict):
            clinic_name = str(self.active_clinic_config.get("config_name") or self.active_clinic_config.get("config_id") or "None Loaded")

        snapshot_name = "None Loaded"
        if isinstance(self.active_schedule_snapshot, dict):
            snapshot_name = str(self.active_schedule_snapshot.get("snapshot_name") or self.active_schedule_snapshot.get("snapshot_id") or "None Loaded")

        if hasattr(self, "clinic_config_display_var"):
            self.clinic_config_display_var.set(f"Clinic Config: {clinic_name}")
        if hasattr(self, "schedule_snapshot_display_var"):
            self.schedule_snapshot_display_var.set(f"Schedule Snapshot: {snapshot_name}")

    def _make_scrollable_tab(self, notebook) -> Any:
        outer, canvas, content = self._make_scrollable_region(notebook)
        setattr(outer, "_scroll_canvas", canvas)
        setattr(outer, "_scroll_content", content)
        return outer

    def _make_scrollable_region(self, parent):
        tk = self.tk
        ttk = self.ttk

        outer = ttk.Frame(parent)
        canvas = tk.Canvas(outer, highlightthickness=0)
        vscroll = ttk.Scrollbar(outer, orient=tk.VERTICAL, command=canvas.yview)
        hscroll = ttk.Scrollbar(outer, orient=tk.HORIZONTAL, command=canvas.xview)
        canvas.configure(yscrollcommand=vscroll.set, xscrollcommand=hscroll.set)

        canvas.grid(row=0, column=0, sticky="nsew")
        vscroll.grid(row=0, column=1, sticky="ns")
        hscroll.grid(row=1, column=0, sticky="ew")
        outer.rowconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)

        content = ttk.Frame(canvas)
        window_id = canvas.create_window((0, 0), window=content, anchor="nw")

        def _sync_scrollregion(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _sync_window_to_canvas(event=None):
            viewport_w = event.width if event else canvas.winfo_width()
            viewport_h = event.height if event else canvas.winfo_height()
            req_w = content.winfo_reqwidth()
            req_h = content.winfo_reqheight()
            canvas.itemconfigure(window_id, width=max(viewport_w, req_w), height=max(viewport_h, req_h))
            _sync_scrollregion()

        content.bind("<Configure>", _sync_window_to_canvas)
        canvas.bind("<Configure>", _sync_window_to_canvas)
        canvas.after_idle(_sync_window_to_canvas)

        return outer, canvas, content

    def _bind_canvas_mouse_scrolling(self, canvas) -> None:
        tk = self.tk

        def _on_mousewheel(event):
            delta = event.delta
            if delta == 0:
                return
            units = -1 * int(delta / 120)
            if units == 0:
                units = -1 if delta > 0 else 1
            canvas.yview_scroll(units, "units")

        def _on_shift_mousewheel(event):
            delta = event.delta
            if delta == 0:
                return
            units = -1 * int(delta / 120)
            if units == 0:
                units = -1 if delta > 0 else 1
            canvas.xview_scroll(units, "units")

        def _on_button4(_event):
            canvas.yview_scroll(-3, "units")

        def _on_button5(_event):
            canvas.yview_scroll(3, "units")

        canvas.bind("<Enter>", lambda _e: canvas.focus_set())
        canvas.bind("<MouseWheel>", _on_mousewheel)
        canvas.bind("<Shift-MouseWheel>", _on_shift_mousewheel)
        if tk.TkVersion >= 8.6:
            canvas.bind("<Button-4>", _on_button4)
            canvas.bind("<Button-5>", _on_button5)

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
            ("Load Clinic Config", self.load_clinic_config),
            ("Load Schedule Snapshot", self.load_schedule_snapshot),
            ("Generate Day", self.generate_from_loaded_profile),
            ("Export Schedule", self.export_schedule),
            ("Save Profile", self.save_current_profile),
            ("Save Clinic Config", self.save_clinic_config),
            ("Save Schedule Snapshot", self.save_schedule_snapshot),
        ]
        for idx, (label, handler) in enumerate(buttons):
            ttk.Button(controls, text=label, command=lambda h=handler: self._safe_action(h)).grid(row=0, column=idx, padx=3, pady=4, sticky="w")

        self.status_var = tk.StringVar(value="Status: Ready")
        ttk.Label(controls, textvariable=self.status_var).grid(row=1, column=0, columnspan=12, sticky="w", padx=6)
        self.profile_var = tk.StringVar(value="Profile: (none loaded)")
        ttk.Label(controls, textvariable=self.profile_var).grid(row=2, column=0, columnspan=12, sticky="w", padx=6)

        artifact_bar = ttk.Frame(main, padding=(0, 2))
        artifact_bar.pack(fill=tk.X, padx=2, pady=(2, 4))
        self.clinic_config_display_var = tk.StringVar(value="Clinic Config: None Loaded")
        self.schedule_snapshot_display_var = tk.StringVar(value="Schedule Snapshot: None Loaded")
        ttk.Label(artifact_bar, textvariable=self.clinic_config_display_var).pack(anchor="w")
        ttk.Label(artifact_bar, textvariable=self.schedule_snapshot_display_var).pack(anchor="w")
        self._update_loaded_artifact_status()

        notebook = ttk.Notebook(main)
        notebook.pack(fill=tk.BOTH, expand=True, pady=(8, 8))
        self.main_notebook = notebook

        manual_tab = self._make_scrollable_tab(notebook)
        self.manual_tab = manual_tab
        notebook.add(manual_tab, text="Manual Scheduler")
        manual_content = getattr(manual_tab, "_scroll_content")

        auto_tab = self._make_scrollable_tab(notebook)
        notebook.add(auto_tab, text="IOP Generator (3-week)")
        auto_content = getattr(auto_tab, "_scroll_content")

        eval_tab = self._make_scrollable_tab(notebook)
        notebook.add(eval_tab, text="EVAL Generator")
        eval_content = getattr(eval_tab, "_scroll_content")

        provider_tab = self._make_scrollable_tab(notebook)
        notebook.add(provider_tab, text="Provider Profiles")
        provider_content = getattr(provider_tab, "_scroll_content")

        room_rules_tab = self._make_scrollable_tab(notebook)
        notebook.add(room_rules_tab, text="Room Rules")
        room_rules_content = getattr(room_rules_tab, "_scroll_content")

        disciplines_tab = self._make_scrollable_tab(notebook)
        notebook.add(disciplines_tab, text="Disciplines")
        disciplines_content = getattr(disciplines_tab, "_scroll_content")

        manual_split = ttk.Panedwindow(manual_content, orient=tk.VERTICAL)
        manual_split.pack(fill=tk.BOTH, expand=True, pady=(0, 8))

        top_section = ttk.Frame(manual_split)
        manual_split.add(top_section, weight=3)

        form_wrap = ttk.Labelframe(top_section, text="Inputs", padding=8)
        form_wrap.pack(fill=tk.BOTH, expand=True)
        self._build_form_panel(form_wrap)

        self.summary_text = None

        bottom_section = ttk.Frame(manual_split)
        manual_split.add(bottom_section, weight=4)

        grid_wrap = ttk.Labelframe(bottom_section, text="Patient Schedule Grid", padding=8)
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
        self._bind_canvas_mouse_scrolling(self.grid_canvas)

        grid_container.rowconfigure(0, weight=1)
        grid_container.columnconfigure(0, weight=1)

        arrow_frame = ttk.Frame(grid_container)
        arrow_frame.grid(row=0, column=2, sticky="ns", padx=(4, 0))
        ttk.Button(arrow_frame, text="▲", width=3, command=lambda: self.grid_canvas.yview_scroll(-6, "units")).pack(pady=4)
        ttk.Button(arrow_frame, text="▼", width=3, command=lambda: self.grid_canvas.yview_scroll(6, "units")).pack(pady=4)

        self.legend_var = tk.StringVar(value="Legend: Add appointments to visualize schedule.")
        ttk.Label(bottom_section, textvariable=self.legend_var).pack(anchor="w", pady=(6, 0))

        view_controls = ttk.Frame(bottom_section)
        view_controls.pack(fill=tk.X, expand=True, pady=(4, 0))
        self.grid_mode_var = tk.StringVar(value="Patient Grid")
        self.program_filter_var = tk.StringVar(value="Both")
        self.patient_view_filter_var = tk.StringVar(value="All Patients")
        ttk.Label(view_controls, text="Grid Mode").pack(side="left")
        ttk.Combobox(view_controls, textvariable=self.grid_mode_var, values=["Patient Grid", "Room Grid", "Provider Grid"], state="readonly", width=16).pack(side="left", padx=4)
        ttk.Label(view_controls, text="Program Filter").pack(side="left", padx=(10, 0))
        ttk.Combobox(view_controls, textvariable=self.program_filter_var, values=["Both", "IOP", "EVAL"], state="readonly", width=10).pack(side="left", padx=4)
        ttk.Label(view_controls, text="Patient").pack(side="left", padx=(10, 0))
        self.patient_view_filter_combo = ttk.Combobox(view_controls, textvariable=self.patient_view_filter_var, values=["All Patients"], state="readonly", width=14)
        self.patient_view_filter_combo.pack(side="left", padx=4)
        ttk.Button(view_controls, text="Apply View", command=lambda: self._safe_action(self.refresh_current_grid_view)).pack(side="left", padx=8)
        ttk.Button(view_controls, text="Export Schedule as PNG", command=lambda: self._safe_action(self.export_view_as_png)).pack(side="left", padx=6)
        ttk.Button(view_controls, text="Export Schedule as Excel", command=lambda: self._safe_action(self.export_view_as_excel)).pack(side="left", padx=6)

        def _init_manual_split_position():
            total_h = manual_split.winfo_height()
            if total_h > 200:
                manual_split.sashpos(0, int(total_h * 0.45))

        manual_split.after_idle(_init_manual_split_position)

        self._build_auto_generator_tab(auto_content)
        self._build_eval_generator_tab(eval_content)
        self._build_provider_profiles_tab(provider_content)
        self._build_room_rules_tab(room_rules_content)
        self._build_disciplines_tab(disciplines_content)

        sizegrip = ttk.Sizegrip(main)
        sizegrip.pack(side=tk.RIGHT, anchor="se", padx=(0, 4), pady=(0, 4))

    def _default_discipline_registry(self) -> List[Dict[str, Any]]:
        return [{"name": d, "show_in_iop": True, "show_in_eval": True} for d in DISCIPLINES]

    def _normalize_discipline_registry(self, incoming: Any) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for row in incoming or []:
            name = str((row or {}).get("name", "")).strip()
            if not name:
                continue
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            rows.append({
                "name": name,
                "show_in_iop": bool((row or {}).get("show_in_iop", True)),
                "show_in_eval": bool((row or {}).get("show_in_eval", True)),
            })
        if not rows:
            rows = self._default_discipline_registry()
        return rows

    def _discipline_names_for(self, scope: str) -> List[str]:
        if scope == "iop":
            names = [d["name"] for d in self.discipline_registry if d.get("show_in_iop", True)]
        elif scope == "eval":
            names = [d["name"] for d in self.discipline_registry if d.get("show_in_eval", True)]
        else:
            names = [d["name"] for d in self.discipline_registry]
        return sorted(names)

    def _apply_discipline_registry_to_ui(self) -> None:
        if hasattr(self, "appt_discipline_var"):
            values = self._discipline_names_for("all")
            self.appt_discipline_var.set(self.appt_discipline_var.get() if self.appt_discipline_var.get() in values else (values[0] if values else ""))
        if hasattr(self, "auto_discipline_var"):
            values = self._discipline_names_for("iop")
            if hasattr(self, "auto_discipline_combo"):
                self.auto_discipline_combo["values"] = values
            self.auto_discipline_var.set(self.auto_discipline_var.get() if self.auto_discipline_var.get() in values else (values[0] if values else ""))
        if hasattr(self, "eval_discipline_var"):
            values = self._discipline_names_for("eval")
            if hasattr(self, "eval_discipline_combo"):
                self.eval_discipline_combo["values"] = values
            self.eval_discipline_var.set(self.eval_discipline_var.get() if self.eval_discipline_var.get() in values else (values[0] if values else ""))

    def _build_disciplines_tab(self, parent) -> None:
        ttk = self.ttk
        tk = self.tk
        wrap = ttk.Frame(parent, padding=8)
        wrap.pack(fill="both", expand=True)

        left = ttk.Labelframe(wrap, text="Disciplines", padding=8)
        left.pack(side="left", fill="both", expand=True)
        right = ttk.Labelframe(wrap, text="Edit Discipline", padding=8)
        right.pack(side="left", fill="y", padx=(8, 0))

        self.discipline_list = tk.Listbox(left, height=18)
        self.discipline_list.pack(fill="both", expand=True)
        self.discipline_list.bind("<<ListboxSelect>>", self._on_discipline_select)

        self.new_discipline_var = tk.StringVar(value="")
        ttk.Entry(right, textvariable=self.new_discipline_var, width=24).pack(fill="x")
        ttk.Button(right, text="Add Discipline", command=lambda: self._safe_action(self.add_discipline)).pack(fill="x", pady=(6, 0))
        ttk.Button(right, text="Remove Discipline", command=lambda: self._safe_action(self.remove_discipline)).pack(fill="x", pady=(6, 0))

        self.discipline_show_iop_var = tk.BooleanVar(value=True)
        self.discipline_show_eval_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(right, text="Show in IOP", variable=self.discipline_show_iop_var, command=lambda: self._safe_action(self.save_selected_discipline_visibility)).pack(anchor="w", pady=(10, 0))
        ttk.Checkbutton(right, text="Show in EVAL", variable=self.discipline_show_eval_var, command=lambda: self._safe_action(self.save_selected_discipline_visibility)).pack(anchor="w")

        self._refresh_discipline_list()

    def _refresh_discipline_list(self) -> None:
        if not hasattr(self, "discipline_list"):
            return
        self.discipline_list.delete(0, self.tk.END)
        for row in self.discipline_registry:
            flags = []
            if row.get("show_in_iop", True):
                flags.append("IOP")
            if row.get("show_in_eval", True):
                flags.append("EVAL")
            self.discipline_list.insert(self.tk.END, f"{row['name']} ({'/'.join(flags) or 'hidden'})")

    def _on_discipline_select(self, _event=None) -> None:
        if not hasattr(self, "discipline_list"):
            return
        sel = self.discipline_list.curselection()
        if not sel:
            return
        row = self.discipline_registry[int(sel[0])]
        self.discipline_show_iop_var.set(bool(row.get("show_in_iop", True)))
        self.discipline_show_eval_var.set(bool(row.get("show_in_eval", True)))

    def add_discipline(self) -> None:
        name = self.new_discipline_var.get().strip()
        if not name:
            raise ValueError("Discipline name is required")
        if any(d["name"].lower() == name.lower() for d in self.discipline_registry):
            raise ValueError("Discipline already exists")
        self.discipline_registry.append({"name": name, "show_in_iop": True, "show_in_eval": True})
        self.new_discipline_var.set("")
        self._refresh_discipline_list()
        self._apply_discipline_registry_to_ui()
        if self.loaded_profile is not None:
            self.loaded_profile["discipline_registry"] = copy.deepcopy(self.discipline_registry)
        self._autosave_profile()

    def remove_discipline(self) -> None:
        if not hasattr(self, "discipline_list") or not self.discipline_list.curselection():
            raise ValueError("Select a discipline to remove")
        idx = int(self.discipline_list.curselection()[0])
        row = self.discipline_registry[idx]
        name = row["name"]
        refs = []
        if any(r.get("discipline") == name for r in self.auto_conditions + self.eval_conditions):
            refs.append("requirements")
        if any(name in (p.get("disciplines") or []) for p in self.provider_profiles):
            refs.append("provider profiles")
        if any(name in ((v or {}).get("allowed_disciplines") or []) for v in (self.room_rules.get("rooms", {}) or {}).values()):
            refs.append("room rules")
        if self.loaded_profile and any(r.get("discipline") == name for r in self.loaded_profile.get("requests", [])):
            refs.append("scheduled appointments")
        if refs:
            raise ValueError(f"Cannot remove '{name}': referenced in {', '.join(refs)}")
        if not self.messagebox.askyesno("Confirm", f"Remove discipline '{name}'?"):
            return
        self.discipline_registry.pop(idx)
        self._refresh_discipline_list()
        self._apply_discipline_registry_to_ui()
        if self.loaded_profile is not None:
            self.loaded_profile["discipline_registry"] = copy.deepcopy(self.discipline_registry)
        self._autosave_profile()

    def save_selected_discipline_visibility(self) -> None:
        if not hasattr(self, "discipline_list") or not self.discipline_list.curselection():
            return
        idx = int(self.discipline_list.curselection()[0])
        self.discipline_registry[idx]["show_in_iop"] = bool(self.discipline_show_iop_var.get())
        self.discipline_registry[idx]["show_in_eval"] = bool(self.discipline_show_eval_var.get())
        self._refresh_discipline_list()
        self._apply_discipline_registry_to_ui()
        if self.loaded_profile is not None:
            self.loaded_profile["discipline_registry"] = copy.deepcopy(self.discipline_registry)
        self._autosave_profile()

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
        self.auto_discipline_combo = ttk.Combobox(cond, textvariable=self.auto_discipline_var, values=self._discipline_names_for("iop"), state="readonly", width=20)
        self.auto_discipline_combo.grid(row=1, column=1, padx=2)

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
        list_frame.rowconfigure(0, weight=1)
        list_frame.columnconfigure(0, weight=1)

        self.requirement_table_columns = (
            "requirement_id",
            "program_type",
            "patient",
            "discipline",
            "duration_minutes",
            "frequency_week",
            "provider_constraint",
            "room_constraint",
            "time_window",
            "notes",
        )
        self.auto_sort_column = "requirement_id"
        self.auto_sort_desc = False
        self.auto_row_lookup: Dict[str, Tuple[str, str]] = {}

        self.auto_condition_list = ttk.Treeview(
            list_frame,
            columns=self.requirement_table_columns,
            show="headings",
            height=10,
        )
        self.auto_condition_list.grid(row=0, column=0, sticky="nsew")

        auto_scroll_y = ttk.Scrollbar(list_frame, orient=self.tk.VERTICAL, command=self.auto_condition_list.yview)
        auto_scroll_y.grid(row=0, column=1, sticky="ns")
        auto_scroll_x = ttk.Scrollbar(list_frame, orient=self.tk.HORIZONTAL, command=self.auto_condition_list.xview)
        auto_scroll_x.grid(row=1, column=0, sticky="ew")
        self.auto_condition_list.configure(yscrollcommand=auto_scroll_y.set, xscrollcommand=auto_scroll_x.set)

        headers = {
            "requirement_id": "Requirement ID",
            "program_type": "Program Type",
            "patient": "Patient",
            "discipline": "Discipline",
            "duration_minutes": "Duration (minutes)",
            "frequency_week": "Frequency / Week",
            "provider_constraint": "Provider Constraint",
            "room_constraint": "Room Constraint",
            "time_window": "Time Window / Preferred",
            "notes": "Notes / Flags",
        }
        for col in self.requirement_table_columns:
            anchor = "e" if col == "duration_minutes" else "w"
            self.auto_condition_list.heading(col, text=headers[col], command=lambda c=col: self._sort_auto_condition_table(c))
            self.auto_condition_list.column(col, width=150, minwidth=100, stretch=True, anchor=anchor)

        self.auto_condition_list.tag_configure("odd", background="#f8f9fa")
        self.auto_condition_list.tag_configure("even", background="#ffffff")
        self.auto_condition_list.tag_configure("iop", foreground="#0b2d5c")
        self.auto_condition_list.tag_configure("eval", foreground="#b00020")
        self.auto_condition_list.bind("<<TreeviewSelect>>", self._on_requirement_select)

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
        self.eval_discipline_combo = ttk.Combobox(req, textvariable=self.eval_discipline_var, values=self._discipline_names_for("eval"), state="readonly", width=20)
        self.eval_discipline_combo.grid(row=1, column=1, padx=2)
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
        eval_list_frame.rowconfigure(0, weight=1)
        eval_list_frame.columnconfigure(0, weight=1)

        self.eval_table_columns = self.requirement_table_columns
        self.eval_sort_column = "requirement_id"
        self.eval_sort_desc = False
        self.eval_row_lookup: Dict[str, str] = {}

        self.eval_condition_list = ttk.Treeview(
            eval_list_frame,
            columns=self.eval_table_columns,
            show="headings",
            height=9,
        )
        self.eval_condition_list.grid(row=0, column=0, sticky="nsew")

        eval_scroll_y = ttk.Scrollbar(eval_list_frame, orient=self.tk.VERTICAL, command=self.eval_condition_list.yview)
        eval_scroll_y.grid(row=0, column=1, sticky="ns")
        eval_scroll_x = ttk.Scrollbar(eval_list_frame, orient=self.tk.HORIZONTAL, command=self.eval_condition_list.xview)
        eval_scroll_x.grid(row=1, column=0, sticky="ew")
        self.eval_condition_list.configure(yscrollcommand=eval_scroll_y.set, xscrollcommand=eval_scroll_x.set)

        eval_headers = {
            "requirement_id": "Requirement ID",
            "program_type": "Program Type",
            "patient": "Patient",
            "discipline": "Discipline",
            "duration_minutes": "Duration (minutes)",
            "frequency_week": "Frequency / Week",
            "provider_constraint": "Provider Constraint",
            "room_constraint": "Room Constraint",
            "time_window": "Time Window / Preferred",
            "notes": "Notes / Flags",
        }
        numeric_cols = {"duration_minutes"}
        for col in self.eval_table_columns:
            anchor = "e" if col in numeric_cols else "w"
            self.eval_condition_list.heading(col, text=eval_headers[col], command=lambda c=col: self._sort_eval_condition_table(c))
            self.eval_condition_list.column(col, width=150, minwidth=100, stretch=True, anchor=anchor)

        self.eval_condition_list.tag_configure("odd", background="#f8f9fa")
        self.eval_condition_list.tag_configure("even", background="#ffffff")
        self.eval_condition_list.bind("<<TreeviewSelect>>", self._on_eval_requirement_select)
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

        split = ttk.Panedwindow(parent, orient=tk.HORIZONTAL)
        split.pack(fill="both", expand=True, padx=8, pady=8)

        left = ttk.Labelframe(split, text="Providers", padding=8)
        split.add(left, weight=1)

        right_outer = ttk.Labelframe(split, text="Provider Profile", padding=0)
        split.add(right_outer, weight=2)
        right_scroll_wrap, _, right = self._make_scrollable_region(right_outer)
        right_scroll_wrap.pack(fill="both", expand=True)
        right.columnconfigure(0, weight=0)
        right.columnconfigure(1, weight=1)

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
        self.provider_exception_list.grid(row=1, column=0, columnspan=12, sticky="ew", pady=(4, 0))
        row += 1

        lunch = ttk.Labelframe(right, text="Lunch", padding=6)
        lunch.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.provider_lunch_enforce_var = tk.BooleanVar(value=False)
        self.provider_lunch_earliest_var = tk.StringVar(value="1130")
        self.provider_lunch_latest_var = tk.StringVar(value="1300")
        ttk.Checkbutton(lunch, text="Enforce 30-min lunch break", variable=self.provider_lunch_enforce_var).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(lunch, text="Earliest Start").grid(row=1, column=0, sticky="w")
        ttk.Combobox(lunch, textvariable=self.provider_lunch_earliest_var, values=time_choices, state="readonly", width=10).grid(row=2, column=0, padx=2)
        ttk.Label(lunch, text="Latest Start").grid(row=1, column=1, sticky="w")
        ttk.Combobox(lunch, textvariable=self.provider_lunch_latest_var, values=time_choices, state="readonly", width=10).grid(row=2, column=1, padx=2)
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

        split = ttk.Panedwindow(parent, orient=tk.HORIZONTAL)
        split.pack(fill="both", expand=True, padx=8, pady=8)

        left = ttk.Labelframe(split, text="Rooms", padding=8)
        split.add(left, weight=1)

        right_outer = ttk.Labelframe(split, text="Room Availability Restrictions", padding=0)
        split.add(right_outer, weight=2)
        right_scroll_wrap, _, right = self._make_scrollable_region(right_outer)
        right_scroll_wrap.pack(fill="both", expand=True)
        right.columnconfigure(0, weight=0)
        right.columnconfigure(1, weight=1)

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

        ttk.Label(right, text="Allowed Disciplines").grid(row=1, column=0, sticky="nw")
        disciplines_frame = ttk.Frame(right)
        disciplines_frame.grid(row=1, column=1, sticky="w", padx=4)
        self.room_allowed_discipline_vars = {}
        for idx, disc in enumerate(DISCIPLINES):
            var = tk.BooleanVar(value=True)
            self.room_allowed_discipline_vars[disc] = var
            ttk.Checkbutton(disciplines_frame, text=disc, variable=var).grid(row=idx // 2, column=idx % 2, sticky="w", padx=(0, 8))

        self.room_tier_var = tk.StringVar(value="0")
        ttk.Label(right, text="Room Preference Tier").grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Combobox(right, textvariable=self.room_tier_var, values=["0", "1", "2", "3"], state="readonly", width=6).grid(row=2, column=1, sticky="w", padx=4, pady=(6, 0))

        self.room_rule_weekday_var = tk.StringVar(value="Monday")
        self.room_rule_start_var = tk.StringVar(value="1100")
        self.room_rule_end_var = tk.StringVar(value="1300")
        self.room_rule_type_var = tk.StringVar(value="unavailable")

        weekly = ttk.Labelframe(right, text="Weekly rules", padding=6)
        weekly.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Combobox(weekly, textvariable=self.room_rule_weekday_var, values=weekdays, state="readonly", width=12).grid(row=0, column=0, padx=2)
        ttk.Combobox(weekly, textvariable=self.room_rule_start_var, values=time_choices, state="readonly", width=10).grid(row=0, column=1, padx=2)
        ttk.Combobox(weekly, textvariable=self.room_rule_end_var, values=time_choices, state="readonly", width=10).grid(row=0, column=2, padx=2)
        ttk.Combobox(weekly, textvariable=self.room_rule_type_var, values=["unavailable", "available-only"], state="readonly", width=14).grid(row=0, column=3, padx=2)
        ttk.Button(weekly, text="Add Window", command=lambda: self._safe_action(self.add_room_weekly_rule)).grid(row=0, column=4, padx=4)
        ttk.Button(weekly, text="Remove Window", command=lambda: self._safe_action(self.remove_room_weekly_rule)).grid(row=0, column=5, padx=4)
        self.room_weekly_rules_list = tk.Listbox(weekly, height=6)
        self.room_weekly_rules_list.grid(row=1, column=0, columnspan=12, sticky="ew", pady=(4, 0))

        self.room_rule_date_var = tk.StringVar(value=date_to_key(datetime.utcnow().date()))
        self.room_rule_date_start_var = tk.StringVar(value="1100")
        self.room_rule_date_end_var = tk.StringVar(value="1300")
        self.room_rule_date_type_var = tk.StringVar(value="unavailable")

        dates = ttk.Labelframe(right, text="Date-specific exceptions", padding=6)
        dates.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Entry(dates, textvariable=self.room_rule_date_var, width=12).grid(row=0, column=0, padx=2)
        ttk.Combobox(dates, textvariable=self.room_rule_date_start_var, values=time_choices, state="readonly", width=10).grid(row=0, column=1, padx=2)
        ttk.Combobox(dates, textvariable=self.room_rule_date_end_var, values=time_choices, state="readonly", width=10).grid(row=0, column=2, padx=2)
        ttk.Combobox(dates, textvariable=self.room_rule_date_type_var, values=["unavailable", "available-only"], state="readonly", width=14).grid(row=0, column=3, padx=2)
        ttk.Button(dates, text="Add Exception", command=lambda: self._safe_action(self.add_room_date_rule)).grid(row=0, column=4, padx=4)
        ttk.Button(dates, text="Remove Exception", command=lambda: self._safe_action(self.remove_room_date_rule)).grid(row=0, column=5, padx=4)
        self.room_date_rules_list = tk.Listbox(dates, height=6)
        self.room_date_rules_list.grid(row=1, column=0, columnspan=12, sticky="ew", pady=(4, 0))

        self.room_rules_preview_canvas = tk.Canvas(right, width=620, height=180, bg="white", highlightthickness=1, highlightbackground="#ced4da")
        self.room_rules_preview_canvas.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(8, 0))

        btns = ttk.Frame(right)
        btns.grid(row=6, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(btns, text="Save/Update", command=lambda: self._safe_action(self.save_room_rules)).pack(side="left", padx=4)
        ttk.Button(btns, text="Save Discipline Matrix", command=lambda: self._safe_action(self.save_room_discipline_matrix_profile)).pack(side="left", padx=4)
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

        solve = ttk.Labelframe(parent, text="Schedule Generation", padding=6)
        solve.pack(fill="x", pady=(0, 6))
        self.max_solve_seconds_var = self.tk.StringVar(value=str(self.app_settings.get("max_solve_seconds", 10)))
        ttk.Label(solve, text="Max Solve Time (seconds)").grid(row=0, column=0, sticky="w")
        ttk.Entry(solve, textvariable=self.max_solve_seconds_var, width=12).grid(row=1, column=0, padx=2, sticky="w")
        ttk.Label(solve, text="Minimum 1 second. Invalid values reset to 10.", foreground="#495057").grid(row=0, column=1, rowspan=2, padx=(10, 0), sticky="w")
        ttk.Button(solve, text="Apply Solve Settings", command=lambda: self._safe_action(self.apply_solve_settings)).grid(row=1, column=2, padx=6)

        self.provider_selected_var = self.tk.StringVar(value=self.provider_catalog[0] if self.provider_catalog else "")
        self.provider_new_var = self.tk.StringVar()
        self.provider_avail_weekday_var = self.tk.StringVar(value="Monday")
        self.provider_avail_start_var = self.tk.StringVar(value="0730")
        self.provider_avail_end_var = self.tk.StringVar(value="1800")

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
        self.appt_discipline_combo = self.ttk.Combobox(appt, textvariable=self.appt_discipline_var, values=self._discipline_names_for("all"), state="readonly", width=22)
        self.appt_discipline_combo.grid(row=1, column=6, padx=2)

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
        if widget is None:
            return
        widget.configure(state=self.tk.NORMAL)
        widget.delete("1.0", self.tk.END)
        widget.insert(self.tk.END, text)
        widget.configure(state=self.tk.DISABLED)

    def _require_profile(self) -> Dict[str, Any]:
        if not self.loaded_profile:
            raise ValueError("No profile loaded. Click 'New Blank Profile' first.")
        return self.loaded_profile

    def _sync_profile_resources(self, profile: Dict[str, Any]) -> None:
        self.discipline_registry = self._normalize_discipline_registry(profile.get("discipline_registry", self.discipline_registry))
        profile["discipline_registry"] = copy.deepcopy(self.discipline_registry)
        self._apply_discipline_registry_to_ui()
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
        if hasattr(self, "provider_lunch_enforce_var"):
            self.provider_lunch_enforce_var.set(bool(profile.get("enforce_lunch_break", False)))
            early = int(profile.get("lunch_earliest_start_minute", 11 * 60 + 30))
            late = int(profile.get("lunch_latest_start_minute", 13 * 60))
            self.provider_lunch_earliest_var.set(f"{early//60:02d}{early%60:02d}")
            self.provider_lunch_latest_var.set(f"{late//60:02d}{late%60:02d}")

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
            "enforce_lunch_break": bool(self.provider_lunch_enforce_var.get()) if hasattr(self, "provider_lunch_enforce_var") else False,
            "lunch_earliest_start_minute": parse_time_input(self.provider_lunch_earliest_var.get()) if hasattr(self, "provider_lunch_earliest_var") else 11 * 60 + 30,
            "lunch_latest_start_minute": parse_time_input(self.provider_lunch_latest_var.get()) if hasattr(self, "provider_lunch_latest_var") else 13 * 60,
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
            if isinstance(self.loaded_profile.get("clinic_config"), dict):
                self.active_clinic_config = dict(self.loaded_profile.get("clinic_config") or {})
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
        if hasattr(self, "provider_lunch_enforce_var"):
            self.provider_lunch_enforce_var.set(False)
            self.provider_lunch_earliest_var.set("1130")
            self.provider_lunch_latest_var.set("1300")
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
            if isinstance(self.loaded_profile.get("clinic_config"), dict):
                self.active_clinic_config = dict(self.loaded_profile.get("clinic_config") or {})
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
                "allowed_disciplines": list(DISCIPLINES),
                "room_preference_tier": ROOM_TIER_DEFAULTS.get(room_name, 0),
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
        allowed = set(bucket.get("allowed_disciplines") or DISCIPLINES)
        for disc, var in getattr(self, "room_allowed_discipline_vars", {}).items():
            var.set(disc in allowed)
        self.room_tier_var.set(str(int(bucket.get("room_preference_tier", 0) or 0)))
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
        room_name = self._selected_room_name()
        bucket = self._room_rule_bucket(room_name)
        bucket["allowed_disciplines"] = [d for d, v in self.room_allowed_discipline_vars.items() if v.get()]
        bucket["room_preference_tier"] = int(self.room_tier_var.get())
        self.room_rules = self.room_rules
        save_room_rules(self.room_rules, valid_rooms=PREDEFINED_ROOMS)
        if self.loaded_profile:
            if isinstance(self.loaded_profile.get("clinic_config"), dict):
                self.active_clinic_config = dict(self.loaded_profile.get("clinic_config") or {})
            self._sync_profile_resources(self.loaded_profile)
            self.last_result = build_live_result_from_profile(self.loaded_profile)
            self._render_patient_grid(self.loaded_profile, self.last_result)
        self.status_var.set("Status: Room availability rules saved")

    def save_room_discipline_matrix_profile(self) -> None:
        room_name = self._selected_room_name()
        bucket = self._room_rule_bucket(room_name)
        bucket["allowed_disciplines"] = [d for d, v in self.room_allowed_discipline_vars.items() if v.get()]
        bucket["room_preference_tier"] = int(self.room_tier_var.get())
        save_room_discipline_profile(self.room_rules, valid_rooms=PREDEFINED_ROOMS)
        save_room_rules(self.room_rules, valid_rooms=PREDEFINED_ROOMS)
        self.status_var.set("Status: Saved room-discipline matrix profile")

    def clear_selected_room_rules(self) -> None:
        room_name = self._selected_room_name()
        self.room_rules.setdefault("rooms", {})[room_name] = {
            "unavailable_weekly": {i: [] for i in range(5)},
            "unavailable_dates": [],
            "available_only_weekly": {i: [] for i in range(5)},
            "available_only_dates": [],
            "allowed_disciplines": list(DISCIPLINES),
            "room_preference_tier": ROOM_TIER_DEFAULTS.get(room_name, 0),
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

    def _refresh_patient_view_filter_options(self, appointments: List[Dict[str, Any]]) -> None:
        if not hasattr(self, "patient_view_filter_combo"):
            return
        patient_ids = sorted(
            {str(pid) for appt in appointments for pid in appt.get("patients", []) if str(pid).strip()},
            key=lambda x: (x[:1], int(x[1:]) if x[1:].isdigit() else x),
        )
        choices = ["All Patients"] + patient_ids
        self.patient_view_filter_combo["values"] = choices
        if self.patient_view_filter_var.get() not in choices:
            self.patient_view_filter_var.set("All Patients")

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
        self._refresh_patient_view_filter_options(appointments)
        selected_patient = self.patient_view_filter_var.get().strip() if hasattr(self, "patient_view_filter_var") else "All Patients"
        if selected_patient and selected_patient != "All Patients":
            appointments = [a for a in appointments if selected_patient in [str(pid) for pid in a.get("patients", [])]]
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
                self.root.after(0, lambda exc=exc: on_error(exc))
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

    def export_view_as_excel(self) -> None:
        profile = self._require_profile()
        if not self.last_result:
            self.messagebox.showinfo("Export", "Add or generate a schedule first.")
            return
        selected_date = self.appt_date_var.get().strip() if hasattr(self, "appt_date_var") else ""
        date_part = selected_date or datetime.utcnow().strftime("%Y-%m-%d_%H%M")
        mode_part = (self.grid_mode_var.get() if hasattr(self, "grid_mode_var") else "Patient Grid").replace(" ", "_").lower()
        path_raw = self.filedialog.asksaveasfilename(
            title="Export current schedule view as Excel",
            defaultextension=".xlsx",
            initialfile=f"schedule_{date_part}_{mode_part}.xlsx",
            filetypes=[("Excel files", "*.xlsx")],
        )
        if not path_raw:
            return
        out_path = Path(path_raw)
        visible = [selected_date] if selected_date else None
        layout = self._build_current_grid_layout(profile, self.last_result, visible_dates=visible)
        self.status_var.set("Status: Exporting Excel...")

        def worker() -> Path:
            title = f"Schedule {date_part} | {self.grid_mode_var.get()} | {self.program_filter_var.get()}"
            export_layout_to_xlsx(layout, out_path, title=title)
            return out_path

        self._run_in_background(
            worker,
            lambda p: (self.status_var.set(f"Status: Exported Excel to {p}"), self.messagebox.showinfo("Export complete", f"Excel exported to:\n{p}")),
            lambda e: self.messagebox.showerror("Excel export failed", str(e)),
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
        self.active_schedule_snapshot = None
        self._update_loaded_artifact_status()
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
        self.active_schedule_snapshot = None
        if isinstance(self.loaded_profile.get("clinic_config"), dict):
            self.active_clinic_config = dict(self.loaded_profile.get("clinic_config") or {})
        self._update_loaded_artifact_status()
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

    def apply_solve_settings(self) -> None:
        self._save_app_settings_from_ui()
        self.status_var.set(f"Status: Max solve time set to {self.app_settings.get('max_solve_seconds', 10)}s")

    def add_new_provider(self) -> None:
        name = self.provider_new_var.get().strip()
        self._push_manual_undo_snapshot("Add provider")
        if not name:
            raise ValueError("Enter a provider name to add")
        if name in self.provider_catalog:
            raise ValueError("Provider already exists")
        self.provider_catalog = sorted(self.provider_catalog + [name])
        self.provider_profiles.append({
            "provider_id": name,
            "provider_name": name,
            "discipline": "",
            "disciplines": [],
            "availability_templates": [],
            "exceptions": [],
            "enforce_lunch_break": False,
            "lunch_earliest_start_minute": 11 * 60 + 30,
            "lunch_latest_start_minute": 13 * 60,
        })
        self._persist_provider_catalog()
        if self.loaded_profile:
            if isinstance(self.loaded_profile.get("clinic_config"), dict):
                self.active_clinic_config = dict(self.loaded_profile.get("clinic_config") or {})
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
            if isinstance(self.loaded_profile.get("clinic_config"), dict):
                self.active_clinic_config = dict(self.loaded_profile.get("clinic_config") or {})
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
                    "disciplines": [],
                    "availability_templates": [{"weekday": weekday, "windows": [{"start_minute": start, "end_minute": end}]}],
                    "exceptions": [],
                    "allowed_rooms": list(PREDEFINED_ROOMS),
                    "enforce_lunch_break": False,
                    "lunch_earliest_start_minute": 11 * 60 + 30,
                    "lunch_latest_start_minute": 13 * 60,
                }
            )

        self._persist_provider_catalog()
        if self.loaded_profile:
            if isinstance(self.loaded_profile.get("clinic_config"), dict):
                self.active_clinic_config = dict(self.loaded_profile.get("clinic_config") or {})
            self._sync_profile_resources(self.loaded_profile)
            self.last_result = build_live_result_from_profile(self.loaded_profile)
            self._render_patient_grid(self.loaded_profile, self.last_result)
        self._refresh_profile_preview()
        self.status_var.set(f"Status: Updated availability window for {selected_name} ({weekday_name})")

    def _allowed_rooms_for_provider(self, provider_profile: Dict[str, Any], discipline: str) -> List[str]:
        allowed = provider_profile.get("allowed_rooms") or ["Any compatible room"]
        compatible = [r for r in PREDEFINED_ROOMS if is_room_discipline_compatible(self.room_rules, r, discipline)]
        if "Any compatible room" in allowed:
            return compatible
        return [r for r in allowed if r in compatible]

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
        if not is_room_discipline_compatible(self.room_rules, room_id, discipline):
            raise ValueError("Selected room is not compatible with the appointment discipline")
        room_tier_warning = room_preference_tier(self.room_rules, room_id) >= 3

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
        status_text = f"Status: Added appointment {req_id}"
        if room_tier_warning:
            status_text += " (warning: room tier 3 last resort)"
        self.status_var.set(status_text)
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
        selected_id = self._selected_eval_requirement_id()
        if selected_id is None:
            return
        idx = self._eval_condition_index_by_id(selected_id)
        if idx is None:
            return
        selected = self.eval_conditions[idx]
        self.eval_req_id_var.set(selected["id"])
        self.eval_discipline_var.set(selected.get("discipline", self.eval_discipline_var.get()))
        self.eval_mode_var.set(selected.get("session_mode", self.eval_mode_var.get()))
        self.eval_duration_var.set(str(selected.get("duration_minutes", self.eval_duration_var.get())))
        self.eval_scope_var.set(selected.get("patient_scope", self.eval_scope_var.get()))
        self.eval_subset_patients_var.set(", ".join(selected.get("patient_ids", [])))
        self.eval_hard_var.set(bool(selected.get("hard_constraint", True)))
        self.eval_priority_var.set(str(selected.get("priority", 100)))

        provider_id = selected.get("provider_id", "any")
        provider_name = "Any provider" if provider_id == "any" else self._provider_display_name(provider_id)
        self.eval_provider_var.set(provider_name)
        self.eval_room_var.set("Any compatible room" if selected.get("room_id", "any") == "any" else selected.get("room_id", ""))

        weekday_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
        weekday_values = [weekday_names[d] for d in selected.get("weekdays", []) if 0 <= d < len(weekday_names)]
        while len(weekday_values) < 3:
            weekday_values.append("(none)")
        self.eval_weekday_1_var.set(weekday_values[0])
        self.eval_weekday_2_var.set(weekday_values[1])
        self.eval_weekday_3_var.set(weekday_values[2])

        window_tokens = [f"{w['start_minute'] // 60:02d}{w['start_minute'] % 60:02d}-{w['end_minute'] // 60:02d}{w['end_minute'] % 60:02d}" for w in selected.get("time_windows", [])]
        self.eval_windows_var.set(", ".join(window_tokens))

    def edit_selected_eval_condition(self) -> None:
        selected_id = self._selected_eval_requirement_id()
        if selected_id is None:
            raise ValueError("Select an EVAL requirement first")
        idx = self._eval_condition_index_by_id(selected_id)
        if idx is None:
            raise ValueError("Selected EVAL requirement was not found")
        existing = self.eval_conditions[idx]

        provider_choice = self.eval_provider_var.get().strip()
        room_choice = self.eval_room_var.get().strip()
        provider_id = "any"
        if provider_choice not in ("", "Any provider"):
            profile = self._provider_profile_by_name(provider_choice)
            provider_id = profile.get("provider_id") if profile else provider_choice

        updated = {
            **existing,
            "discipline": self.eval_discipline_var.get().strip(),
            "provider_id": provider_id,
            "provider_ids": [] if provider_id == "any" else [provider_id],
            "room_id": "any" if room_choice == "Any compatible room" else room_choice,
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
        selected_id = self._selected_eval_requirement_id()
        if selected_id is None:
            raise ValueError("Select an EVAL requirement first")
        idx = self._eval_condition_index_by_id(selected_id)
        if idx is None:
            raise ValueError("Selected EVAL requirement was not found")
        self.eval_conditions.pop(idx)
        self._persist_requirements_catalog()
        self._refresh_eval_condition_list()
        self._refresh_auto_condition_list()

    def _selected_eval_requirement_id(self) -> str | None:
        if not hasattr(self, "eval_condition_list"):
            return None
        selection = self.eval_condition_list.selection()
        if not selection:
            return None
        return self.eval_row_lookup.get(selection[0])

    def _eval_condition_index_by_id(self, requirement_id: str) -> int | None:
        for idx, condition in enumerate(self.eval_conditions):
            if condition.get("id") == requirement_id:
                return idx
        return None

    def _eval_requirement_row(self, condition: Dict[str, Any]) -> Dict[str, str]:
        return self._requirement_table_row("EVAL", condition)

    def _sort_eval_condition_table(self, column: str) -> None:
        if not hasattr(self, "eval_condition_list"):
            return
        if self.eval_sort_column == column:
            self.eval_sort_desc = not self.eval_sort_desc
        else:
            self.eval_sort_column = column
            self.eval_sort_desc = False
        self._refresh_eval_condition_list()

    def _eval_sort_value(self, column: str, row: Dict[str, str]) -> Any:
        return self._table_sort_value(column, row)

    def _refresh_eval_condition_list(self) -> None:
        if not hasattr(self, "eval_condition_list"):
            return
        selected_requirement_id = self._selected_eval_requirement_id()
        self.eval_condition_list.delete(*self.eval_condition_list.get_children())
        self.eval_row_lookup = {}
        if not self.eval_conditions:
            return

        rows = [(idx, c, self._eval_requirement_row(c)) for idx, c in enumerate(self.eval_conditions)]
        sorted_rows = sorted(
            rows,
            key=lambda item: (self._eval_sort_value(self.eval_sort_column, item[2]), item[0]),
            reverse=self.eval_sort_desc,
        )
        for display_idx, (_idx, condition, row) in enumerate(sorted_rows):
            item_id = f"eval::{condition['id']}"
            values = [row[col] for col in self.eval_table_columns]
            tag = "even" if display_idx % 2 == 0 else "odd"
            self.eval_condition_list.insert("", self.tk.END, iid=item_id, values=values, tags=(tag,))
            self.eval_row_lookup[item_id] = condition["id"]

        if selected_requirement_id is not None:
            selected_item = f"eval::{selected_requirement_id}"
            if self.eval_condition_list.exists(selected_item):
                self.eval_condition_list.selection_set(selected_item)
                self.eval_condition_list.focus(selected_item)
                self.eval_condition_list.see(selected_item)

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
        selected = self._selected_auto_requirement_key()
        if selected is None:
            return
        source, req_id = selected
        real_idx = self._auto_condition_index_by_id(source, req_id)
        if real_idx is None:
            return
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
        selected = self._selected_auto_requirement_key()
        if selected is None:
            raise ValueError("Select a requirement in the list first")
        source, req_id = selected
        real_idx = self._auto_condition_index_by_id(source, req_id)
        if real_idx is None:
            raise ValueError("Selected requirement was not found")
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
        selected = self._selected_auto_requirement_key()
        if selected is None:
            raise ValueError("Select a requirement in the list first")
        source_program, req_id = selected
        real_idx = self._auto_condition_index_by_id(source_program, req_id)
        if real_idx is None:
            raise ValueError("Selected requirement was not found")
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
        selected = self._selected_auto_requirement_key()
        if selected is None:
            raise ValueError("Select a requirement in the list first")
        source, req_id = selected
        real_idx = self._auto_condition_index_by_id(source, req_id)
        if real_idx is None:
            raise ValueError("Selected requirement was not found")
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
        selected = self._selected_auto_requirement_key()
        self.auto_condition_list.delete(*self.auto_condition_list.get_children())
        self.auto_row_lookup = {}
        if not self.auto_conditions and not (hasattr(self, "show_eval_requirements_var") and self.show_eval_requirements_var.get() and self.eval_conditions):
            return

        display_rows: List[Tuple[str, Dict[str, Any], int, Dict[str, str]]] = [
            ("IOP", c, idx, self._requirement_table_row("IOP", c)) for idx, c in enumerate(self.auto_conditions)
        ]
        if hasattr(self, "show_eval_requirements_var") and self.show_eval_requirements_var.get():
            display_rows.extend(("EVAL", c, idx, self._requirement_table_row("EVAL", c)) for idx, c in enumerate(self.eval_conditions))

        sorted_rows = sorted(
            display_rows,
            key=lambda item: (self._table_sort_value(self.auto_sort_column, item[3]), item[2]),
            reverse=self.auto_sort_desc,
        )

        for disp_idx, (source_program, c, _original_idx, row) in enumerate(sorted_rows):
            item_id = f"{source_program.lower()}::{c['id']}"
            values = [row[col] for col in self.requirement_table_columns]
            tags = ("even" if disp_idx % 2 == 0 else "odd", "iop" if source_program == "IOP" else "eval")
            self.auto_condition_list.insert("", self.tk.END, iid=item_id, values=values, tags=tags)
            self.auto_row_lookup[item_id] = (source_program, c["id"])

        if selected is not None:
            selected_item = f"{selected[0].lower()}::{selected[1]}"
            if self.auto_condition_list.exists(selected_item):
                self.auto_condition_list.selection_set(selected_item)
                self.auto_condition_list.focus(selected_item)

    def _sort_auto_condition_table(self, column: str) -> None:
        if self.auto_sort_column == column:
            self.auto_sort_desc = not self.auto_sort_desc
        else:
            self.auto_sort_column = column
            self.auto_sort_desc = False
        self._refresh_auto_condition_list()

    def _selected_auto_requirement_key(self) -> Tuple[str, str] | None:
        selection = self.auto_condition_list.selection()
        if not selection:
            return None
        return self.auto_row_lookup.get(selection[0])

    def _auto_condition_index_by_id(self, source_program: str, requirement_id: str) -> int | None:
        data = self.auto_conditions if source_program == "IOP" else self.eval_conditions
        for idx, req in enumerate(data):
            if req.get("id") == requirement_id:
                return idx
        return None

    def _requirement_table_row(self, source_program: str, condition: Dict[str, Any]) -> Dict[str, str]:
        patient_scope = condition.get("patient_scope", "")
        patient_ids = condition.get("patient_ids", [])
        patient_text = patient_scope if patient_scope == "all" else ",".join(patient_ids)
        provider_keys = condition.get("provider_ids") or ([condition.get("provider_id", "any")] if condition.get("provider_id", "any") != "any" else [])
        provider_text = ", ".join(self._provider_display_name(p) for p in provider_keys) if provider_keys else "Any"
        room_text = "Any" if condition.get("room_id", "any") == "any" else str(condition.get("room_id"))
        windows = condition.get("time_windows", [])
        time_window = "; ".join(f"{_to_ampm(w['start_minute'])}-{_to_ampm(w['end_minute'])}" for w in windows)
        weeks = ",".join(f"W{w}" for w in condition.get("weeks", []))
        freq = f"{condition.get('sessions_per_week', 1)}/wk"
        if source_program == "IOP" and weeks:
            freq = f"{freq} ({weeks})"
        elif source_program == "EVAL":
            weekday_names = ["Mon", "Tue", "Wed", "Thu", "Fri"]
            freq = ",".join(weekday_names[d] for d in condition.get("weekdays", []) if 0 <= d < len(weekday_names)) or freq
        notes = "hard" if condition.get("hard_constraint", True) else "soft"
        notes = f"{notes}; mode={condition.get('session_mode', '')}"

        return {
            "requirement_id": str(condition.get("id", "")),
            "program_type": source_program,
            "patient": patient_text,
            "discipline": str(condition.get("discipline", "")),
            "duration_minutes": str(condition.get("duration_minutes", "")),
            "frequency_week": freq,
            "provider_constraint": provider_text,
            "room_constraint": room_text,
            "time_window": time_window,
            "notes": notes,
        }

    def _table_sort_value(self, column: str, row: Dict[str, str]) -> Any:
        value = row.get(column, "")
        if column in {"duration_minutes"}:
            try:
                return int(str(value).strip())
            except ValueError:
                return -1
        if column == "frequency_week":
            text = str(value).strip()
            digits = ""
            for ch in text:
                if ch.isdigit():
                    digits += ch
                elif digits:
                    break
            return int(digits) if digits else 0
        if column == "time_window":
            text = str(value).strip()
            if not text:
                return -1
            token = text.split(";", 1)[0].split("-", 1)[0].strip()
            try:
                return parse_time_input(token)
            except ValueError:
                return text.casefold()
        if column == "requirement_id":
            text = str(value)
            head = ""
            tail = ""
            for i, ch in enumerate(text):
                if ch.isdigit():
                    head = text[:i].casefold()
                    tail = text[i:]
                    break
            if tail.isdigit():
                return (head, int(tail))
            return (text.casefold(), 0)
        return str(value).casefold()

    def _solver_limits_from_ui(self) -> Dict[str, int]:
        effort = self.auto_solver_effort_var.get().strip().lower()
        mapping = {
            "standard": {"max_backtrack_states": 250000, "max_candidates_per_request": 5000},
            "high": {"max_backtrack_states": 750000, "max_candidates_per_request": 10000},
            "very high": {"max_backtrack_states": 1500000, "max_candidates_per_request": 20000},
            "maximum": {"max_backtrack_states": 3000000, "max_candidates_per_request": 30000},
        }
        limits = dict(mapping.get(effort, mapping["high"]))
        limits["max_solve_seconds"] = int(self.app_settings.get("max_solve_seconds", 10))
        return limits

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

    def _build_eval_profile_template(self) -> Dict[str, Any]:
        start = parse_date_parts(self.eval_start_year_var.get(), self.eval_start_month_var.get(), self.eval_start_day_var.get())
        cohort_type = self.eval_cohort_var.get().strip()
        if cohort_type not in {"Mon-Wed", "Tue-Thu"}:
            raise ValueError("Cohort type must be Mon-Wed or Tue-Thu")

        target_weekday = 1 if cohort_type == "Tue-Thu" else 0
        while start.weekday() != target_weekday:
            start += timedelta(days=1)

        eval_dates = [(start + timedelta(days=offset)).isoformat() for offset in (0, 1, 2)]
        day_start = parse_time_input(self.day_start_var.get())
        day_end = parse_time_input(self.day_end_var.get())
        eval_patient_count = int(self.eval_patient_count_var.get())
        if eval_patient_count < 1:
            raise ValueError("EVAL patient count must be at least 1")
        eval_patient_ids = [f"E{i}" for i in range(1, eval_patient_count + 1)]

        base_profile = {
            "date_key": eval_dates[0],
            "planning_dates": eval_dates,
            "weekday": start.weekday(),
            "day_window": {"start_minute": day_start, "end_minute": day_end},
            "providers": build_provider_records_from_profiles(self.provider_profiles, day_start, day_end)
            or build_provider_records(self.provider_catalog, day_start, day_end),
            "patients": [],
            "rooms": build_room_records(self.room_rules),
        }
        return self._ensure_profile_patients(base_profile, eval_patient_ids)

    def _ensure_profile_patients(self, profile_template: Dict[str, Any], patient_ids: List[str]) -> Dict[str, Any]:
        ensured = copy.deepcopy(profile_template)
        planning_dates = list(ensured.get("planning_dates", []))
        day_window = ensured.get("day_window", {})
        day_start = int(day_window.get("start_minute", GRID_START_MINUTE))
        day_end = int(day_window.get("end_minute", GRID_END_MINUTE))

        availability = {d: [{"start_minute": day_start, "end_minute": day_end}] for d in planning_dates}
        existing_patients = list(ensured.get("patients", []))
        existing_ids = {str(p.get("id", "")) for p in existing_patients}

        for pid in patient_ids:
            if pid in existing_ids:
                continue
            existing_patients.append({"id": pid, "name": f"Patient {pid}", "availability": availability})
            existing_ids.add(pid)

        ensured["patients"] = existing_patients
        return ensured

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
            prefix = "Preflight feasibility check failed." if result.get("report", {}).get("preflight") else "Auto-generation failed."
            text = prefix + "\n" + "\n".join(issues[:5] or ["No detailed bottlenecks available."])
            self._set_text(self.auto_report_text, text)
            self.status_var.set("Status: IOP preflight failed" if result.get("report", {}).get("preflight") else "Status: Auto-generation failed")
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
        if not self.eval_conditions:
            raise ValueError("Add at least one EVAL requirement before generation")
        profile_template = self._build_eval_profile_template()
        result = generate_three_week_schedule(
            profile_template=profile_template,
            requirements=self.eval_conditions,
            previous_assignments=self._existing_assignment_map(),
            solver_limits=self._solver_limits_from_ui(),
            locked_request_ids=self._soft_locked_request_ids(),
        )
        if not result.get("ok"):
            issues = result.get("report", {}).get("issues", [])
            prefix = "Preflight feasibility check failed." if result.get("report", {}).get("preflight") else "EVAL generation failed."
            self._set_text(self.eval_report_text, prefix + "\n" + "\n".join(issues[:5] or ["No detailed bottlenecks available."]))
            self.status_var.set("Status: EVAL preflight failed" if result.get("report", {}).get("preflight") else "Status: EVAL generation failed")
            return
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
        if not iop_result.get("ok"):
            issues = iop_result.get("report", {}).get("issues", [])
            prefix = "IOP preflight feasibility check failed." if iop_result.get("report", {}).get("preflight") else "Combined generation failed during IOP stage."
            self._set_text(self.eval_report_text, prefix + "\n" + "\n".join(issues[:5] or ["No detailed bottlenecks available."]))
            self.status_var.set("Status: Combined generation stopped at IOP preflight")
            return

        if not self.eval_conditions:
            raise ValueError("Add at least one EVAL requirement before combined generation")
        eval_profile_template = self._build_eval_profile_template()
        eval_result = generate_three_week_schedule(
            profile_template=eval_profile_template,
            requirements=self.eval_conditions,
            previous_assignments={**existing, **iop_result.get("assignments", {})},
            solver_limits=self._solver_limits_from_ui(),
            locked_request_ids=soft_locked_ids,
        )
        if not eval_result.get("ok"):
            issues = eval_result.get("report", {}).get("issues", [])
            prefix = "EVAL preflight feasibility check failed." if eval_result.get("report", {}).get("preflight") else "Combined generation failed during EVAL stage."
            self._set_text(self.eval_report_text, prefix + "\n" + "\n".join(issues[:5] or ["No detailed bottlenecks available."]))
            self.status_var.set("Status: Combined generation stopped at EVAL preflight")
            return

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

    def _current_clinic_config(self, config_name: str = "Clinic Config") -> Dict[str, Any]:
        config_id = "current-clinic-config"
        if self.loaded_profile:
            config_id = str((self.loaded_profile.get("clinic_config") or {}).get("config_id") or config_id)
        return {
            "artifact_type": "clinic_config",
            "schema_version": 1,
            "config_id": config_id,
            "config_name": config_name,
            "saved_at": datetime.utcnow().isoformat(),
            "provider_profiles": copy.deepcopy(self.provider_profiles),
            "provider_catalog": list(self.provider_catalog),
            "room_rules": copy.deepcopy(self.room_rules),
            "discipline_registry": copy.deepcopy(self.discipline_registry),
            "default_settings": {
                "day_start": self.day_start_var.get() if hasattr(self, "day_start_var") else "0730",
                "day_end": self.day_end_var.get() if hasattr(self, "day_end_var") else "1800",
            },
        }

    def _clinic_config_hash(self, payload: Dict[str, Any]) -> str:
        core = json.dumps({
            "provider_profiles": payload.get("provider_profiles", []),
            "provider_catalog": payload.get("provider_catalog", []),
            "room_rules": payload.get("room_rules", {}),
            "discipline_registry": payload.get("discipline_registry", []),
        }, sort_keys=True)
        return str(abs(hash(core)))

    def save_clinic_config(self) -> None:
        path_raw = self.filedialog.asksaveasfilename(
            title="Save Clinic Config",
            defaultextension=".clinic.json",
            initialfile=f"clinic_config_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.clinic.json",
            filetypes=[("Clinic config", "*.clinic.json"), ("JSON files", "*.json")],
        )
        if not path_raw:
            return
        payload = self._current_clinic_config()
        self.active_clinic_config = payload
        self._update_loaded_artifact_status()
        save_json(Path(path_raw), payload)
        self.status_var.set(f"Status: Clinic config saved to {path_raw}")

    def _apply_clinic_config(self, payload: Dict[str, Any]) -> None:
        self.provider_profiles = normalize_provider_catalog(
            payload.get("provider_profiles") or payload.get("providers") or self.provider_profiles,
            defaults=DEFAULT_PROVIDER_NAMES,
            all_rooms=PREDEFINED_ROOMS,
            disciplines=DISCIPLINES,
        )
        self.provider_catalog = [p["provider_name"] for p in self.provider_profiles]
        self.room_rules = payload.get("room_rules") or self.room_rules
        self.discipline_registry = self._normalize_discipline_registry(payload.get("discipline_registry", self.discipline_registry))
        self._persist_provider_catalog()
        save_room_rules(self.room_rules, valid_rooms=PREDEFINED_ROOMS)
        self._apply_discipline_registry_to_ui()
        self._refresh_provider_dropdowns()
        if hasattr(self, "room_rule_list"):
            self.revert_room_rules_editor()
        if hasattr(self, "provider_search_list"):
            self.refresh_provider_profile_list()
        if self.loaded_profile:
            self.loaded_profile["clinic_config"] = {
                "config_id": payload.get("config_id", "current-clinic-config"),
                "schema_version": payload.get("schema_version", 1),
            }
            self.loaded_profile["discipline_registry"] = copy.deepcopy(self.discipline_registry)
            self._sync_profile_resources(self.loaded_profile)

    def load_clinic_config(self) -> None:
        path_raw = self.filedialog.askopenfilename(
            title="Load Clinic Config",
            filetypes=[("Clinic config", "*.clinic.json"), ("JSON files", "*.json")],
        )
        if not path_raw:
            return
        payload = json.loads(Path(path_raw).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Clinic config file must be a JSON object")
        self._apply_clinic_config(payload)
        self.active_clinic_config = payload
        self._update_loaded_artifact_status()
        self.status_var.set(f"Status: Clinic config loaded from {path_raw}")

    def save_schedule_snapshot(self) -> None:
        profile = self._require_profile()
        clinic = self._current_clinic_config()
        snapshot = {
            "artifact_type": "schedule_snapshot",
            "schema_version": 1,
            "snapshot_id": uuid.uuid4().hex,
            "snapshot_name": f"snapshot_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}",
            "saved_at": datetime.utcnow().isoformat(),
            "clinic_config_ref": {
                "config_id": clinic.get("config_id"),
                "schema_version": clinic.get("schema_version", 1),
                "optional_hash": self._clinic_config_hash(clinic),
            },
            "embedded_clinic_config": clinic,
            "schedule_grid_data": {
                "profile": copy.deepcopy(profile),
                "last_result": copy.deepcopy(self.last_result),
            },
            "generator_state": {
                "iop_requirements": copy.deepcopy(self.auto_conditions),
                "eval_requirements": copy.deepcopy(self.eval_conditions),
                "settings": {
                    "auto_start": [self.auto_start_year_var.get(), self.auto_start_month_var.get(), self.auto_start_day_var.get()] if hasattr(self, "auto_start_year_var") else [],
                    "eval_start": [self.eval_start_year_var.get(), self.eval_start_month_var.get(), self.eval_start_day_var.get()] if hasattr(self, "eval_start_year_var") else [],
                    "eval_cohort": self.eval_cohort_var.get() if hasattr(self, "eval_cohort_var") else "Mon-Wed",
                    "eval_patient_count": self.eval_patient_count_var.get() if hasattr(self, "eval_patient_count_var") else "3",
                },
            },
            "manual_edits_metadata": {"undo_depth": len(self.manual_undo_stack)},
        }
        path_raw = self.filedialog.asksaveasfilename(
            title="Save Schedule Snapshot",
            defaultextension=".schedule.json",
            initialfile=f"schedule_snapshot_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.schedule.json",
            filetypes=[("Schedule snapshot", "*.schedule.json"), ("JSON files", "*.json")],
        )
        if not path_raw:
            return
        self.active_schedule_snapshot = snapshot
        self._update_loaded_artifact_status()
        save_json(Path(path_raw), snapshot)
        self.status_var.set(f"Status: Schedule snapshot saved to {path_raw}")

    def load_schedule_snapshot(self) -> None:
        path_raw = self.filedialog.askopenfilename(
            title="Load Schedule Snapshot",
            filetypes=[("Schedule snapshot", "*.schedule.json"), ("JSON files", "*.json")],
        )
        if not path_raw:
            return
        payload = json.loads(Path(path_raw).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Schedule snapshot file must be a JSON object")

        # Backward compatibility: old profile format
        if payload.get("artifact_type") not in {"schedule_snapshot", "clinic_config"} and "requests" in payload:
            self.loaded_profile = payload
            self._sync_profile_resources(self.loaded_profile)
            self.last_result = build_live_result_from_profile(self.loaded_profile)
            self._render_patient_grid(self.loaded_profile, self.last_result)
            self._refresh_profile_preview()
            self.active_schedule_snapshot = {"snapshot_name": "Legacy Profile"}
            self._update_loaded_artifact_status()
            self.status_var.set("Status: Loaded legacy profile as schedule snapshot")
            return

        if payload.get("artifact_type") == "clinic_config":
            self._apply_clinic_config(payload)
            self.active_clinic_config = payload
            self._update_loaded_artifact_status()
            self.status_var.set("Status: Loaded clinic config file")
            return

        clinic_ref = payload.get("clinic_config_ref", {})
        current_clinic = self._current_clinic_config()
        current_id = current_clinic.get("config_id")
        ref_id = clinic_ref.get("config_id")
        if ref_id and current_id and ref_id != current_id:
            use_current = self.messagebox.askyesno(
                "Clinic Config Mismatch",
                "Snapshot references a different clinic config.\n\nYes: load with CURRENT clinic config (best effort).\nNo: load EMBEDDED clinic config from snapshot (if present).",
            )
            if not use_current:
                embedded = payload.get("embedded_clinic_config")
                if isinstance(embedded, dict):
                    self._apply_clinic_config(embedded)
                    self.active_clinic_config = embedded

        grid_data = payload.get("schedule_grid_data", {})
        profile = grid_data.get("profile") or {}
        if not profile:
            raise ValueError("Snapshot missing schedule profile data")
        self.loaded_profile = profile
        self._sync_profile_resources(self.loaded_profile)
        self.last_result = grid_data.get("last_result") or build_live_result_from_profile(self.loaded_profile)

        gen_state = payload.get("generator_state", {})
        self.auto_conditions = [validate_requirement(c) for c in gen_state.get("iop_requirements", []) if isinstance(c, dict)]
        self.eval_conditions = [validate_requirement(c) for c in gen_state.get("eval_requirements", []) if isinstance(c, dict)]
        self._persist_requirements_catalog()
        if hasattr(self, "auto_condition_list"):
            self._refresh_auto_condition_list()
        if hasattr(self, "eval_condition_list"):
            self._refresh_eval_condition_list()

        self._render_patient_grid(self.loaded_profile, self.last_result)
        self._refresh_profile_preview()
        self.active_schedule_snapshot = payload
        self._update_loaded_artifact_status()
        self.status_var.set(f"Status: Schedule snapshot loaded from {path_raw}")

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
        if isinstance(profile.get("clinic_config"), dict):
            self.active_clinic_config = dict(profile.get("clinic_config") or {})
            self._update_loaded_artifact_status()
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
