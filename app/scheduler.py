from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional, Sequence, Tuple

SLOT_MINUTES = 15

MAX_REQUESTS = 240
MAX_PROVIDERS = 64
MAX_PATIENTS = 256
MAX_ROOMS = 64
MAX_CANDIDATES_PER_REQUEST = 5000
MAX_BACKTRACK_STATES = 250000
MAX_CANDIDATES_PER_REQUEST_HARD_CAP = 50000
MAX_BACKTRACK_STATES_HARD_CAP = 5000000
MAX_SOLVE_SECONDS = 10.0


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
    allowed_rooms: set[str] = field(default_factory=set)
    enforce_lunch_break: bool = False
    lunch_earliest_start_minute: int = 11 * 60 + 30
    lunch_latest_start_minute: int = 13 * 60

    def is_available(self, date_key: str, weekday: int, start: int, end: int) -> bool:
        in_template = any(w.contains(start, end) for t in self.templates if t.weekday == weekday for w in t.windows)
        if not in_template and not self.templates and not self.exceptions:
            in_template = True
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
    unavailable_weekly: Dict[int, List[TimeWindow]] = field(default_factory=dict)
    unavailable_dates: Dict[str, List[TimeWindow]] = field(default_factory=dict)
    available_only_weekly: Dict[int, List[TimeWindow]] = field(default_factory=dict)
    available_only_dates: Dict[str, List[TimeWindow]] = field(default_factory=dict)
    room_preference_tier: int = 0

    def is_available(self, date_key: str, weekday: int, start: int, end: int) -> bool:
        for window in self.unavailable_weekly.get(weekday, []):
            if not (end <= window.start_minute or start >= window.end_minute):
                return False
        for window in self.unavailable_dates.get(date_key, []):
            if not (end <= window.start_minute or start >= window.end_minute):
                return False

        weekly_allowed = self.available_only_weekly.get(weekday, [])
        if weekly_allowed and not any(w.start_minute <= start and end <= w.end_minute for w in weekly_allowed):
            return False
        date_allowed = self.available_only_dates.get(date_key, [])
        if date_allowed and not any(w.start_minute <= start and end <= w.end_minute for w in date_allowed):
            return False
        return True


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
    provider_ids: Optional[Tuple[str, ...]] = None
    room_id: Optional[str] = None

    @property
    def slot_count(self) -> int:
        return self.duration_minutes // SLOT_MINUTES


@dataclass(frozen=True)
class Assignment:
    request_id: str
    provider_id: Optional[str]
    room_id: str
    start_minute: int
    end_minute: int
    label: str
    mode: Mode


class UnschedulableError(RuntimeError):
    pass


class SolveTimeoutError(UnschedulableError):
    pass


class GenerationCancelledError(UnschedulableError):
    pass


