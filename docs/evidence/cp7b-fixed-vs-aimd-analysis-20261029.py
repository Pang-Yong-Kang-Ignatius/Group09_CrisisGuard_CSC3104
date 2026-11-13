"""Summarise and graph the raw Checkpoint 7B policy-comparison evidence."""
from __future__ import annotations

import argparse
import csv
import itertools
import json
import math
import random
import statistics
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


POLICIES = ("fixed", "aimd")
COLORS = {"fixed": "#3569a8", "aimd": "#d16a32"}


def mean(values):
    return statistics.fmean(values)


def summary(values):
    return {
        "n": len(values), "mean": mean(values), "median": statistics.median(values),
        "sample_sd": statistics.stdev(values) if len(values) > 1 else 0.0,
        "min": min(values), "max": max(values),
        "cv": statistics.stdev(values) / mean(values) if len(values) > 1 and mean(values) else 0.0,
    }


def exact_permutation(a, b):
    observed = abs(mean(a) - mean(b))
    pooled = a + b
    exceed = 0
    total = 0
    for selected in itertools.combinations(range(len(pooled)), len(a)):
        chosen = set(selected)
        left = [value for index, value in enumerate(pooled) if index in chosen]
        right = [value for index, value in enumerate(pooled) if index not in chosen]
        total += 1
        exceed += abs(mean(left) - mean(right)) >= observed - 1e-12
    return exceed / total


def cliffs_delta(a, b):
    return sum((x > y) - (x < y) for x in a for y in b) / (len(a) * len(b))


def bootstrap_difference(a, b, iterations=20000):
    rng = random.Random(20261029)
    differences = []
    for _ in range(iterations):
        differences.append(mean([rng.choice(b) for _ in b]) - mean([rng.choice(a) for _ in a]))
    differences.sort()
    return [differences[int(.025 * iterations)], differences[int(.975 * iterations) - 1]]


def comparison(rows, metric):
    fixed = [row[metric] for row in rows if row["policy"] == "fixed"]
    aimd = [row[metric] for row in rows if row["policy"] == "aimd"]
    interval = bootstrap_difference(fixed, aimd)
    p_value = exact_permutation(fixed, aimd)
    return {
        "fixed": summary(fixed), "aimd": summary(aimd),
        "aimd_minus_fixed_mean": mean(aimd) - mean(fixed),
        "aimd_minus_fixed_bootstrap_95pct_ci": interval,
        "two_sided_exact_permutation_p": p_value,
        "statistically_meaningful": p_value < .05 and not (interval[0] <= 0 <= interval[1]),
    }


def transitions(samples):
    found = []
    previous = None
    for sample in samples:
        controller = sample.get("controller")
        if not controller:
            continue
        value = (controller.get("state"), controller.get("rate_rps"))
        if value != previous:
            found.append({"elapsed_seconds": sample["elapsed_seconds"],
                          "state": value[0], "rate_rps": value[1],
                          "reason": controller.get("reason")})
            previous = value
    return found


def trial_row(trial):
    metrics = list(trial["middleware_metrics"].values())
    sample_count = sum(row["latency_sample_count"] for row in metrics)
    failed = sum(row["failed_requests"] for row in metrics)
    retry_total = sum(row["retry_count_total"] for row in trial["hospital_journals"].values())
    rates = [sample.get("controller", {}).get("rate_rps") for sample in trial["samples"]]
    rates = [value for value in rates if isinstance(value, (int, float))]
    states = [sample.get("controller", {}).get("state") for sample in trial["samples"]]
    states = [value for value in states if value]
    queue_depths = [sample.get("cloud", {}).get(service, {}).get("executor_queue_depth")
                    for sample in trial["samples"] for service in ("patient", "emergency", "admin")]
    queue_pressures = [sample.get("cloud", {}).get(service, {}).get("executor_queue_pressure")
                       for sample in trial["samples"] for service in ("patient", "emergency", "admin")]
    queue_depths = [value for value in queue_depths if isinstance(value, (int, float))]
    queue_pressures = [value for value in queue_pressures if isinstance(value, (int, float))]
    return {
        "policy": trial["policy"], "trial": trial["ordinal"],
        "recovery_completion_seconds": trial["recovery_completion_seconds"],
        "hospital_a_completion_seconds": trial["site_completion_seconds"]["Hospital_A"],
        "hospital_b_completion_seconds": trial["site_completion_seconds"]["Hospital_B"],
        "site_completion_gap_seconds": abs(trial["site_completion_seconds"]["Hospital_A"] - trial["site_completion_seconds"]["Hospital_B"]),
        "middleware_max_service_p95_ms": max(row["p95_latency_ms"] for row in metrics if row["p95_latency_ms"] is not None),
        "middleware_total_samples": sample_count,
        "middleware_failed_requests": failed,
        "middleware_weighted_error_rate": failed / sample_count if sample_count else 0,
        "middleware_sum_process_throughput_rps": sum(row["throughput_rps"] for row in metrics),
        "client_status_read_p95_ms": trial["client_status_read_p95_ms"],
        "hospital_transmission_retry_total": retry_total,
        "maximum_executor_queue_depth": max(queue_depths),
        "maximum_executor_queue_pressure": max(queue_pressures),
        "controller_denied": trial["controller_final"]["denied"],
        "controller_allowed": trial["controller_final"]["allowed"],
        "minimum_observed_rate_rps": min(rates), "maximum_observed_rate_rps": max(rates),
        "states_observed": ">".join(dict.fromkeys(states)),
        "postgres_events": trial["integrity"]["postgres_events"],
        "completed_grants": trial["integrity"]["completed_grants"],
        "loss_or_duplication": trial["integrity"]["loss_or_duplication"],
    }


