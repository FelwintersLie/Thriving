from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from app.provider_catalog import normalize_provider_catalog
from app.room_rules import normalize_room_rules


STATE_PATH = Path("data") / "app_state.json"
LAST_PROFILE_PATH = Path("data") / "last_profile.json"
LAST_SCHEDULE_PATH = Path("data") / "last_schedule.json"
PROVIDER_CATALOG_PATH = Path("data") / "provider_catalog.json"
ROOM_RULES_PATH = Path("data") / "room_rules.json"
ROOM_DISCIPLINE_PROFILE_PATH = Path("data") / "room_discipline_profile.json"

REQUIREMENTS_CATALOG_PATH = Path("data") / "requirements_catalog.json"
LAST_GENERATED_SCHEDULE_PATH = Path("data") / "last_generated_schedule.json"
PROVIDER_PROFILES_PATH = Path("data") / "provider_profiles.json"
APP_SETTINGS_PATH = Path("data") / "app_settings.json"


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def load_state() -> Dict[str, Any]:
    return _read_json(STATE_PATH)


def save_state(state: Dict[str, Any]) -> Path:
    return _write_json(STATE_PATH, state)


def load_app_settings() -> Dict[str, Any]:
    payload = _read_json(APP_SETTINGS_PATH)
    return payload if isinstance(payload, dict) else {}


def save_app_settings(settings: Dict[str, Any]) -> Path:
    cleaned = dict(settings) if isinstance(settings, dict) else {}
    _write_json(APP_SETTINGS_PATH, cleaned)
    state = load_state()
    state["app_settings_file"] = str(APP_SETTINGS_PATH)
    save_state(state)
    return APP_SETTINGS_PATH


def save_last_profile(profile: Dict[str, Any], source_path: str | None = None) -> Path:
    payload = {"profile": profile, "source_path": source_path}
    _write_json(LAST_PROFILE_PATH, payload)
    state = load_state()
    state["last_profile_file"] = str(LAST_PROFILE_PATH)
    if source_path:
        state["last_profile_source"] = source_path
    save_state(state)
    return LAST_PROFILE_PATH


def load_last_profile() -> Dict[str, Any] | None:
    payload = _read_json(LAST_PROFILE_PATH)
    return payload.get("profile") if payload else None


def save_last_schedule(schedule: Dict[str, Any]) -> Path:
    _write_json(LAST_SCHEDULE_PATH, schedule)
    state = load_state()
    state["last_schedule_file"] = str(LAST_SCHEDULE_PATH)
    save_state(state)
    return LAST_SCHEDULE_PATH


def load_provider_catalog(defaults: List[str]) -> List[str]:
    entries = load_provider_catalog_entries(defaults)
    return [e["provider_name"] for e in entries]


def save_provider_catalog(providers: List[str]) -> Path:
    entries = [
        {
            "provider_id": p.strip(),
            "provider_name": p.strip(),
            "discipline": "",
            "availability_templates": [],
            "exceptions": [],
        }
        for p in providers
        if p.strip()
    ]
    return save_provider_catalog_entries(entries)




def load_provider_catalog_entries(defaults: List[str]) -> List[Dict[str, Any]]:
    payload = _read_json(PROVIDER_CATALOG_PATH)
    values = payload.get("providers") if payload else None
    entries: List[Dict[str, Any]] = []
    if isinstance(values, list):
        for item in values:
            if isinstance(item, dict):
                name = str(item.get("provider_name") or item.get("name") or item.get("provider_id") or "").strip()
                if not name:
                    continue
                entries.append(
                    {
                        "provider_id": str(item.get("provider_id") or name),
                        "provider_name": name,
                        "discipline": str(item.get("discipline", "")).strip(),
                        "disciplines": item.get("disciplines") or [],
                        "availability_templates": item.get("availability_templates") or item.get("templates") or [],
                        "exceptions": item.get("exceptions") or [],
                        "allowed_rooms": item.get("allowed_rooms") or ["Any compatible room"],
                        "enforce_lunch_break": bool(item.get("enforce_lunch_break", False)),
                        "lunch_earliest_start_minute": item.get("lunch_earliest_start_minute", 11 * 60 + 30),
                        "lunch_latest_start_minute": item.get("lunch_latest_start_minute", 13 * 60),
                    }
                )
            else:
                name = str(item).strip()
                if not name:
                    continue
                entries.append(
                    {
                        "provider_id": name,
                        "provider_name": name,
                        "discipline": "",
                        "disciplines": [],
                        "availability_templates": [],
                        "exceptions": [],
                        "allowed_rooms": ["Any compatible room"],
                        "enforce_lunch_break": False,
                        "lunch_earliest_start_minute": 11 * 60 + 30,
                        "lunch_latest_start_minute": 13 * 60,
                    }
                )

    normalized = normalize_provider_catalog(
        entries,
        defaults=defaults,
        all_rooms=[],
        disciplines=[],
    )
    if entries != normalized:
        save_provider_catalog_entries(normalized)
    return normalized