class ScheduleEngine:
    """
    Constraint-based scheduler with composable hard/soft rules and diagnostic logging.
    """

    def __init__(self) -> None:
        self.logger = logging.getLogger("app.scheduler")
        self.last_diagnostics: List[str] = []

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
        max_backtrack_states: Optional[int] = None,
        max_candidates_per_request: Optional[int] = None,
        max_solve_seconds: Optional[float] = None,
        cancel_event: Optional[object] = None,
        progress_callback: Optional[Callable[[Dict[str, object]], None]] = None,
    ) -> Dict[str, Assignment]:
        previous_assignments = previous_assignments or {}
        locked_request_ids = locked_request_ids or set()
        candidate_limit = max_candidates_per_request or MAX_CANDIDATES_PER_REQUEST
        candidate_limit = max(100, min(int(candidate_limit), MAX_CANDIDATES_PER_REQUEST_HARD_CAP))
        self.last_diagnostics = []
        solve_timeout_seconds = float(max_solve_seconds if max_solve_seconds is not None else MAX_SOLVE_SECONDS)
        if solve_timeout_seconds <= 0:
            solve_timeout_seconds = MAX_SOLVE_SECONDS
        start_perf = time.perf_counter()

        self._validate_input_sizes(requests=requests, providers=providers, patients=patients, rooms=rooms, day_window=day_window)

        patient_by_id = {p.id: p for p in patients}
        request_by_id = {r.id: r for r in requests}
        if len(patient_by_id) != len(patients):
            raise ValueError("Duplicate patient IDs detected")
        if len(request_by_id) != len(requests):
            raise ValueError("Duplicate request IDs detected")

        candidate_map = self._build_candidate_map(
            date_key=date_key,
            weekday=weekday,
            requests=requests,
            providers=providers,
            patients=patients,
            rooms=rooms,
            day_window=day_window,
            previous_assignments=previous_assignments,
            locked_request_ids=locked_request_ids,
            candidate_limit=candidate_limit,
            solve_start_perf=start_perf,
            solve_timeout_seconds=solve_timeout_seconds,
            cancel_event=cancel_event,
            progress_callback=progress_callback,
        )
        solution = self._solve_with_constraints(
            requests=requests,
            candidate_map=candidate_map,
            previous_assignments=previous_assignments,
            max_backtrack_states=max_backtrack_states,
            rooms=rooms,
            providers=providers,
            date_key=date_key,
            weekday=weekday,
            day_window=day_window,
            solve_start_perf=start_perf,
            solve_timeout_seconds=solve_timeout_seconds,
            cancel_event=cancel_event,
            progress_callback=progress_callback,
        )
        return solution

    def _build_candidate_map(
        self,
        *,
        date_key: str,
        weekday: int,
        requests: Sequence[SessionRequest],
        providers: Sequence[Provider],
        patients: Sequence[Patient],
        rooms: Sequence[Room],
        day_window: TimeWindow,
        previous_assignments: Dict[str, Assignment],
        locked_request_ids: set[str],
        candidate_limit: int,
        solve_start_perf: float,
        solve_timeout_seconds: float,
        cancel_event: Optional[object],
        progress_callback: Optional[Callable[[Dict[str, object]], None]],
    ) -> Dict[str, List[Assignment]]:
        patient_by_id = {p.id: p for p in patients}
        candidates: Dict[str, List[Assignment]] = {}
        attempts = 0

        for req in requests:
            if cancel_event is not None and hasattr(cancel_event, "is_set") and cancel_event.is_set():
                raise GenerationCancelledError("Generation cancelled.")
            elapsed = time.perf_counter() - solve_start_perf
            if elapsed > solve_timeout_seconds:
                raise SolveTimeoutError(
                    f"No solution found within {solve_timeout_seconds:g}s (timed out). "
                    f"Tried candidate generation for {len(candidates)} request(s) in {elapsed:.2f}s."
                )
            if req.mode == Mode.GROUP and req.group_key is None:
                raise ValueError(f"Group request {req.id} needs group_key")
            if req.duration_minutes % SLOT_MINUTES != 0:
                raise ValueError(f"Request {req.id} duration must be multiple of 15")

            if req.id in locked_request_ids and req.id in previous_assignments:
                candidates[req.id] = [previous_assignments[req.id]]
                self._log_decision(f"Request {req.id} locked to previous assignment")
                continue

            rejection_counts = {
                "provider_discipline": 0,
                "provider_specific": 0,
                "room_discipline": 0,
                "room_rules": 0,
                "patient": 0,
                "provider": 0,
            }
            options: List[Assignment] = []
            provider_pool: List[Optional[Provider]] = [None] if req.provider_id == "NO_PROVIDER" else list(providers)
            for provider in provider_pool:
                attempts += 1
                if cancel_event is not None and hasattr(cancel_event, "is_set") and cancel_event.is_set():
                    raise GenerationCancelledError("Generation cancelled.")
                if time.perf_counter() - solve_start_perf > solve_timeout_seconds:
                    raise SolveTimeoutError(
                        f"No solution found within {solve_timeout_seconds:g}s (timed out). "
                        f"Try relaxing constraints. elapsed={time.perf_counter() - solve_start_perf:.2f}s attempts={attempts}"
                    )
                if progress_callback and attempts % 250 == 0:
                    progress_callback({
                        "stage": "candidate_generation",
                        "elapsed": time.perf_counter() - solve_start_perf,
                        "attempts": attempts,
                        "request_id": req.id,
                        "discipline": req.discipline,
                        "patients": list(req.patient_ids),
                    })
                if provider is not None:
                    if req.discipline not in provider.disciplines:
                        rejection_counts["provider_discipline"] += 1
                        continue
                    if req.provider_id and provider.id != req.provider_id:
                        rejection_counts["provider_specific"] += 1
                        continue
                    if req.provider_ids and provider.id not in req.provider_ids:
                        rejection_counts["provider_specific"] += 1
                        continue

                for room in rooms:
                    if req.room_id and room.id != req.room_id:
                        continue
                    if provider is not None and provider.allowed_rooms and room.id not in provider.allowed_rooms:
                        continue
                    if req.discipline not in room.allowed_disciplines:
                        rejection_counts["room_discipline"] += 1
                        continue
                    if len(req.patient_ids) > room.capacity:
                        continue

                    for start in range(day_window.start_minute, day_window.end_minute, SLOT_MINUTES):
                        end = start + req.duration_minutes
                        if end > day_window.end_minute:
                            break
                        if provider is not None and not provider.is_available(date_key, weekday, start, end):
                            rejection_counts["provider"] += 1
                            continue
                        if not room.is_available(date_key, weekday, start, end):
                            rejection_counts["room_rules"] += 1
                            continue
                        if not all(patient_by_id[p].is_available(date_key, start, end) for p in req.patient_ids):
                            rejection_counts["patient"] += 1
                            continue
                        options.append(
                            Assignment(
                                request_id=req.id,
                                provider_id=provider.id if provider is not None else None,
                                room_id=room.id,
                                start_minute=start,
                                end_minute=end,
                                label=req.label or f"{req.discipline.title()} {'Group' if req.mode == Mode.GROUP else 'Individual'}",
                                mode=req.mode,
                            )
                        )
                        if len(options) > candidate_limit:
                            raise UnschedulableError(
                                f"Request {req.id} has too many options ({len(options)}). Narrow time windows or reduce resources."
                            )
            if not options:
                reason = ", ".join(f"{k}={v}" for k, v in rejection_counts.items() if v > 0)
                self._log_constraint_failure(req.id, reason or "no feasible candidates")
                raise UnschedulableError(f"No feasible options for request {req.id}{f' ({reason})' if reason else ''}")
            candidates[req.id] = options
            self._log_decision(f"Request {req.id} generated {len(options)} candidates")
        return candidates

    def _solve_with_constraints(
        self,
        *,
        requests: Sequence[SessionRequest],
        candidate_map: Dict[str, List[Assignment]],
        previous_assignments: Dict[str, Assignment],
        max_backtrack_states: Optional[int],
        rooms: Sequence[Room],
        providers: Sequence[Provider],
        date_key: str,
        weekday: int,
        day_window: TimeWindow,
        solve_start_perf: float,
        solve_timeout_seconds: float,
        cancel_event: Optional[object],
        progress_callback: Optional[Callable[[Dict[str, object]], None]],
    ) -> Dict[str, Assignment]:
        backtrack_limit = max_backtrack_states or MAX_BACKTRACK_STATES
        backtrack_limit = max(1000, min(int(backtrack_limit), MAX_BACKTRACK_STATES_HARD_CAP))
        request_by_id = {r.id: r for r in requests}
        sorted_ids = sorted([r.id for r in requests], key=lambda rid: len(candidate_map[rid]))
        fixed_assignments = [a for rid, a in previous_assignments.items() if rid not in request_by_id]
        room_by_id = {room.id: room for room in rooms}

        def overlaps(a: Assignment, b: Assignment) -> bool:
            return not (a.end_minute <= b.start_minute or a.start_minute >= b.end_minute)

        def hard_conflict(req_id: str, candidate: Assignment, assigned: Dict[str, Assignment]) -> Optional[str]:
            req = request_by_id[req_id]
            req_patients = set(req.patient_ids)
            for other_id, other in assigned.items():
                other_req = request_by_id[other_id]
                if not overlaps(candidate, other):
                    continue
                if candidate.provider_id and other.provider_id and candidate.provider_id == other.provider_id:
                    return f"provider_conflict:{candidate.provider_id}"
                if req_patients.intersection(other_req.patient_ids):
                    return "patient_conflict"
                if candidate.room_id == other.room_id and (req.mode == Mode.INDIVIDUAL or other_req.mode == Mode.INDIVIDUAL):
                    return f"room_conflict:{candidate.room_id}"
            for other in fixed_assignments:
                if not overlaps(candidate, other):
                    continue
                if candidate.provider_id and other.provider_id and candidate.provider_id == other.provider_id:
                    return f"provider_conflict_fixed:{candidate.provider_id}"
                if candidate.room_id == other.room_id:
                    return f"room_conflict_fixed:{candidate.room_id}"
            return None

        def soft_score(req_id: str, candidate: Assignment) -> int:
            req = request_by_id[req_id]
            penalty = 0
            previous = previous_assignments.get(req_id)
            if previous and previous != candidate:
                penalty += 10
            if req.preferred_window and not req.preferred_window.contains(candidate.start_minute, candidate.end_minute):
                penalty += 3
            room_tier = room_by_id.get(candidate.room_id).room_preference_tier if candidate.room_id in room_by_id else 0
            penalty += max(0, int(room_tier))
            return penalty

        best: Optional[Dict[str, Assignment]] = None
        best_score: Optional[int] = None
        states = 0
        conflict_logs = 0
        most_constrained = sorted_ids[0] if sorted_ids else "(none)"

        provider_by_id = {p.id: p for p in providers}

        def _has_lunch_slot(provider: Provider, solution: Dict[str, Assignment]) -> bool:
            if not provider.enforce_lunch_break:
                return True
            start_bound = max(day_window.start_minute, int(provider.lunch_earliest_start_minute))
            end_bound = min(day_window.end_minute - 30, int(provider.lunch_latest_start_minute))
            if end_bound < start_bound:
                return True
            provider_assignments = [a for a in list(solution.values()) + fixed_assignments if a.provider_id == provider.id]
            for start in range(start_bound, end_bound + 1, SLOT_MINUTES):
                end = start + 30
                if not provider.is_available(date_key, weekday, start, end):
                    continue
                occupied = any(not (end <= a.start_minute or start >= a.end_minute) for a in provider_assignments)
                if not occupied:
                    return True
            return False

        def dfs(index: int, assigned: Dict[str, Assignment], running_penalty: int) -> None:
            nonlocal best, best_score, states, conflict_logs
            states += 1
            if cancel_event is not None and hasattr(cancel_event, "is_set") and cancel_event.is_set():
                raise GenerationCancelledError("Generation cancelled.")
            elapsed = time.perf_counter() - solve_start_perf
            if elapsed > solve_timeout_seconds:
                raise SolveTimeoutError(
                    f"No solution found within {solve_timeout_seconds:g}s (timed out). "
                    f"Try relaxing constraints. elapsed={elapsed:.2f}s attempts={states} most_constrained={most_constrained}"
                )
            if states > backtrack_limit:
                raise UnschedulableError("Search limit reached while scheduling. Narrow windows or increase solver effort.")
            if index == len(sorted_ids):
                for provider in provider_by_id.values():
                    if not _has_lunch_slot(provider, assigned):
                        self._log_constraint_failure("lunch", f"provider {provider.id} has no 30-min lunch slot")
                        return
                if best_score is None or running_penalty < best_score:
                    best = dict(assigned)
                    best_score = running_penalty
                    self._log_decision(f"New best solution penalty={running_penalty} states={states}")
                return

            rid = sorted_ids[index]
            if progress_callback and states % 250 == 0:
                req = request_by_id[rid]
                progress_callback({
                    "stage": "search",
                    "elapsed": elapsed,
                    "attempts": states,
                    "request_id": rid,
                    "discipline": req.discipline,
                    "patients": list(req.patient_ids),
                })
            options = sorted(candidate_map[rid], key=lambda c: soft_score(rid, c))
            for option in options:
                if cancel_event is not None and hasattr(cancel_event, "is_set") and cancel_event.is_set():
                    raise GenerationCancelledError("Generation cancelled.")
                elapsed = time.perf_counter() - solve_start_perf
                if elapsed > solve_timeout_seconds:
                    raise SolveTimeoutError(
                        f"No solution found within {solve_timeout_seconds:g}s (timed out). "
                        f"Try relaxing constraints. elapsed={elapsed:.2f}s attempts={states} most_constrained={most_constrained}"
                    )
                reason = hard_conflict(rid, option, assigned)
                if reason:
                    if conflict_logs < 120:
                        self._log_constraint_failure(rid, reason)
                        conflict_logs += 1
                    continue
                next_penalty = running_penalty + soft_score(rid, option)
                if best_score is not None and next_penalty >= best_score:
                    continue
                assigned[rid] = option
                self._log_decision(f"Assign {rid} -> {option.room_id}@{option.start_minute} provider={option.provider_id}")
                dfs(index + 1, assigned, next_penalty)
                del assigned[rid]

        dfs(0, {}, 0)
        self._log_decision(f"Backtracking attempts={states}")
        if best is None:
            raise UnschedulableError("No full solution found for the day; all candidate combinations violate hard constraints.")
        return best

    def _log_constraint_failure(self, request_id: str, reason: str) -> None:
        message = f"constraint_failure request={request_id} reason={reason}"
        self.last_diagnostics.append(message)
        self.logger.debug(message)

    def _log_decision(self, message: str) -> None:
        tagged = f"decision {message}"
        self.last_diagnostics.append(tagged)
        self.logger.debug(tagged)

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
