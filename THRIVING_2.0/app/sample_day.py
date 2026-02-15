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
    format_assignment,
)


def build_sample_inputs(date_key: str = "2026-02-11", weekday: int = 2):
    providers = [
        Provider(
            id="prov_pt_1",
            name="Alex PT",
            disciplines={"physical_therapy"},
            templates=[ProviderTemplate("prov_pt_1", weekday, [TimeWindow(8 * 60, 16 * 60)])],
        ),
        Provider(
            id="prov_ot_1",
            name="Jamie OT",
            disciplines={"occupational_therapy"},
            templates=[ProviderTemplate("prov_ot_1", weekday, [TimeWindow(8 * 60, 16 * 60)])],
        ),
    ]

    patients = []
    for i in range(1, 7):
        patients.append(
            Patient(
                id=f"pat_{i}",
                name=f"Patient {i}",
                availability={date_key: [TimeWindow(8 * 60, 16 * 60)]},
            )
        )

    rooms = [
        Room("room_gym", "Gym", 6, {"physical_therapy", "athletic_training"}),
        Room("room_ot", "OT Room", 2, {"occupational_therapy"}),
        Room("room_flex", "Flex Room", 2, {"physical_therapy", "occupational_therapy"}),
    ]

    requests = [
        SessionRequest(
            id="req_pt_1",
            patient_ids=("pat_1",),
            discipline="physical_therapy",
            duration_minutes=45,
            mode=Mode.INDIVIDUAL,
            date_key=date_key,
            preferred_window=TimeWindow(8 * 60, 11 * 60),
            label="PT Individual",
        ),
        SessionRequest(
            id="req_ot_1",
            patient_ids=("pat_2",),
            discipline="occupational_therapy",
            duration_minutes=30,
            mode=Mode.INDIVIDUAL,
            date_key=date_key,
            preferred_window=TimeWindow(9 * 60, 12 * 60),
            label="OT Individual",
        ),
        SessionRequest(
            id="req_pt_group",
            patient_ids=("pat_3", "pat_4"),
            discipline="physical_therapy",
            duration_minutes=60,
            mode=Mode.GROUP,
            group_key="grp_am_pt",
            date_key=date_key,
            preferred_window=TimeWindow(10 * 60, 13 * 60),
            label="PT Group",
        ),
    ]

    return providers, patients, rooms, requests


def main() -> None:
    date_key = "2026-02-11"
    weekday = 2
    engine = ScheduleEngine()

    providers, patients, rooms, requests = build_sample_inputs(date_key, weekday)

    print("=== Initial schedule ===")
    initial = engine.generate_schedule(
        date_key=date_key,
        weekday=weekday,
        requests=requests,
        providers=providers,
        patients=patients,
        rooms=rooms,
        day_window=TimeWindow(8 * 60, 16 * 60),
    )
    for assignment in initial.values():
        print("-", format_assignment(assignment))

    # Simulate disruption: PT provider unavailable 10:30-12:00
    providers[0].exceptions.append(
        ProviderException(
            provider_id="prov_pt_1",
            date_key=date_key,
            window=TimeWindow(10 * 60 + 30, 12 * 60),
            available_override=False,
        )
    )

    print("\n=== Re-optimized schedule after provider outage ===")
    reworked = engine.generate_schedule(
        date_key=date_key,
        weekday=weekday,
        requests=requests,
        providers=providers,
        patients=patients,
        rooms=rooms,
        day_window=TimeWindow(8 * 60, 16 * 60),
        previous_assignments=initial,
    )

    for req_id in sorted(reworked.keys()):
        old = initial[req_id]
        new = reworked[req_id]
        marker = "UNCHANGED" if old == new else "MOVED"
        print(f"- [{marker}] {format_assignment(new)}")


if __name__ == "__main__":
    main()
