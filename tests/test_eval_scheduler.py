import unittest
from datetime import date

from app.auto_scheduler import build_eval_requests


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


if __name__ == "__main__":
    unittest.main()
