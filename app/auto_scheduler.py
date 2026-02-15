from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Tuple

from app.api_server import handle_generate

SLOT_MINUTES = 15
MAX_REQUIREMENTS = 300
MAX_EXPANDED_REQUESTS = 6000


class AutoScheduleError(RuntimeError):
    pass


@dataclass
class Bottleneck:
    requirement_id: str
    date_key: str
    reason: str


WEEKDAY_NAME_TO_INDEX = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
}


def planning_dates(start_monday: date, weeks: int = 3) -> List[str]:
    out: List[str] = []
    current = start_monday
    while len(out) < weeks * 5:
        if current.weekday() < 5:
            out.append(current.isoformat())
        current += timedelta(days=1)
    return out


def _normalize_windows(windows: Iterable[Dict[str, int]]) -> List[Dict[str, int]]:
    normalized: List[Dict[str, int]] = []
    for window in windows:
        start = int(window["start_minute"])
        end = int(window["end_minute"])
        if end <= start:
            raise ValueError("Time window end must be greater than start")
        if start % SLOT_MINUTES != 0 or end % SLOT_MINUTES != 0:
            raise ValueError("Time windows must align to 15-minute increments")
        normalized.append({"start_minute": start, "end_minute": end})
    if not normalized:
        raise ValueError("At least one time window is required")
    return normalized


def validate_requirement(requirement: Dict[str, Any]) -> Dict[str, Any]:
    rid = str(requirement.get("id", "")).strip()
    if not rid:
        raise ValueError("Requirement id is required")

    mode = str(requirement.get("session_mode", "individual")).lower()
    if mode not in {"individual", "group"}:
        raise ValueError(f"Requirement {rid}: session_mode must be individual or group")

    duration = int(requirement.get("duration_minutes", 0))
    if duration <= 0 or duration % SLOT_MINUTES != 0:
        raise ValueError(f"Requirement {rid}: duration must be positive and divisible by 15")

    weekdays = [int(w) for w in requirement.get("weekdays", [])]
    if not weekdays:
        raise ValueError(f"Requirement {rid}: at least one weekday is required")
    if any(w < 0 or w > 4 for w in weekdays):
        raise ValueError(f"Requirement {rid}: weekdays must be in range 0-4")

    weeks = sorted({int(w) for w in requirement.get("weeks", [1, 2, 3])})
    if not weeks or any(w < 1 or w > 3 for w in weeks):
        raise ValueError(f"Requirement {rid}: weeks must be 1..3")

    patient_scope = requirement.get("patient_scope", "all")
    patient_ids = [str(p) for p in requirement.get("patient_ids", [])]
    if patient_scope not in {"all", "single", "subset"}:
        raise ValueError(f"Requirement {rid}: invalid patient_scope")
    if patient_scope == "single" and len(patient_ids) != 1:
        raise ValueError(f"Requirement {rid}: single scope requires exactly one patient")
    if patient_scope == "subset" and not patient_ids:
        raise ValueError(f"Requirement {rid}: subset scope requires patient_ids")

    windows = _normalize_windows(requirement.get("time_windows", []))

    out = {
        "id": rid,
        "discipline": str(requirement["discipline"]),
        "provider_id": str(requirement.get("provider_id", "any")).strip() or "any",
        "provider_ids": [str(v).strip() for v in requirement.get("provider_ids", []) if str(v).strip()],
        "room_id": str(requirement.get("room_id", "any")).strip() or "any",
        "duration_minutes": duration,
        "session_mode": mode,
        "patient_scope": patient_scope,
        "patient_ids": patient_ids,
        "weekdays": weekdays,
        "weeks": weeks,
        "time_windows": windows,
        "hard_constraint": bool(requirement.get("hard_constraint", True)),
        "priority": int(requirement.get("priority", 100)),
        "group_size": int(requirement.get("group_size", 6)),
    }
    if out["provider_ids"]:
        out["provider_ids"] = sorted(dict.fromkeys(out["provider_ids"]))
        out["provider_id"] = out["provider_ids"][0]

    return out


