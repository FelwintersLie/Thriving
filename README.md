# Therapy Scheduler MVP (Beginner-Friendly)

This repo is a **Windows-first proof of concept** with a click-through GUI.

## Are we stuck?
No. The project is operational as a local proof of concept right now. If you have zero coding background, use the exact beginner steps below.

## 10-minute beginner setup (no coding knowledge needed)

### Step 0: Download this project folder
- If someone sent you a zip, unzip it.
- Keep the folder somewhere easy (for example: `Desktop\Thriving`).

### Step 1: Install Python (one-time)
1. Go to <https://www.python.org/downloads/windows/>.
2. Install Python 3.11+.
3. **Important:** during install, check **"Add python.exe to PATH"**.

### Step 2: Open the project folder
- Open File Explorer and go into the project folder.
- You should see files like:
  - `setup_and_launch_windows.bat`
  - `launch_gui_windows.bat`
  - `run_healthcheck_windows.bat`

### Step 3: Double-click `setup_and_launch_windows.bat`
This does two things:
1. Runs health check.
2. Launches the GUI.

If Python is missing, it gives a readable error and tells you exactly what to install.

### Step 4: In the GUI, click buttons in this order
1. **Run Health Check**
2. **Save Sample Profile**
3. **Load Profile JSON**
4. **Generate From Loaded Profile**
5. **Export Generated Schedule**

That is the current no-code workflow.

---

## What this is right now
This is not a finished production app yet, but it includes:
- Scheduling engine (constraints + timeline labels).
- Desktop GUI (Tkinter) for non-coders.
- Local profile save/load from JSON.
- Health check and safer error popups.
- Optional local API and optional Windows EXE packaging.

---

## Profile JSON support
Profiles are local JSON files containing:
- providers
- patients
- rooms
- requests
- day window/date/weekday

So you can iterate without touching Python code:
1. Save sample profile from GUI.
2. Edit JSON in Notepad/VS Code.
3. Load profile in GUI.
4. Regenerate schedule.

---

## Quick troubleshooting (plain English)
- **"Python launcher not found"**
  - Install Python 3.11+ and ensure "Add python.exe to PATH" is checked.
- **GUI opens then errors**
  - Run `run_healthcheck_windows.bat` first and follow messages.
- **I exported but can't find my file**
  - Re-run export and pick a known folder like Desktop.
- **I want to start over**
  - Delete your exported JSON files and re-run using the sample profile.

---

## Safety notes ("won't break my computer")
- Standard Python only (no admin rights required for normal use).
- No registry editing.
- No system services installed.
- Files are local unless you manually share them.
- Easiest rollback: delete this project folder.

---

## CLI commands (optional)
```bat
python -m app.health_check
python -m app.windows_program health
python -m app.windows_program gui
python -m app.windows_program demo
python -m app.windows_program export --out output\sample_schedule.json
python -m app.windows_program api --host 127.0.0.1 --port 8080
```

Helper launchers:
- `setup_and_launch_windows.bat` (best first run)
- `run_healthcheck_windows.bat`
- `launch_gui_windows.bat`
- `run_demo_windows.bat`
- `export_schedule_windows.bat`
- `start_api_windows.bat`

---

## Build a single Windows executable (optional)
If you want one-file sharing for non-technical users:
1. Open PowerShell in repo root.
2. Install PyInstaller:
   ```powershell
   python -m pip install pyinstaller
   ```
3. Build EXE:
   ```powershell
   .\installer\windows\build_exe.ps1
   ```
4. Find output at:
   - `dist\TherapySchedulerPOC.exe`

More detail: `installer/windows/README.md`.

---

## Honest status
Operational as a beginner-friendly Windows POC with GUI + JSON profiles.
Next major step for layperson use is richer form-based data entry (instead of editing JSON directly) and persistent storage.


## New tonight: form-based editing + persistence
- You can now add **providers, patients, rooms, and requests** directly in the GUI using form fields (no manual JSON required for basic use).
- The app now auto-saves the most recent working profile to `data/last_profile.json` and restores it on next launch.
- The most recent generated schedule is auto-saved to `data/last_schedule.json`.

Recommended first test tonight:
1. Double-click `setup_and_launch_windows.bat`.
2. Click **New Blank Profile**.
3. Use form sections to add at least:
   - 1 provider
   - 1 patient
   - 1 room
   - 1 request
4. Click **Generate**.
5. Click **Export**.
6. Close and reopen GUI to verify your last profile is restored.

