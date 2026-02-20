from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List

from app.api_server import handle_generate, run_server
from app.health_check import run_health_check
from app.profile_io import build_sample_profile
from app.scheduler import Assignment


def assignment_to_dict(assignment: Assignment) -> Dict[str, Any]:
    return {
        "request_id": assignment.request_id,
        "provider_id": assignment.provider_id,
        "room_id": assignment.room_id,
        "start_minute": assignment.start_minute,
        "end_minute": assignment.end_minute,
        "label": assignment.label,
        "mode": assignment.mode.value,
    }


def build_sample_payload(date_key: str = "2026-02-11", weekday: int = 2) -> Dict[str, Any]:
    return build_sample_profile(date_key=date_key, weekday=weekday)


def generate_sample_schedule() -> Dict[str, Any]:
    payload = build_sample_payload()
    return handle_generate(payload)


def save_json(path: Path, data: Dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def run_cli(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Windows-friendly therapy scheduler utility",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("demo", help="Generate sample schedule and print JSON summary")

    subparsers.add_parser("health", help="Run quick environment + sample scheduling health check")

    export = subparsers.add_parser("export", help="Generate sample schedule and save to JSON file")
    export.add_argument(
        "--out",
        default="output/sample_schedule.json",
        help="Output JSON path (default: output/sample_schedule.json)",
    )

    subparsers.add_parser("gui", help="Launch beginner-friendly desktop GUI")

    api = subparsers.add_parser("api", help="Run local API server")
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=8080)

    args = parser.parse_args(argv)

    if args.command == "health":
        result = run_health_check()
        print(json.dumps(result, indent=2))
        return 0 if result["all_ok"] else 1

    if args.command == "demo":
        result = generate_sample_schedule()
        print(
            json.dumps(
                {"assignment_count": len(result["assignments"]), "rooms": sorted(result["room_timeline"].keys())},
                indent=2,
            )
        )
        return 0

    if args.command == "export":
        result = generate_sample_schedule()
        path = save_json(Path(args.out), result)
        print(f"Saved schedule JSON to: {path}")
        return 0

    if args.command == "gui":
        from app.windows_gui import launch_gui

        return launch_gui()

    if args.command == "api":
        run_server(host=args.host, port=args.port)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(run_cli())
