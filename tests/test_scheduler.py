import unittest

from app.scheduler import (
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


if __name__ == "__main__":
    unittest.main()
