import unittest
from pathlib import Path

from app.persistence import (
    LAST_PROFILE_PATH,
    LAST_SCHEDULE_PATH,
    PROVIDER_CATALOG_PATH,
    LAST_GENERATED_SCHEDULE_PATH,
    PROVIDER_PROFILES_PATH,
    ROOM_RULES_PATH,
    REQUIREMENTS_CATALOG_PATH,
    STATE_PATH,
    load_last_generated_schedule,
    load_last_profile,
    load_provider_catalog_entries,
    load_provider_profiles,
    load_requirements_catalog,
    load_provider_catalog,
    load_state,
    save_last_generated_schedule,
    save_last_profile,
    save_provider_catalog_entries,
    save_provider_profiles,
    save_requirements_catalog,
    save_last_schedule,
    save_provider_catalog,
    load_room_rules,
    save_room_rules,
)


class PersistenceTests(unittest.TestCase):
    def tearDown(self):
        for path in [STATE_PATH, LAST_PROFILE_PATH, LAST_SCHEDULE_PATH, PROVIDER_CATALOG_PATH, REQUIREMENTS_CATALOG_PATH, PROVIDER_PROFILES_PATH, LAST_GENERATED_SCHEDULE_PATH, ROOM_RULES_PATH]:
            if path.exists():
                path.unlink()
        data_dir = Path("data")
        if data_dir.exists() and not any(data_dir.iterdir()):
            data_dir.rmdir()

    def test_profile_and_schedule_autosave(self):
        profile = {
            "date_key": "2026-02-11",
            "weekday": 2,
            "day_window": {"start_minute": 480, "end_minute": 960},
            "providers": [],
            "patients": [],
            "rooms": [],
            "requests": [],
        }
        schedule = {"assignments": {}, "room_timeline": {}}

        save_last_profile(profile, "profiles/test.json")
        save_last_schedule(schedule)

        self.assertEqual(load_last_profile(), profile)
        state = load_state()
        self.assertIn("last_profile_file", state)
        self.assertIn("last_schedule_file", state)


    def test_provider_catalog_persistence(self):
        defaults = ["A", "B"]
        loaded = load_provider_catalog(defaults)
        self.assertEqual(loaded, defaults)

        save_provider_catalog(["X", "Y", "Y"])
        loaded_again = load_provider_catalog(defaults)
        self.assertEqual(loaded_again, ["X", "Y"])


    def test_requirements_profiles_and_last_generated_persistence(self):
        reqs = [{"id": "r1", "discipline": "Speech-Language Pathology"}]
        profiles = [{"provider_id": "p1", "provider_name": "Provider 1", "discipline": "Speech-Language Pathology"}]
        generated = {"ok": True, "assignments": {"a": {"request_id": "a"}}}

        save_requirements_catalog(reqs)
        save_provider_profiles(profiles)
        save_last_generated_schedule(generated)

        self.assertEqual(load_requirements_catalog(), reqs)
        self.assertEqual(load_provider_profiles().get("providers"), profiles)
        self.assertEqual(load_last_generated_schedule(), generated)

    def test_provider_catalog_entries_roundtrip(self):
        entries = [
            {
                "provider_id": "p1",
                "provider_name": "Provider One",
                "discipline": "Speech-Language Pathology",
                "availability_templates": [{"weekday": 0, "windows": [{"start_minute": 480, "end_minute": 720}]}],
                "exceptions": [],
            }
        ]
        save_provider_catalog_entries(entries)
        loaded = load_provider_catalog_entries(["Fallback"])
        self.assertEqual(loaded[0]["provider_name"], "Provider One")
        self.assertEqual(loaded[0]["availability_templates"][0]["weekday"], 0)


    def test_room_rules_roundtrip(self):
        rules = {
            "rooms": {
                "Room 1": {
                    "unavailable_weekly": {"Monday": [{"start": 660, "end": 780}]},
                    "unavailable_dates": [{"date": "2026-03-10", "start": 480, "end": 720}],
                    "available_only_weekly": {},
                    "available_only_dates": [],
                }
            }
        }
        save_room_rules(rules, valid_rooms=["Room 1", "Room 2"])
        loaded = load_room_rules(["Room 1", "Room 2"])
        self.assertIn("Room 1", loaded["rooms"])
        self.assertEqual(loaded["rooms"]["Room 1"]["unavailable_weekly"][0][0]["start_minute"], 660)


if __name__ == "__main__":
    unittest.main()
