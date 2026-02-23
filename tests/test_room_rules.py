import unittest

from app.room_rules import (
    is_room_discipline_compatible,
    normalize_room_rules,
    room_is_available,
    room_preference_tier,
    room_rule_violations,
    with_default_eval_group_reservation,
)


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

    def test_default_eval_reservation_blocks_conference_room_mon_tue_morning(self):
        rules = with_default_eval_group_reservation({"rooms": {"Conference Room": {}}})
        self.assertFalse(
            room_is_available(
                rules,
                room_id="Conference Room",
                date_key="2026-03-09",
                weekday=0,
                start_minute=8 * 60 + 30,
                end_minute=9 * 60,
            )
        )

    def test_room_discipline_compatibility_and_tier(self):
        rules = normalize_room_rules(
            {"rooms": {"Room 1": {"allowed_disciplines": ["Physical Therapy"], "room_preference_tier": 2}}},
            valid_rooms=["Room 1"],
        )
        self.assertTrue(is_room_discipline_compatible(rules, "Room 1", "Physical Therapy"))
        self.assertFalse(is_room_discipline_compatible(rules, "Room 1", "Audiology"))
        self.assertEqual(room_preference_tier(rules, "Room 1"), 2)


if __name__ == "__main__":
    unittest.main()
