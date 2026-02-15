import unittest

from app.windows_gui import DISCIPLINES, build_patient_grid_data, summarize_schedule


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

    def test_provider_discipline_catalog_contains_requested_items(self):
        self.assertIn("Physical Therapy", DISCIPLINES)
        self.assertIn("Yoga", DISCIPLINES)
        self.assertIn("PT/Audiology Group", DISCIPLINES)

    def test_build_patient_grid_data_maps_assignments_to_patient_slots(self):
        profile = {
            "patients": [{"id": "p1", "name": "Patient 1"}],
            "requests": [
                {
                    "id": "req_1",
                    "patient_ids": ["p1"],
                    "discipline": "Physical Therapy",
                }
            ],
        }
        result = {
            "assignments": {
                "req_1": {
                    "request_id": "req_1",
                    "start_minute": 450,
                    "end_minute": 480,
                    "label": "PT Block",
                }
            }
        }

        minutes, patients, cell_map = build_patient_grid_data(profile, result)
        self.assertEqual(patients, ["Patient 1"])
        self.assertIn(450, minutes)
        self.assertIn((450, "Patient 1"), cell_map)
        self.assertEqual(cell_map[(450, "Patient 1")]["discipline"], "Physical Therapy")


if __name__ == "__main__":
    unittest.main()
