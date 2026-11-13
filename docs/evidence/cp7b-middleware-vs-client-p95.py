"""Generate the Checkpoint 7B middleware-versus-client p95 figure.

Reads only preserved calibration evidence. Refuses to overwrite output files.
The two series have different measurement boundaries and populations; the
figure and CSV retain those definitions explicitly.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "docs/evidence/cp7b-cloud-telemetry-calibration-20261028.json"
SUMMARY = ROOT / "docs/evidence/cp7b-cloud-telemetry-calibration-summary-20261028.csv"
DEFAULT_FIGURE = ROOT / "docs/evidence/figures/cp7b-middleware-vs-client-p95.png"
DEFAULT_DATA = ROOT / "docs/evidence/figures/cp7b-middleware-vs-client-p95.csv"
SERVICES = ("patient", "emergency", "admin")


def measurements() -> list[dict]:
    raw = json.loads(RAW.read_text(encoding="utf-8"))
    with SUMMARY.open(encoding="utf-8", newline="") as handle:
        phase_rows = list(csv.DictReader(handle))
    burst = {row["service"]: row for row in phase_rows if row["phase"] == "burst_160"}
    rows = []
    for service in SERVICES:
        middleware = raw["post_workload_metrics"][service]
        client = burst[service]
        assert middleware["live_latency_sample_count"] == 360
        assert middleware["replay_latency_sample_count"] == 0
        assert middleware["unclassified_latency_sample_count"] == 0
        assert int(client["requests"]) == 160 and int(client["http_200"]) == 160
        middleware_p95 = float(middleware["live_p95_latency_ms"])
        client_p95 = float(client["client_p95_latency_ms_nearest_rank"])
        rows.append(
            {
                "service": service.title(),
                "middleware_live_p95_ms": middleware_p95,
                "middleware_population": 360,
                "middleware_boundary": "Cloud middleware entry through response-header availability; rolling mixed-phase live population",
                "client_burst160_p95_ms": client_p95,
                "client_population": 160,
                "client_boundary": "Local workload client request/response duration; Burst-160 phase only",
                "client_to_middleware_ratio": client_p95 / middleware_p95,
            }
        )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--figure", type=Path, default=DEFAULT_FIGURE)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    args = parser.parse_args()
    for path in (args.figure, args.data):
        if path.exists():
            parser.error(f"refusing to overwrite {path}")
        path.parent.mkdir(parents=True, exist_ok=True)

    rows = measurements()
    with args.data.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    labels = [row["service"] for row in rows]
    middleware = np.array([row["middleware_live_p95_ms"] for row in rows])
    client = np.array([row["client_burst160_p95_ms"] for row in rows])
    ratios = np.array([row["client_to_middleware_ratio"] for row in rows])
    x = np.arange(len(labels))
    width = 0.34

    plt.rcParams.update({
        "font.family": "DejaVu Sans",
        "font.size": 10.5,
        "axes.titlesize": 17,
        "axes.labelsize": 12,
        "xtick.labelsize": 11,
        "ytick.labelsize": 10,
        "legend.fontsize": 10,
    })
    fig, ax = plt.subplots(figsize=(11.6, 7.4), dpi=160)
    fig.patch.set_facecolor("white")
    ax.set_facecolor("#FBFCFE")
    bars_middleware = ax.bar(
        x - width / 2,
        middleware,
        width,
        color="#2563A6",
        label="Cloud middleware live p95 (360 rolling live samples)",
        zorder=3,
    )
    bars_client = ax.bar(
        x + width / 2,
        client,
        width,
        color="#D97706",
        label="Local client Burst-160 p95 (160 requests)",
        zorder=3,
    )
    ax.set_yscale("log")
    ax.set_ylim(7, 1800)
    ax.set_ylabel("p95 latency (milliseconds, logarithmic scale)")
    ax.set_xticks(x, labels)
    ax.grid(axis="y", which="both", color="#DCE2EA", linewidth=0.8, alpha=0.9, zorder=0)
    ax.spines[["top", "right"]].set_visible(False)
    ax.spines[["left", "bottom"]].set_color("#9AA5B1")
    ax.set_title("Checkpoint 7B: Cloud Middleware vs Client-Observed p95 Latency", weight="bold", pad=18)
    ax.text(
        0.5,
        1.015,
        "Verified original-stack measurements • different boundaries and populations • not like-for-like estimates",
        transform=ax.transAxes,
        ha="center",
        va="bottom",
        color="#4B5563",
        fontsize=10.5,
    )
    ax.legend(loc="upper left", frameon=True, facecolor="white", edgecolor="#CBD3DD")

    for bars, values in ((bars_middleware, middleware), (bars_client, client)):
        for bar, value in zip(bars, values):
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                value * 1.08,
                f"{value:,.2f} ms",
                ha="center",
                va="bottom",
                fontsize=10,
                fontweight="bold",
                color="#1F2937",
            )
    for i, ratio in enumerate(ratios):
        ax.text(
            x[i],
            8.3,
            f"client / middleware = {ratio:.1f}×",
            ha="center",
            va="bottom",
            fontsize=9.2,
            color="#4B5563",
        )

    boundary_note = (
        "Boundary warning: middleware p95 starts after Cloud middleware entry and combines all 360 live requests; "
        "client p95 includes local request/response delay and uses only the 160-request burst. The difference cannot "
        "be interpreted as an estimate of one omitted queue or network component."
    )
    fig.text(0.075, 0.025, boundary_note, ha="left", va="bottom", fontsize=9, color="#374151", wrap=True)
    fig.tight_layout(rect=(0.045, 0.105, 0.99, 0.94))
    fig.savefig(
        args.figure,
        dpi=300,
        bbox_inches="tight",
        facecolor="white",
        metadata={
            "Title": "CrisisGuard Checkpoint 7B middleware versus client p95",
            "Subject": "Different measured latency boundaries and populations; not directly equivalent",
            "Author": "CrisisGuard verification evidence",
        },
    )
    plt.close(fig)
    print(json.dumps({"figure": str(args.figure), "data": str(args.data), "rows": rows}, indent=2))


if __name__ == "__main__":
    main()
