from __future__ import annotations

import platform
import sys
from typing import Any, Dict

from app.api_server import handle_generate
from app.profile_io import build_sample_profile

MIN_PYTHON = (3, 10)


def _build_sample_payload(date_key: str = "2026-02-11", weekday: int = 2) -> Dict[str, Any]:
    return build_sample_profile(date_key=date_key, weekday=weekday)


def run_health_check() -> Dict[str, Any]:
    py_ok = sys.version_info >= MIN_PYTHON
    os_name = platform.system()

    schedule_result = handle_generate(_build_sample_payload())
    assignment_count = len(schedule_result["assignments"])
    room_count = len(schedule_result["room_timeline"])

    checks = {
        "python_version_ok": py_ok,
        "python_version": platform.python_version(),
        "os": os_name,
        "assignment_count": assignment_count,
        "room_count": room_count,
        "sample_run_ok": assignment_count > 0 and room_count > 0,
    }

    checks["all_ok"] = checks["python_version_ok"] and checks["sample_run_ok"]
    return checks


def print_human_report(result: Dict[str, Any]) -> None:
    print("=== Therapy Scheduler Health Check ===")
    print(f"OS: {result['os']}")
    print(f"Python: {result['python_version']} (min required: {MIN_PYTHON[0]}.{MIN_PYTHON[1]})")
    print(f"Sample assignment count: {result['assignment_count']}")
    print(f"Sample room timeline count: {result['room_count']}")
    print(f"Overall status: {'PASS' if result['all_ok'] else 'FAIL'}")


def main() -> int:
    result = run_health_check()
    print_human_report(result)
    return 0 if result["all_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
