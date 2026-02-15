import unittest

from app.api_server import handle_generate
from app.sample_day import build_sample_inputs


class ApiContractTests(unittest.TestCase):
    def test_generate_returns_assignments_and_timeline(self):
        date_key = "2026-02-11"
        weekday = 2
        providers, patients, rooms, requests = build_sample_inputs(date_key, weekday)

        payload = {
            "date_key": date_key,
            "weekday": weekday,
            "day_window": {"start_minute": 8 * 60, "end_minute": 16 * 60},
            "providers": [
                {
                    "id": p.id,
                    "name": p.name,
                    "disciplines": sorted(p.disciplines),
                    "templates": [
                        {
                            "weekday": t.weekday,
                            "windows": [
                                {
                                    "start_minute": w.start_minute,
                                    "end_minute": w.end_minute,
                                }
                                for w in t.windows
                            ],
                        }
                        for t in p.templates
                    ],
                    "exceptions": [],
                }
                for p in providers
            ],
            "patients": [
                {
                    "id": p.id,
                    "name": p.name,
                    "availability": {
                        day: [
                            {
                                "start_minute": w.start_minute,
                                "end_minute": w.end_minute,
                            }
                            for w in windows
                        ]
                        for day, windows in p.availability.items()
                    },
                }
                for p in patients
            ],
            "rooms": [
                {
                    "id": r.id,
                    "name": r.name,
                    "capacity": r.capacity,
                    "allowed_disciplines": sorted(r.allowed_disciplines),
                }
                for r in rooms
            ],
            "requests": [
                {
                    "id": r.id,
                    "patient_ids": list(r.patient_ids),
                    "discipline": r.discipline,
                    "duration_minutes": r.duration_minutes,
                    "mode": r.mode.value,
                    "date_key": r.date_key,
                    "preferred_window": {
                        "start_minute": r.preferred_window.start_minute,
                        "end_minute": r.preferred_window.end_minute,
                    }
                    if r.preferred_window
                    else None,
                    "group_key": r.group_key,
                    "label": r.label,
                }
                for r in requests
            ],
        }

        result = handle_generate(payload)

        self.assertEqual(len(result["assignments"]), len(requests))
        self.assertEqual(set(result["room_timeline"].keys()), {r.id for r in rooms})

        any_booked = any(
            slot["status"] == "booked"
            for room_slots in result["room_timeline"].values()
            for slot in room_slots
        )
        self.assertTrue(any_booked)


    def test_generate_rejects_unknown_patient_reference(self):
        payload = {
            "date_key": "2026-02-11",
            "weekday": 2,
            "day_window": {"start_minute": 480, "end_minute": 960},
            "providers": [],
            "patients": [{"id": "p1", "name": "P1", "availability": {"2026-02-11": [{"start_minute": 480, "end_minute": 960}]}}],
            "rooms": [],
            "requests": [
                {
                    "id": "r1",
                    "patient_ids": ["missing_patient"],
                    "discipline": "pt",
                    "duration_minutes": 30,
                    "mode": "individual",
                    "date_key": "2026-02-11",
                    "preferred_window": None,
                    "group_key": None,
                    "label": "Test",
                }
            ],
        }

        with self.assertRaises(ValueError):
            handle_generate(payload)


if __name__ == "__main__":
    unittest.main()
