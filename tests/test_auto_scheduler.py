import unittest

from app.auto_scheduler import auto_reconfigure_schedule, explain_infeasibility, generate_three_week_schedule, preflight_check, validate_requirement


class AutoSchedulerTests(unittest.TestCase):
    def _template(self):
        date_keys = [
            "2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09",
            "2026-01-12", "2026-01-13", "2026-01-14", "2026-01-15", "2026-01-16",
            "2026-01-19", "2026-01-20", "2026-01-21", "2026-01-22", "2026-01-23",
        ]
        providers = [
            {
                "id": "Daniel Fenton",
                "name": "Daniel Fenton",
                "disciplines": ["Speech-Language Pathology"],
                "templates": [{"weekday": i, "windows": [{"start_minute": 480, "end_minute": 900}]} for i in range(5)],
                "exceptions": [],
            }
        ]
        rooms = [
            {
                "id": "Room 1",
                "name": "Room 1",
                "capacity": 6,
                "allowed_disciplines": ["Speech-Language Pathology", "Yoga"],
            }
        ]
        patients = [
            {
                "id": "1",
                "name": "Patient 1",
                "availability": {d: [{"start_minute": 480, "end_minute": 900}] for d in date_keys},
            },
            {
                "id": "2",
                "name": "Patient 2",
                "availability": {d: [{"start_minute": 480, "end_minute": 900}] for d in date_keys},
            },
        ]
        return {
            "planning_dates": date_keys,
            "day_window": {"start_minute": 450, "end_minute": 1080},
            "providers": providers,
            "rooms": rooms,
            "patients": patients,
        }

    def test_generate_three_week_schedule_hard_requirement(self):
        template = self._template()
        requirements = [
            {
                "id": "slp_mw",
                "discipline": "Speech-Language Pathology",
                "provider_id": "Daniel Fenton",
                "room_id": "Room 1",
                "duration_minutes": 60,
                "session_mode": "individual",
                "patient_scope": "all",
                "patient_ids": [],
                "weekdays": [0, 2],
                "weeks": [1, 2, 3],
                "time_windows": [{"start_minute": 480, "end_minute": 840}],
                "hard_constraint": True,
                "priority": 100,
            }
        ]

        result = generate_three_week_schedule(profile_template=template, requirements=requirements)
        self.assertTrue(result["ok"])
        self.assertGreater(len(result["assignments"]), 0)

    def test_explain_infeasibility_reports_provider_capacity(self):
        template = self._template()
        requirements = [
            {
                "id": "dense",
                "discipline": "Speech-Language Pathology",
                "provider_id": "Daniel Fenton",
                "room_id": "Room 1",
                "duration_minutes": 360,
                "session_mode": "individual",
                "patient_scope": "all",
                "patient_ids": [],
                "weekdays": [0],
                "weeks": [1, 2, 3],
                "time_windows": [{"start_minute": 480, "end_minute": 600}],
                "hard_constraint": True,
                "priority": 100,
            }
        ]

        report = explain_infeasibility(
            requirements,
            template["planning_dates"],
            template["providers"],
            template["rooms"],
            [p["id"] for p in template["patients"]],
        )
        self.assertFalse(report["ok"])
        self.assertTrue(any("demand" in issue or "incompatible" in issue for issue in report["issues"]))

    def test_auto_reconfigure_prefers_existing_assignments(self):
        template = self._template()
        requirements = [
            {
                "id": "slp_m",
                "discipline": "Speech-Language Pathology",
                "provider_id": "Daniel Fenton",
                "room_id": "Room 1",
                "duration_minutes": 60,
                "session_mode": "individual",
                "patient_scope": "single",
                "patient_ids": ["1"],
                "weekdays": [0],
                "weeks": [1],
                "time_windows": [{"start_minute": 480, "end_minute": 840}],
                "hard_constraint": True,
                "priority": 100,
            }
        ]
        first = generate_three_week_schedule(profile_template=template, requirements=requirements)
        second = auto_reconfigure_schedule(
            profile_template=template,
            requirements=requirements,
            existing_assignments=first["assignments"],
        )
        self.assertTrue(second["ok"])
        self.assertGreaterEqual(second["diff"]["unchanged"], 1)

    def test_requirement_allows_multiple_provider_options(self):
        template = self._template()
        # add second eligible provider
        template["providers"].append(
            {
                "id": "Heidi Greata",
                "name": "Heidi Greata",
                "disciplines": ["Speech-Language Pathology"],
                "templates": [{"weekday": i, "windows": [{"start_minute": 480, "end_minute": 900}]} for i in range(5)],
                "exceptions": [],
            }
        )
        requirements = [
            {
                "id": "slp_choice",
                "discipline": "Speech-Language Pathology",
                "provider_id": "any",
                "provider_ids": ["Daniel Fenton", "Heidi Greata"],
                "room_id": "Room 1",
                "duration_minutes": 60,
                "session_mode": "individual",
                "patient_scope": "single",
                "patient_ids": ["1"],
                "weekdays": [0],
                "weeks": [1],
                "time_windows": [{"start_minute": 480, "end_minute": 840}],
                "hard_constraint": True,
                "priority": 100,
            }
        ]
        result = generate_three_week_schedule(profile_template=template, requirements=requirements)
        self.assertTrue(result["ok"])
        providers_used = {a["provider_id"] for a in result["assignments"].values()}
        self.assertTrue(providers_used.intersection({"Daniel Fenton", "Heidi Greata"}))


    def test_validate_requirement_allows_partial_optional_fields(self):
        req = validate_requirement(
            {
                "id": "partial",
                "discipline": "Speech-Language Pathology",
                "duration_minutes": 60,
                "session_mode": "individual",
                "patient_scope": "all",
                "patient_ids": [],
                "weekdays": [0, 2],
                "weeks": [1, 2, 3],
                "time_windows": [{"start_minute": 480, "end_minute": 840}],
                "hard_constraint": True,
                "priority": 50,
            }
        )
        self.assertEqual(req["provider_id"], "any")
        self.assertEqual(req["room_id"], "any")
        self.assertEqual(req["sessions_per_week"], 1)

    def test_generate_three_week_schedule_respects_provider_exceptions(self):
        template = self._template()
        template["providers"][0]["exceptions"] = [
            {
                "date_key": "2026-01-05",
                "window": {"start_minute": 480, "end_minute": 900},
                "available_override": False,
            }
        ]
        requirements = [
            {
                "id": "slp_blocked",
                "discipline": "Speech-Language Pathology",
                "provider_id": "Daniel Fenton",
                "room_id": "Room 1",
                "duration_minutes": 60,
                "session_mode": "individual",
                "patient_scope": "single",
                "patient_ids": ["1"],
                "weekdays": [0],
                "weeks": [1],
                "time_windows": [{"start_minute": 480, "end_minute": 840}],
                "hard_constraint": True,
                "priority": 100,
            }
        ]
        result = generate_three_week_schedule(profile_template=template, requirements=requirements)
        self.assertFalse(result["ok"])

    def test_preflight_check_detects_missing_provider_capacity(self):
        template = self._template()
        requirements = [
            {
                "id": "no_provider",
                "discipline": "Psychiatry",
                "provider_id": "any",
                "room_id": "Room 1",
                "duration_minutes": 60,
                "session_mode": "individual",
                "patient_scope": "single",
                "patient_ids": ["1"],
                "weekdays": [0],
                "weeks": [1],
                "time_windows": [{"start_minute": 480, "end_minute": 840}],
                "hard_constraint": True,
                "priority": 100,
            }
        ]
        ok, issues, details = preflight_check(
            requirements,
            template["providers"],
            template["rooms"],
            rules={},
            date_range=template["planning_dates"],
            settings={"day_window": template["day_window"]},
        )
        self.assertFalse(ok)
        self.assertTrue(any("No eligible providers" in issue for issue in issues))
        self.assertIn("no_provider", details["requirements"])


    def test_failure_report_includes_ranked_bottlenecks(self):
        template = self._template()
        for d in list(template["patients"][0]["availability"].keys()):
            template["patients"][0]["availability"][d] = [{"start_minute": 480, "end_minute": 540}]
        requirements = [
            {
                "id": "r1",
                "discipline": "Speech-Language Pathology",
                "provider_id": "Daniel Fenton",
                "room_id": "Room 1",
                "duration_minutes": 60,
                "session_mode": "individual",
                "patient_scope": "single",
                "patient_ids": ["1"],
                "weekdays": [0],
                "weeks": [1],
                "time_windows": [{"start_minute": 480, "end_minute": 540}],
                "hard_constraint": True,
                "priority": 100,
            },
            {
                "id": "r2",
                "discipline": "Speech-Language Pathology",
                "provider_id": "Daniel Fenton",
                "room_id": "Room 1",
                "duration_minutes": 60,
                "session_mode": "individual",
                "patient_scope": "single",
                "patient_ids": ["1"],
                "weekdays": [0],
                "weeks": [1],
                "time_windows": [{"start_minute": 480, "end_minute": 540}],
                "hard_constraint": True,
                "priority": 100,
            },
        ]
        result = generate_three_week_schedule(profile_template=template, requirements=requirements)
        self.assertFalse(result["ok"])
        report = result.get("report", {}).get("bottleneck_report", {})
        self.assertEqual("No feasible schedule found", report.get("headline"))
        self.assertTrue(report.get("top_bottlenecks"))
        first = report["top_bottlenecks"][0]
        self.assertIn(first.get("reason_category"), {"ALL_CONFLICTS", "NO_TIME_SLOTS", "NO_PROVIDER", "NO_ROOM"})
        self.assertIn("provider_candidates", first)
        self.assertIn("room_candidates", first)
        self.assertIn("time_slot_candidates", first)


    def test_partial_schedule_opt_in_only(self):
        template = self._template()
        for d in list(template["patients"][0]["availability"].keys()):
            template["patients"][0]["availability"][d] = [{"start_minute": 480, "end_minute": 540}]
        requirements = [
            {
                "id": "p1",
                "discipline": "Speech-Language Pathology",
                "provider_id": "Daniel Fenton",
                "room_id": "Room 1",
                "duration_minutes": 60,
                "session_mode": "individual",
                "patient_scope": "single",
                "patient_ids": ["1"],
                "weekdays": [0],
                "weeks": [1],
                "time_windows": [{"start_minute": 480, "end_minute": 540}],
                "hard_constraint": True,
                "priority": 100,
            },
            {
                "id": "p2",
                "discipline": "Speech-Language Pathology",
                "provider_id": "Daniel Fenton",
                "room_id": "Room 1",
                "duration_minutes": 60,
                "session_mode": "individual",
                "patient_scope": "single",
                "patient_ids": ["1"],
                "weekdays": [0],
                "weeks": [1],
                "time_windows": [{"start_minute": 480, "end_minute": 540}],
                "hard_constraint": True,
                "priority": 100,
            },
        ]
        off_result = generate_three_week_schedule(profile_template=template, requirements=requirements, solver_limits={"enable_partial_schedule_on_failure": False})
        self.assertFalse(off_result["ok"])
        self.assertFalse(off_result.get("partial", False))
        self.assertEqual({}, off_result.get("assignments", {}))

        on_result = generate_three_week_schedule(profile_template=template, requirements=requirements, solver_limits={"enable_partial_schedule_on_failure": True})
        self.assertFalse(on_result["ok"])
        self.assertTrue(on_result.get("partial", False))
        self.assertGreaterEqual(len(on_result.get("assignments", {})), 1)

    def test_generate_three_week_schedule_returns_preflight_report(self):
        template = self._template()
        requirements = [
            {
                "id": "room_mismatch",
                "discipline": "Psychiatry",
                "provider_id": "Daniel Fenton",
                "room_id": "Room 1",
                "duration_minutes": 60,
                "session_mode": "individual",
                "patient_scope": "single",
                "patient_ids": ["1"],
                "weekdays": [0],
                "weeks": [1],
                "time_windows": [{"start_minute": 480, "end_minute": 840}],
                "hard_constraint": True,
                "priority": 100,
            }
        ]
        result = generate_three_week_schedule(profile_template=template, requirements=requirements)
        self.assertFalse(result["ok"])
        self.assertTrue(result.get("report", {}).get("preflight"))
        self.assertTrue(result.get("report", {}).get("issues"))



if __name__ == "__main__":
    unittest.main()
