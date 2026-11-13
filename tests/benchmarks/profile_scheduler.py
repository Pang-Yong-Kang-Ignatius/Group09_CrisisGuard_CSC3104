
import cProfile
import pstats

from benchmark_full_drain import benchmark_full_drain


def main():
    print("CrisisGuard Scheduler Profiling")
    print("=" * 65)

    print("Workload: 1000 events")
    print("Graph: Chain")
    print("Priority inheritance: ON")
    print()

    profiler = cProfile.Profile()

    # Start profiling
    profiler.enable()

    benchmark_full_drain(
        count=1000,
        shape="chain",
        inheritance=True
    )

    # Stop profiling
    profiler.disable()

    print("Top 20 functions by cumulative execution time:")
    print("-" * 65)

    # Show which functions consume the most time.
    stats = pstats.Stats(profiler)

    stats.strip_dirs()
    stats.sort_stats(pstats.SortKey.CUMULATIVE)
    stats.print_stats(20)

    print()
    print(
        "Note: Profiling adds overhead. "
        "Use the previous benchmarks for timing comparisons."
    )


if __name__ == "__main__":
    main()
