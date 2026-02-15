# iOS Therapy Scheduling App Blueprint

## 1. Product Goal
Build an iOS-first scheduling system for a care facility that:
- Schedules up to **6 patients** per day.
- Coordinates appointments across **14 rooms** with room-discipline constraints.
- Supports multiple disciplines and interventions (PT, OT, speech, athletic training, nursing interventions, etc.).
- Accounts for provider and patient availability, weekly templates, and one-off exceptions.
- Handles both **individual** and **group** sessions with room occupancy rules.
- Visualizes room activity in **15-minute intervals**.
- Can quickly re-optimize after disruptions (e.g., provider outage) while minimizing unnecessary schedule changes.

---

## 2. Core Scheduling Rules

### 2.1 Time Model
- Scheduling horizon: a single day at a time (with reusable weekly templates).
- Slot granularity: **15 minutes**.
- Represent a day as slots (e.g., 8:00–17:00 => 36 slots).

### 2.2 Entities
- **Patient**: availability windows, required sessions by discipline and duration.
- **Provider**: discipline(s), weekly recurring template, exceptions, and daily unavailability.
- **Room**: allowed discipline types, max capacity, equipment tags.
- **Session Request**: patient, discipline, duration, preferred windows, group/individual mode.
- **Scheduled Session**: assigned patient(s), provider, room, start/end slots, label/type.

### 2.3 Hard Constraints (must never be violated)
1. Provider cannot be double-booked.
2. Patient cannot be double-booked.
3. Room cannot exceed capacity.
4. Room-discipline compatibility must hold.
5. Individual session: room occupancy for that room+time cannot include another unrelated individual session.
6. Group session: room may host multiple patients only if all are assigned to the same group session instance.
7. Assigned slot must be inside both provider and patient available windows.
8. Duration must fit exact slot count.

### 2.4 Soft Constraints (optimize)
1. Minimize changes from existing schedule (stability objective).
2. Respect preferred windows (patient and provider preferences).
3. Minimize patient idle gaps.
4. Balance provider load.
5. Prefer discipline-specialized rooms where desired.

---

## 3. Data Model (Recommended)

Use SQLite locally (via GRDB/Core Data wrapper) with cloud sync later.

### 3.1 Tables / Models
- `patients(id, name, active)`
- `providers(id, name, disciplines_json, active)`
- `rooms(id, name, capacity, allowed_disciplines_json)`
- `provider_weekly_templates(id, provider_id, weekday, start_minute, end_minute, available)`
- `provider_exceptions(id, provider_id, date, start_minute, end_minute, available_override)`
- `patient_availability(id, patient_id, date, start_minute, end_minute, available)`
- `session_requests(id, patient_id, discipline, duration_min, date, preferred_start_minute, preferred_end_minute, mode)`
- `group_session_members(id, session_request_id, group_key)`
- `schedules(id, date, status, created_at, based_on_schedule_id)`
- `scheduled_sessions(id, schedule_id, session_request_id, provider_id, room_id, start_minute, end_minute, label, mode, is_locked)`

### 3.2 Useful Derived Structures
- Availability bitsets per day (96 bits for 24h at 15-min intervals).
- Compatibility matrix:
  - provider ↔ discipline
  - room ↔ discipline
  - patient ↔ slot

---

## 4. Scheduling Engine Architecture

### 4.1 Why a Constraint Solver
The problem is a constrained optimization problem. For reliability and maintainability:
- Use **Google OR-Tools CP-SAT** in a backend service.
- iOS app sends problem payload and receives schedule result.

For iOS-only MVP without backend, use a local heuristic solver first, then migrate.

### 4.2 Decision Variables
For each session request `s`, provider `p`, room `r`, slot start `t`:
- Binary variable `x[s,p,r,t] = 1` if session `s` starts at `t` with provider `p` in room `r`.

Optional:
- `unscheduled[s]` binary (only if partial scheduling allowed).
- `changed[s]` binary relative to prior schedule for stability penalty.

### 4.3 Constraints Encoding
- Exactly one assignment per required session:
  - `sum_{p,r,t} x[s,p,r,t] = 1`
- Provider non-overlap per slot.
- Patient non-overlap per slot.
- Room capacity and non-overlap rules by session mode.
- Compatibility filters remove illegal `(p,r,t)` variable candidates up front.
- Group sessions:
  - Shared start/provider/room across members of same `group_key`, or model as single multi-patient request.

