from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Sequence, Tuple

SLOT_MINUTES = 15

MAX_REQUESTS = 48
MAX_PROVIDERS = 64
MAX_PATIENTS = 256
MAX_ROOMS = 64
MAX_CANDIDATES_PER_REQUEST = 5000
MAX_BACKTRACK_STATES = 250000


class Mode(str, Enum):
    INDIVIDUAL = "individual"
    GROUP = "group"


@dataclass(frozen=True)
class TimeWindow:
    start_minute: int
    end_minute: int

    def contains(self, start: int, end: int) -> bool:
        return self.start_minute <= start and end <= self.end_minute


@dataclass
class ProviderTemplate:
    provider_id: str
    weekday: int  # Monday=0
    windows: List[TimeWindow]


@dataclass
class ProviderException:
    provider_id: str
    date_key: str  # YYYY-MM-DD
    window: TimeWindow
    available_override: bool


@dataclass
class Provider:
    id: str
    name: str
    disciplines: set[str]
    templates: List[ProviderTemplate] = field(default_factory=list)
    exceptions: List[ProviderException] = field(default_factory=list)

    def is_available(self, date_key: str, weekday: int, start: int, end: int) -> bool:
        in_template = any(w.contains(start, end) for t in self.templates if t.weekday == weekday for w in t.windows)
        if not in_template:
            return False

        for ex in self.exceptions:
            if ex.provider_id != self.id or ex.date_key != date_key:
                continue
            overlaps = not (end <= ex.window.start_minute or start >= ex.window.end_minute)
            if overlaps:
                if ex.available_override:
                    return True
                return False
        return True


@dataclass
class Patient:
    id: str
    name: str
    availability: Dict[str, List[TimeWindow]]  # date_key -> windows

    def is_available(self, date_key: str, start: int, end: int) -> bool:
        return any(w.contains(start, end) for w in self.availability.get(date_key, []))


@dataclass
class Room:
    id: str
    name: str
    capacity: int
    allowed_disciplines: set[str]


@dataclass
class SessionRequest:
    id: str
    patient_ids: Tuple[str, ...]
    discipline: str
    duration_minutes: int
    mode: Mode
    date_key: str
    preferred_window: Optional[TimeWindow] = None
    group_key: Optional[str] = None
    label: Optional[str] = None
    provider_id: Optional[str] = None
    room_id: Optional[str] = None

    @property
    def slot_count(self) -> int:
        return self.duration_minutes // SLOT_MINUTES


@dataclass(frozen=True)
class Assignment:
    request_id: str
    provider_id: str
    room_id: str
    start_minute: int
    end_minute: int
    label: str
    mode: Mode


class UnschedulableError(RuntimeError):
    pass


