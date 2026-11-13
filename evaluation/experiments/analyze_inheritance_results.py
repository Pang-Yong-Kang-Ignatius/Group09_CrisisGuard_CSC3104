"""Audit a completed local inheritance matrix and create plots and a Markdown report."""

from __future__ import annotations

import argparse
from collections import defaultdict
import csv
import hashlib
import json
import math
from pathlib import Path
import re
import statistics


INPUT_FILES = (
    "manifest.json", "matrix_summary.json", "per_seed_metrics.csv",
    "paired_differences.csv", "condition_summary.csv",
)
CONDITION = re.compile(r"ratio(\d+)-(\d+)_depth(\d+)_(balanced|a_only)\Z")


def number(value, *, optional=False):
    if value is None or value == "":
        if optional:
            return None
        raise ValueError("required numeric result is missing")
    if isinstance(value, bool):
        raise ValueError("boolean used as numeric result")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite numeric result")
    return result


def count(value):
    result = number(value)
    if result < 0 or not result.is_integer():
        raise ValueError("counts must be nonnegative integers")
    return int(result)


def same(actual, expected, context):
    if isinstance(expected, bool):
        accepted = (True, "True", "true") if expected else (False, "False", "false")
        if actual not in accepted:
            raise ValueError(f"{context}: boolean mismatch")
    elif isinstance(expected, (int, float)):
        observed = number(actual)
        if not math.isclose(observed, expected, rel_tol=1e-8, abs_tol=1e-8):
            raise ValueError(f"{context}: numeric mismatch ({observed} vs {expected})")
    elif expected is None:
        if actual is not None and actual != "":
            raise ValueError(f"{context}: expected an undefined/empty value")
    elif actual != expected:
        raise ValueError(f"{context}: value mismatch")


