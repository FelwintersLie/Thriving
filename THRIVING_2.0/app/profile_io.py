from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from app.sample_day import build_sample_inputs


REQUIRED_PROFILE_KEYS = {
    "date_key",
    "weekday",
    "day_window",
    "providers",
    "patients",
    "rooms",
    "requests",
}


def provider_to_dict(provider) -> Dict[str, Any]:
    return {
        "id": provider.id,
        "name": provider.name,
        "disciplines": sorted(provider.disciplines),
        "templates": [
            {
                "weekday": template.weekday,
                "windows": [
                    {
                        "start_minute": window.start_minute,
                        "end_minute": window.end_minute,
                    }
                    for window in template.windows
                ],
            }
            for template in provider.templates
        ],
        "exceptions": [
            {
                "date_key": ex.date_key,
                "window": {
                    "start_minute": ex.window.start_minute,
                    "end_minute": ex.window.end_minute,
                },
                "available_override": ex.available_override,
            }
            for ex in provider.exceptions
        ],
    }


def patient_to_dict(patient) -> Dict[str, Any]:
    return {
        "id": patient.id,
        "name": patient.name,
        "availability": {
            date_key: [
                {
                    "start_minute": window.start_minute,
                    "end_minute": window.end_minute,
                }
                for window in windows
            ]
            for date_key, windows in patient.availability.items()
        },
    }


def room_to_dict(room) -> Dict[str, Any]:
    return {
        "id": room.id,
        "name": room.name,
        "capacity": room.capacity,
        "allowed_disciplines": sorted(room.allowed_disciplines),
    }


def request_to_dict(request) -> Dict[str, Any]:
    return {
        "id": request.id,
        "patient_ids": list(request.patient_ids),
        "discipline": request.discipline,
        "duration_minutes": request.duration_minutes,
        "mode": request.mode.value,
        "date_key": request.date_key,
        "preferred_window": (
            {
                "start_minute": request.preferred_window.start_minute,
                "end_minute": request.preferred_window.end_minute,
            }
            if request.preferred_window
            else None
        ),
        "group_key": request.group_key,
        "label": request.label,
    }


def build_sample_profile(date_key: str = "2026-02-11", weekday: int = 2) -> Dict[str, Any]:
    providers, patients, rooms, requests = build_sample_inputs(date_key, weekday)
    return {
        "date_key": date_key,
        "weekday": weekday,
        "day_window": {"start_minute": 8 * 60, "end_minute": 16 * 60},
        "providers": [provider_to_dict(p) for p in providers],
        "patients": [patient_to_dict(p) for p in patients],
        "rooms": [room_to_dict(r) for r in rooms],
        "requests": [request_to_dict(r) for r in requests],
    }


def validate_profile(profile: Dict[str, Any]) -> None:
    missing = REQUIRED_PROFILE_KEYS.difference(profile.keys())
    if missing:
        missing_str = ", ".join(sorted(missing))
        raise ValueError(f"Profile is missing required keys: {missing_str}")

    if not isinstance(profile["providers"], list):
        raise ValueError("Profile providers must be a list")
    if not isinstance(profile["patients"], list):
        raise ValueError("Profile patients must be a list")
    if not isinstance(profile["rooms"], list):
        raise ValueError("Profile rooms must be a list")
    if not isinstance(profile["requests"], list):
        raise ValueError("Profile requests must be a list")


def load_profile(path: Path) -> Dict[str, Any]:
    profile = json.loads(path.read_text(encoding="utf-8"))
    validate_profile(profile)
    return profile


def save_profile(path: Path, profile: Dict[str, Any]) -> Path:
    validate_profile(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(profile, indent=2), encoding="utf-8")
    return path
