import unittest

from app.room_rules import normalize_room_rules, room_is_available, room_rule_violations


class RoomRulesTests(unittest.TestCase):
    def test_normalize_room_rules_validates_and_fills_defaults(self):
        rules = normalize_room_rules(
            {
                "rooms": {
                    "Room 1": {
                        "unavailable_weekly": {"Monday": [{"start": 660, "end": 780}]},
                        "unavailable_dates": [{"date": "2026-03-10", "start": 480, "end": 720}],
                    }
                }
            },
            valid_rooms=["Room 1", "Room 2"],
        )
        self.assertIn("Room 2", rules["rooms"])
        self.assertEqual(rules["rooms"]["Room 1"]["unavailable_weekly"][0][0]["start_minute"], 660)

    def test_room_overlap_detection_for_unavailable_and_available_only(self):
        rules = {
            "rooms": {
                "Room 1": {
                    "unavailable_weekly": {0: [{"start_minute": 660, "end_minute": 780}]},
                    "unavailable_dates": [],
                    "available_only_weekly": {0: [{"start_minute": 480, "end_minute": 900}]},
                    "available_only_dates": [],
                }
            }
        }
        self.assertFalse(room_is_available(rules, room_id="Room 1", date_key="2026-03-09", weekday=0, start_minute=700, end_minute=730))
        self.assertTrue(room_is_available(rules, room_id="Room 1", date_key="2026-03-09", weekday=0, start_minute=800, end_minute=830))
        violations = room_rule_violations(rules, room_id="Room 1", date_key="2026-03-09", weekday=0, start_minute=930, end_minute=960)
        self.assertIn("room_available_only_weekly", violations)


if __name__ == "__main__":
    unittest.main()