def read_table(path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = reader.fieldnames
        if not headers or len(headers) != len(set(headers)):
            raise ValueError(f"{path.name}: missing or duplicate column names")
        rows = list(reader)
    if not rows or any(None in row or any(v is None for v in row.values()) for row in rows):
        raise ValueError(f"{path.name}: empty or malformed table")
    return rows


def condition_key(condition):
    match = CONDITION.fullmatch(condition)
    if not match:
        raise ValueError("unrecognized matrix condition: " + condition)
    a, b, depth, distribution = match.groups()
    return int(a), int(b), int(depth), distribution


def condition_label(condition):
    a, b, depth, distribution = condition_key(condition)
    return f"{a}:{b} | depth {depth} | {'balanced' if distribution == 'balanced' else 'A only'}"


def recompute_pair(off, on):
    """Recalculate paired effects from the per-mode summaries, independently of batch code."""
    for field in ("case_id", "condition", "seed", "site_id", "event_count", "workflow_count"):
        same(on[field], off[field], "paired " + field)
    pair = {field: off[field] for field in ("case_id", "condition", "site_id")}
    seed = number(off["seed"])
    if not seed.is_integer():
        raise ValueError("seed must be an integer")
    pair["seed"] = int(seed)
    pair["event_count"] = count(off["event_count"])
    for mode, row in (("off", off), ("on", on)):
        total, completed = count(row["event_count"]), count(row["completed_events"])
        unresolved = count(row["unresolved_events"])
        if total < 1 or completed + unresolved != total:
            raise ValueError("completed and unresolved events do not reconcile")
        pair[f"{mode}_unresolved_events"] = unresolved
        if count(row["urgent_workflow_count"]) + count(row["routine_workflow_count"]) != count(row["workflow_count"]):
            raise ValueError("workflow populations do not reconcile")
        for label in ("urgent", "routine"):
            population = count(row[f"{label}_workflow_count"])
            finished = count(row[f"{label}_completed_workflows"])
            unfinished = count(row[f"{label}_unresolved_workflows"])
            if finished + unfinished != population:
                raise ValueError("completed and unresolved workflows do not reconcile")
            p95 = number(row[f"{label}_completion_position_p95"], optional=True)
            if (finished == 0) != (p95 is None):
                raise ValueError("p95 availability disagrees with completed population")
            if p95 is not None and (not p95.is_integer() or not 1 <= p95 <= total):
                raise ValueError("p95 completion position lies outside this site's backlog")
    for label in ("urgent", "routine"):
        population = count(off[f"{label}_workflow_count"])
        same(on[f"{label}_workflow_count"], population, "paired population")
        pair[f"{label}_workflow_count"] = population
        for mode, source in (("off", off), ("on", on)):
            pair[f"{label}_{mode}_unresolved"] = count(source[f"{label}_unresolved_workflows"])
            pair[f"{label}_{mode}_p95_position"] = number(source[f"{label}_completion_position_p95"], optional=True)
        eligible = population > 0 and all(pair[f"{label}_{mode}_unresolved"] == 0 for mode in ("off", "on"))
        pair[f"{label}_pair_eligible"] = eligible
        first, second = pair[f"{label}_off_p95_position"], pair[f"{label}_on_p95_position"]
        pair[f"{label}_p95_position_reduction"] = first - second if eligible else None
        pair[f"{label}_p95_position_reduction_pct"] = 100 * (first - second) / first if eligible else None
    for mode, source in (("off", off), ("on", on)):
        cpu = number(source["scheduler_call_cpu_ms_median"])
        if cpu < 0:
            raise ValueError("negative CPU duration")
        pair[f"{mode}_scheduler_cpu_ms"] = cpu
    return pair


def summarize(pairs):
    groups = defaultdict(list)
    for pair in pairs:
        groups[(pair["condition"], pair["site_id"])].append(pair)
    rows = []
    for (condition, site), group in sorted(groups.items()):
        row = {"condition": condition, "site_id": site, "seed_count": len(group)}
        for mode in ("off", "on"):
            row[f"{mode}_unresolved_events_total"] = sum(p[f"{mode}_unresolved_events"] for p in group)
        for label in ("urgent", "routine"):
            valid = [p for p in group if p[f"{label}_pair_eligible"]]
            changes = [p[f"{label}_p95_position_reduction"] for p in valid]
            percentages = [p[f"{label}_p95_position_reduction_pct"] for p in valid]
            row.update({
                f"{label}_eligible_seed_pairs": len(valid),
                f"{label}_empty_population_pairs": sum(p[f"{label}_workflow_count"] == 0 for p in group),
                f"{label}_incomplete_pairs": sum(p[f"{label}_workflow_count"] > 0 and not p[f"{label}_pair_eligible"] for p in group),
                f"{label}_mean_p95_position_reduction": statistics.mean(changes) if changes else None,
                f"{label}_median_p95_position_reduction": statistics.median(changes) if changes else None,
                f"{label}_min_p95_position_reduction": min(changes) if changes else None,
                f"{label}_max_p95_position_reduction": max(changes) if changes else None,
                f"{label}_mean_p95_position_reduction_pct": statistics.mean(percentages) if percentages else None,
                f"{label}_improved_pairs": sum(x > 0 for x in changes),
                f"{label}_unchanged_pairs": sum(x == 0 for x in changes),
                f"{label}_worsened_pairs": sum(x < 0 for x in changes),
            })
        rows.append(row)
    return rows


def audit_matrix(directory: Path):
    if (directory / "failure.json").exists():
        raise ValueError("matrix contains failure.json; do not report it as a completed run")
    for name in INPUT_FILES:
        if not (directory / name).is_file():
            raise ValueError("required input is missing: " + name)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    completion = json.loads((directory / "matrix_summary.json").read_text(encoding="utf-8"))
    if completion.get("status") != "completed":
        raise ValueError("matrix_summary.json is not a successful completion marker")
    config = manifest["matrix_config"]
    seeds = config["seeds"]
    if not seeds or any(type(seed) is not int for seed in seeds) or len(seeds) != len(set(seeds)):
        raise ValueError("invalid or duplicate seeds in manifest")
    conditions = {
        f"ratio{a}-{b}_depth{depth}_{distribution}"
        for a, b in ((1, 1), (10, 1)) for depth in (0, 3)
        for distribution in ("balanced", "a_only")
    }
    expected = {(f"{condition}_seed{seed}", site): (condition, seed)
                for condition in conditions for seed in seeds for site in ("Hospital_A", "Hospital_B")}
    case_count = 8 * len(seeds)
    same(manifest["case_count"], case_count, "manifest case count")
    same(manifest["condition_count"], 8, "manifest condition count")
    same(completion["case_count"], case_count, "completed case count")
    same(completion["seed_count"], len(seeds), "completed seed count")
    modes = {}
    for row in read_table(directory / "per_seed_metrics.csv"):
        key = row["case_id"], row["site_id"]
        if key not in expected or row["inheritance"] not in ("off", "on"):
            raise ValueError("unknown case/site/policy in per-seed results")
        condition, seed = expected[key]
        same(row["condition"], condition, "condition identity")
        same(row["seed"], seed, "seed identity")
        identity = (*key, row["inheritance"])
        if identity in modes:
            raise ValueError("duplicate per-mode result")
        modes[identity] = row
    if len(modes) != len(expected) * 2:
        raise ValueError("missing OFF/ON result or missing case/site")
    for case_id in {key[0] for key in expected}:
        total = sum(count(modes[(case_id, site, "off")]["event_count"]) for site in ("Hospital_A", "Hospital_B"))
        same(total, count(config["total_events"]), "total original backlog")
    observed_pairs = {}
    for row in read_table(directory / "paired_differences.csv"):
        key = row["case_id"], row["site_id"]
        if key in observed_pairs:
            raise ValueError("duplicate paired result")
        observed_pairs[key] = row
    if set(observed_pairs) != set(expected):
        raise ValueError("paired result identities do not match the experiment plan")
    pairs = []
    for key in sorted(expected):
        pair = recompute_pair(modes[(*key, "off")], modes[(*key, "on")])
        for field, value in pair.items():
            same(observed_pairs[key][field], value, "paired " + field)
        pairs.append(pair)
    groups = summarize(pairs)
    for source, records in (
        ("condition_summary.csv", read_table(directory / "condition_summary.csv")),
        ("matrix_summary.json", completion["condition_site_groups"]),
    ):
        indexed = {}
        for row in records:
            key = row["condition"], row["site_id"]
            if key in indexed:
                raise ValueError(source + ": duplicate summary group")
            indexed[key] = row
        if set(indexed) != {(r["condition"], r["site_id"]) for r in groups}:
            raise ValueError(source + ": missing or unexpected summary groups")
        for row in groups:
            for field, value in row.items():
                same(indexed[(row["condition"], row["site_id"])][field], value, source + " " + field)
    off_unresolved = sum(p["off_unresolved_events"] for p in pairs)
    on_unresolved = sum(p["on_unresolved_events"] for p in pairs)
    same(completion["off_unresolved_events_total"], off_unresolved, "OFF unresolved total")
    same(completion["on_unresolved_events_total"], on_unresolved, "ON unresolved total")
    controls = [p for p in pairs if condition_key(p["condition"])[2] == 0]
    failures = [p["case_id"] + ":" + p["site_id"] + ":" + label
                for p in controls for label in ("urgent", "routine")
                if p[f"{label}_pair_eligible"] and abs(p[f"{label}_p95_position_reduction"]) > 1e-8]
    dependency_urgent = [p for p in pairs if condition_key(p["condition"])[2] > 0 and p["urgent_pair_eligible"]]
    audit = {
        "audit_status": "control_failed" if failures else "passed", "run_id": config["run_id"],
        "case_count": case_count, "seeds_per_condition": len(seeds),
        "condition_site_groups": len(groups), "paired_site_results": len(pairs),
        "off_unresolved_events_total": off_unresolved, "on_unresolved_events_total": on_unresolved,
        "depth0_control_failures": failures,
        "dependency_urgent_eligible_pairs": len(dependency_urgent),
        "dependency_urgent_improved_pairs": sum(p["urgent_p95_position_reduction"] > 0 for p in dependency_urgent),
        "urgent_incomplete_pairs": sum(g["urgent_incomplete_pairs"] for g in groups),
        "routine_incomplete_pairs": sum(g["routine_incomplete_pairs"] for g in groups),
        "input_sha256": {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in INPUT_FILES},
        "scope": "Cross-file summary audit; does not independently verify raw event traces, cloud application or source execution.",
    }
    return {"manifest": manifest, "audit": audit, "pairs": pairs, "groups": groups}


def plot_results(analysis, output_dir):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10,
                         "axes.spines.top": False, "axes.spines.right": False,
                         "axes.spines.left": False, "svg.fonttype": "none"})
    pairs = analysis["pairs"]
    conditions = sorted({p["condition"] for p in pairs}, key=condition_key)
    sites = ("Hospital_A", "Hospital_B")
    recipes = (
        ("urgent_position_reduction", "Effect of inheritance on urgent workflow completion",
         "OFF p95 position minus ON p95 position (selections)", "urgent"),
        ("routine_position_change", "Routine workflow trade-offs across workload seeds",
         "Increase in routine p95 completion position (%)", "routine"),
        ("scheduler_cpu_ratio", "Scheduler computation overhead with inheritance",
         "Scheduler-call CPU time: ON / OFF (ratio)", "cpu"),
    )
    paths = []
    for filename, title, xlabel, population in recipes:
        fig, axes = plt.subplots(1, 2, figsize=(13.5, 7.3), sharey=True)
        all_values = []
        for ax, site in zip(axes, sites):
            for y, condition in enumerate(conditions):
                selected = [p for p in pairs if p["condition"] == condition and p["site_id"] == site]
                if population == "cpu":
                    values = [p["on_scheduler_cpu_ms"] / p["off_scheduler_cpu_ms"]
                              for p in selected if p["off_scheduler_cpu_ms"] > 0 and p["on_scheduler_cpu_ms"] > 0]
                    center = statistics.median(values) if values else None
                elif population == "urgent":
                    values = [p["urgent_p95_position_reduction"] for p in selected if p["urgent_pair_eligible"]]
                    center = statistics.mean(values) if values else None
                else:
                    values = [-p["routine_p95_position_reduction_pct"] for p in selected if p["routine_pair_eligible"]]
                    center = statistics.mean(values) if values else None
                if center is None:
                    message = "CPU timing unavailable" if population == "cpu" else "No urgent workflows" if population == "urgent" and all(p["urgent_workflow_count"] == 0 for p in selected) else "No complete comparable pairs"
                    ax.text(0.03, y, message, transform=ax.get_yaxis_transform(), color="#6b7280", fontsize=9)
                    continue
                all_values.extend(values)
                color = "#b86326" if (population == "routine" and center > 0) or (population == "urgent" and center < 0) else "#176c8d"
                ax.errorbar(center, y, xerr=[[max(0, center - min(values))], [max(0, max(values) - center)]],
                            fmt="o", color=color, ecolor="#99a6b1", capsize=4, markersize=7, linewidth=1.5)
                # Label near each point without changing the meaning of observed ranges.
                ax.annotate(f" {center:.2f}" + ("%" if population == "routine" else "x" if population == "cpu" else ""),
                            (center, y), xytext=(5, -14), textcoords="offset points", fontsize=8, color=color)
            ax.set_title(site.replace("_", " "), fontsize=12, fontweight="bold", pad=16)
            ax.set_yticks(range(len(conditions)), [condition_label(c) for c in conditions])
            ax.grid(axis="x", color="#e5e7eb", linewidth=0.7)
            ax.tick_params(axis="y", length=0, pad=8)
            ax.set_xlabel(xlabel, fontsize=9, labelpad=12)
            ax.axvline(1 if population == "cpu" else 0, color="#6b7280", linestyle="--", linewidth=1)
        if population == "cpu":
            from matplotlib.ticker import FuncFormatter
            for ax in axes:
                ax.set_xscale("log", base=2)
                ax.set_xlim(min([1] + all_values) * 0.7, max([1] + all_values) * 1.5)
                ax.xaxis.set_major_formatter(FuncFormatter(lambda x, _: f"{x:g}x"))
        else:
            low, high = min([0] + all_values), max([0] + all_values)
            padding = max((high - low) * 0.18, 0.5)
            for ax in axes:
                ax.set_xlim(low - padding, high + padding)
        axes[0].invert_yaxis()
        fig.suptitle(title, fontsize=17, fontweight="bold", x=0.04, ha="left", y=0.97)
        fig.text(0.04, 0.915, f"Run: {analysis['audit']['run_id']}  |  {analysis['audit']['seeds_per_condition']} workload seeds per condition  |  hospitals drained independently", color="#4b5563", fontsize=10)
        fig.text(0.04, 0.06, ("Point = median CPU ratio" if population == "cpu" else "Point = mean paired change") + "; whiskers = observed seed min–max, not a confidence interval.", fontsize=9, color="#4b5563")
        fig.text(0.04, 0.025, "Fixed-time scheduler simulation with immediate successful ACKs. Selection positions do not measure cloud recovery latency.", fontsize=9, color="#4b5563")
        fig.subplots_adjust(left=0.225, right=0.965, top=0.84, bottom=0.18, wspace=0.18)
        for extension in ("png", "svg", "pdf"):
            path = output_dir / f"{filename}.{extension}"
            fig.savefig(path, dpi=190, facecolor="white")
            paths.append(path.name)
        plt.close(fig)
    return paths