## Safety guardrails added (doom-loop prevention)
- Scheduling now enforces practical upper limits on request/provider/patient/room counts in this MVP.
- Candidate explosion is capped per request with a clear error telling you to narrow windows.
- Backtracking search has a maximum-state guard to prevent runaway solve attempts.
- Payload validation now catches missing keys and invalid patient references before scheduling.


## Visual update
- The GUI now renders a color-coded patient schedule grid (time down left from 7:30 AM to 6:00 PM, patient columns across top).
- Provider and request discipline selection now uses dropdown menus with the full requested discipline list.
- The profile panel now shows a cleaner summary instead of raw JSON to reduce under-the-hood noise.

- Time entry fields in the GUI now accept HHMM military-time input (examples: `0730`, `1100`, `1600`) and convert automatically to scheduler minutes.

- Appointment entry is now a single **Add Appointment** line with dropdowns for patient, provider, room, appointment type, begin time, and end time, and the schedule grid refreshes immediately after each add (no Generate click needed for visualization).

- Rooms are now a fixed predefined list shown only in Add Appointment, and provider selection is from a built-in provider-name catalog with Add New Provider / Remove Provider buttons that persist permanently.

- Calendar Settings now uses dropdowns for year/month/day and 15-minute start/end times, Add Appointment includes a date dropdown, and a beta Re-jigger rule can shift appointments across planning dates with auto-reorganization attempts.

- Added an **Auto Generator (3-week)** tab where you can set patient count, add scheduling conditions, and auto-generate a 3-week weekday schedule with bottleneck warnings when constraints cannot be met.
- Renamed Re-jigger to **Auto reconfigure** and added **Undo Auto reconfigure**.
- Auto Generator now includes a requirements builder for 3-week scheduling with hard/soft constraints, week selection, patient scope, multi-window time rules, and bottleneck reporting.
- Auto reconfigure existing schedule now runs a minimal-disruption pass and reports unchanged/moved/added/removed appointments.
- Requirements, provider profiles (availability templates/exceptions), and last generated 3-week schedules persist under `data/` for restart-safe planning.

### How to generate a 3-week schedule
1. Open **Auto Generator (3-week)** tab.
2. Select patient count and start date (Monday recommended).
3. Build one or more requirements (discipline/provider/room/mode/duration/weekdays/weeks/windows/hard-soft).
4. Click **Generate 3-week Schedule**.
5. Use the report panel to review bottlenecks if generation fails.
6. If requirements change, click **Auto reconfigure existing schedule** to preserve as many existing appointments as possible.
7. Use **Undo Auto reconfigure** from the manual tab to roll back one step.


### Auto Generator controls explained
- **W1 / W2 / W3**: choose which week(s) in the 3-week horizon the requirement applies to (week 1, week 2, week 3).
- **Appointment Window Start / Appointment Window End**: define one allowable scheduling range for that requirement (in HHMM, 15-minute increments).
- **Add Appointment Window**: add another allowed scheduling range for the same requirement (for example `0800-1400` and `1600-1700`).
- **Appointment Windows**: shows all allowed appointment windows currently attached to the requirement being built.
- **Hard constraint**: if enabled, the scheduler treats the requirement as mandatory. If disabled, it is a soft preference and may be skipped when infeasible.
- **Priority**: relative importance score used for soft preference handling (higher means more important).


- Manual Scheduler now supports **Undo Manual Action** (up to 10 recent actions), including appointment add/delete and key manual edits.
- You can now right-click an appointment block in the grid to select it, then click **Delete Appointment** to remove it.
- Manual Scheduler auto-reconfigure controls were removed; reconfiguration remains in the Auto Generator area.
- The manual grid now shows appointments across planning dates in one horizontal timeline (date + patient columns), so you can scroll left/right across days.

- Requirements Builder now labels recurrence selectors as **Weekday 1..5** (with `None` available for Weekday 2-5).
- After Auto Generator runs, the produced schedule is loaded into Manual Scheduler so you can review and edit it manually.

- **Solver Effort** (Auto Generator): increases search/candidate limits so complex schedules can use more compute before hitting search-limit errors.
- **Provider Availability Window** (Provider List Management): sets weekday availability windows for selected providers, separate from appointment requirement windows.


- Auto Generator now has two linked builders:
  - **Section A: Appointment Requirements** (required sessions/frequency/windows/priority),
  - **Section B: Provider Availability Rules** (weekly availability templates by provider + live Mon-Fri preview grid).

