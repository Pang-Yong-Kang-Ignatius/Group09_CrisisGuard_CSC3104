
from statistics import median
from time import perf_counter

from benchmark_full_drain import generate_events, NOW
from edge.scheduler.scheduler import select_next_event
from edge.scheduler.session import SchedulingSession


def benchmark_drain(events, inheritance, use_session):
    applied_ids = set()
    selected_order = []

    start = perf_counter()

    # Include session creation in its measured runtime.
    if use_session:
        session = SchedulingSession(
            events,
            enable_inheritance=inheritance
        )

    for _ in range(len(events)):
        if use_session:
            selected = session.select_next(
                applied_ids=applied_ids,
                now=NOW
            )
        else:
            selected = select_next_event(
                events,
                applied_ids=applied_ids,
                now=NOW,
                enable_inheritance=inheritance
            )

        assert selected is not None

        event_id = selected["event_id"]

        # Verify that no event is replayed twice.
        assert event_id not in applied_ids

        # Verify dependency ordering.
        assert all(
            prerequisite in applied_ids
            for prerequisite in selected["prerequisite_ids"]
        )

        selected_order.append(event_id)
        applied_ids.add(event_id)

    elapsed_ms = (perf_counter() - start) * 1000

    assert len(applied_ids) == len(events)

    return elapsed_ms, selected_order


def main():
    print("\nCrisisGuard SchedulingSession Benchmark")
    print("=" * 86)

    print(
        f"{'Events':>7} "
        f"{'Graph':<10} "
        f"{'Mode':<5} "
        f"{'Original (ms)':>15} "
        f"{'Session (ms)':>15} "
        f"{'Speedup':>10}"
    )

    print("-" * 86)

    for count in [100, 500, 1000]:
        for shape in ["chain", "shallow"]:
            events = generate_events(count, shape)

            for inheritance in [True, False]:
                original_times = []
                session_times = []

                # Repeat measurements to reduce noise.
                for _ in range(3):
                    original_ms, original_order = benchmark_drain(
                        events,
                        inheritance,
                        use_session=False
                    )

                    session_ms, session_order = benchmark_drain(
                        events,
                        inheritance,
                        use_session=True
                    )

                    # Performance improvements must
                    # preserve the exact event order.
                    assert original_order == session_order, (
                        "Scheduling order mismatch!"
                    )

                    original_times.append(original_ms)
                    session_times.append(session_ms)

                original_median = median(original_times)
                session_median = median(session_times)

                speedup = original_median / session_median

                mode = "ON" if inheritance else "OFF"

                print(
                    f"{count:>7} "
                    f"{shape:<10} "
                    f"{mode:<5} "
                    f"{original_median:>15.3f} "
                    f"{session_median:>15.3f} "
                    f"{speedup:>9.2f}x"
                )

    print("\nAll scheduling orders matched.")


if __name__ == "__main__":
    main()
