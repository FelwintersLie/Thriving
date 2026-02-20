from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List


STATE_PATH = Path("data") / "app_state.json"
LAST_PROFILE_PATH = Path("data") / "last_profile.json"
LAST_SCHEDULE_PATH = Path("data") / "last_schedule.json"
PROVIDER_CATALOG_PATH = Path("data") / "provider_catalog.json"


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
    payload = _read_json(PROVIDER_CATALOG_PATH)
    values = payload.get("providers") if payload else None
    if not isinstance(values, list) or not values:
        save_provider_catalog(defaults)
        return list(defaults)
    cleaned = [str(v).strip() for v in values if str(v).strip()]
    if not cleaned:
        save_provider_catalog(defaults)
        return list(defaults)
    return sorted(dict.fromkeys(cleaned))


def save_provider_catalog(providers: List[str]) -> Path:
    unique = sorted(dict.fromkeys([p.strip() for p in providers if p.strip()]))
    _write_json(PROVIDER_CATALOG_PATH, {"providers": unique})
    state = load_state()
    state["provider_catalog_file"] = str(PROVIDER_CATALOG_PATH)
    save_state(state)
    return PROVIDER_CATALOG_PATH
