
from datetime import datetime, timedelta, timezone
from time import perf_counter

from edge.scheduler.scheduler import select_next_event


NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


def generate_events(count, shape):
    events = []

    for i in range(count):
        if shape == "chain":
            prerequisites = (
                [f"E{i - 1:05d}"] if i > 0 else []
            )
        else:
            # Mostly independent events,
            # with occasional dependencies.
            prerequisites = (
                [f"E{i - 1:05d}"] if i % 5 == 1 else []
            )

        events.append({
            "event_id": f"E{i:05d}",
            "base_priority": 1 + (i % 10),
            "prerequisite_ids": prerequisites,
            "status": "pending",
            "created_at": (
                NOW - timedelta(minutes=i % 10)
            ).isoformat()
        })

    return events


def benchmark_full_drain(count, shape, inheritance):
    events = generate_events(count, shape)

    # Simulates events confirmed applied in the cloud.
    applied_ids = set()

    start = perf_counter()

    for _ in range(count):
        selected = select_next_event(
            events,
            applied_ids=applied_ids,
            now=NOW,
            enable_inheritance=inheritance
        )

        if selected is None:
            raise AssertionError(
                "Backlog could not be fully drained"
            )

        event_id = selected["event_id"]

        # The same event must never be selected twice.
        assert event_id not in applied_ids

        # Every prerequisite must already be applied.
        assert all(
            prerequisite in applied_ids
            for prerequisite in selected["prerequisite_ids"]
        )

        # Simulate successful cloud acknowledgement.
        applied_ids.add(event_id)

    elapsed_ms = (perf_counter() - start) * 1000

    assert len(applied_ids) == count

    return elapsed_ms


def main():
    print("\nCrisisGuard Full-Drain Scheduler Benchmark")
    print("=" * 75)

    print(
        f"{'Events':>8} "
        f"{'Graph':<12} "
        f"{'Inheritance':<14} "
        f"{'Total (ms)':>14} "
        f"{'Avg/Event (ms)':>16}"
    )

    print("-" * 75)

    for count in [100, 500, 1000]:
        for shape in ["chain", "shallow"]:
            for inheritance in [True, False]:
                total_ms = benchmark_full_drain(
                    count,
                    shape,
                    inheritance
                )

                mode = "ON" if inheritance else "OFF"

                print(
                    f"{count:>8} "
                    f"{shape:<12} "
                    f"{mode:<14} "
                    f"{total_ms:>14.3f} "
                    f"{total_ms / count:>16.4f}"
                )


if __name__ == "__main__":
    main()