def save_csv(path, rows):
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)


def graph_recovery(rows, path):
    fig, ax = plt.subplots(figsize=(8, 5), constrained_layout=True)
    for index, policy in enumerate(POLICIES):
        values = [r["recovery_completion_seconds"] for r in rows if r["policy"] == policy]
        ax.boxplot(values, positions=[index], widths=.42, patch_artist=True,
                   boxprops={"facecolor": COLORS[policy], "alpha": .22}, medianprops={"color": COLORS[policy], "linewidth": 2})
        jitter = [index + (i - (len(values)-1)/2) * .035 for i in range(len(values))]
        ax.scatter(jitter, values, color=COLORS[policy], label=policy.upper(), zorder=3)
    ax.set_xticks(range(2), ["Fixed-rate", "AIMD"])
    ax.set_ylabel("Recovery completion time (s)")
    ax.set_title("CrisisGuard isolated eight-event recovery by admission policy")
    ax.grid(axis="y", alpha=.25)
    ax.text(.5, -.18, "Points are independent fresh-volume trials; boxes show trial variability.",
            transform=ax.transAxes, ha="center", fontsize=9)
    fig.savefig(path, dpi=180)
    plt.close(fig)


def graph_rates(trials, path):
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.8), sharey=True, constrained_layout=True)
    for ax, policy in zip(axes, POLICIES):
        for trial in [row for row in trials if row["policy"] == policy]:
            points = [(s["elapsed_seconds"], s.get("controller", {}).get("rate_rps")) for s in trial["samples"]]
            points = [(x, y) for x, y in points if isinstance(y, (int, float))]
            ax.step([x for x, _ in points], [y for _, y in points], where="post", alpha=.62,
                    label=f"Trial {trial['ordinal']}")
        ax.set_title(policy.upper())
        ax.set_xlabel("Elapsed time since recovery start (s)")
        ax.grid(alpha=.25)
    axes[0].set_ylabel("Controller admission rate (RPS)")
    axes[1].legend(fontsize=8, ncol=2)
    fig.suptitle("Measured controller admission-rate trajectories during isolated recovery")
    fig.savefig(path, dpi=180)
    plt.close(fig)


def graph_boundaries(rows, path):
    fig, ax = plt.subplots(figsize=(9, 5.2), constrained_layout=True)
    x = range(len(rows))
    labels = [f"{r['policy'][0].upper()}{r['trial']}" for r in rows]
    ax.scatter([i-.12 for i in x], [r["middleware_max_service_p95_ms"] for r in rows], marker="o", label="Max process middleware p95 (replay writes/status checks)")
    ax.scatter([i+.12 for i in x], [r["client_status_read_p95_ms"] for r in rows], marker="^", label="Host client p95 (post-run status GETs)")
    ax.set_xticks(list(x), labels)
    ax.set_xlabel("Policy/trial (F=fixed, A=AIMD)")
    ax.set_ylabel("Latency (ms, log scale)")
    ax.set_yscale("log")
    ax.set_title("Latency observations at different boundaries and workload populations")
    ax.grid(axis="y", alpha=.25, which="both")
    ax.legend(fontsize=9)
    ax.text(.5, -.18, "These series are intentionally not treated as equivalent estimates: route populations and measurement boundaries differ.",
            transform=ax.transAxes, ha="center", fontsize=8.5)
    fig.savefig(path, dpi=180)
    plt.close(fig)


