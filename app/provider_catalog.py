from __future__ import annotations

from datetime import date
from typing import Any, Dict, Iterable, List, Tuple

SLOT_MINUTES = 15


class ProviderCatalogError(ValueError):
    pass


def _as_int(raw: Any, field: str) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ProviderCatalogError(f"{field} must be an integer") from exc
    return value


def _validate_window(start: int, end: int, field: str) -> Tuple[int, int]:
    if start % SLOT_MINUTES != 0 or end % SLOT_MINUTES != 0:
        raise ProviderCatalogError(f"{field}: times must align to 15-minute increments")
    if end <= start:
        raise ProviderCatalogError(f"{field}: end must be after start")
    return start, end


def _normalize_templates(raw_templates: Any) -> List[Dict[str, Any]]:
    templates: List[Dict[str, Any]] = []
    if not isinstance(raw_templates, list):
        return templates
    for idx, tmpl in enumerate(raw_templates):
        if not isinstance(tmpl, dict):
            continue
        weekday = _as_int(tmpl.get("weekday", -1), f"availability_templates[{idx}].weekday")
        if weekday < 0 or weekday > 6:
            raise ProviderCatalogError("availability template weekday must be 0..6")
        windows: List[Dict[str, int]] = []
        for w_idx, w in enumerate(tmpl.get("windows", [])):
            if not isinstance(w, dict):
                continue
            start = _as_int(w.get("start_minute"), f"availability_templates[{idx}].windows[{w_idx}].start_minute")
            end = _as_int(w.get("end_minute"), f"availability_templates[{idx}].windows[{w_idx}].end_minute")
            start, end = _validate_window(start, end, f"availability_templates[{idx}].windows[{w_idx}]")
            windows.append({"start_minute": start, "end_minute": end})
        templates.append({"weekday": weekday, "windows": windows})
    return templates


def _normalize_exceptions(raw_exceptions: Any) -> List[Dict[str, Any]]:
    exceptions: List[Dict[str, Any]] = []
    if not isinstance(raw_exceptions, list):
        return exceptions
    for idx, item in enumerate(raw_exceptions):
        if not isinstance(item, dict):
            continue
        date_key = str(item.get("date") or item.get("date_key") or "").strip()
        if not date_key:
            continue
        # validate date format
        date.fromisoformat(date_key)
        kind = str(item.get("kind") or "").strip().lower()
        if not kind:
            kind = "added" if bool(item.get("available_override", False)) else "unavailable"
        if kind not in {"unavailable", "added"}:
            raise ProviderCatalogError(f"exceptions[{idx}].kind must be unavailable or added")

        if "window" in item and isinstance(item["window"], dict):
            start_raw = item["window"].get("start_minute")
            end_raw = item["window"].get("end_minute")
        else:
            start_raw = item.get("start_minute")
            end_raw = item.get("end_minute")

        start = _as_int(start_raw, f"exceptions[{idx}].start_minute")
        end = _as_int(end_raw, f"exceptions[{idx}].end_minute")
        start, end = _validate_window(start, end, f"exceptions[{idx}]")
        exceptions.append(
            {
                "date": date_key,
                "kind": kind,
                "start_minute": start,
                "end_minute": end,
            }
        )
    return exceptions