class ScheduleEngine:
    """
    Backtracking + scoring scheduler tuned for small daily census (<= 6 patients).
    """

    def generate_schedule(
        self,
        *,
        date_key: str,
        weekday: int,
        requests: Sequence[SessionRequest],
        providers: Sequence[Provider],
        patients: Sequence[Patient],
        rooms: Sequence[Room],
        day_window: TimeWindow,
        previous_assignments: Optional[Dict[str, Assignment]] = None,
        locked_request_ids: Optional[set[str]] = None,
    ) -> Dict[str, Assignment]:
        previous_assignments = previous_assignments or {}
        locked_request_ids = locked_request_ids or set()

        self._validate_input_sizes(requests=requests, providers=providers, patients=patients, rooms=rooms, day_window=day_window)

        patient_by_id = {p.id: p for p in patients}
        request_by_id = {r.id: r for r in requests}

        if len(patient_by_id) != len(patients):
            raise ValueError("Duplicate patient IDs detected")
        if len(request_by_id) != len(requests):
            raise ValueError("Duplicate request IDs detected")

        candidates: Dict[str, List[Assignment]] = {}

        for req in requests:
            if req.mode == Mode.GROUP and req.group_key is None:
                raise ValueError(f"Group request {req.id} needs group_key")
            if req.duration_minutes % SLOT_MINUTES != 0:
                raise ValueError(f"Request {req.id} duration must be multiple of 15")

            if req.id in locked_request_ids and req.id in previous_assignments:
                locked = previous_assignments[req.id]
                candidates[req.id] = [locked]
                continue

            options: List[Assignment] = []
            for provider in providers:
                if req.discipline not in provider.disciplines:
                    continue
                if req.provider_id and provider.id != req.provider_id:
                    continue

                for room in rooms:
                    if req.room_id and room.id != req.room_id:
                        continue
                    if req.discipline not in room.allowed_disciplines:
                        continue
                    if len(req.patient_ids) > room.capacity:
                        continue

                    for start in range(day_window.start_minute, day_window.end_minute, SLOT_MINUTES):
                        end = start + req.duration_minutes
                        if end > day_window.end_minute:
                            break
                        if not provider.is_available(date_key, weekday, start, end):
                            continue
                        if not all(patient_by_id[p].is_available(date_key, start, end) for p in req.patient_ids):
                            continue

                        label = req.label or f"{req.discipline.title()} {'Group' if req.mode == Mode.GROUP else 'Individual'}"
                        options.append(
                            Assignment(
                                request_id=req.id,
                                provider_id=provider.id,
                                room_id=room.id,
                                start_minute=start,
                                end_minute=end,
                                label=label,
                                mode=req.mode,
                            )
                        )
                        if len(options) > MAX_CANDIDATES_PER_REQUEST:
                            raise UnschedulableError(
                                f"Request {req.id} has too many options ({len(options)}). Narrow time windows or reduce resources."
                            )

            if not options:
                raise UnschedulableError(f"No feasible options for request {req.id}")
            candidates[req.id] = options

        sorted_requests = sorted(requests, key=lambda r: len(candidates[r.id]))

        best_solution: Optional[Dict[str, Assignment]] = None
        best_score: Optional[Tuple[int, int]] = None
        states_visited = 0

        def overlaps(a: Assignment, b: Assignment) -> bool:
            return not (a.end_minute <= b.start_minute or a.start_minute >= b.end_minute)

        def conflicts_with_assigned(a: Assignment, assigned: Dict[str, Assignment]) -> bool:
            req = request_by_id[a.request_id]
            req_patients = set(req.patient_ids)
            for other_req_id, other in assigned.items():
                other_req = request_by_id[other_req_id]
                if not overlaps(a, other):
                    continue
                if a.provider_id == other.provider_id:
                    return True
                if req_patients.intersection(other_req.patient_ids):
                    return True
                if a.room_id == other.room_id:
                    return True
            return False

        def score(solution: Dict[str, Assignment]) -> Tuple[int, int]:
            # Lower is better. Score[0] stabilizes previous schedule.
            changes = 0
            preference_penalty = 0
            for req in requests:
                new = solution[req.id]
                old = previous_assignments.get(req.id)
                if old and old != new:
                    changes += 1

                if req.preferred_window and not req.preferred_window.contains(new.start_minute, new.end_minute):
                    preference_penalty += 1
            return (changes, preference_penalty)

        def backtrack(index: int, assigned: Dict[str, Assignment]) -> None:
            nonlocal best_solution, best_score, states_visited
            states_visited += 1
            if states_visited > MAX_BACKTRACK_STATES:
                raise UnschedulableError(
                    "Search limit reached while scheduling. Narrow windows or schedule fewer sessions."
                )
            if index == len(sorted_requests):
                current_score = score(assigned)
                if best_score is None or current_score < best_score:
                    best_score = current_score
                    best_solution = dict(assigned)
                return

            req = sorted_requests[index]
            options = sorted(
                candidates[req.id],
                key=lambda a: (
                    0 if previous_assignments.get(req.id) == a else 1,
                    abs((req.preferred_window.start_minute if req.preferred_window else a.start_minute) - a.start_minute),
                ),
            )

            for option in options:
                if conflicts_with_assigned(option, assigned):
                    continue
                assigned[req.id] = option
                backtrack(index + 1, assigned)
                del assigned[req.id]

        backtrack(0, {})

        if not best_solution:
            raise UnschedulableError("No full solution found for the day")

        return best_solution

    def _validate_input_sizes(
        self,
        *,
        requests: Sequence[SessionRequest],
        providers: Sequence[Provider],
        patients: Sequence[Patient],
        rooms: Sequence[Room],
        day_window: TimeWindow,
    ) -> None:
        if day_window.end_minute <= day_window.start_minute:
            raise ValueError("day_window end_minute must be greater than start_minute")
        if (day_window.end_minute - day_window.start_minute) % SLOT_MINUTES != 0:
            raise ValueError("day_window must align to 15-minute slots")
        if len(requests) > MAX_REQUESTS:
            raise UnschedulableError(f"Too many requests for this MVP limit ({MAX_REQUESTS})")
        if len(providers) > MAX_PROVIDERS:
            raise UnschedulableError(f"Too many providers for this MVP limit ({MAX_PROVIDERS})")
        if len(patients) > MAX_PATIENTS:
            raise UnschedulableError(f"Too many patients for this MVP limit ({MAX_PATIENTS})")
        if len(rooms) > MAX_ROOMS:
            raise UnschedulableError(f"Too many rooms for this MVP limit ({MAX_ROOMS})")


def build_room_timeline(
    *,
    rooms: Sequence[Room],
    assignments: Dict[str, Assignment],
    day_window: TimeWindow,
) -> Dict[str, List[Dict[str, object]]]:
    """
    Builds 15-minute labels for each room so UI can display what is happening each interval.
    """
    room_timeline: Dict[str, List[Dict[str, object]]] = {}
    assignments_by_room: Dict[str, List[Assignment]] = {room.id: [] for room in rooms}
    for assignment in assignments.values():
        assignments_by_room.setdefault(assignment.room_id, []).append(assignment)

    for room in rooms:
        slots: List[Dict[str, object]] = []
        for minute in range(day_window.start_minute, day_window.end_minute, SLOT_MINUTES):
            active = [
                a
                for a in assignments_by_room.get(room.id, [])
                if a.start_minute <= minute < a.end_minute
            ]
            if not active:
                slots.append(
                    {
                        "start_minute": minute,
                        "end_minute": minute + SLOT_MINUTES,
                        "status": "free",
                        "label": None,
                        "mode": None,
                        "request_ids": [],
                    }
                )
                continue

            # In this MVP overlap is prevented for the same room, but we keep list for future group expansion.
            labels = sorted({a.label for a in active})
            modes = sorted({a.mode.value for a in active})
            request_ids = sorted({a.request_id for a in active})
            slots.append(
                {
                    "start_minute": minute,
                    "end_minute": minute + SLOT_MINUTES,
                    "status": "booked",
                    "label": " + ".join(labels),
                    "mode": ",".join(modes),
                    "request_ids": request_ids,
                }
            )

        room_timeline[room.id] = slots

    return room_timeline


# Utility helpers for printing / display

def to_hhmm(minutes: int) -> str:
    h = minutes // 60
    m = minutes % 60
    return f"{h:02d}:{m:02d}"


def format_assignment(assignment: Assignment) -> str:
    return (
        f"{assignment.request_id}: {assignment.label} | provider={assignment.provider_id} "
        f"room={assignment.room_id} {to_hhmm(assignment.start_minute)}-{to_hhmm(assignment.end_minute)}"
    )
