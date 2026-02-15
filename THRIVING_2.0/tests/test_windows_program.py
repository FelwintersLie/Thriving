import json
import tempfile
import unittest
from pathlib import Path

from app.windows_program import build_sample_payload, generate_sample_schedule, save_json


class WindowsProgramTests(unittest.TestCase):
    def test_build_sample_payload_shape(self):
        payload = build_sample_payload()
        self.assertIn("providers", payload)
        self.assertIn("patients", payload)
        self.assertIn("rooms", payload)
        self.assertIn("requests", payload)
        self.assertEqual(payload["day_window"], {"start_minute": 480, "end_minute": 960})

    def test_generate_sample_schedule_contains_timeline(self):
        result = generate_sample_schedule()
        self.assertIn("assignments", result)
        self.assertIn("room_timeline", result)
        self.assertGreaterEqual(len(result["assignments"]), 1)

    def test_save_json_writes_file(self):
        data = {"ok": True}
        with tempfile.TemporaryDirectory() as temp_dir:
            out = Path(temp_dir) / "nested" / "result.json"
            save_json(out, data)
            loaded = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(loaded, data)


if __name__ == "__main__":
    unittest.main()
