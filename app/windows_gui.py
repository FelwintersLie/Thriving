from __future__ import annotations

import json
from dataclasses import dataclass
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


@dataclass
class DashboardSummary:
    assignment_count: int
    room_count: int
    booked_slot_count: int


def summarize_schedule(result: Dict[str, Any]) -> DashboardSummary:
    assignments = result.get("assignments", {})
    room_timeline = result.get("room_timeline", {})
    booked_slot_count = sum(
        1
        for slots in room_timeline.values()
        for slot in slots
        if slot.get("status") == "booked"
    )
    return DashboardSummary(
        assignment_count=len(assignments),
        room_count=len(room_timeline),
        booked_slot_count=booked_slot_count,
    )


def _safe_tk_import():
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext, ttk

    return tk, ttk, messagebox, scrolledtext, filedialog


def _to_ampm(minute: int) -> str:
    hour_24 = minute // 60
    minute_of_hour = minute % 60
    period = "AM" if hour_24 < 12 else "PM"
    hour_12 = hour_24 % 12
    if hour_12 == 0:
        hour_12 = 12
    return f"{hour_12}:{minute_of_hour:02d} {period}"


def _grid_minutes() -> List[int]:
    return list(range(GRID_START_MINUTE, GRID_END_MINUTE, GRID_SLOT_MINUTES))


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


def build_provider_records(provider_names: List[str], weekday: int, day_start: int, day_end: int) -> List[Dict[str, Any]]:
    return [
        {
            "id": name,
            "name": name,
            "disciplines": list(DISCIPLINES),
            "templates": [{"weekday": int(weekday), "windows": [{"start_minute": day_start, "end_minute": day_end}]}],
            "exceptions": [],
        }
        for name in provider_names
    ]


def build_room_records() -> List[Dict[str, Any]]:
    return [
        {"id": room, "name": room, "capacity": 10, "allowed_disciplines": list(DISCIPLINES)}
        for room in PREDEFINED_ROOMS
    ]


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
        }
    return {"assignments": assignments, "room_timeline": {}}