### 4.4 Objective Function (weighted)
Minimize:
- `W1 * total_changed_assignments`
- `W2 * total_preference_violations`
- `W3 * total_patient_idle_time`
- `W4 * provider_load_imbalance`

Set `W1` highest during re-jigger events to preserve existing schedule.

---

## 5. Re-jigger / Rapid Re-Optimization

When a provider becomes unavailable during a timeframe:
1. Mark exception in `provider_exceptions`.
2. Freeze unaffected sessions where possible (`is_locked = true`) to preserve stability.
3. Identify impacted sessions only (provider overlap window + cascading conflicts).
4. Re-run solver with:
   - Hard constraints updated.
   - Large penalty for moving unaffected assignments.
5. Return diff view:
   - unchanged sessions
   - moved sessions
   - unscheduled sessions (if any)

This supports the “click one button and rework minimally” requirement.

---

## 6. iOS App Architecture

### 6.1 Stack
- **SwiftUI** for UI.
- **The Composable Architecture (TCA)** or MVVM for predictable state.
- **SQLite + GRDB** (or Core Data) for local persistence.
- Optional API layer for solver service.

### 6.2 Suggested Modules
- `Domain`: entities + business rules.
- `SchedulingEngineClient`: local heuristic and/or remote solver adapter.
- `DataStore`: persistence and template/exception CRUD.
- `Features`:
  - Patients
  - Providers
  - Rooms
  - Daily Scheduler
  - Reoptimize Action

### 6.3 Key Screens
1. **Day Grid View**
   - Time axis in 15-min increments.
   - Columns by room (14 rooms).
   - Session cards labeled by type/discipline.
2. **Provider Template Editor**
   - Weekly recurring blocks.
3. **Exceptions Editor**
   - One-off and date-range overrides.
4. **Patient Availability Editor**
5. **Session Request Builder**
6. **Re-optimize Confirmation + Diff Result**

---

## 7. Group vs Individual Session Behavior

### Individual
- Capacity requirement usually 1 patient per session.
- Room can host only one individual session at a time unless explicitly partitioned (optional advanced rule).

### Group
- Session object has one provider + one room + many patients.
- Room capacity checked against group size.
- Any patient conflict removes that patient from candidate set or blocks assignment, depending on policy.

---

## 8. MVP Delivery Plan

### Phase 1 (2–4 weeks): Foundations
- Data models for patients/providers/rooms/templates/exceptions.
- Manual schedule entry and day grid rendering in 15-minute intervals.
- Validation of room-discipline compatibility.

### Phase 2 (3–5 weeks): Auto-Scheduling
- Implement solver payload builder.
- Integrate CP-SAT service (or local heuristic fallback).
- One-click generate schedule.

### Phase 3 (2–3 weeks): Re-jigger + Stability
- Exception-triggered reoptimization.
- Change-minimization objective.
- Diff visualization and acceptance flow.

### Phase 4 (2 weeks): Reliability + UX
- Scenario tests for conflict-heavy days.
- Save/load snapshots, undo, and export.

---

## 9. Recommended Technical Decisions

1. **Represent all times as integer minutes from midnight** to avoid date math bugs.
2. **Precompute candidate assignments** to reduce solver variable count.
3. **Version schedules** so each re-jigger produces an auditable revision.
4. **Keep scheduling logic deterministic** (fixed random seed, stable tie-breaks).
5. **Start with max 6 patients but design schema for growth** so scaling to 20+ is not a rewrite.

---

## 10. Example Re-Optimization Workflow

1. Existing schedule has Provider A booked 10:00–14:00.
2. New event: Provider A unavailable 11:30–13:00.
3. User taps **Re-jigger Day**.
4. Engine locks unaffected assignments and re-solves impacted interval.
5. App presents:
   - 8 unchanged sessions
   - 2 moved sessions
   - 0 dropped sessions
6. User accepts and saves as new schedule version.

---

## 11. Risks and Mitigations

- **Over-constrained days** (no feasible solution):
  - Show reason codes (provider conflict, room mismatch, patient window limits).
  - Offer interactive relaxations (allow alternate provider, extend window).

- **Performance as complexity grows**:
  - Keep horizon to one day per solve.
  - Cache candidate sets.
  - Move solve to backend for larger deployments.

- **User trust**:
  - Always show before/after diff.
  - Explain why changes occurred.

---

## 12. Next Build Step
A practical next step is to scaffold an iOS app with:
- Day grid UI in 15-minute slots,
- local SQLite models,
- and a stub scheduling engine interface (`generateSchedule` and `reoptimizeSchedule`) so the solver can be integrated without redesigning the app.
