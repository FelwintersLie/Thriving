import unittest

from app.windows_gui import (
    DEFAULT_PROVIDER_NAMES,
    PREDEFINED_ROOMS,
    build_live_result_from_profile,
    build_provider_availability_preview_data,
    build_patient_grid_data,
    build_provider_records,
    build_provider_records_from_profiles,
    build_room_records,
    military_time_choices,
    parse_time_input,
    summarize_schedule,
)


class WindowsGuiHelperTests(unittest.TestCase):
    def test_summarize_schedule_counts_booked_slots(self):
        fake = {
            "assignments": {"a": {"request_id": "a"}, "b": {"request_id": "b"}},
            "room_timeline": {"room1": [{"status": "booked"}, {"status": "free"}, {"status": "booked"}]},
        }
        summary = summarize_schedule(fake)
        self.assertEqual(summary.assignment_count, 2)
        self.assertEqual(summary.booked_slot_count, 2)

    def test_predefined_rooms_and_default_providers_present(self):
        self.assertIn("Gym", PREDEFINED_ROOMS)
        self.assertIn("Off-site", PREDEFINED_ROOMS)
        self.assertIn("Daniel Fenton", DEFAULT_PROVIDER_NAMES)
        self.assertIn("Devon Weist", DEFAULT_PROVIDER_NAMES)

    def test_build_room_records_uses_predefined_rooms(self):
        rooms = build_room_records()
        self.assertEqual(len(rooms), len(PREDEFINED_ROOMS))
        self.assertEqual(rooms[0]["id"], PREDEFINED_ROOMS[0])

    def test_build_provider_records_uses_names(self):
        providers = build_provider_records(["A", "B"], day_start=450, day_end=1080)
        self.assertEqual(providers[0]["id"], "A")
        self.assertEqual(providers[1]["name"], "B")

    def test_build_patient_grid_data_maps_assignments_to_patient_slots(self):
        profile = {
            "patients": [{"id": "p1", "name": "Patient 1"}],
            "requests": [{"id": "req_1", "patient_ids": ["p1"], "discipline": "Physical Therapy"}],
        }
        result = {
            "assignments": {
                "req_1": {"request_id": "req_1", "start_minute": 450, "end_minute": 480, "label": "PT Block"}
            }
        }

        minutes, patients, cell_map = build_patient_grid_data(profile, result)
        self.assertEqual(patients, ["p1"])
        self.assertIn(450, minutes)
        self.assertEqual(cell_map[(450, "p1")]["discipline"], "Physical Therapy")

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
        self.assertEqual(live["assignments"]["appt_1"]["provider_id"], "prov_1")
        self.assertEqual(live["assignments"]["appt_1"]["room_id"], "room_1")

    def test_military_time_choices_include_expected_values(self):
        values = military_time_choices()
        self.assertIn("0730", values)
        self.assertIn("1600", values)

    def test_build_provider_records_from_profiles_keeps_allowed_rooms(self):
        records = build_provider_records_from_profiles(
            [
                {
                    "provider_id": "p1",
                    "provider_name": "Provider 1",
                    "discipline": "Physical Therapy",
                    "allowed_rooms": ["Room 1"],
                    "availability_templates": [{"weekday": 0, "windows": [{"start_minute": 450, "end_minute": 510}]}],
                    "exceptions": [],
                }
            ],
            day_start=450,
            day_end=1080,
        )
        self.assertEqual(records[0]["allowed_rooms"], ["Room 1"])

    def test_build_provider_availability_preview_data_maps_windows(self):
        profile = {
            "availability_templates": [
                {"weekday": 0, "windows": [{"start_minute": 450, "end_minute": 510}]},
                {"weekday": 2, "windows": [{"start_minute": 600, "end_minute": 660}]},
            ]
        }
        preview = build_provider_availability_preview_data(profile, 450, 1080)
        self.assertEqual(preview[0][0], (450, 510))
        self.assertEqual(preview[2][0], (600, 660))



if __name__ == "__main__":
    unittest.main()
