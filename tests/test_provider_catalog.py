import unittest

from app.provider_catalog import normalize_provider_catalog, provider_is_available


class ProviderCatalogTests(unittest.TestCase):
    def test_normalize_provider_catalog_keeps_stable_id_on_rename(self):
        entries = [
            {
                "provider_id": "provider_001",
                "provider_name": "Old Name",
                "discipline": "Physical Therapy",
                "allowed_rooms": ["Room 1"],
                "availability_templates": [{"weekday": 0, "windows": [{"start_minute": 480, "end_minute": 600}]}],
                "exceptions": [],
            }
        ]
        normalized = normalize_provider_catalog(
            entries,
            defaults=[],
            all_rooms=["Room 1", "Room 2"],
            disciplines=["Physical Therapy", "Speech-Language Pathology"],
        )
        normalized[0]["provider_name"] = "New Name"
        normalized2 = normalize_provider_catalog(
            normalized,
            defaults=[],
            all_rooms=["Room 1", "Room 2"],
            disciplines=["Physical Therapy", "Speech-Language Pathology"],
        )
        self.assertEqual(normalized2[0]["provider_id"], "provider_001")

    def test_provider_is_available_respects_unavailable_and_added_exceptions(self):
        profile = {
            "availability_templates": [{"weekday": 0, "windows": [{"start_minute": 480, "end_minute": 600}]}],
            "exceptions": [
                {"date": "2026-01-05", "kind": "unavailable", "start_minute": 510, "end_minute": 540},
                {"date": "2026-01-05", "kind": "added", "start_minute": 600, "end_minute": 660},
            ],
        }
        self.assertTrue(provider_is_available(profile, date_key="2026-01-05", weekday=0, start_minute=480, end_minute=510))
        self.assertFalse(provider_is_available(profile, date_key="2026-01-05", weekday=0, start_minute=510, end_minute=525))
        self.assertTrue(provider_is_available(profile, date_key="2026-01-05", weekday=0, start_minute=615, end_minute=630))

    def test_provider_with_no_templates_and_no_exceptions_is_available(self):
        profile = {"availability_templates": [], "exceptions": []}
        self.assertTrue(provider_is_available(profile, date_key="2026-01-05", weekday=0, start_minute=480, end_minute=510))

    def test_enforced_lunch_range_blocks_overlap(self):
        profile = {
            "availability_templates": [{"weekday": 0, "windows": [{"start_minute": 450, "end_minute": 1080}]}],
            "exceptions": [],
            "enforce_lunch_break": True,
            "lunch_earliest_start_minute": 720,
            "lunch_latest_start_minute": 780,
        }
        self.assertFalse(provider_is_available(profile, date_key="2026-01-05", weekday=0, start_minute=750, end_minute=780))
        self.assertTrue(provider_is_available(profile, date_key="2026-01-05", weekday=0, start_minute=810, end_minute=840))


if __name__ == "__main__":
    unittest.main()
