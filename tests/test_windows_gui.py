import unittest

from app.windows_gui import summarize_schedule


class WindowsGuiHelperTests(unittest.TestCase):
    def test_summarize_schedule_counts_booked_slots(self):
        fake = {
            "assignments": {
                "a": {"request_id": "a"},
                "b": {"request_id": "b"},
            },
            "room_timeline": {
                "room1": [
                    {"status": "booked"},
                    {"status": "free"},
                    {"status": "booked"},
                ],
                "room2": [
                    {"status": "free"},
                ],
            },
        }
        summary = summarize_schedule(fake)
        self.assertEqual(summary.assignment_count, 2)
        self.assertEqual(summary.room_count, 2)
        self.assertEqual(summary.booked_slot_count, 2)


if __name__ == "__main__":
    unittest.main()
