from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Tuple

from app.api_server import handle_generate
from app.health_check import run_health_check
from app.persistence import (
    load_last_profile,
    load_provider_catalog,
    save_last_profile,
    save_last_schedule,
    save_provider_catalog,
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
        self.last_result: Dict[str, Any] | None = None
        self.auto_conditions: List[Dict[str, Any]] = []
        self.auto_reconfigure_history: List[List[Dict[str, Any]]] = []
        self.loaded_profile: Dict[str, Any] | None = load_last_profile()
        self.loaded_profile_path: Path | None = None

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

        manual_tab = ttk.Frame(notebook)
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

        frame = ttk.Labelframe(parent, text="3-Week Schedule Ingredients", padding=10)
        frame.pack(fill="x", padx=6, pady=6)

        self.auto_patients_var = self.tk.StringVar(value="6")
        self.auto_start_year_var = self.tk.StringVar(value=str(today.year))
        self.auto_start_month_var = self.tk.StringVar(value=f"{today.month:02d}")
        self.auto_start_day_var = self.tk.StringVar(value=f"{today.day:02d}")

        self.auto_provider_var = self.tk.StringVar(value=self.provider_catalog[0] if self.provider_catalog else "")
        self.auto_discipline_var = self.tk.StringVar(value=DISCIPLINES[1])
        self.auto_weekday_a_var = self.tk.StringVar(value="Monday")
        self.auto_weekday_b_var = self.tk.StringVar(value="Wednesday")
        self.auto_duration_var = self.tk.StringVar(value="60")
        self.auto_window_start_var = self.tk.StringVar(value="0800")
        self.auto_window_end_var = self.tk.StringVar(value="1400")

        weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

        ttk.Label(frame, text="Patient count").grid(row=0, column=0, sticky="w")
        ttk.Combobox(frame, textvariable=self.auto_patients_var, values=[str(i) for i in range(1, 100)], state="readonly", width=8).grid(row=1, column=0, padx=2)

        ttk.Label(frame, text="Start Year").grid(row=0, column=1, sticky="w")
        ttk.Combobox(frame, textvariable=self.auto_start_year_var, values=[str(today.year + i) for i in range(0, 3)], state="readonly", width=8).grid(row=1, column=1, padx=2)
        ttk.Label(frame, text="Start Month").grid(row=0, column=2, sticky="w")
        ttk.Combobox(frame, textvariable=self.auto_start_month_var, values=[f"{m:02d}" for m in range(1, 13)], state="readonly", width=6).grid(row=1, column=2, padx=2)
        ttk.Label(frame, text="Start Day").grid(row=0, column=3, sticky="w")
        ttk.Combobox(frame, textvariable=self.auto_start_day_var, values=[f"{d:02d}" for d in range(1, 32)], state="readonly", width=6).grid(row=1, column=3, padx=2)

        ttk.Separator(parent, orient="horizontal").pack(fill="x", padx=6, pady=6)

        cond = ttk.Labelframe(parent, text="Add Condition", padding=10)
        cond.pack(fill="x", padx=6, pady=6)

        ttk.Label(cond, text="Provider").grid(row=0, column=0, sticky="w")
        self.auto_provider_combo = ttk.Combobox(cond, textvariable=self.auto_provider_var, values=self.provider_catalog, state="readonly", width=22)
        self.auto_provider_combo.grid(row=1, column=0, padx=2)

        ttk.Label(cond, text="Discipline").grid(row=0, column=1, sticky="w")
        ttk.Combobox(cond, textvariable=self.auto_discipline_var, values=DISCIPLINES, state="readonly", width=22).grid(row=1, column=1, padx=2)

        ttk.Label(cond, text="Weekday A").grid(row=0, column=2, sticky="w")
        ttk.Combobox(cond, textvariable=self.auto_weekday_a_var, values=weekdays, state="readonly", width=12).grid(row=1, column=2, padx=2)
        ttk.Label(cond, text="Weekday B").grid(row=0, column=3, sticky="w")
        ttk.Combobox(cond, textvariable=self.auto_weekday_b_var, values=["(none)"] + weekdays, state="readonly", width=12).grid(row=1, column=3, padx=2)
        if self.auto_weekday_b_var.get() == "":
            self.auto_weekday_b_var.set("Wednesday")

        ttk.Label(cond, text="Duration (min)").grid(row=0, column=4, sticky="w")
        ttk.Combobox(cond, textvariable=self.auto_duration_var, values=["30", "45", "60", "75", "90"], state="readonly", width=10).grid(row=1, column=4, padx=2)

        ttk.Label(cond, text="Window Start").grid(row=0, column=5, sticky="w")
        ttk.Combobox(cond, textvariable=self.auto_window_start_var, values=time_choices, state="readonly", width=10).grid(row=1, column=5, padx=2)
        ttk.Label(cond, text="Window End").grid(row=0, column=6, sticky="w")
        ttk.Combobox(cond, textvariable=self.auto_window_end_var, values=time_choices, state="readonly", width=10).grid(row=1, column=6, padx=2)

        ttk.Button(cond, text="Add Condition", command=lambda: self._safe_action(self.add_auto_condition)).grid(row=1, column=7, padx=6)

        list_frame = ttk.Labelframe(parent, text="Condition List", padding=10)
        list_frame.pack(fill="both", expand=True, padx=6, pady=6)

        self.auto_condition_list = self.scrolledtext.ScrolledText(list_frame, height=10, wrap=self.tk.WORD)
        self.auto_condition_list.pack(fill="both", expand=True)
        self.auto_condition_list.insert(self.tk.END, "No conditions added yet.\n")
        self.auto_condition_list.configure(state=self.tk.DISABLED)

        action_frame = ttk.Frame(parent)
        action_frame.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Button(action_frame, text="Auto-generate 3-week schedule", command=lambda: self._safe_action(self.generate_auto_schedule)).pack(side="left", padx=4)

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

        self.view_date_var = self.tk.StringVar(value=date_to_key(current))
        self.ttk.Label(cal, text="View Date").grid(row=0, column=5, sticky="w")
        self.view_date_combo = self.ttk.Combobox(cal, textvariable=self.view_date_var, values=[date_to_key(current)], state="readonly", width=14)
        self.view_date_combo.grid(row=1, column=5, padx=2)

        ttk.Button(cal, text="Apply Calendar Settings", command=lambda: self._safe_action(self.apply_calendar_settings)).grid(row=1, column=6, padx=6)

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

        shift = ttk.Labelframe(parent, text="Auto reconfigure", padding=6)
        shift.pack(fill="x")
        self.shift_patient_var = self.tk.StringVar(value="All")
        self.shift_discipline_var = self.tk.StringVar(value=DISCIPLINES[1])
        self.shift_from_date_var = self.tk.StringVar(value=date_to_key(current))
        self.shift_to_date_var = self.tk.StringVar(value=date_to_key(current))

        self.ttk.Label(shift, text="Patient").grid(row=0, column=0, sticky="w")
        self.shift_patient_combo = self.ttk.Combobox(shift, textvariable=self.shift_patient_var, values=["All"] + PATIENT_ID_CHOICES, state="readonly", width=12)
        self.shift_patient_combo.grid(row=1, column=0, padx=2)

        self.ttk.Label(shift, text="Discipline").grid(row=0, column=1, sticky="w")
        self.ttk.Combobox(shift, textvariable=self.shift_discipline_var, values=DISCIPLINES, state="readonly", width=22).grid(row=1, column=1, padx=2)

        self.ttk.Label(shift, text="From Date").grid(row=0, column=2, sticky="w")
        self.shift_from_combo = self.ttk.Combobox(shift, textvariable=self.shift_from_date_var, values=[date_to_key(current)], state="readonly", width=14)
        self.shift_from_combo.grid(row=1, column=2, padx=2)

        self.ttk.Label(shift, text="To Date").grid(row=0, column=3, sticky="w")
        self.shift_to_combo = self.ttk.Combobox(shift, textvariable=self.shift_to_date_var, values=[date_to_key(current)], state="readonly", width=14)
        self.shift_to_combo.grid(row=1, column=3, padx=2)

        ttk.Button(shift, text="Apply Auto reconfigure", command=lambda: self._safe_action(self.apply_auto_reconfigure)).grid(row=1, column=4, padx=6)
        ttk.Button(shift, text="Undo Auto reconfigure", command=lambda: self._safe_action(self.undo_auto_reconfigure)).grid(row=1, column=5, padx=6)

    def _safe_action(self, fn) -> None:
        try:
            fn()
        except Exception as exc:
            self.status_var.set("Status: Error")
            self.messagebox.showerror("Action failed", str(exc))

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
        profile["providers"] = build_provider_records(self.provider_catalog, day_start, day_end)
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
        for combo in [self.view_date_combo, self.appt_date_combo, self.shift_from_combo, self.shift_to_combo]:
            combo["values"] = dates

        for var in [self.view_date_var, self.appt_date_var, self.shift_from_date_var, self.shift_to_date_var]:
            if var.get() not in dates:
                var.set(dates[0])

    def _refresh_provider_dropdowns(self) -> None:
        self.appt_provider_combo["values"] = self.provider_catalog
        self.provider_manage_combo["values"] = self.provider_catalog
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
        view_date = self.view_date_var.get().strip()

        request_map = {r["id"]: r for r in profile.get("requests", []) if "id" in r}
        appointments = []
        patient_ids = set()
        for assignment in result.get("assignments", {}).values():
            req = request_map.get(assignment.get("request_id", ""), {})
            if req.get("date_key") != view_date:
                continue
            pids = [str(pid) for pid in req.get("patient_ids", [])]
            if not pids:
                continue
            appointments.append(
                {
                    "start": int(assignment["start_minute"]),
                    "end": int(assignment["end_minute"]),
                    "discipline": req.get("discipline", "Other"),
                    "provider": assignment.get("provider_id", ""),
                    "room": assignment.get("room_id", ""),
                    "patients": pids,
                }
            )
            patient_ids.update(pids)

        patient_labels = sorted(patient_ids, key=lambda x: int(x) if x.isdigit() else x)
        if not patient_labels:
            canvas.create_text(16, 20, anchor="w", text=f"No appointments on {view_date}.", fill="#003049", font=("Segoe UI", 11, "bold"))
            return

        time_col_w, header_h, row_h, patient_col_w = 85, 34, 22, 180
        total_w = time_col_w + len(patient_labels) * patient_col_w
        total_h = header_h + len(minutes) * row_h
        canvas.config(scrollregion=(0, 0, total_w, total_h))

        canvas.create_rectangle(0, 0, time_col_w, header_h, fill="#0b4f6c", outline="#0b4f6c")
        canvas.create_text(time_col_w // 2, header_h // 2, text="Time", fill="white", font=("Segoe UI", 10, "bold"))

        for idx, label in enumerate(patient_labels):
            x0 = time_col_w + idx * patient_col_w
            x1 = x0 + patient_col_w
            canvas.create_rectangle(x0, 0, x1, header_h, fill="#1d3557", outline="#f1faee")
            canvas.create_text((x0 + x1) // 2, header_h // 2, text=f"Patient {label}", fill="white", font=("Segoe UI", 9, "bold"))

        for row_idx, minute in enumerate(minutes):
            y0 = header_h + row_idx * row_h
            y1 = y0 + row_h
            canvas.create_rectangle(0, y0, time_col_w, y1, fill="#f8f9fa" if row_idx % 2 == 0 else "#e9ecef", outline="#adb5bd")
            canvas.create_text(time_col_w // 2, (y0 + y1) // 2, text=_to_ampm(minute), fill="#1b263b", font=("Segoe UI", 8))
            for col_idx in range(len(patient_labels)):
                x0 = time_col_w + col_idx * patient_col_w
                x1 = x0 + patient_col_w
                canvas.create_rectangle(x0, y0, x1, y1, fill="#ffffff", outline="#dee2e6")

        used_disciplines = set()
        for appt in appointments:
            start_idx = max(0, (appt["start"] - GRID_START_MINUTE) // GRID_SLOT_MINUTES)
            end_idx = min(len(minutes), (appt["end"] - GRID_START_MINUTE) // GRID_SLOT_MINUTES)
            if end_idx <= start_idx:
                continue
            for pid in appt["patients"]:
                if pid not in patient_labels:
                    continue
                col_idx = patient_labels.index(pid)
                x0 = time_col_w + col_idx * patient_col_w + 1
                x1 = x0 + patient_col_w - 2
                y0 = header_h + start_idx * row_h + 1
                y1 = header_h + end_idx * row_h - 1
                color = DISCIPLINE_COLORS.get(appt["discipline"], "#ffb3c1")
                canvas.create_rectangle(x0, y0, x1, y1, fill=color, outline="#495057", width=2)
                text = f"{appt['discipline']}\n{appt['room']}\n{appt['provider']}"
                canvas.create_text((x0 + x1) // 2, (y0 + y1) // 2, text=text, fill="#1b263b", font=("Segoe UI", 8), justify="center")
                used_disciplines.add(appt["discipline"])

        self.legend_var.set("Legend: " + " | ".join(sorted(used_disciplines)) if used_disciplines else "Legend: No assigned sessions")

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
        self._sync_profile_resources(self.loaded_profile)
        self.profile_var.set(f"Profile: {path}")
        self.status_var.set("Status: Profile loaded")
        self._refresh_profile_preview()

    def apply_calendar_settings(self) -> None:
        profile = self._require_profile()
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
        if not name:
            raise ValueError("Enter a provider name to add")
        if name in self.provider_catalog:
            raise ValueError("Provider already exists")
        self.provider_catalog = sorted(self.provider_catalog + [name])
        save_provider_catalog(self.provider_catalog)
        if self.loaded_profile:
            self._sync_profile_resources(self.loaded_profile)
        self.provider_new_var.set("")
        self.status_var.set(f"Status: Added provider {name}")
        self._refresh_profile_preview()

    def remove_provider(self) -> None:
        name = self.provider_selected_var.get().strip()
        if not name:
            raise ValueError("Select a provider to remove")
        if name not in self.provider_catalog:
            raise ValueError("Selected provider not found")
        if len(self.provider_catalog) <= 1:
            raise ValueError("Cannot remove the last provider")
        self.provider_catalog = [p for p in self.provider_catalog if p != name]
        save_provider_catalog(self.provider_catalog)
        if self.loaded_profile:
            self._sync_profile_resources(self.loaded_profile)
        self.status_var.set(f"Status: Removed provider {name}")
        self._refresh_profile_preview()

    def add_appointment(self) -> None:
        profile = self._require_profile()
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
        live_result = build_live_result_from_profile(profile)
        self.last_result = live_result
        self.status_var.set(f"Status: Added appointment {req_id}")
        self._render_patient_grid(profile, live_result)
        self._refresh_profile_preview()

    def apply_auto_reconfigure(self) -> None:
        profile = self._require_profile()
        patient = self.shift_patient_var.get().strip()
        discipline = self.shift_discipline_var.get().strip()
        from_date = self.shift_from_date_var.get().strip()
        to_date = self.shift_to_date_var.get().strip()
        if from_date == to_date:
            raise ValueError("From Date and To Date must be different")

        self.auto_reconfigure_history.append(copy.deepcopy(profile.get("requests", [])))
        moved = 0
        for req in profile.get("requests", []):
            if req.get("date_key") != from_date:
                continue
            if req.get("discipline") != discipline:
                continue
            if patient != "All" and patient not in req.get("patient_ids", []):
                continue
            req["date_key"] = to_date
            moved += 1

        if moved == 0:
            raise ValueError("No matching appointments found for this re-jigger rule")

        self._run_auto_reorganize_for_dates([from_date, to_date])
        live_result = build_live_result_from_profile(profile)
        self.last_result = live_result
        self.status_var.set(f"Status: Auto reconfigure moved {moved} appointment(s)")
        self._render_patient_grid(profile, live_result)
        self._refresh_profile_preview()

    def add_auto_condition(self) -> None:
        weekday_map = {"Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3, "Friday": 4}

        window_start = parse_time_input(self.auto_window_start_var.get())
        window_end = parse_time_input(self.auto_window_end_var.get())
        if window_end <= window_start:
            raise ValueError("Condition window end must be after window start")

        weekdays = [self.auto_weekday_a_var.get()]
        if self.auto_weekday_b_var.get() not in ("", "(none)"):
            weekdays.append(self.auto_weekday_b_var.get())

        condition = {
            "provider": self.auto_provider_var.get().strip(),
            "discipline": self.auto_discipline_var.get().strip(),
            "weekdays": [weekday_map[w] for w in weekdays],
            "duration": int(self.auto_duration_var.get()),
            "window_start": window_start,
            "window_end": window_end,
        }
        self.auto_conditions.append(condition)
        self._refresh_auto_condition_list()
        self.status_var.set(f"Status: Added auto-condition #{len(self.auto_conditions)}")

    def _refresh_auto_condition_list(self) -> None:
        lines: List[str] = []
        if not self.auto_conditions:
            lines.append("No conditions added yet.")
        else:
            name_map = ["Mon", "Tue", "Wed", "Thu", "Fri"]
            for idx, c in enumerate(self.auto_conditions, start=1):
                days = ",".join(name_map[d] for d in c["weekdays"])
                lines.append(
                    f"{idx}) {c['discipline']} with {c['provider']} on {days} "
                    f"for {c['duration']} min between {_to_ampm(c['window_start'])}-{_to_ampm(c['window_end'])}"
                )

        self._set_text(self.auto_condition_list, "\n".join(lines))

    def generate_auto_schedule(self) -> None:
        if not self.auto_conditions:
            raise ValueError("Add at least one condition before auto-generating")

        start = parse_date_parts(self.auto_start_year_var.get(), self.auto_start_month_var.get(), self.auto_start_day_var.get())
        patient_count = int(self.auto_patients_var.get())
        if patient_count < 1:
            raise ValueError("Patient count must be at least 1")

        date_keys = planning_dates(start, weeks=3)
        day_start = parse_time_input(self.day_start_var.get())
        day_end = parse_time_input(self.day_end_var.get())

        profile = {
            "date_key": date_keys[0],
            "planning_dates": date_keys,
            "weekday": start.weekday(),
            "day_window": {"start_minute": day_start, "end_minute": day_end},
            "providers": build_provider_records(self.provider_catalog, day_start, day_end),
            "patients": build_patient_records(date_keys, day_start, day_end)[:patient_count],
            "rooms": build_room_records(),
            "requests": [],
        }

        patient_ids = [p["id"] for p in profile["patients"]]
        date_by_weekday: Dict[int, List[str]] = {i: [] for i in range(5)}
        for d in date_keys:
            wd = date.fromisoformat(d).weekday()
            if wd < 5:
                date_by_weekday[wd].append(d)

        req_num = 1
        for cond in self.auto_conditions:
            for wd in cond["weekdays"]:
                for d in date_by_weekday.get(wd, []):
                    for pid in patient_ids:
                        profile["requests"].append(
                            {
                                "id": f"auto_{req_num}",
                                "patient_ids": [pid],
                                "discipline": cond["discipline"],
                                "duration_minutes": cond["duration"],
                                "mode": "individual",
                                "date_key": d,
                                "preferred_window": {
                                    "start_minute": cond["window_start"],
                                    "end_minute": cond["window_end"],
                                },
                                "group_key": None,
                                "label": f"{cond['discipline']} (Patient {pid})",
                                "provider_id": cond["provider"],
                                "room_id": PREDEFINED_ROOMS[0],
                            }
                        )
                        req_num += 1

        bottlenecks: List[str] = []
        request_by_id = {r["id"]: r for r in profile["requests"]}
        for d in date_keys:
            payload = {
                "date_key": d,
                "weekday": date.fromisoformat(d).weekday(),
                "day_window": profile["day_window"],
                "providers": profile["providers"],
                "patients": profile["patients"],
                "rooms": profile["rooms"],
                "requests": [r for r in profile["requests"] if r["date_key"] == d],
            }
            if not payload["requests"]:
                continue

            try:
                result = handle_generate(payload)
            except Exception as exc:
                bottlenecks.append(f"{d}: {exc}")
                continue

            for req_id, a in result.get("assignments", {}).items():
                r = request_by_id.get(req_id)
                if not r:
                    continue
                r["provider_id"] = a["provider_id"]
                r["room_id"] = a["room_id"]
                r["preferred_window"] = {"start_minute": a["start_minute"], "end_minute": a["end_minute"]}

        self.loaded_profile = profile
        self.loaded_profile_path = None
        self.last_result = build_live_result_from_profile(profile)
        self._refresh_profile_preview()
        self._render_patient_grid(profile, self.last_result)

        if bottlenecks:
            msg = "Auto-generation completed with bottlenecks\n\n" + "\n".join(bottlenecks[:20])
            self.status_var.set("Status: Auto-generation completed with bottlenecks")
            self.messagebox.showwarning("Auto-generation bottlenecks", msg)
        else:
            self.status_var.set("Status: Auto-generation completed successfully")

    def undo_auto_reconfigure(self) -> None:
        profile = self._require_profile()
        if not self.auto_reconfigure_history:
            raise ValueError("No Auto reconfigure action to undo")
        profile["requests"] = self.auto_reconfigure_history.pop()
        self.last_result = build_live_result_from_profile(profile)
        self._render_patient_grid(profile, self.last_result)
        self._refresh_profile_preview()
        self.status_var.set("Status: Undid last Auto reconfigure")

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
        payload = self._payload_for_date(self.view_date_var.get().strip())
        result = handle_generate(payload) if payload["requests"] else {"assignments": {}, "room_timeline": {}}
        self.last_result = build_live_result_from_profile(profile)
        save_last_schedule(self.last_result)
        summary = summarize_schedule(result)
        self.status_var.set(f"Status: Generated {summary.assignment_count} sessions for {payload['date_key']}")
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