def normalize_provider_entry(
    entry: Dict[str, Any],
    *,
    fallback_id: str,
    all_rooms: Iterable[str],
    disciplines: Iterable[str],
) -> Dict[str, Any]:
    name = str(entry.get("provider_name") or entry.get("name") or entry.get("provider_id") or fallback_id).strip()
    if not name:
        raise ProviderCatalogError("provider_name is required")

    provider_id = str(entry.get("provider_id") or fallback_id).strip()
    if not provider_id:
        raise ProviderCatalogError("provider_id is required")

    discipline = str(entry.get("discipline", "")).strip()
    raw_disciplines = entry.get("disciplines") if isinstance(entry.get("disciplines"), list) else []
    disciplines_list = [str(d).strip() for d in raw_disciplines if str(d).strip()]
    if discipline and discipline not in disciplines_list:
        disciplines_list.insert(0, discipline)
    disciplines_list = list(dict.fromkeys(disciplines_list))
    if len(disciplines_list) > 5:
        disciplines_list = disciplines_list[:5]

    discipline_set = set(disciplines)
    if discipline_set and discipline and discipline not in discipline_set:
        raise ProviderCatalogError(f"Unknown discipline '{discipline}'")
    for item in disciplines_list:
        if discipline_set and item not in discipline_set:
            raise ProviderCatalogError(f"Unknown discipline '{item}'")

    room_set = set(all_rooms)
    raw_allowed = entry.get("allowed_rooms", [])
    allowed_rooms: List[str] = []
    if raw_allowed is None:
        raw_allowed = []
    for room in raw_allowed:
        room_name = str(room).strip()
        if room_name == "Any compatible room":
            allowed_rooms = ["Any compatible room"]
            break
        if room_name and (not room_set or room_name in room_set):
            allowed_rooms.append(room_name)
    if not allowed_rooms:
        allowed_rooms = ["Any compatible room"]

    templates = _normalize_templates(entry.get("availability_templates") or entry.get("templates") or [])
    exceptions = _normalize_exceptions(entry.get("exceptions") or [])

    return {
        "provider_id": provider_id,
        "provider_name": name,
        "discipline": disciplines_list[0] if disciplines_list else discipline,
        "disciplines": sorted(disciplines_list, key=lambda x: x.split()[0].lower()),
        "allowed_rooms": sorted(dict.fromkeys(allowed_rooms), key=lambda x: x.split()[0].lower()),
        "availability_templates": templates,
        "exceptions": exceptions,
    }


def normalize_provider_catalog(
    entries: Iterable[Dict[str, Any]],
    *,
    defaults: Iterable[str],
    all_rooms: Iterable[str],
    disciplines: Iterable[str],
) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for idx, item in enumerate(entries):
        if not isinstance(item, dict):
            item = {"provider_name": str(item)}
        normalized.append(
            normalize_provider_entry(
                item,
                fallback_id=str(item.get("provider_id") or item.get("provider_name") or f"provider_{idx+1}"),
                all_rooms=all_rooms,
                disciplines=disciplines,
            )
        )

    if not normalized:
        for idx, name in enumerate(defaults, start=1):
            normalized.append(
                normalize_provider_entry(
                    {"provider_id": name, "provider_name": name},
                    fallback_id=f"provider_{idx}",
                    all_rooms=all_rooms,
                    disciplines=disciplines,
                )
            )

    ids_seen: set[str] = set()
    names_seen: set[str] = set()
    deduped: List[Dict[str, Any]] = []
    for entry in normalized:
        name_key = entry["provider_name"].lower()
        if entry["provider_id"] in ids_seen or name_key in names_seen:
            continue
        ids_seen.add(entry["provider_id"])
        names_seen.add(name_key)
        deduped.append(entry)

    deduped.sort(key=lambda e: e["provider_name"].lower())
    return deduped


def provider_is_available(profile: Dict[str, Any], *, date_key: str, weekday: int, start_minute: int, end_minute: int) -> bool:
    templates = profile.get("availability_templates") or []
    in_template = False
    for tmpl in templates:
        if int(tmpl.get("weekday", -1)) != weekday:
            continue
        for window in tmpl.get("windows", []):
            start = int(window.get("start_minute", 0))
            end = int(window.get("end_minute", 0))
            if start <= start_minute and end_minute <= end:
                in_template = True
                break
        if in_template:
            break
    if not in_template and not templates:
        in_template = True

    for ex in profile.get("exceptions", []):
        ex_date = str(ex.get("date") or ex.get("date_key") or "")
        if ex_date != date_key:
            continue
        start = int(ex.get("start_minute") if "start_minute" in ex else ex.get("window", {}).get("start_minute", 0))
        end = int(ex.get("end_minute") if "end_minute" in ex else ex.get("window", {}).get("end_minute", 0))
        overlaps = not (end_minute <= start or start_minute >= end)
        if not overlaps:
            continue
        kind = str(ex.get("kind") or "").strip().lower()
        if not kind:
            kind = "added" if bool(ex.get("available_override", False)) else "unavailable"
        if kind == "added":
            return True
        return False
    return in_template
