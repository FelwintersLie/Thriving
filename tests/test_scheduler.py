import unittest
from unittest.mock import patch

from app.scheduler import (
    GenerationCancelledError,
    Mode,
    Patient,
    Provider,
    ProviderException,
    ProviderTemplate,
    Room,
    ScheduleEngine,
    SessionRequest,
    TimeWindow,
)


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.date_key = "2026-02-11"
        self.weekday = 2
        self.engine = ScheduleEngine()

    def _core_inputs(self):
        providers = [
            Provider(
                id="pt",
                name="PT",
                disciplines={"pt"},
                templates=[ProviderTemplate("pt", self.weekday, [TimeWindow(8 * 60, 12 * 60)])],
            ),
            Provider(
                id="ot",
                name="OT",
                disciplines={"ot"},
                templates=[ProviderTemplate("ot", self.weekday, [TimeWindow(8 * 60, 12 * 60)])],
            ),
        ]
        patients = [
            Patient("a", "A", {self.date_key: [TimeWindow(8 * 60, 12 * 60)]}),
            Patient("b", "B", {self.date_key: [TimeWindow(8 * 60, 12 * 60)]}),
            Patient("c", "C", {self.date_key: [TimeWindow(8 * 60, 12 * 60)]}),
        ]
        rooms = [
            Room("gym", "Gym", 3, {"pt"}),
            Room("ot1", "OT1", 1, {"ot"}),
            Room("flex", "Flex", 2, {"pt", "ot"}),
        ]
        return providers, patients, rooms

    def test_respects_room_discipline_and_non_overlap(self):
        providers, patients, rooms = self._core_inputs()
        requests = [
            SessionRequest("r1", ("a",), "pt", 30, Mode.INDIVIDUAL, self.date_key),
            SessionRequest("r2", ("b",), "ot", 30, Mode.INDIVIDUAL, self.date_key),
            SessionRequest("r3", ("c",), "pt", 30, Mode.INDIVIDUAL, self.date_key),
        ]

        schedule = self.engine.generate_schedule(
            date_key=self.date_key,
            weekday=self.weekday,
            requests=requests,
            providers=providers,
            patients=patients,
            rooms=rooms,
            day_window=TimeWindow(8 * 60, 12 * 60),
        )

        self.assertEqual(set(schedule.keys()), {"r1", "r2", "r3"})
        self.assertIn(schedule["r2"].room_id, {"ot1", "flex"})

        # No provider double-booking
        intervals = {}
        for a in schedule.values():
            intervals.setdefault(a.provider_id, []).append((a.start_minute, a.end_minute))
        for chunks in intervals.values():
            chunks.sort()
            for i in range(1, len(chunks)):
                self.assertGreaterEqual(chunks[i][0], chunks[i - 1][1])

    def test_group_session_allows_multiple_patients_same_session(self):
        providers, patients, rooms = self._core_inputs()
        requests = [
            SessionRequest("grp", ("a", "b"), "pt", 60, Mode.GROUP, self.date_key, group_key="g1"),
            SessionRequest("solo", ("c",), "ot", 30, Mode.INDIVIDUAL, self.date_key),
        ]

        schedule = self.engine.generate_schedule(
            date_key=self.date_key,
            weekday=self.weekday,
            requests=requests,
            providers=providers,
            patients=patients,
            rooms=rooms,
            day_window=TimeWindow(8 * 60, 12 * 60),
        )

        self.assertEqual(schedule["grp"].mode, Mode.GROUP)
        self.assertEqual(schedule["grp"].room_id, "gym")

    def test_group_sessions_can_share_room_when_overlapping(self):
        providers, patients, rooms = self._core_inputs()
        requests = [
            SessionRequest("g1", ("a", "b"), "pt", 60, Mode.GROUP, self.date_key, group_key="g1"),
            SessionRequest("g2", ("c",), "pt", 60, Mode.GROUP, self.date_key, group_key="g2"),
        ]
        schedule = self.engine.generate_schedule(
            date_key=self.date_key,
            weekday=self.weekday,
            requests=requests,
            providers=providers,
            patients=patients,
            rooms=rooms,
            day_window=TimeWindow(8 * 60, 12 * 60),
        )
        self.assertEqual(schedule["g1"].room_id, schedule["g2"].room_id)

    def test_room_only_session_can_schedule_without_provider(self):
        _, patients, rooms = self._core_inputs()
        requests = [
            SessionRequest(
                "eval_group",
                ("a", "b"),
                "pt",
                60,
                Mode.GROUP,
                self.date_key,
                group_key="eval",
                provider_id="NO_PROVIDER",
                room_id="gym",
            )
        ]
        schedule = self.engine.generate_schedule(
            date_key=self.date_key,
            weekday=self.weekday,
            requests=requests,
            providers=[],
            patients=patients,
            rooms=rooms,
            day_window=TimeWindow(8 * 60, 12 * 60),
        )
        self.assertIsNone(schedule["eval_group"].provider_id)

    def test_reoptimization_prefers_minimal_changes(self):
        providers, patients, rooms = self._core_inputs()
        requests = [
            SessionRequest("r1", ("a",), "pt", 30, Mode.INDIVIDUAL, self.date_key),
            SessionRequest("r2", ("b",), "pt", 30, Mode.INDIVIDUAL, self.date_key),
            SessionRequest("r3", ("c",), "ot", 30, Mode.INDIVIDUAL, self.date_key),
        ]

        initial = self.engine.generate_schedule(
            date_key=self.date_key,
            weekday=self.weekday,
            requests=requests,
            providers=providers,
            patients=patients,
            rooms=rooms,
            day_window=TimeWindow(8 * 60, 12 * 60),
        )

        providers[0].exceptions.append(
            ProviderException(
                provider_id="pt",
                date_key=self.date_key,
                window=TimeWindow(8 * 60, 9 * 60),
                available_override=False,
            )
        )

        reworked = self.engine.generate_schedule(
            date_key=self.date_key,
            weekday=self.weekday,
            requests=requests,
            providers=providers,
            patients=patients,
            rooms=rooms,
            day_window=TimeWindow(8 * 60, 12 * 60),
            previous_assignments=initial,
        )

        self.assertEqual(initial["r3"], reworked["r3"], "Unrelated OT visit should stay stable")


    def test_room_unavailable_rule_blocks_slot(self):
        providers, patients, rooms = self._core_inputs()
        rooms[0].unavailable_weekly = {self.weekday: [TimeWindow(8 * 60, 10 * 60)]}
        requests = [SessionRequest("r1", ("a",), "pt", 30, Mode.INDIVIDUAL, self.date_key, room_id="gym")]

        with self.assertRaises(Exception) as ctx:
            self.engine.generate_schedule(
                date_key=self.date_key,
                weekday=self.weekday,
                requests=requests,
                providers=providers,
                patients=patients,
                rooms=rooms,
                day_window=TimeWindow(8 * 60, 9 * 60),
            )
        self.assertIn("room_rules", str(ctx.exception))

    def test_provider_allowed_rooms_constrain_candidates(self):
        providers, patients, rooms = self._core_inputs()
        providers[0].allowed_rooms = {"gym"}
        requests = [SessionRequest("r1", ("a",), "pt", 30, Mode.INDIVIDUAL, self.date_key)]

        schedule = self.engine.generate_schedule(
            date_key=self.date_key,
            weekday=self.weekday,
            requests=requests,
            providers=providers,
            patients=patients,
            rooms=rooms,
            day_window=TimeWindow(8 * 60, 12 * 60),
        )

        self.assertEqual(schedule["r1"].room_id, "gym")

    def test_rejects_invalid_day_window(self):
        providers, patients, rooms = self._core_inputs()
        requests = [SessionRequest("r1", ("a",), "pt", 30, Mode.INDIVIDUAL, self.date_key)]

        with self.assertRaises(ValueError):
            self.engine.generate_schedule(
                date_key=self.date_key,
                weekday=self.weekday,
                requests=requests,
                providers=providers,
                patients=patients,
                rooms=rooms,
                day_window=TimeWindow(600, 600),
            )

    def test_custom_backtrack_limit_is_supported(self):
        requests = [
            SessionRequest(
                id="r1",
                patient_ids=("a",),
                discipline="pt",
                duration_minutes=15,
                mode=Mode.INDIVIDUAL,
                date_key=self.date_key,
            )
        ]
        providers, patients, rooms = self._core_inputs()
        schedule = self.engine.generate_schedule(
            date_key=self.date_key,
            weekday=self.weekday,
            requests=requests,
            providers=providers,
            patients=patients,
            rooms=rooms,
            day_window=TimeWindow(8 * 60, 12 * 60),
            max_backtrack_states=500000,
            max_candidates_per_request=10000,
        )
        self.assertIn("r1", schedule)

    def test_previous_assignments_block_resources_across_programs(self):
        providers, patients, rooms = self._core_inputs()
        requests = [SessionRequest("r1", ("a",), "pt", 30, Mode.INDIVIDUAL, self.date_key)]
        from app.scheduler import Assignment

        previous = {
            "existing_eval": Assignment(
                request_id="existing_eval",
                provider_id="pt",
                room_id="gym",
                start_minute=8 * 60,
                end_minute=9 * 60,
                label="existing",
                mode=Mode.INDIVIDUAL,
            )
        }
        schedule = self.engine.generate_schedule(
            date_key=self.date_key,
            weekday=self.weekday,
            requests=requests,
            providers=providers,
            patients=patients,
            rooms=rooms,
            day_window=TimeWindow(8 * 60, 12 * 60),
            previous_assignments=previous,
        )
        self.assertGreaterEqual(schedule["r1"].start_minute, 9 * 60)

    def test_diagnostics_capture_constraint_failures(self):
        providers, patients, rooms = self._core_inputs()
        rooms[0].unavailable_weekly = {self.weekday: [TimeWindow(8 * 60, 12 * 60)]}
        request = SessionRequest("r_blocked", ("a",), "pt", 30, Mode.INDIVIDUAL, self.date_key, room_id="gym")
        with self.assertRaises(Exception):
            self.engine.generate_schedule(
                date_key=self.date_key,
                weekday=self.weekday,
                requests=[request],
                providers=providers,
                patients=patients,
                rooms=rooms,
                day_window=TimeWindow(8 * 60, 12 * 60),
            )
        self.assertTrue(any("constraint_failure" in d for d in self.engine.last_diagnostics))

    def test_room_tier_soft_penalty_prefers_lower_tier(self):
        providers, patients, rooms = self._core_inputs()
        rooms[0].room_preference_tier = 3
        rooms[2].room_preference_tier = 0
        requests = [SessionRequest("r_pref", ("a",), "pt", 30, Mode.INDIVIDUAL, self.date_key)]
        schedule = self.engine.generate_schedule(
            date_key=self.date_key,
            weekday=self.weekday,
            requests=requests,
            providers=providers,
            patients=patients,
            rooms=rooms,
            day_window=TimeWindow(8 * 60, 12 * 60),
        )
        self.assertEqual(schedule["r_pref"].room_id, "flex")

    def test_lunch_enforcement_requires_free_30_min_slot(self):
        providers, patients, rooms = self._core_inputs()
        providers[0].enforce_lunch_break = True
        providers[0].lunch_earliest_start_minute = 8 * 60
        providers[0].lunch_latest_start_minute = 8 * 60
        requests = [
            SessionRequest("r1", ("a",), "pt", 30, Mode.INDIVIDUAL, self.date_key),
            SessionRequest("r2", ("b",), "pt", 30, Mode.INDIVIDUAL, self.date_key),
        ]
        with self.assertRaises(Exception):
            self.engine.generate_schedule(
                date_key=self.date_key,
                weekday=self.weekday,
                requests=requests,
                providers=providers,
                patients=patients,
                rooms=rooms,
                day_window=TimeWindow(8 * 60, 9 * 60),
            )

    def test_timeout_aborts_with_clear_message(self):
        providers, patients, rooms = self._core_inputs()
        requests = [
            SessionRequest("r1", ("a",), "pt", 30, Mode.INDIVIDUAL, self.date_key),
            SessionRequest("r2", ("b",), "pt", 30, Mode.INDIVIDUAL, self.date_key),
            SessionRequest("r3", ("c",), "pt", 30, Mode.INDIVIDUAL, self.date_key),
        ]

        tick = {"n": -1}

        def fake_perf_counter():
            tick["n"] += 1
            return tick["n"] * 0.02

        with patch("app.scheduler.time.perf_counter", side_effect=fake_perf_counter):
            with self.assertRaises(Exception) as ctx:
                self.engine.generate_schedule(
                    date_key=self.date_key,
                    weekday=self.weekday,
                    requests=requests,
                    providers=providers,
                    patients=patients,
                    rooms=rooms,
                    day_window=TimeWindow(8 * 60, 12 * 60),
                    max_solve_seconds=0.03,
                )
        self.assertIn("timed out", str(ctx.exception))
        self.assertIn("attempts=", str(ctx.exception))

    def test_cancel_event_aborts_generation(self):
        providers, patients, rooms = self._core_inputs()
        requests = [SessionRequest("r1", ("a",), "pt", 30, Mode.INDIVIDUAL, self.date_key)]
        cancel_event = type("CancelToken", (), {"is_set": lambda self: True})()
        with self.assertRaises(GenerationCancelledError):
            self.engine.generate_schedule(
                date_key=self.date_key,
                weekday=self.weekday,
                requests=requests,
                providers=providers,
                patients=patients,
                rooms=rooms,
                day_window=TimeWindow(8 * 60, 12 * 60),
                cancel_event=cancel_event,
            )


if __name__ == "__main__":
    unittest.main()