- Unified scheduling now supports both program types in one appointment model (`program_type`, `appointment_id`, `soft_locked`) with IOP + EVAL combined generation and move reporting.
- EVAL day-1 intake group can be scheduled as a room-only session (no provider resource), and Conference Room Monday/Tuesday 08:30-11:00 reservation can be enforced via room rules defaults.
- Requirement rows now support add/edit/duplicate/remove and include **sessions per week**.
- Provider availability rules now persist in the provider catalog schema so updates are reused by the generator immediately.


### Provider Profiles (single source of truth)
- Use the **Provider Profiles** tab to manage all provider data used by both Manual Scheduler and Auto Generator.
- Each profile stores:
  - stable `provider_id` + editable `provider_name`,
  - up to 5 disciplines,
  - allowed rooms,
  - weekly availability templates (Mon-Fri, multiple windows/day),
  - date exceptions (`unavailable` or `added` windows).
- Save/Update edits profiles in place and persists them to both `data/provider_catalog.json` and `data/provider_profiles.json`.
- Manual Scheduler integration:
  - Add Appointment provider dropdown now pulls from Provider Profiles.
  - Selecting a provider auto-fills discipline (if profile discipline is set) and filters room options to allowed rooms.
  - Appointment add validates provider availability using selected date + weekly template + exceptions.
- Auto Generator integration:
  - Requirement provider dropdowns pull from Provider Profiles.
  - `Any provider` + discipline only selects providers with matching profile discipline.
  - Solver respects provider availability and provider allowed-room constraints when generating assignments.


### Room Availability Restrictions
- Use the **Room Rules** tab to define room-level constraints that are enforced in both Manual Scheduler and Auto Generator.
- Rule types:
  - **unavailable**: room cannot be used during overlapping windows,
  - **available-only**: room can only be used inside allowed windows.
- Scopes:
  - weekly templates (Mon-Fri, multiple windows/day),
  - date-specific exceptions (`YYYY-MM-DD`, multiple windows/day).
- Saved in `data/room_rules.json` and applied immediately.
- Manual Scheduler blocks Add Appointment when selected room violates room rules.
- Auto-generator treats room rules as hard constraints and includes room-rule rejection counts in infeasibility details.
- Core day scheduler now uses a constraint-based optimization/backtracking engine with composable hard constraints (room/provider/patient/resource) and soft scoring (stability/preferences), plus diagnostics logs for constraint failures and scheduling decisions.


### IOP + EVAL Integrated Scheduling
- The app now supports a unified appointment model for both programs with patient IDs:
  - **IOP**: `I1..I30`
  - **EVAL**: `E1..E10`
- Each appointment carries program metadata (`program_type`), stable `appointment_id`, and `soft_locked` support for imported schedules.
- New **EVAL Generator** tab supports:
  - cohort selection (`Mon-Wed` or `Tue-Thu`),
  - EVAL patient count,
  - day-1 eval group start (`0830` / `0930` / `1000`) and duration (up to 4 hours),
  - an EVAL requirements builder with add/edit/remove workflow matching IOP (without week selectors).
  - combined generation alongside IOP.
- IOP requirement duration builder also supports durations up to 4 hours.
- IOP requirements list includes optional read-only visualization of EVAL requirements via **Show EVAL Requirements** toggle (`[IOP]` blue rows, `[EVAL]` red rows) without persisting duplicates.
- New **Generate Combined Schedule** mode tries to place IOP and EVAL together while honoring existing soft-locked appointments.
- New manual-grid controls support:
  - Program filter (`IOP` / `EVAL` / `Both`),
  - Grid mode switch (`Patient Grid` / `Room Grid` / `Provider Grid`).
- Import existing schedules via EVAL tab (**Import Existing Schedule JSON**); imported appointments are marked `soft_locked=true` by default.

### Export Current Schedule View (PNG + PowerPoint)

- In **Manual Scheduler**, use:
  - **Export Schedule as PNG** to save the currently selected date + active grid mode + program filter as a deterministic rendered PNG.
  - **Export as PowerPoint** to export the same layout into a PowerPoint slide with title metadata (`date | view mode | filter`).
- Export rendering uses the same shared layout model as the on-screen canvas, preserving:
  - merged appointment blocks by duration,
  - time/header geometry and column layout,
  - discipline colors,
  - appointment text (`discipline`, `room`, `provider`, `program`).
- Exports run in a background thread to keep the UI responsive.
- Dependency notes (if not already installed):
  - `python3 -m pip install Pillow` for PNG export
  - `python3 -m pip install python-pptx` for PPTX export