def graph_backlog(trials, path):
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.8), sharey=True, constrained_layout=True)
    for ax, policy in zip(axes, POLICIES):
        for trial in [row for row in trials if row["policy"] == policy]:
            points = []
            for sample in trial["samples"]:
                hospitals = sample.get("hospitals", {})
                if all("pending" in hospitals.get(site, {}) for site in ("Hospital_A", "Hospital_B")):
                    points.append((sample["elapsed_seconds"], sum(hospitals[site]["pending"] for site in ("Hospital_A", "Hospital_B"))))
            ax.step([x for x, _ in points], [y for _, y in points], where="post", alpha=.62,
                    label=f"Trial {trial['ordinal']}")
        ax.set_title(policy.upper())
        ax.set_xlabel("Elapsed time since recovery start (s)")
        ax.grid(alpha=.25)
    axes[0].set_ylabel("Durable hospital events pending")
    axes[1].legend(fontsize=8, ncol=2)
    fig.suptitle("Observed durable backlog drain under fixed-rate and AIMD admission")
    fig.savefig(path, dpi=180)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw", type=Path)
    parser.add_argument("--summary-csv", type=Path, required=True)
    parser.add_argument("--timeseries-csv", type=Path, required=True)
    parser.add_argument("--statistics", type=Path, required=True)
    parser.add_argument("--figures", type=Path, required=True)
    args = parser.parse_args()
    raw = json.loads(args.raw.read_text(encoding="utf-8"))
    assert raw["result"] == "passed" and raw["original_resources_preserved"]["result"] == "passed"
    trials = raw["trials"]
    assert all(row["result"] == "passed" for row in trials)
    rows = [trial_row(row) for row in trials]
    save_csv(args.summary_csv, rows)
    time_rows = []
    for trial in trials:
        for sample in trial["samples"]:
            controller = sample.get("controller", {})
            hospitals = sample.get("hospitals", {})
            time_rows.append({
                "policy": trial["policy"], "trial": trial["ordinal"], "elapsed_seconds": sample["elapsed_seconds"],
                "state": controller.get("state"), "rate_rps": controller.get("rate_rps"),
                "telemetry_fresh": controller.get("telemetry_fresh"), "allowed": controller.get("allowed"),
                "denied": controller.get("denied"),
                "hospital_a_pending": hospitals.get("Hospital_A", {}).get("pending"),
                "hospital_b_pending": hospitals.get("Hospital_B", {}).get("pending"),
                **{f"{service}_{field}": sample.get("cloud", {}).get(service, {}).get(field)
                   for service in ("patient", "emergency", "admin")
                   for field in ("p95_latency_ms", "throughput_rps", "error_rate", "latency_sample_count", "executor_queue_depth", "executor_queue_pressure")},
            })
    save_csv(args.timeseries_csv, time_rows)
    fixed = [r["recovery_completion_seconds"] for r in rows if r["policy"] == "fixed"]
    aimd = [r["recovery_completion_seconds"] for r in rows if r["policy"] == "aimd"]
    ci = bootstrap_difference(fixed, aimd)
    p_value = exact_permutation(fixed, aimd)
    stats = {
        "recovery_completion_seconds": {"fixed": summary(fixed), "aimd": summary(aimd),
            "aimd_minus_fixed_mean_seconds": mean(aimd) - mean(fixed),
            "aimd_minus_fixed_bootstrap_95pct_ci_seconds": ci,
            "two_sided_exact_permutation_p": p_value,
            "cliffs_delta_aimd_vs_fixed": cliffs_delta(aimd, fixed),
            "statistically_meaningful_rule": "p < 0.05 and bootstrap interval excludes zero",
            "statistically_meaningful": p_value < .05 and not (ci[0] <= 0 <= ci[1])},
        "metrics": {metric: comparison(rows, metric)
                    for metric in ("middleware_max_service_p95_ms", "middleware_sum_process_throughput_rps",
                                   "middleware_weighted_error_rate", "client_status_read_p95_ms",
                                   "hospital_transmission_retry_total", "site_completion_gap_seconds",
                                   "maximum_executor_queue_depth", "maximum_executor_queue_pressure",
                                   "controller_allowed", "controller_denied")},
        "transitions": [{"policy": trial["policy"], "trial": trial["ordinal"], "transitions": transitions(trial["samples"])} for trial in trials],
        "integrity": {"all_trials_same_eight_eligible_events_exactly_once": all(
            r["postgres_events"] == 8 and r["completed_grants"] == 8 and not r["loss_or_duplication"] for r in rows)},
    }
    args.statistics.write_text(json.dumps(stats, indent=2), encoding="utf-8")
    args.figures.mkdir(parents=True, exist_ok=True)
    graph_recovery(rows, args.figures / "cp7b-fixed-vs-aimd-recovery-time-20261029.png")
    graph_rates(trials, args.figures / "cp7b-fixed-vs-aimd-rate-trajectories-20261029.png")
    graph_boundaries(rows, args.figures / "cp7b-fixed-vs-aimd-latency-boundaries-20261029.png")
    graph_backlog(trials, args.figures / "cp7b-fixed-vs-aimd-backlog-drain-20261029.png")
    print(json.dumps(stats["recovery_completion_seconds"], indent=2))


if __name__ == "__main__":
    main()
