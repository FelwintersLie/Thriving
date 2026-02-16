from __future__ import annotations

from datetime import date
from typing import Any, Dict, Iterable, List, Tuple

SLOT_MINUTES = 15
WEEKDAY_INDEX = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3, "Friday": 4}


class RoomRuleError(ValueError):
    pass


def _to_int(raw: Any, field: str) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise RoomRuleError(f"{field} must be an integer") from exc


def _normalize_window(window: Dict[str, Any], field: str) -> Dict[str, int]:
    start = _to_int(window.get("start_minute", window.get("start")), f"{field}.start")
    end = _to_int(window.get("end_minute", window.get("end")), f"{field}.end")
    if end <= start:
        raise RoomRuleError(f"{field}: end must be after start")
    if start % SLOT_MINUTES != 0 or end % SLOT_MINUTES != 0:
        raise RoomRuleError(f"{field}: times must align to 15-minute increments")
    return {"start_minute": start, "end_minute": end}


def _normalize_weekly(raw: Any, field: str) -> Dict[int, List[Dict[str, int]]]:
    out: Dict[int, List[Dict[str, int]]] = {i: [] for i in range(5)}
    if not isinstance(raw, dict):
        return out
    for key, windows in raw.items():
        if isinstance(key, int):
            weekday = key
        else:
            key_str = str(key)
            weekday = WEEKDAY_INDEX.get(key_str, -1)
            if weekday == -1 and key_str.isdigit():
                weekday = int(key_str)
        if weekday < 0 or weekday > 4:
            continue
        if not isinstance(windows, list):
            continue
        out[weekday] = [_normalize_window(w, f"{field}.{key}[{idx}]") for idx, w in enumerate(windows) if isinstance(w, dict)]
        out[weekday].sort(key=lambda w: (w["start_minute"], w["end_minute"]))
    return out


def _normalize_date_rules(raw: Any, field: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        date_key = str(item.get("date") or "").strip()
        if not date_key:
            continue
        date.fromisoformat(date_key)
        window = _normalize_window(item, f"{field}[{idx}]")
        out.append({"date": date_key, **window})
    out.sort(key=lambda x: (x["date"], x["start_minute"], x["end_minute"]))
    return out


def normalize_room_rules(raw: Dict[str, Any] | None, *, valid_rooms: Iterable[str]) -> Dict[str, Any]:
    valid = set(valid_rooms)
    source = raw or {}
    by_room = source.get("rooms", {}) if isinstance(source, dict) else {}

    result: Dict[str, Any] = {"rooms": {}}
    for room in valid:
        room_raw = by_room.get(room, {}) if isinstance(by_room, dict) else {}
        if not isinstance(room_raw, dict):
            room_raw = {}
        result["rooms"][room] = {
            "unavailable_weekly": _normalize_weekly(room_raw.get("unavailable_weekly", {}), f"{room}.unavailable_weekly"),
            "unavailable_dates": _normalize_date_rules(room_raw.get("unavailable_dates", []), f"{room}.unavailable_dates"),
            "available_only_weekly": _normalize_weekly(room_raw.get("available_only_weekly", {}), f"{room}.available_only_weekly"),
            "available_only_dates": _normalize_date_rules(room_raw.get("available_only_dates", []), f"{room}.available_only_dates"),
        }
    return result


def _overlaps(start_a: int, end_a: int, start_b: int, end_b: int) -> bool:
    return not (end_a <= start_b or start_a >= end_b)


def _contains(window: Dict[str, int], start: int, end: int) -> bool:
    return int(window["start_minute"]) <= start and end <= int(window["end_minute"])


def _weekly_windows(weekly: Dict[int, List[Dict[str, int]]] | Dict[str, Any], weekday: int) -> List[Dict[str, int]]:
    if weekday in weekly:
        return list(weekly.get(weekday, []))
    return list(weekly.get(str(weekday), []))


def room_rule_violations(
    rules: Dict[str, Any],
    *,
    room_id: str,
    date_key: str,
    weekday: int,
    start_minute: int,
    end_minute: int,
) -> List[str]:
    rooms = rules.get("rooms", {}) if isinstance(rules, dict) else {}
    room_rules = rooms.get(room_id, {})
    if not room_rules:
        return []

    violations: List[str] = []

    unavailable_weekly = _weekly_windows(room_rules.get("unavailable_weekly", {}), weekday)
    for window in unavailable_weekly:
        if _overlaps(start_minute, end_minute, int(window["start_minute"]), int(window["end_minute"])):
            violations.append("room_unavailable_weekly")
            break

    for ex in room_rules.get("unavailable_dates", []):
        if ex.get("date") == date_key and _overlaps(start_minute, end_minute, int(ex["start_minute"]), int(ex["end_minute"])):
            violations.append("room_unavailable_date")
            break

    available_only_windows = _weekly_windows(room_rules.get("available_only_weekly", {}), weekday)
    if available_only_windows and not any(_contains(w, start_minute, end_minute) for w in available_only_windows):
        violations.append("room_available_only_weekly")

    available_only_dates = [w for w in room_rules.get("available_only_dates", []) if w.get("date") == date_key]
    if available_only_dates and not any(_contains(w, start_minute, end_minute) for w in available_only_dates):
        violations.append("room_available_only_date")

    return violations


def room_is_available(
    rules: Dict[str, Any],
    *,
    room_id: str,
    date_key: str,
    weekday: int,
    start_minute: int,
    end_minute: int,
) -> bool:
    return not room_rule_violations(
        rules,
        room_id=room_id,
        date_key=date_key,
        weekday=weekday,
        start_minute=start_minute,
        end_minute=end_minute,
    )
