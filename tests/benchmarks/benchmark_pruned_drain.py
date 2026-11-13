
from time import perf_counter

from benchmark_full_drain import generate_events, NOW
from edge.scheduler.scheduler import select_next_event


def drain(events, inheritance, prune_applied):
    applied_ids = set()
    selected_order = []

    start = perf_counter()

    for _ in range(len(events)):
        if prune_applied:
            # Keep only unfinished events.
            candidates = [
                event
                for event in events
                if event["event_id"] not in applied_ids
            ]
        else:
            # Original approach: pass the full backlog.
            candidates = events

        selected = select_next_event(
            candidates,
            applied_ids=applied_ids,
            now=NOW,
            enable_inheritance=inheritance
        )

        assert selected is not None

        event_id = selected["event_id"]

        # Correctness checks.
        assert event_id not in applied_ids

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
    print("\nCrisisGuard Applied-Event Pruning Benchmark")
    print("=" * 78)

    print(
        f"{'Events':>7} "
        f"{'Graph':<10} "
        f"{'Mode':<5} "
        f"{'Original (ms)':>15} "
        f"{'Pruned (ms)':>13} "
        f"{'Speedup':>9}"
    )

    print("-" * 78)

    for count in [500, 1000]:
        for shape in ["chain", "shallow"]:
            events = generate_events(count, shape)

            for inheritance in [True, False]:
                original_ms, original_order = drain(
                    events,
                    inheritance,
                    prune_applied=False
                )

                pruned_ms, pruned_order = drain(
                    events,
                    inheritance,
                    prune_applied=True
                )

                # Optimization must not change
                # the scheduling decisions.
                assert original_order == pruned_order, (
                    "Scheduling order changed after pruning!"
                )

                mode = "ON" if inheritance else "OFF"
                speedup = original_ms / pruned_ms

                print(
                    f"{count:>7} "
                    f"{shape:<10} "
                    f"{mode:<5} "
                    f"{original_ms:>15.3f} "
                    f"{pruned_ms:>13.3f} "
                    f"{speedup:>8.2f}x"
                )

    print("\nAll scheduling orders matched.")


if __name__ == "__main__":
    main()