def save_provider_catalog_entries(entries: List[Dict[str, Any]]) -> Path:
    cleaned = normalize_provider_catalog(
        entries,
        defaults=[],
        all_rooms=[],
        disciplines=[],
    )
    _write_json(PROVIDER_CATALOG_PATH, {"providers": cleaned})
    state = load_state()
    state["provider_catalog_file"] = str(PROVIDER_CATALOG_PATH)
    save_state(state)
    return PROVIDER_CATALOG_PATH
def load_requirements_catalog() -> Dict[str, List[Dict[str, Any]]]:
    payload = _read_json(REQUIREMENTS_CATALOG_PATH)
    if not payload:
        return {"iop_requirements": [], "eval_requirements": []}
    if isinstance(payload.get("requirements"), list):
        return {"iop_requirements": payload.get("requirements", []), "eval_requirements": []}
    iop = payload.get("iop_requirements") if isinstance(payload.get("iop_requirements"), list) else []
    eval_reqs = payload.get("eval_requirements") if isinstance(payload.get("eval_requirements"), list) else []
    return {"iop_requirements": iop, "eval_requirements": eval_reqs}


def save_requirements_catalog(requirements: List[Dict[str, Any]], eval_requirements: List[Dict[str, Any]] | None = None) -> Path:
    payload = {"iop_requirements": requirements}
    if eval_requirements is not None:
        payload["eval_requirements"] = eval_requirements
    _write_json(REQUIREMENTS_CATALOG_PATH, payload)
    state = load_state()
    state["requirements_catalog_file"] = str(REQUIREMENTS_CATALOG_PATH)
    save_state(state)
    return REQUIREMENTS_CATALOG_PATH


def load_provider_profiles() -> Dict[str, Any]:
    payload = _read_json(PROVIDER_PROFILES_PATH)
    profiles = payload.get("providers") if payload else None
    if not isinstance(profiles, list):
        return {"providers": []}
    return {"providers": profiles}


def save_provider_profiles(providers: List[Dict[str, Any]]) -> Path:
    _write_json(PROVIDER_PROFILES_PATH, {"providers": providers})
    state = load_state()
    state["provider_profiles_file"] = str(PROVIDER_PROFILES_PATH)
    save_state(state)
    return PROVIDER_PROFILES_PATH


def load_last_generated_schedule() -> Dict[str, Any] | None:
    payload = _read_json(LAST_GENERATED_SCHEDULE_PATH)
    return payload if payload else None


def save_last_generated_schedule(schedule: Dict[str, Any]) -> Path:
    _write_json(LAST_GENERATED_SCHEDULE_PATH, schedule)
    state = load_state()
    state["last_generated_schedule_file"] = str(LAST_GENERATED_SCHEDULE_PATH)
    save_state(state)
    return LAST_GENERATED_SCHEDULE_PATH




def load_room_discipline_profile(valid_rooms: List[str]) -> Dict[str, Any] | None:
    payload = _read_json(ROOM_DISCIPLINE_PROFILE_PATH)
    rooms = payload.get("rooms") if isinstance(payload, dict) else None
    if not isinstance(rooms, dict):
        return None
    normalized = normalize_room_rules({"rooms": rooms}, valid_rooms=valid_rooms)
    return normalized


def save_room_discipline_profile(rules: Dict[str, Any], *, valid_rooms: List[str]) -> Path:
    normalized = normalize_room_rules(rules, valid_rooms=valid_rooms)
    room_payload: Dict[str, Any] = {"rooms": {}}
    for room_name in valid_rooms:
        room = normalized.get("rooms", {}).get(room_name, {})
        room_payload["rooms"][room_name] = {
            "allowed_disciplines": list(room.get("allowed_disciplines") or []),
            "room_preference_tier": int(room.get("room_preference_tier", 0) or 0),
        }
    _write_json(ROOM_DISCIPLINE_PROFILE_PATH, room_payload)
    state = load_state()
    state["room_discipline_profile_file"] = str(ROOM_DISCIPLINE_PROFILE_PATH)
    save_state(state)
    return ROOM_DISCIPLINE_PROFILE_PATH
def load_room_rules(valid_rooms: List[str]) -> Dict[str, Any]:
    payload = _read_json(ROOM_RULES_PATH)
    normalized = normalize_room_rules(payload, valid_rooms=valid_rooms)
    if payload != normalized:
        save_room_rules(normalized, valid_rooms=valid_rooms)
    return normalized


def save_room_rules(rules: Dict[str, Any], *, valid_rooms: List[str]) -> Path:
    normalized = normalize_room_rules(rules, valid_rooms=valid_rooms)
    _write_json(ROOM_RULES_PATH, normalized)
    state = load_state()
    state["room_rules_file"] = str(ROOM_RULES_PATH)
    save_state(state)
    return ROOM_RULES_PATH