def markdown_report(analysis):
    audit, manifest, groups = analysis["audit"], analysis["manifest"], analysis["groups"]
    config = manifest["matrix_config"]
    lines = ["# Local inheritance evaluation results", "",
             f"Run ID: `{audit['run_id']}`. This report is generated from the supplied completed matrix.", "",
             f"The summary audit reconciled {audit['case_count']} cases, {audit['paired_site_results']} paired site results and {audit['condition_site_groups']} condition/site groups.",
             f"There are {audit['seeds_per_condition']} workload seeds per condition, {config['total_events']} events per case and {config['repetitions']} timing repetitions per mode/site/seed.", "",
             "## Findings", "",
             f"Among dependency cases with complete urgent populations, {audit['dependency_urgent_improved_pairs']} of {audit['dependency_urgent_eligible_pairs']} eligible seed/site pairs improved urgent p95 completion position.",
             f"Unresolved event totals: OFF {audit['off_unresolved_events_total']}; ON {audit['on_unresolved_events_total']}.",
             f"Incomplete seed/site pairs excluded from position effects: urgent {audit['urgent_incomplete_pairs']}; routine {audit['routine_incomplete_pairs']}.",
             f"Depth-0 control failures: {len(audit['depth0_control_failures'])}. A failure must be investigated before interpreting benefits.", "",
             "Percentages below are means of within-seed paired percentages. Negative reductions indicate worse outcomes with inheritance.", "",
             "| Condition | Hospital | Urgent p95 reduction (%) | Routine p95 increase (%) | Urgent eligible / empty / incomplete pairs | Routine eligible / empty / incomplete pairs |",
             "|---|---|---:|---:|---|---|" ]
    for row in sorted(groups, key=lambda r: (condition_key(r["condition"]), r["site_id"])):
        urgent = row["urgent_mean_p95_position_reduction_pct"]
        routine = row["routine_mean_p95_position_reduction_pct"]
        fmt = lambda value: "undefined" if value is None else f"{0.0 if abs(value) < 0.005 else value:.2f}"
        lines.append(f"| {condition_label(row['condition']).replace('|', '/')} | {row['site_id']} | {fmt(urgent)} | {fmt(-routine if routine is not None else None)} | {row['urgent_eligible_seed_pairs']} / {row['urgent_empty_population_pairs']} / {row['urgent_incomplete_pairs']} | {row['routine_eligible_seed_pairs']} / {row['routine_empty_population_pairs']} / {row['routine_incomplete_pairs']} |")
    lines += ["", "## Read the figures", "",
              "- Urgent figure: positive values mean fewer local selections before urgent workflow completion.",
              "- Routine figure: positive values mean routine workflows complete later; retain this trade-off in presentations.",
              "- CPU figure: ratios above 1 mean scheduler calls use more CPU with inheritance. This is computation overhead, not HTTP latency.",
              "- Whiskers show observed seed min–max. They are not confidence intervals. Undefined groups are labelled rather than drawn as zero.", "",
              "## Limits and evidence", "",
              "Hospitals drain independently; these ratios do not measure inter-site contention or admission fairness. Time remains frozen at the snapshot and ACKs are simulated immediately. No live traffic, HTTP, database timing or controller is exercised.",
              "Seeds vary routine priorities and urgency placement within the generator's chain family. Depth changes workflow and urgent-terminal counts. Timing repetitions reuse one workload and are not independent seeds.",
              "Cross-file consistency does not establish that raw event traces are correct or that a deployed system meets the research acceptance targets. This audit recalculates paired and aggregate metrics from per-mode summary rows; it does not independently replay the workload.",
              "If either policy leaves a population incomplete, its paired position comparison is excluded and the excluded count remains visible. Inspect incomplete populations and raw traces rather than interpreting completed-only percentiles alone.", "",
              "Input hashes and the original matrix environment/source fingerprints are retained in `analysis_audit.json`. Preserve the original matrix directory alongside these figures.", "",
              "## Suggested explanation", "",
              "We generated reproducible synthetic hospital backlogs and compared the existing local scheduler with inheritance disabled and enabled on identical inputs. We checked dependency-free cases as a control, measured urgent and routine completion positions, and retained unresolved work and routine trade-offs. These results establish local ordering behaviour; shared-cloud latency and controller fairness require later system experiments.", ""]
    if audit["depth0_control_failures"]:
        lines += ["## Control failures requiring investigation", ""] + ["- " + x for x in audit["depth0_control_failures"]] + [""]
    return "\n".join(lines)


