import unittest
from datetime import date

from app.auto_scheduler import build_eval_requests, generate_combined_schedule


class EvalSchedulerTests(unittest.TestCase):
    def test_build_eval_requests_creates_first_session_group_in_conference_room(self):
        requests, dates = build_eval_requests(
            cohort_start=date(2026, 3, 2),
            cohort_type="Mon-Wed",
            eval_patient_ids=["E1", "E2"],
            group_duration_minutes=60,
            group_start_time=8 * 60 + 30,
        )
        self.assertEqual(len(dates), 3)
        group = requests[0]
        self.assertEqual(group["room_id"], "Conference Room")
        self.assertEqual(group["mode"], "group")
        self.assertEqual(group["patient_ids"], ["E1", "E2"])
        self.assertEqual(group["provider_id"], "NO_PROVIDER")
        self.assertTrue(group["appointment_id"].startswith("appt_"))

    def test_generate_combined_schedule_merges_iop_and_eval(self):
        template = {
            "planning_dates": ["2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05", "2026-03-06"],
            "day_window": {"start_minute": 450, "end_minute": 1080},
            "providers": [
                {
                    "id": "pt1",
                    "name": "PT",
                    "disciplines": ["Physical Therapy", "Evaluation Group"],
                    "templates": [{"weekday": i, "windows": [{"start_minute": 450, "end_minute": 1080}]} for i in range(5)],
                    "exceptions": [],
                }
            ],
            "rooms": [
                {"id": "Conference Room", "name": "Conference Room", "capacity": 12, "allowed_disciplines": ["Evaluation Group", "Physical Therapy"]},
                {"id": "Room 1", "name": "Room 1", "capacity": 12, "allowed_disciplines": ["Physical Therapy"]},
            ],
            "patients": [{"id": "I1", "name": "I1", "availability": {d: [{"start_minute": 450, "end_minute": 1080}] for d in ["2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05", "2026-03-06"]}}] + [
                {"id": "E1", "name": "E1", "availability": {d: [{"start_minute": 450, "end_minute": 1080}] for d in ["2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05", "2026-03-06"]}}
            ],
        }
        iop_requirements = [{
            "id": "iop_pt",
            "discipline": "Physical Therapy",
            "provider_id": "pt1",
            "room_id": "Room 1",
            "duration_minutes": 60,
            "session_mode": "individual",
            "patient_scope": "single",
            "patient_ids": ["I1"],
            "weekdays": [0],
            "weeks": [1],
            "time_windows": [{"start_minute": 600, "end_minute": 900}],
            "hard_constraint": True,
            "priority": 100,
        }]
        result = generate_combined_schedule(
            profile_template=template,
            iop_requirements=iop_requirements,
            cohort_start=date(2026, 3, 2),
            cohort_type="Mon-Wed",
            eval_patient_count=1,
            group_duration_minutes=60,
            group_start_time=8 * 60 + 30,
            template_sessions=[{"discipline": "Physical Therapy", "duration_minutes": 60, "count": 1}],
        )
        self.assertTrue(result["ok"])
        programs = {r.get("program_type") for r in result["requests"]}
        self.assertIn("EVAL", programs)


if __name__ == "__main__":
    unittest.main()
