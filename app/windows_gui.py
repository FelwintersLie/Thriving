from __future__ import annotations

import json
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

from app.api_server import handle_generate
from app.health_check import run_health_check
from app.persistence import load_last_profile, save_last_profile, save_last_schedule
from app.profile_io import build_sample_profile, load_profile, save_profile, validate_profile
from app.windows_program import save_json


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


def _parse_csv(raw: str) -> List[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


class SchedulerDesktopApp:
    def __init__(self) -> None:
        tk, ttk, messagebox, scrolledtext, filedialog = _safe_tk_import()
        self.tk = tk
        self.ttk = ttk
        self.messagebox = messagebox
        self.scrolledtext = scrolledtext
        self.filedialog = filedialog

        self.root = tk.Tk()
        self.root.title("Therapy Scheduler - Proof of Concept")
        self.root.geometry("1180x820")

        self.last_result: Dict[str, Any] | None = None
        self.loaded_profile: Dict[str, Any] | None = load_last_profile()
        self.loaded_profile_path: Path | None = None

        self._build_layout()

        if self.loaded_profile:
            self.status_var.set("Status: Restored last profile from persistent storage")
            self.profile_var.set("Profile: restored from data/last_profile.json")
            self._refresh_profile_preview()

    def _build_layout(self) -> None:
        tk = self.tk
        ttk = self.ttk

        header = ttk.Frame(self.root, padding=10)
        header.pack(fill=tk.X)

        ttk.Label(
            header,
            text="Therapy Scheduler (Windows Proof of Concept)",
            font=("Segoe UI", 15, "bold"),
        ).pack(anchor="w")

        ttk.Label(
            header,
            text=(
                "Beginner flow: Health Check → Create/Load Profile → Add Data With Forms → Generate → Export"
            ),
        ).pack(anchor="w", pady=(4, 0))

        controls = ttk.Frame(self.root, padding=10)
        controls.pack(fill=tk.X)

        buttons = [
            ("1) Run Health Check", self.run_health_check),
            ("2) New Blank Profile", self.new_blank_profile),
            ("3) Save Sample Profile", self.save_sample_profile),
            ("4) Load Profile JSON", self.load_profile_from_file),
            ("5) Generate", self.generate_from_loaded_profile),
            ("6) Export", self.export_schedule),
            ("Save Profile", self.save_current_profile),
        ]

        for idx, (label, handler) in enumerate(buttons):
            ttk.Button(controls, text=label, command=lambda h=handler: self._safe_action(h)).grid(
                row=0, column=idx, padx=3, pady=4, sticky="w"
            )

        self.status_var = tk.StringVar(value="Status: Ready")
        ttk.Label(controls, textvariable=self.status_var).grid(row=1, column=0, columnspan=7, sticky="w", padx=6)

        self.profile_var = tk.StringVar(value="Profile: (none loaded)")
        ttk.Label(controls, textvariable=self.profile_var).grid(row=2, column=0, columnspan=7, sticky="w", padx=6)

        middle = ttk.Panedwindow(self.root, orient=tk.HORIZONTAL)
        middle.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        form_wrap = ttk.Labelframe(middle, text="Form-based Data Entry", padding=8)
        middle.add(form_wrap, weight=1)

        self._build_form_panel(form_wrap)

        preview_wrap = ttk.Labelframe(middle, text="Profile Preview", padding=8)
        middle.add(preview_wrap, weight=1)
        self.profile_text = self.scrolledtext.ScrolledText(preview_wrap, height=28, wrap=tk.WORD)
        self.profile_text.pack(fill=tk.BOTH, expand=True)
        self.profile_text.insert(tk.END, "Create a blank profile or load/save one to begin.\n")
        self.profile_text.configure(state=tk.DISABLED)

        bottom = ttk.Panedwindow(self.root, orient=tk.VERTICAL)
        bottom.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        summary_frame = ttk.Labelframe(bottom, text="Schedule Snapshot", padding=8)
        bottom.add(summary_frame, weight=1)

        self.summary_text = self.scrolledtext.ScrolledText(summary_frame, height=8, wrap=tk.WORD)
        self.summary_text.pack(fill=tk.BOTH, expand=True)
        self.summary_text.insert(tk.END, "Generate a schedule to see assignments.\n")
        self.summary_text.configure(state=tk.DISABLED)

        timeline_frame = ttk.Labelframe(bottom, text="Room Timeline Preview (15-minute labels)", padding=8)
        bottom.add(timeline_frame, weight=2)

        self.timeline_text = self.scrolledtext.ScrolledText(timeline_frame, height=12, wrap=tk.WORD)
        self.timeline_text.pack(fill=tk.BOTH, expand=True)
        self.timeline_text.insert(tk.END, "Generate a schedule to view room timelines.\n")
        self.timeline_text.configure(state=tk.DISABLED)

    def _build_form_panel(self, parent) -> None:
        ttk = self.ttk

        day_frame = ttk.Labelframe(parent, text="Day Settings", padding=6)
        day_frame.pack(fill="x", pady=(0, 6))
        self.date_var = self.tk.StringVar(value="2026-02-11")
        self.weekday_var = self.tk.StringVar(value="2")
        self.day_start_var = self.tk.StringVar(value="480")
        self.day_end_var = self.tk.StringVar(value="960")

        ttk.Label(day_frame, text="Date (YYYY-MM-DD)").grid(row=0, column=0, sticky="w")
        ttk.Entry(day_frame, textvariable=self.date_var, width=14).grid(row=0, column=1, padx=4)
        ttk.Label(day_frame, text="Weekday (Mon=0)").grid(row=0, column=2, sticky="w")
        ttk.Entry(day_frame, textvariable=self.weekday_var, width=6).grid(row=0, column=3, padx=4)
        ttk.Label(day_frame, text="Day Start Minute").grid(row=1, column=0, sticky="w")
        ttk.Entry(day_frame, textvariable=self.day_start_var, width=8).grid(row=1, column=1, padx=4)
        ttk.Label(day_frame, text="Day End Minute").grid(row=1, column=2, sticky="w")
        ttk.Entry(day_frame, textvariable=self.day_end_var, width=8).grid(row=1, column=3, padx=4)
        ttk.Button(day_frame, text="Apply Day Settings", command=lambda: self._safe_action(self.apply_day_settings)).grid(
            row=2, column=0, columnspan=4, pady=(6, 0), sticky="w"
        )

        provider_frame = ttk.Labelframe(parent, text="Add Provider", padding=6)
        provider_frame.pack(fill="x", pady=(0, 6))
        self.provider_id_var = self.tk.StringVar()
        self.provider_name_var = self.tk.StringVar()
        self.provider_disciplines_var = self.tk.StringVar(value="physical_therapy")
        self.provider_start_var = self.tk.StringVar(value="480")
        self.provider_end_var = self.tk.StringVar(value="960")

        self._labeled_entry(provider_frame, 0, "ID", self.provider_id_var)
        self._labeled_entry(provider_frame, 1, "Name", self.provider_name_var)
        self._labeled_entry(provider_frame, 2, "Disciplines (csv)", self.provider_disciplines_var)
        self._labeled_entry(provider_frame, 3, "Avail Start", self.provider_start_var)
        self._labeled_entry(provider_frame, 4, "Avail End", self.provider_end_var)
        ttk.Button(provider_frame, text="Add Provider", command=lambda: self._safe_action(self.add_provider)).grid(row=0, column=5, rowspan=2, padx=4)

        patient_frame = ttk.Labelframe(parent, text="Add Patient", padding=6)
        patient_frame.pack(fill="x", pady=(0, 6))
        self.patient_id_var = self.tk.StringVar()
        self.patient_name_var = self.tk.StringVar()
        self.patient_start_var = self.tk.StringVar(value="480")
        self.patient_end_var = self.tk.StringVar(value="960")

        self._labeled_entry(patient_frame, 0, "ID", self.patient_id_var)
        self._labeled_entry(patient_frame, 1, "Name", self.patient_name_var)
        self._labeled_entry(patient_frame, 2, "Avail Start", self.patient_start_var)
        self._labeled_entry(patient_frame, 3, "Avail End", self.patient_end_var)
        ttk.Button(patient_frame, text="Add Patient", command=lambda: self._safe_action(self.add_patient)).grid(row=0, column=4, rowspan=2, padx=4)

        room_frame = ttk.Labelframe(parent, text="Add Room", padding=6)
        room_frame.pack(fill="x", pady=(0, 6))
        self.room_id_var = self.tk.StringVar()
        self.room_name_var = self.tk.StringVar()
        self.room_capacity_var = self.tk.StringVar(value="1")
        self.room_disciplines_var = self.tk.StringVar(value="physical_therapy")

        self._labeled_entry(room_frame, 0, "ID", self.room_id_var)
        self._labeled_entry(room_frame, 1, "Name", self.room_name_var)
        self._labeled_entry(room_frame, 2, "Capacity", self.room_capacity_var)
        self._labeled_entry(room_frame, 3, "Allowed disciplines (csv)", self.room_disciplines_var)
        ttk.Button(room_frame, text="Add Room", command=lambda: self._safe_action(self.add_room)).grid(row=0, column=4, rowspan=2, padx=4)

        req_frame = ttk.Labelframe(parent, text="Add Session Request", padding=6)
        req_frame.pack(fill="x")
        self.req_id_var = self.tk.StringVar()
        self.req_patient_ids_var = self.tk.StringVar()
        self.req_discipline_var = self.tk.StringVar(value="physical_therapy")
        self.req_duration_var = self.tk.StringVar(value="30")
        self.req_mode_var = self.tk.StringVar(value="individual")
        self.req_pref_start_var = self.tk.StringVar(value="480")
        self.req_pref_end_var = self.tk.StringVar(value="960")
        self.req_label_var = self.tk.StringVar()

        self._labeled_entry(req_frame, 0, "ID", self.req_id_var)
        self._labeled_entry(req_frame, 1, "Patient IDs (csv)", self.req_patient_ids_var)
        self._labeled_entry(req_frame, 2, "Discipline", self.req_discipline_var)
        self._labeled_entry(req_frame, 3, "Duration", self.req_duration_var)
        self._labeled_entry(req_frame, 4, "Mode", self.req_mode_var)
        self._labeled_entry(req_frame, 5, "Preferred Start", self.req_pref_start_var)
        self._labeled_entry(req_frame, 6, "Preferred End", self.req_pref_end_var)
        self._labeled_entry(req_frame, 7, "Label", self.req_label_var)
        ttk.Button(req_frame, text="Add Request", command=lambda: self._safe_action(self.add_request)).grid(row=0, column=8, rowspan=2, padx=4)

    def _labeled_entry(self, frame, column: int, label: str, var) -> None:
        self.ttk.Label(frame, text=label).grid(row=0, column=column, sticky="w")
        self.ttk.Entry(frame, textvariable=var, width=14).grid(row=1, column=column, padx=2)

    def _safe_action(self, fn) -> None:
        try:
            fn()
        except Exception as exc:
            self.status_var.set("Status: Error (see popup)")
            self.messagebox.showerror(
                "Something went wrong",
                f"{exc}\n\nTechnical details:\n{traceback.format_exc(limit=3)}",
            )

    def _set_text(self, widget, text: str) -> None:
        widget.configure(state=self.tk.NORMAL)
        widget.delete("1.0", self.tk.END)
        widget.insert(self.tk.END, text)
        widget.configure(state=self.tk.DISABLED)

    def _require_profile(self) -> Dict[str, Any]:
        if not self.loaded_profile:
            raise ValueError("No profile loaded. Click 'New Blank Profile' or load/save sample profile first.")
        return self.loaded_profile

    def _autosave_profile(self) -> None:
        if self.loaded_profile:
            save_last_profile(self.loaded_profile, str(self.loaded_profile_path) if self.loaded_profile_path else None)

    def _refresh_profile_preview(self) -> None:
        if not self.loaded_profile:
            return
        pretty = json.dumps(self.loaded_profile, indent=2)
        self._set_text(self.profile_text, pretty[:12000])
        self._autosave_profile()

    def run_health_check(self) -> None:
        result = run_health_check()
        self.status_var.set(f"Status: Health Check {'PASS' if result['all_ok'] else 'FAIL'}")
        self._set_text(self.summary_text, json.dumps(result, indent=2))
        if not result["all_ok"]:
            self.messagebox.showwarning("Health Check", "Health check failed. Please verify Python installation and rerun.")

    def new_blank_profile(self) -> None:
        self.loaded_profile = {
            "date_key": "2026-02-11",
            "weekday": 2,
            "day_window": {"start_minute": 8 * 60, "end_minute": 16 * 60},
            "providers": [],
            "patients": [],
            "rooms": [],
            "requests": [],
        }
        self.loaded_profile_path = None
        self.profile_var.set("Profile: unsaved in-memory profile")
        self.status_var.set("Status: Created blank profile")
        self._refresh_profile_preview()

    def save_sample_profile(self) -> None:
        profile = build_sample_profile()
        default = Path("profiles") / "sample_profile.json"
        path_raw = self.filedialog.asksaveasfilename(
            title="Save sample profile",
            defaultextension=".json",
            initialfile="sample_profile.json",
            initialdir=str(default.parent),
            filetypes=[("JSON files", "*.json")],
        )
        if not path_raw:
            return
        path = Path(path_raw)
        save_profile(path, profile)
        self.loaded_profile = profile
        self.loaded_profile_path = path
        self.profile_var.set(f"Profile: {path}")
        self.status_var.set(f"Status: Saved sample profile to {path}")
        self._refresh_profile_preview()
        self.messagebox.showinfo("Profile saved", f"Saved sample profile to:\n{path}")

    def load_profile_from_file(self) -> None:
        path_raw = self.filedialog.askopenfilename(
            title="Select profile JSON",
            filetypes=[("JSON files", "*.json")],
        )
        if not path_raw:
            return
        path = Path(path_raw)
        profile = load_profile(path)
        self.loaded_profile = profile
        self.loaded_profile_path = path
        self.profile_var.set(f"Profile: {path}")
        self.status_var.set("Status: Profile loaded")
        self._refresh_profile_preview()

    def apply_day_settings(self) -> None:
        profile = self._require_profile()
        profile["date_key"] = self.date_var.get().strip()
        profile["weekday"] = int(self.weekday_var.get())
        profile["day_window"] = {
            "start_minute": int(self.day_start_var.get()),
            "end_minute": int(self.day_end_var.get()),
        }
        validate_profile(profile)
        self.status_var.set("Status: Day settings applied")
        self._refresh_profile_preview()

    def add_provider(self) -> None:
        profile = self._require_profile()
        provider_id = self.provider_id_var.get().strip()
        if not provider_id:
            raise ValueError("Provider ID is required")
        provider = {
            "id": provider_id,
            "name": self.provider_name_var.get().strip() or provider_id,
            "disciplines": _parse_csv(self.provider_disciplines_var.get()),
            "templates": [
                {
                    "weekday": int(profile["weekday"]),
                    "windows": [
                        {
                            "start_minute": int(self.provider_start_var.get()),
                            "end_minute": int(self.provider_end_var.get()),
                        }
                    ],
                }
            ],
            "exceptions": [],
        }
        profile["providers"].append(provider)
        self.status_var.set(f"Status: Added provider {provider_id}")
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
                date_key: [
                    {
                        "start_minute": int(self.patient_start_var.get()),
                        "end_minute": int(self.patient_end_var.get()),
                    }
                ]
            },
        }
        profile["patients"].append(patient)
        self.status_var.set(f"Status: Added patient {patient_id}")
        self._refresh_profile_preview()

    def add_room(self) -> None:
        profile = self._require_profile()
        room_id = self.room_id_var.get().strip()
        if not room_id:
            raise ValueError("Room ID is required")
        room = {
            "id": room_id,
            "name": self.room_name_var.get().strip() or room_id,
            "capacity": int(self.room_capacity_var.get()),
            "allowed_disciplines": _parse_csv(self.room_disciplines_var.get()),
        }
        profile["rooms"].append(room)
        self.status_var.set(f"Status: Added room {room_id}")
        self._refresh_profile_preview()

    def add_request(self) -> None:
        profile = self._require_profile()
        req_id = self.req_id_var.get().strip()
        if not req_id:
            raise ValueError("Request ID is required")
        request = {
            "id": req_id,
            "patient_ids": _parse_csv(self.req_patient_ids_var.get()),
            "discipline": self.req_discipline_var.get().strip(),
            "duration_minutes": int(self.req_duration_var.get()),
            "mode": self.req_mode_var.get().strip().lower(),
            "date_key": profile["date_key"],
            "preferred_window": {
                "start_minute": int(self.req_pref_start_var.get()),
                "end_minute": int(self.req_pref_end_var.get()),
            },
            "group_key": None,
            "label": self.req_label_var.get().strip() or req_id,
        }
        profile["requests"].append(request)
        self.status_var.set(f"Status: Added request {req_id}")
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
        self.status_var.set(f"Status: Profile saved to {path}")
        self._autosave_profile()

    def generate_from_loaded_profile(self) -> None:
        profile = self._require_profile()
        result = handle_generate(profile)
        self.last_result = result
        save_last_schedule(result)
        summary = summarize_schedule(result)

        assignments = result.get("assignments", {})
        lines = [
            f"Assignments scheduled: {summary.assignment_count}",
            f"Rooms in timeline: {summary.room_count}",
            f"Booked 15-min slots: {summary.booked_slot_count}",
            "",
            "Assignments:",
        ]
        for req_id in sorted(assignments.keys()):
            a = assignments[req_id]
            lines.append(
                f"- {req_id}: {a['label']} | provider={a['provider_id']} | room={a['room_id']} "
                f"{a['start_minute']}-{a['end_minute']}"
            )

        timeline_lines: List[str] = []
        for room_id in sorted(result.get("room_timeline", {}).keys()):
            timeline_lines.append(f"[{room_id}]")
            room_slots = result["room_timeline"][room_id]
            booked_slots = [s for s in room_slots if s["status"] == "booked"]
            if not booked_slots:
                timeline_lines.append("  - no booked slots")
            else:
                for s in booked_slots[:20]:
                    timeline_lines.append(f"  - {s['start_minute']}-{s['end_minute']}: {s['label']} ({s['mode']})")
            timeline_lines.append("")

        self._set_text(self.summary_text, "\n".join(lines))
        self._set_text(self.timeline_text, "\n".join(timeline_lines))
        self.status_var.set("Status: Schedule generated")

    def export_schedule(self) -> None:
        if not self.last_result:
            self.messagebox.showinfo("Export", "Generate a schedule first.")
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
        self.messagebox.showinfo("Export Complete", f"Saved: {out_path}")

    def run(self) -> None:
        self.root.mainloop()


def launch_gui() -> int:
    app = SchedulerDesktopApp()
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(launch_gui())