def _dates_for_requirement(requirement: Dict[str, Any], planning_date_keys: List[str]) -> List[str]:
    dates: List[str] = []
    for d in planning_date_keys:
        as_date = date.fromisoformat(d)
        week_number = ((as_date - date.fromisoformat(planning_date_keys[0])).days // 7) + 1
        if week_number not in requirement["weeks"]:
            continue
        if as_date.weekday() not in requirement["weekdays"]:
            continue
        dates.append(d)
    return dates


def _patient_targets(requirement: Dict[str, Any], patient_ids: List[str]) -> List[List[str]]:
    scope = requirement["patient_scope"]
    if scope == "all":
        if requirement["session_mode"] == "group":
            return [list(patient_ids)]
        return [[pid] for pid in patient_ids]
    if scope == "single":
        return [[requirement["patient_ids"][0]]]
    if scope == "subset":
        if requirement["session_mode"] == "group":
            return [list(requirement["patient_ids"])]
        return [[pid] for pid in requirement["patient_ids"]]
    return []


def _build_expanded_requests(
    requirements: List[Dict[str, Any]],
    planning_date_keys: List[str],
    patient_ids: List[str],
) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    expanded: List[Dict[str, Any]] = []
    source_by_request_id: Dict[str, Dict[str, Any]] = {}

    for req in requirements:
        for date_key in _dates_for_requirement(req, planning_date_keys):
            for window_idx, window in enumerate(req["time_windows"]):
                for target_idx, target_patient_ids in enumerate(_patient_targets(req, patient_ids)):
                    request_id = f"{req['id']}|{date_key}|w{window_idx}|p{target_idx}"
                    expanded_req = {
                        "id": request_id,
                        "patient_ids": target_patient_ids,
                        "discipline": req["discipline"],
                        "duration_minutes": req["duration_minutes"],
                        "mode": req["session_mode"],
                        "date_key": date_key,
                        "preferred_window": dict(window),
                        "group_key": req["id"] if req["session_mode"] == "group" else None,
                        "label": req["discipline"],
                    }
                    if req.get("provider_ids"):
                        expanded_req["provider_ids"] = list(req["provider_ids"])
                    elif req["provider_id"] != "any":
                        expanded_req["provider_id"] = req["provider_id"]
                    if req["room_id"] != "any":
                        expanded_req["room_id"] = req["room_id"]
                    expanded.append(expanded_req)
                    source_by_request_id[request_id] = req

    if len(expanded) > MAX_EXPANDED_REQUESTS:
        raise AutoScheduleError(
            f"Expanded requests exceeded safety limit ({MAX_EXPANDED_REQUESTS}). Reduce patients, weeks, or requirement count."
        )
    return expanded, source_by_request_id


def _deepcopy_json(payload: Any) -> Any:
    # json-safe light copy to avoid importing copy in many call sites
    import json

    return json.loads(json.dumps(payload))


def _apply_requirement_filters(
    requests: List[Dict[str, Any]],
    source_by_request_id: Dict[str, Dict[str, Any]],
    hard_only: bool,
) -> List[Dict[str, Any]]:
    if not hard_only:
        return requests
    return [r for r in requests if source_by_request_id[r["id"]]["hard_constraint"]]


def _solve_multiday(
    *,
    profile_template: Dict[str, Any],
    requests: List[Dict[str, Any]],
    previous_assignments: Dict[str, Dict[str, Any]] | None = None,
    solver_limits: Dict[str, int] | None = None,
) -> Tuple[Dict[str, Dict[str, Any]], List[Bottleneck]]:
    assignments: Dict[str, Dict[str, Any]] = {}
    bottlenecks: List[Bottleneck] = []

    previous_assignments = previous_assignments or {}
    solver_limits = solver_limits or {}
    requests_by_date: Dict[str, List[Dict[str, Any]]] = {}
    for request in requests:
        requests_by_date.setdefault(request["date_key"], []).append(request)

    for date_key in profile_template["planning_dates"]:
        daily_requests = requests_by_date.get(date_key, [])
        if not daily_requests:
            continue
        weekday = date.fromisoformat(date_key).weekday()
        daily_previous = {rid: raw for rid, raw in previous_assignments.items() if rid in {r["id"] for r in daily_requests}}

        payload = {
            "date_key": date_key,
            "weekday": weekday,
            "day_window": profile_template["day_window"],
            "providers": profile_template["providers"],
            "patients": profile_template["patients"],
            "rooms": profile_template["rooms"],
            "requests": daily_requests,
            "previous_assignments": daily_previous,
            "locked_request_ids": [],
            "max_backtrack_states": solver_limits.get("max_backtrack_states"),
            "max_candidates_per_request": solver_limits.get("max_candidates_per_request"),
        }

        try:
            result = handle_generate(payload)
            assignments.update(result.get("assignments", {}))
        except Exception as exc:
            # collect per-request failure reasons to keep report actionable
            reason = str(exc)
            for request in daily_requests:
                bottlenecks.append(Bottleneck(request["id"], date_key, reason))

    return assignments, bottlenecks


def explain_infeasibility(
    requirements: List[Dict[str, Any]],
    planning_date_keys: List[str],
    providers: List[Dict[str, Any]],
    rooms: List[Dict[str, Any]],
    patient_ids: List[str],
) -> Dict[str, Any]:
    validated = [validate_requirement(r) for r in requirements]
    expanded, source_by_request_id = _build_expanded_requests(validated, planning_date_keys, patient_ids)

    provider_ids = {p["id"] for p in providers}
    room_ids = {r["id"] for r in rooms}
    room_disciplines = {r["id"]: set(r.get("allowed_disciplines", [])) for r in rooms}

    issues: List[str] = []
    by_requirement: Dict[str, List[str]] = {req["id"]: [] for req in validated}

    for req in validated:
        listed_providers = req.get("provider_ids") or ([] if req["provider_id"] == "any" else [req["provider_id"]])
        missing_providers = [pid for pid in listed_providers if pid not in provider_ids]
        if missing_providers:
            message = f"Unknown provider(s): {', '.join(missing_providers)}"
            issues.append(f"{req['id']}: {message}")
            by_requirement[req["id"]].append(message)
        if req["room_id"] != "any" and req["room_id"] not in room_ids:
            message = f"Unknown room '{req['room_id']}'"
            issues.append(f"{req['id']}: {message}")
            by_requirement[req["id"]].append(message)
        if req["room_id"] != "any" and req["room_id"] in room_disciplines and req["discipline"] not in room_disciplines[req["room_id"]]:
            message = f"Room '{req['room_id']}' is incompatible with discipline '{req['discipline']}'"
            issues.append(f"{req['id']}: {message}")
            by_requirement[req["id"]].append(message)

    if not issues:
        # attempt rough capacity check by provider/day for quick bottleneck identification
        demand: Dict[Tuple[str, str], int] = {}
        for exp in expanded:
            source = source_by_request_id[exp["id"]]
            provider_choices = source.get("provider_ids") or ([] if source["provider_id"] == "any" else [source["provider_id"]])
            if not provider_choices:
                continue
            share = max(1, int(exp["duration_minutes"]) // len(provider_choices))
            for provider_id in provider_choices:
                key = (provider_id, exp["date_key"])
                demand[key] = demand.get(key, 0) + share

        provider_avail: Dict[Tuple[str, str], int] = {}
        for provider in providers:
            pid = provider["id"]
            template_by_weekday = {int(t["weekday"]): t.get("windows", []) for t in provider.get("templates", [])}
            exception_by_date = provider.get("exceptions", [])
            for d in planning_date_keys:
                weekday = date.fromisoformat(d).weekday()
                windows = template_by_weekday.get(weekday, [])
                available_minutes = sum(int(w["end_minute"]) - int(w["start_minute"]) for w in windows)
                for ex in exception_by_date:
                    if ex.get("date_key") != d:
                        continue
                    w = ex.get("window", {})
                    delta = int(w.get("end_minute", 0)) - int(w.get("start_minute", 0))
                    if bool(ex.get("available_override")):
                        available_minutes += max(delta, 0)
                    else:
                        available_minutes -= max(delta, 0)
                provider_avail[(pid, d)] = max(available_minutes, 0)

        for (pid, d), mins in sorted(demand.items()):
            avail = provider_avail.get((pid, d), 0)
            if mins > avail:
                req_id = next(
                    (r["id"] for r in validated if pid in (r.get("provider_ids") or ([] if r["provider_id"] == "any" else [r["provider_id"]]))),
                    "(unknown requirement)",
                )
                msg = f"Provider {pid} has demand {mins} min but only {avail} min available on {d}"
                issues.append(msg)
                by_requirement.setdefault(req_id, []).append(msg)

    return {
        "ok": len(issues) == 0,
        "issues": issues,
        "requirement_issues": by_requirement,
    }


def generate_three_week_schedule(
    *,
    profile_template: Dict[str, Any],
    requirements: List[Dict[str, Any]],
    previous_assignments: Dict[str, Dict[str, Any]] | None = None,
    solver_limits: Dict[str, int] | None = None,
) -> Dict[str, Any]:
    if len(requirements) > MAX_REQUIREMENTS:
        raise AutoScheduleError(f"Too many requirements ({len(requirements)}), max is {MAX_REQUIREMENTS}")

    validated = [validate_requirement(req) for req in requirements]
    patient_ids = [p["id"] for p in profile_template.get("patients", [])]

    expanded_requests, source_by_request_id = _build_expanded_requests(
        validated,
        profile_template["planning_dates"],
        patient_ids,
    )

    hard_requests = _apply_requirement_filters(expanded_requests, source_by_request_id, hard_only=True)
    hard_assignments, hard_bottlenecks = _solve_multiday(
        profile_template=profile_template,
        requests=hard_requests,
        previous_assignments=previous_assignments,
        solver_limits=solver_limits,
    )

    if hard_bottlenecks:
        return {
            "ok": False,
            "assignments": {},
            "requests": expanded_requests,
            "bottlenecks": [b.__dict__ for b in hard_bottlenecks],
            "report": explain_infeasibility(
                validated,
                profile_template["planning_dates"],
                profile_template["providers"],
                profile_template["rooms"],
                patient_ids,
            ),
            "diff": {"unchanged": 0, "moved": 0, "added": 0, "removed": 0, "by_date": {}},
        }

    soft_requests = [r for r in expanded_requests if not source_by_request_id[r["id"]]["hard_constraint"]]
    merged_assignments = dict(hard_assignments)
    soft_bottlenecks: List[Bottleneck] = []
    if soft_requests:
        soft_assignments, soft_bottlenecks = _solve_multiday(
            profile_template=profile_template,
            requests=soft_requests,
            previous_assignments=previous_assignments,
            solver_limits=solver_limits,
        )
        merged_assignments.update(soft_assignments)

    diff = diff_assignments(previous_assignments or {}, merged_assignments)
    return {
        "ok": True,
        "assignments": merged_assignments,
        "requests": expanded_requests,
        "bottlenecks": [b.__dict__ for b in soft_bottlenecks],
        "report": {"ok": True, "issues": []},
        "diff": diff,
    }


def diff_assignments(
    previous_assignments: Dict[str, Dict[str, Any]],
    new_assignments: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    unchanged = moved = added = removed = 0
    by_date: Dict[str, Dict[str, int]] = {}
    all_ids = set(previous_assignments) | set(new_assignments)
    for rid in all_ids:
        old = previous_assignments.get(rid)
        new = new_assignments.get(rid)
        date_key = (new or old or {}).get("date_key", "(unknown)")
        bucket = by_date.setdefault(date_key, {"unchanged": 0, "moved": 0, "added": 0, "removed": 0})
        if old and new:
            old_key = (old.get("provider_id"), old.get("room_id"), old.get("start_minute"), old.get("end_minute"))
            new_key = (new.get("provider_id"), new.get("room_id"), new.get("start_minute"), new.get("end_minute"))
            if old_key == new_key:
                unchanged += 1
                bucket["unchanged"] += 1
            else:
                moved += 1
                bucket["moved"] += 1
        elif new and not old:
            added += 1
            bucket["added"] += 1
        elif old and not new:
            removed += 1
            bucket["removed"] += 1

    return {
        "unchanged": unchanged,
        "moved": moved,
        "added": added,
        "removed": removed,
        "by_date": by_date,
    }


def auto_reconfigure_schedule(
    *,
    profile_template: Dict[str, Any],
    requirements: List[Dict[str, Any]],
    existing_assignments: Dict[str, Dict[str, Any]],
    solver_limits: Dict[str, int] | None = None,
) -> Dict[str, Any]:
    # Reuse generate flow with previous assignments for minimal disruption preference.
    return generate_three_week_schedule(
        profile_template=profile_template,
        requirements=requirements,
        previous_assignments=_deepcopy_json(existing_assignments),
        solver_limits=solver_limits,
    )
