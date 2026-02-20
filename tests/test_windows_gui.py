import unittest

from app.windows_gui import (
    DEFAULT_PROVIDER_NAMES,
    PREDEFINED_ROOMS,
    DISCIPLINE_OPTIONS,
    ROOM_OPTIONS,
    PATIENT_ID_CHOICES,
    patient_sort_key,
    provider_names_for_discipline,
    build_live_result_from_profile,
    build_provider_availability_preview_data,
    build_patient_grid_data,
    build_provider_records,
    days_in_month,
    format_date_short,
    build_provider_records_from_profiles,
    build_room_records,
    military_time_choices,
    parse_date_parts,
    normalize_restored_profile,
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
        self.assertEqual(rooms[0]["id"], sorted(PREDEFINED_ROOMS)[0])

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


    def test_default_provider_preview_is_all_day_when_no_templates_or_exceptions(self):
        profile = {"availability_templates": [], "exceptions": []}
        preview = build_provider_availability_preview_data(profile, 450, 1080)
        for weekday in range(5):
            self.assertEqual(preview[weekday], [(450, 1080)])


    def test_sorted_ui_option_lists(self):
        self.assertEqual(DISCIPLINE_OPTIONS, sorted(DISCIPLINE_OPTIONS))
        self.assertEqual(ROOM_OPTIONS, sorted(ROOM_OPTIONS))

    def test_provider_names_for_discipline_filters_and_sorts(self):
        names = provider_names_for_discipline(
            [
                {"provider_name": "Zed", "discipline": "Physical Therapy"},
                {"provider_name": "amy", "discipline": "Physical Therapy"},
                {"provider_name": "Bob", "discipline": "Psychiatry"},
            ],
            "Physical Therapy",
        )
        self.assertEqual(names, ["amy", "Zed"])

    def test_days_in_month_handles_leap_year(self):
        self.assertEqual(days_in_month(2024, 2), 29)
        self.assertEqual(days_in_month(2023, 2), 28)

    def test_format_date_short(self):
        self.assertEqual(format_date_short("2026-12-25"), "Fri 12/25/26")

    def test_parse_date_parts_rejects_invalid_date(self):
        with self.assertRaises(ValueError):
            parse_date_parts("2026", "02", "31")


    def test_patient_id_choices_and_sort_key(self):
        self.assertEqual(PATIENT_ID_CHOICES[0], "IOP1")
        self.assertIn("IOP15", PATIENT_ID_CHOICES)
        self.assertIn("EV10", PATIENT_ID_CHOICES)
        ordered = sorted(["EV2", "IOP10", "IOP2", "EV1"], key=patient_sort_key)
        self.assertEqual(ordered, ["IOP2", "IOP10", "EV1", "EV2"])


    def test_normalize_restored_profile_handles_missing_planning_dates(self):
        profile = {"date_key": "2026-01-05", "day_window": {"start_minute": 450, "end_minute": 1080}}
        normalized = normalize_restored_profile(profile)
        self.assertEqual(normalized["date_key"], "2026-01-05")
        self.assertTrue(len(normalized["planning_dates"]) >= 5)

    def test_normalize_restored_profile_rejects_bad_day_window(self):
        with self.assertRaises(ValueError):
            normalize_restored_profile({"date_key": "2026-01-05", "day_window": {"start_minute": "bad", "end_minute": 1080}})


if __name__ == "__main__":
    unittest.main()
