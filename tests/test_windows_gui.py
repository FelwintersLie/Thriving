import unittest

from app.windows_gui import (
    DISCIPLINES,
    build_live_result_from_profile,
    build_patient_grid_data,
    military_time_choices,
    parse_time_input,
    summarize_schedule,
)


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


    def test_parse_time_input_military_time(self):
        self.assertEqual(parse_time_input("1600"), 960)
        self.assertEqual(parse_time_input("0730"), 450)

        with self.assertRaises(ValueError):
            parse_time_input("1661")


    def test_build_live_result_from_profile_uses_provider_room_and_times(self):
        profile = {
            "requests": [
                {
                    "id": "appt_1",
                    "discipline": "Physical Therapy",
                    "mode": "individual",
                    "provider_id": "prov_1",
                    "room_id": "room_1",
                    "preferred_window": {"start_minute": 450, "end_minute": 480},
                }
            ]
        }

        live = build_live_result_from_profile(profile)
        self.assertIn("appt_1", live["assignments"])
        self.assertEqual(live["assignments"]["appt_1"]["provider_id"], "prov_1")
        self.assertEqual(live["assignments"]["appt_1"]["room_id"], "room_1")

    def test_military_time_choices_include_expected_values(self):
        values = military_time_choices()
        self.assertIn("0730", values)
        self.assertIn("1600", values)


if __name__ == "__main__":
    unittest.main()
