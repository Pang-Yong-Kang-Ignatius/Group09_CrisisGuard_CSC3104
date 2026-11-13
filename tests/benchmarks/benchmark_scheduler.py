
from datetime import datetime, timedelta, timezone
from statistics import median
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
            # Shallow dependency graph:
            # approximately 20% of events depend
            # on the immediately previous event.
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


def benchmark(events, inheritance):
    # Warm up before measuring.
    select_next_event(
        events,
        applied_ids=set(),
        now=NOW,
        enable_inheritance=inheritance
    )

    measurements = []

    for _ in range(5):
        start = perf_counter()

        selected = select_next_event(
            events,
            applied_ids=set(),
            now=NOW,
            enable_inheritance=inheritance
        )

        elapsed = perf_counter() - start

        assert selected is not None
        measurements.append(elapsed * 1000)

    return median(measurements)


def main():
    print("\nCrisisGuard Scheduler Benchmark")
    print("=" * 65)

    print(
        f"{'Events':>8} "
        f"{'Graph':<12} "
        f"{'Inheritance':<14} "
        f"{'Median (ms)':>14}"
    )

    print("-" * 65)

    for count in [100, 500, 1500, 5000]:
        for shape in ["chain", "shallow"]:
            events = generate_events(count, shape)

            for inheritance in [True, False]:
                elapsed = benchmark(events, inheritance)

                mode = "ON" if inheritance else "OFF"

                print(
                    f"{count:>8} "
                    f"{shape:<12} "
                    f"{mode:<14} "
                    f"{elapsed:>14.3f}"
                )


if __name__ == "__main__":
    main()
