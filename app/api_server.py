from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List

from app.scheduler import (
    Assignment,
    Mode,
    Patient,
    Provider,
    ProviderException,
    ProviderTemplate,
    Room,
    ScheduleEngine,
    SessionRequest,
    TimeWindow,
    UnschedulableError,
    build_room_timeline,
)


def _time_window(raw: Dict[str, Any]) -> TimeWindow:
    return TimeWindow(start_minute=int(raw["start_minute"]), end_minute=int(raw["end_minute"]))


def _providers(raw_list: List[Dict[str, Any]]) -> List[Provider]:
    providers: List[Provider] = []
    for raw in raw_list:
        templates = [
            ProviderTemplate(
                provider_id=raw["id"],
                weekday=int(t["weekday"]),
                windows=[_time_window(w) for w in t.get("windows", [])],
            )
            for t in raw.get("templates", [])
        ]
        exceptions = [
            ProviderException(
                provider_id=raw["id"],
                date_key=e["date_key"],
                window=_time_window(e["window"]),
                available_override=bool(e["available_override"]),
            )
            for e in raw.get("exceptions", [])
        ]
        providers.append(
            Provider(
                id=raw["id"],
                name=raw["name"],
                disciplines=set(raw["disciplines"]),
                templates=templates,
                exceptions=exceptions,
                allowed_rooms=set(raw.get("allowed_rooms", [])) if raw.get("allowed_rooms") else set(),
            )
        )
    return providers


def _patients(raw_list: List[Dict[str, Any]]) -> List[Patient]:
    patients: List[Patient] = []
    for raw in raw_list:
        availability = {
            date_key: [_time_window(w) for w in windows]
            for date_key, windows in raw.get("availability", {}).items()
        }
        patients.append(Patient(id=raw["id"], name=raw["name"], availability=availability))
    return patients


def _rooms(raw_list: List[Dict[str, Any]]) -> List[Room]:
    return [
        Room(
            id=raw["id"],
            name=raw["name"],
            capacity=int(raw["capacity"]),
            allowed_disciplines=set(raw["allowed_disciplines"]),
        )
        for raw in raw_list
    ]


def _requests(raw_list: List[Dict[str, Any]], date_key: str) -> List[SessionRequest]:
    out: List[SessionRequest] = []
    for raw in raw_list:
        preferred = raw.get("preferred_window")
        out.append(
            SessionRequest(
                id=raw["id"],
                patient_ids=tuple(raw["patient_ids"]),
                discipline=raw["discipline"],
                duration_minutes=int(raw["duration_minutes"]),
                mode=Mode(raw["mode"]),
                date_key=raw.get("date_key", date_key),
                preferred_window=_time_window(preferred) if preferred else None,
                group_key=raw.get("group_key"),
                label=raw.get("label"),
                provider_id=raw.get("provider_id"),
                provider_ids=tuple(raw.get("provider_ids", [])) if raw.get("provider_ids") else None,
                room_id=raw.get("room_id"),
            )
        )
    return out


def _assignment_to_dict(a: Assignment, date_key: str | None = None) -> Dict[str, Any]:
    return {
        "request_id": a.request_id,
        "provider_id": a.provider_id,
        "room_id": a.room_id,
        "start_minute": a.start_minute,
        "end_minute": a.end_minute,
        "label": a.label,
        "mode": a.mode.value,
        "date_key": date_key,
    }


def _assignments_from_dict(raw: Dict[str, Dict[str, Any]]) -> Dict[str, Assignment]:
    return {
        request_id: Assignment(
            request_id=raw_assignment["request_id"],
            provider_id=raw_assignment["provider_id"],
            room_id=raw_assignment["room_id"],
            start_minute=int(raw_assignment["start_minute"]),
            end_minute=int(raw_assignment["end_minute"]),
            label=raw_assignment["label"],
            mode=Mode(raw_assignment["mode"]),
        )
        for request_id, raw_assignment in raw.items()
    }


def _validate_payload_shape(payload: Dict[str, Any]) -> None:
    required = {"date_key", "weekday", "day_window", "providers", "patients", "rooms", "requests"}
    missing = required.difference(payload.keys())
    if missing:
        raise ValueError(f"Payload missing required keys: {', '.join(sorted(missing))}")


def _validate_references(raw_requests: List[Dict[str, Any]], raw_patients: List[Dict[str, Any]]) -> None:
    patient_ids = {p.get("id") for p in raw_patients}
    if None in patient_ids:
        raise ValueError("Patient entries must include id")

    for req in raw_requests:
        req_id = req.get("id", "(unknown)")
        request_patients = req.get("patient_ids", [])
        if not request_patients:
            raise ValueError(f"Request {req_id} must include at least one patient_id")
        unknown = [pid for pid in request_patients if pid not in patient_ids]
        if unknown:
            raise ValueError(f"Request {req_id} references unknown patient IDs: {', '.join(unknown)}")


def handle_generate(payload: Dict[str, Any]) -> Dict[str, Any]:
    engine = ScheduleEngine()

    _validate_payload_shape(payload)
    _validate_references(payload["requests"], payload["patients"])

    date_key = payload["date_key"]
    weekday = int(payload["weekday"])
    day_window = _time_window(payload["day_window"])

    providers = _providers(payload["providers"])
    patients = _patients(payload["patients"])
    rooms = _rooms(payload["rooms"])
    requests = _requests(payload["requests"], date_key)

    previous = _assignments_from_dict(payload.get("previous_assignments", {}))
    locked_request_ids = set(payload.get("locked_request_ids", []))
    max_backtrack_states = payload.get("max_backtrack_states")
    max_candidates_per_request = payload.get("max_candidates_per_request")

    generated = engine.generate_schedule(
        date_key=date_key,
        weekday=weekday,
        requests=requests,
        providers=providers,
        patients=patients,
        rooms=rooms,
        day_window=day_window,
        previous_assignments=previous,
        locked_request_ids=locked_request_ids,
        max_backtrack_states=int(max_backtrack_states) if max_backtrack_states is not None else None,
        max_candidates_per_request=int(max_candidates_per_request) if max_candidates_per_request is not None else None,
    )

    timeline = build_room_timeline(rooms=rooms, assignments=generated, day_window=day_window)

    return {
        "assignments": {req_id: _assignment_to_dict(a, date_key) for req_id, a in generated.items()},
        "room_timeline": timeline,
    }


class SchedulerHandler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/generate":
            self._send_json(404, {"error": "Not found"})
            return

        content_len = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(content_len)

        try:
            payload = json.loads(raw.decode("utf-8"))
            result = handle_generate(payload)
            self._send_json(200, result)
        except UnschedulableError as exc:
            self._send_json(422, {"error": str(exc)})
        except Exception as exc:  # broad by design for API safety
            self._send_json(400, {"error": f"Bad request: {exc}"})


def run_server(host: str = "127.0.0.1", port: int = 8080) -> None:
    server = HTTPServer((host, port), SchedulerHandler)
    print(f"Scheduler API listening on http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run_server()
