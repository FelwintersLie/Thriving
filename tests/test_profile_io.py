import tempfile
import unittest
from pathlib import Path

from app.profile_io import build_sample_profile, load_profile, save_profile, validate_profile


class ProfileIoTests(unittest.TestCase):
    def test_sample_profile_has_required_keys(self):
        profile = build_sample_profile()
        validate_profile(profile)
        self.assertIn("providers", profile)
        self.assertIn("patients", profile)
        self.assertIn("rooms", profile)
        self.assertIn("requests", profile)

    def test_save_and_load_profile_roundtrip(self):
        profile = build_sample_profile()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "profile.json"
            save_profile(path, profile)
            loaded = load_profile(path)
            self.assertEqual(loaded["weekday"], profile["weekday"])
            self.assertEqual(len(loaded["providers"]), len(profile["providers"]))

    def test_validate_profile_rejects_missing_keys(self):
        bad = {"providers": []}
        with self.assertRaises(ValueError):
            validate_profile(bad)


if __name__ == "__main__":
    unittest.main()