def build_patient_grid_data(
    profile: Dict[str, Any],
    result: Dict[str, Any],
) -> Tuple[List[int], List[str], Dict[Tuple[int, str], Dict[str, str]]]:
    patients = profile.get("patients", [])
    patient_ids = [p.get("id", "Unknown") for p in patients]
    patient_labels = [p.get("name") or p.get("id", "Unknown") for p in patients]
    label_by_patient_id = dict(zip(patient_ids, patient_labels))
    request_map = {r["id"]: r for r in profile.get("requests", []) if "id" in r}

    cell_map: Dict[Tuple[int, str], Dict[str, str]] = {}
    for assignment in result.get("assignments", {}).values():
        req = request_map.get(assignment.get("request_id", ""), {})
        discipline = req.get("discipline", "Other")
        label = assignment.get("label") or discipline
        for minute in range(int(assignment["start_minute"]), int(assignment["end_minute"]), GRID_SLOT_MINUTES):
            if minute < GRID_START_MINUTE or minute >= GRID_END_MINUTE:
                continue
            for patient_id in req.get("patient_ids", []):
                patient_label = label_by_patient_id.get(patient_id, patient_id)
                cell_map[(minute, patient_label)] = {"discipline": discipline, "label": label}

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
        self.root.geometry("1320x860")

        self.provider_catalog = load_provider_catalog(DEFAULT_PROVIDER_NAMES)
        self.last_result: Dict[str, Any] | None = None
        self.loaded_profile: Dict[str, Any] | None = load_last_profile()
        self.loaded_profile_path: Path | None = None

        self._apply_style()
        self._build_layout()

        if self.loaded_profile:
            self._sync_profile_resources(self.loaded_profile)
            self.status_var.set("Status: Restored last profile")
            self.profile_var.set("Profile: restored from data/last_profile.json")
            self._refresh_profile_preview()

    def _apply_style(self) -> None:
        style = self.ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background="#eef6ff")
        style.configure("TLabelframe", background="#eef6ff", borderwidth=2)
        style.configure("TLabelframe.Label", background="#eef6ff", foreground="#0a3d62", font=("Segoe UI", 10, "bold"))
        style.configure("TLabel", background="#eef6ff", foreground="#1b263b")
        style.configure("TButton", font=("Segoe UI", 9, "bold"), padding=6)

    def _build_layout(self) -> None:
        tk = self.tk
        ttk = self.ttk

        main = ttk.Frame(self.root, padding=10)
        main.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(main)
        header.pack(fill=tk.X)
        ttk.Label(header, text="Therapy Day Scheduler", font=("Segoe UI", 20, "bold"), foreground="#0b4f6c").pack(anchor="w")

        controls = ttk.Frame(main)
        controls.pack(fill=tk.X)
        buttons = [
            ("Health Check", self.run_health_check),
            ("New Blank Profile", self.new_blank_profile),
            ("Load Profile", self.load_profile_from_file),
            ("Generate Schedule", self.generate_from_loaded_profile),
            ("Export Schedule", self.export_schedule),
            ("Save Profile", self.save_current_profile),
        ]
        for idx, (label, handler) in enumerate(buttons):
            ttk.Button(controls, text=label, command=lambda h=handler: self._safe_action(h)).grid(row=0, column=idx, padx=3, pady=4, sticky="w")

        self.status_var = tk.StringVar(value="Status: Ready")
        ttk.Label(controls, textvariable=self.status_var).grid(row=1, column=0, columnspan=6, sticky="w", padx=6)
        self.profile_var = tk.StringVar(value="Profile: (none loaded)")
        ttk.Label(controls, textvariable=self.profile_var).grid(row=2, column=0, columnspan=6, sticky="w", padx=6)

        upper = ttk.Panedwindow(main, orient=tk.HORIZONTAL)
        upper.pack(fill=tk.BOTH, expand=True, pady=(8, 8))

        form_wrap = ttk.Labelframe(upper, text="Inputs", padding=8)
        upper.add(form_wrap, weight=2)
        self._build_form_panel(form_wrap)

        preview_wrap = ttk.Labelframe(upper, text="Profile Summary", padding=8)
        upper.add(preview_wrap, weight=1)
        self.summary_text = self.scrolledtext.ScrolledText(preview_wrap, height=22, wrap=tk.WORD, font=("Consolas", 10))
        self.summary_text.pack(fill=tk.BOTH, expand=True)
        self.summary_text.insert(tk.END, "Create or load a profile to begin.\n")
        self.summary_text.configure(state=tk.DISABLED)

        grid_wrap = ttk.Labelframe(main, text="Patient Schedule Grid (7:30 AM to 6:00 PM)", padding=8)
        grid_wrap.pack(fill=tk.BOTH, expand=True)
        self.grid_canvas = tk.Canvas(grid_wrap, bg="white", highlightthickness=0)
        self.grid_canvas.pack(fill=tk.BOTH, expand=True)

        self.legend_var = tk.StringVar(value="Legend: Add appointments to visualize schedule.")
        ttk.Label(main, textvariable=self.legend_var).pack(anchor="w", pady=(6, 0))

    def _build_form_panel(self, parent) -> None:
        ttk = self.ttk

        day_frame = ttk.Labelframe(parent, text="Day Settings", padding=6)
        day_frame.pack(fill="x", pady=(0, 6))
        self.date_var = self.tk.StringVar(value="2026-02-11")
        self.weekday_var = self.tk.StringVar(value="2")
        self.day_start_var = self.tk.StringVar(value="0730")
        self.day_end_var = self.tk.StringVar(value="1800")
        ttk.Label(day_frame, text="Date").grid(row=0, column=0, sticky="w")
        ttk.Entry(day_frame, textvariable=self.date_var, width=12).grid(row=0, column=1, padx=4)
        ttk.Label(day_frame, text="Weekday (Mon=0)").grid(row=0, column=2, sticky="w")
        ttk.Entry(day_frame, textvariable=self.weekday_var, width=6).grid(row=0, column=3, padx=4)
        ttk.Label(day_frame, text="Start (HHMM)").grid(row=1, column=0, sticky="w")
        ttk.Entry(day_frame, textvariable=self.day_start_var, width=8).grid(row=1, column=1, padx=4)
        ttk.Label(day_frame, text="End (HHMM)").grid(row=1, column=2, sticky="w")
        ttk.Entry(day_frame, textvariable=self.day_end_var, width=8).grid(row=1, column=3, padx=4)
        ttk.Button(day_frame, text="Apply Day Settings", command=lambda: self._safe_action(self.apply_day_settings)).grid(row=2, column=0, columnspan=4, pady=(6, 0), sticky="w")

        patient_frame = ttk.Labelframe(parent, text="Add Patient", padding=6)
        patient_frame.pack(fill="x", pady=(0, 6))
        self.patient_id_var = self.tk.StringVar()
        self.patient_name_var = self.tk.StringVar()
        self.patient_start_var = self.tk.StringVar(value="0730")
        self.patient_end_var = self.tk.StringVar(value="1800")
        self._labeled_entry(patient_frame, 0, "ID", self.patient_id_var)
        self._labeled_entry(patient_frame, 1, "Name", self.patient_name_var)
        self._labeled_entry(patient_frame, 2, "Avail Start (HHMM)", self.patient_start_var)
        self._labeled_entry(patient_frame, 3, "Avail End (HHMM)", self.patient_end_var)
        ttk.Button(patient_frame, text="Add Patient", command=lambda: self._safe_action(self.add_patient)).grid(row=0, column=4, rowspan=2, padx=4)

        provider_frame = ttk.Labelframe(parent, text="Provider List Management", padding=6)
        provider_frame.pack(fill="x", pady=(0, 6))
        self.provider_selected_var = self.tk.StringVar(value=self.provider_catalog[0] if self.provider_catalog else "")
        self.provider_new_var = self.tk.StringVar()
        ttk.Label(provider_frame, text="Provider Name").grid(row=0, column=0, sticky="w")
        self.provider_manage_combo = self.ttk.Combobox(provider_frame, textvariable=self.provider_selected_var, values=self.provider_catalog, state="readonly", width=30)
        self.provider_manage_combo.grid(row=1, column=0, padx=2)
        ttk.Label(provider_frame, text="New Provider Name").grid(row=0, column=1, sticky="w")
        ttk.Entry(provider_frame, textvariable=self.provider_new_var, width=28).grid(row=1, column=1, padx=2)
        ttk.Button(provider_frame, text="Add New Provider", command=lambda: self._safe_action(self.add_new_provider)).grid(row=1, column=2, padx=4)
        ttk.Button(provider_frame, text="Remove Provider", command=lambda: self._safe_action(self.remove_provider)).grid(row=1, column=3, padx=4)

        appt_frame = ttk.Labelframe(parent, text="Add Appointment", padding=6)
        appt_frame.pack(fill="x")
        times = military_time_choices()
        self.appt_patient_var = self.tk.StringVar(value="")
        self.appt_provider_var = self.tk.StringVar(value=self.provider_catalog[0] if self.provider_catalog else "")
        self.appt_room_var = self.tk.StringVar(value=PREDEFINED_ROOMS[0])
        self.appt_mode_var = self.tk.StringVar(value="individual")
        self.appt_discipline_var = self.tk.StringVar(value=DISCIPLINES[1])
        self.appt_start_var = self.tk.StringVar(value="0730")
        self.appt_end_var = self.tk.StringVar(value="0800")

        self.ttk.Label(appt_frame, text="Patient ID").grid(row=0, column=0, sticky="w")
        self.appt_patient_combo = self.ttk.Combobox(appt_frame, textvariable=self.appt_patient_var, values=[], state="readonly", width=16)
        self.appt_patient_combo.grid(row=1, column=0, padx=2)

        self.ttk.Label(appt_frame, text="Provider Name").grid(row=0, column=1, sticky="w")
        self.appt_provider_combo = self.ttk.Combobox(appt_frame, textvariable=self.appt_provider_var, values=self.provider_catalog, state="readonly", width=20)
        self.appt_provider_combo.grid(row=1, column=1, padx=2)

        self.ttk.Label(appt_frame, text="Room").grid(row=0, column=2, sticky="w")
        self.appt_room_combo = self.ttk.Combobox(appt_frame, textvariable=self.appt_room_var, values=PREDEFINED_ROOMS, state="readonly", width=16)
        self.appt_room_combo.grid(row=1, column=2, padx=2)

        self.ttk.Label(appt_frame, text="Appointment Type").grid(row=0, column=3, sticky="w")
        self.ttk.Combobox(appt_frame, textvariable=self.appt_mode_var, values=["individual", "group"], state="readonly", width=12).grid(row=1, column=3, padx=2)

        self.ttk.Label(appt_frame, text="Discipline").grid(row=0, column=4, sticky="w")
        self.ttk.Combobox(appt_frame, textvariable=self.appt_discipline_var, values=DISCIPLINES, state="readonly", width=22).grid(row=1, column=4, padx=2)

        self.ttk.Label(appt_frame, text="Begin (HHMM)").grid(row=0, column=5, sticky="w")
        self.ttk.Combobox(appt_frame, textvariable=self.appt_start_var, values=times, state="readonly", width=10).grid(row=1, column=5, padx=2)

        self.ttk.Label(appt_frame, text="End (HHMM)").grid(row=0, column=6, sticky="w")
        self.ttk.Combobox(appt_frame, textvariable=self.appt_end_var, values=times, state="readonly", width=10).grid(row=1, column=6, padx=2)

        ttk.Button(appt_frame, text="Add Appointment", command=lambda: self._safe_action(self.add_appointment)).grid(row=0, column=7, rowspan=2, padx=4)

    def _labeled_entry(self, frame, column: int, label: str, var) -> None:
        self.ttk.Label(frame, text=label).grid(row=0, column=column, sticky="w")
        self.ttk.Entry(frame, textvariable=var, width=14).grid(row=1, column=column, padx=2)

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
        profile["rooms"] = build_room_records()
        profile["providers"] = build_provider_records(
            self.provider_catalog,
            int(profile.get("weekday", 2)),
            int(day_window.get("start_minute", GRID_START_MINUTE)),
            int(day_window.get("end_minute", GRID_END_MINUTE)),
        )

    def _autosave_profile(self) -> None:
        if self.loaded_profile:
            save_last_profile(self.loaded_profile, str(self.loaded_profile_path) if self.loaded_profile_path else None)

    def _refresh_appointment_dropdowns(self) -> None:
        if not self.loaded_profile:
            return
        patients = [p.get("id", "") for p in self.loaded_profile.get("patients", []) if p.get("id")]
        self.appt_patient_combo["values"] = patients
        self.appt_provider_combo["values"] = self.provider_catalog
        self.appt_room_combo["values"] = PREDEFINED_ROOMS
        self.provider_manage_combo["values"] = self.provider_catalog

        if patients and self.appt_patient_var.get() not in patients:
            self.appt_patient_var.set(patients[0])
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
            f"Date: {profile.get('date_key')}",
            f"Weekday: {profile.get('weekday')}",
            f"Providers in catalog: {len(self.provider_catalog)}",
            f"Patients: {len(profile.get('patients', []))}",
            f"Rooms: {len(PREDEFINED_ROOMS)} (fixed)",
            f"Appointments: {len(profile.get('requests', []))}",
        ]
        self._set_text(self.summary_text, "\n".join(lines))
        self._refresh_appointment_dropdowns()
        self._autosave_profile()

    def _render_patient_grid(self, profile: Dict[str, Any], result: Dict[str, Any]) -> None:
        canvas = self.grid_canvas
        canvas.delete("all")

        minutes, patient_labels, cell_map = build_patient_grid_data(profile, result)
        if not patient_labels:
            canvas.create_text(16, 20, anchor="w", text="Add at least one patient to render grid.", fill="#003049", font=("Segoe UI", 11, "bold"))
            return

        time_col_w, header_h, row_h, patient_col_w = 85, 34, 22, 130
        canvas.config(scrollregion=(0, 0, time_col_w + len(patient_labels) * patient_col_w, header_h + len(minutes) * row_h))
        canvas.create_rectangle(0, 0, time_col_w, header_h, fill="#0b4f6c", outline="#0b4f6c")
        canvas.create_text(time_col_w // 2, header_h // 2, text="Time", fill="white", font=("Segoe UI", 10, "bold"))

        for idx, label in enumerate(patient_labels):
            x0 = time_col_w + idx * patient_col_w
            x1 = x0 + patient_col_w
            canvas.create_rectangle(x0, 0, x1, header_h, fill="#1d3557", outline="#f1faee")
            canvas.create_text((x0 + x1) // 2, header_h // 2, text=label, fill="white", font=("Segoe UI", 9, "bold"))

        used_disciplines = set()
        for row_idx, minute in enumerate(minutes):
            y0 = header_h + row_idx * row_h
            y1 = y0 + row_h
            canvas.create_rectangle(0, y0, time_col_w, y1, fill="#f8f9fa" if row_idx % 2 == 0 else "#e9ecef", outline="#adb5bd")
            canvas.create_text(time_col_w // 2, (y0 + y1) // 2, text=_to_ampm(minute), fill="#1b263b", font=("Segoe UI", 8))

            for col_idx, patient_label in enumerate(patient_labels):
                x0 = time_col_w + col_idx * patient_col_w
                x1 = x0 + patient_col_w
                cell = cell_map.get((minute, patient_label))
                if cell:
                    discipline = cell["discipline"]
                    used_disciplines.add(discipline)
                    fill = DISCIPLINE_COLORS.get(discipline, "#ffb3c1")
                    text = discipline
                else:
                    fill = "#ffffff"
                    text = ""
                canvas.create_rectangle(x0, y0, x1, y1, fill=fill, outline="#dee2e6")
                if text:
                    canvas.create_text((x0 + x1) // 2, (y0 + y1) // 2, text=text, fill="#1b263b", font=("Segoe UI", 7))

        self.legend_var.set("Legend: " + " | ".join(sorted(used_disciplines)) if used_disciplines else "Legend: No assigned sessions in 7:30 AM - 6:00 PM window")

    def run_health_check(self) -> None:
        result = run_health_check()
        self.status_var.set(f"Status: Health Check {'PASS' if result['all_ok'] else 'FAIL'}")
        self._set_text(self.summary_text, json.dumps(result, indent=2))

    def new_blank_profile(self) -> None:
        self.loaded_profile = {
            "date_key": "2026-02-11",
            "weekday": 2,
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
        profile = load_profile(path)
        self.loaded_profile = profile
        self.loaded_profile_path = path
        self._sync_profile_resources(self.loaded_profile)
        self.profile_var.set(f"Profile: {path}")
        self.status_var.set("Status: Profile loaded")
        self._refresh_profile_preview()

    def apply_day_settings(self) -> None:
        profile = self._require_profile()
        profile["date_key"] = self.date_var.get().strip()
        profile["weekday"] = int(self.weekday_var.get())
        profile["day_window"] = {
            "start_minute": parse_time_input(self.day_start_var.get()),
            "end_minute": parse_time_input(self.day_end_var.get()),
        }
        self._sync_profile_resources(profile)
        validate_profile(profile)
        self.status_var.set("Status: Day settings applied")
        self._refresh_profile_preview()

    def add_patient(self) -> None:
        profile = self._require_profile()
        patient_id = self.patient_id_var.get().strip()
        if not patient_id:
            raise ValueError("Patient ID is required")
        date_key = profile["date_key"]
        patient = {
            "id": patient_id,
            "name": self.patient_name_var.get().strip() or patient_id,
            "availability": {
                date_key: [{"start_minute": parse_time_input(self.patient_start_var.get()), "end_minute": parse_time_input(self.patient_end_var.get())}]
            },
        }
        profile["patients"].append(patient)
        self.status_var.set(f"Status: Added patient {patient_id}")
        self._refresh_profile_preview()

    def add_new_provider(self) -> None:
        name = self.provider_new_var.get().strip()
        if not name:
            raise ValueError("Enter a provider name to add")
        if name in self.provider_catalog:
            raise ValueError("Provider already exists")
        self.provider_catalog.append(name)
        self.provider_catalog = sorted(self.provider_catalog)
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
            raise ValueError("Selected provider was not found")
        self.provider_catalog = [p for p in self.provider_catalog if p != name]
        if not self.provider_catalog:
            raise ValueError("Cannot remove last provider")
        save_provider_catalog(self.provider_catalog)
        if self.loaded_profile:
            self._sync_profile_resources(self.loaded_profile)
        self.status_var.set(f"Status: Removed provider {name}")
        self._refresh_profile_preview()

    def add_appointment(self) -> None:
        profile = self._require_profile()
        patient_id = self.appt_patient_var.get().strip()
        provider_id = self.appt_provider_var.get().strip()
        room_id = self.appt_room_var.get().strip()
        mode = self.appt_mode_var.get().strip().lower()
        discipline = self.appt_discipline_var.get().strip()

        if not patient_id or not provider_id or not room_id:
            raise ValueError("Patient, Provider Name, and Room are required")

        start_minute = parse_time_input(self.appt_start_var.get())
        end_minute = parse_time_input(self.appt_end_var.get())
        if end_minute <= start_minute:
            raise ValueError("Appointment end time must be after begin time")

        for existing in profile.get("requests", []):
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
        profile["requests"].append(
            {
                "id": req_id,
                "patient_ids": [patient_id],
                "discipline": discipline,
                "duration_minutes": end_minute - start_minute,
                "mode": mode,
                "date_key": profile["date_key"],
                "preferred_window": {"start_minute": start_minute, "end_minute": end_minute},
                "group_key": None,
                "label": f"{discipline} ({patient_id})",
                "provider_id": provider_id,
                "room_id": room_id,
            }
        )

        live_result = build_live_result_from_profile(profile)
        self.last_result = live_result
        self.status_var.set(f"Status: Added appointment {req_id}")
        self._render_patient_grid(profile, live_result)
        self._refresh_profile_preview()

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
        result = handle_generate(profile)
        self.last_result = result
        save_last_schedule(result)
        summary = summarize_schedule(result)
        self.status_var.set(f"Status: Generated {summary.assignment_count} sessions across {summary.room_count} rooms")
        self._render_patient_grid(profile, result)

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