def generate_report(matrix_dir: Path, output_dir: Path):
    if output_dir.exists():
        raise ValueError("output directory already exists; choose a new directory")
    analysis = audit_matrix(matrix_dir)
    # Import plotting before creating outputs so missing dependencies do not leave a misleading report.
    import matplotlib
    output_dir.mkdir(parents=True, exist_ok=False)
    plots = plot_results(analysis, output_dir)
    provenance = {**analysis["audit"], "matrix_manifest": analysis["manifest"],
                  "analyzer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  "matplotlib_version": matplotlib.__version__, "figures": plots}
    (output_dir / "analysis_audit.json").write_text(json.dumps(provenance, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    (output_dir / "results_report.md").write_text(markdown_report(analysis), encoding="utf-8")
    return analysis["audit"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix-dir", required=True, type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--audit-only", action="store_true")
    args = parser.parse_args()
    if not args.audit_only and args.output_dir is None:
        parser.error("--output-dir is required unless --audit-only is used")
    try:
        audit = audit_matrix(args.matrix_dir)["audit"] if args.audit_only else generate_report(args.matrix_dir, args.output_dir)
        print(json.dumps(audit, indent=2, sort_keys=True))
        if args.output_dir is not None and not args.audit_only:
            print("Saved figures and report:", args.output_dir)
        if audit["depth0_control_failures"]:
            raise SystemExit(2)
    except (OSError, ValueError, KeyError, TypeError, ImportError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
