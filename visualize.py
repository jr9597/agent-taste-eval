#!/usr/bin/env python3
"""
Plot behavioral scores as quadrant scatter charts.

Usage:
    python visualize.py scope_creep clarification_seeking
    python visualize.py confidence_signaling scope_creep
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib.pyplot as plt

ROOT = Path(__file__).parent
SCORES_FILE = ROOT / "results" / "scores.jsonl"

AXIS_LABELS = {
    "scope_creep": "Scope Creep",
    "clarification_seeking": "Clarification Seeking",
    "confidence_signaling": "Confidence Signaling",
}

VALID_AXES = set(AXIS_LABELS)


def load_scores(path: Path) -> list[dict]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    records = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def extract_point(record: dict, axis1: str, axis2: str) -> tuple[float, float, str] | None:
    scores = record.get("scores", {})
    if axis1 not in scores or axis2 not in scores:
        return None
    x = scores[axis1]["score"]
    y = scores[axis2]["score"]
    label = f"{record.get('model', '?')} / {record.get('task_id', '?')}"
    return x, y, label


def plot_quadrant(
    records: list[dict],
    axis1: str,
    axis2: str,
    output_path: Path,
) -> None:
    points = []
    for record in records:
        point = extract_point(record, axis1, axis2)
        if point:
            points.append(point)

    if not points:
        print(f"No data for {axis1} vs {axis2}. Run some evaluations first.")
        return

    fig, ax = plt.subplots(figsize=(10, 8))

    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    labels = [p[2] for p in points]

    ax.scatter(xs, ys, s=120, alpha=0.75, edgecolors="black", linewidths=0.5)

    for x, y, label in points:
        ax.annotate(
            label,
            (x, y),
            textcoords="offset points",
            xytext=(6, 6),
            fontsize=8,
            alpha=0.85,
        )

    midpoint = 2.5
    ax.axvline(midpoint, color="gray", linestyle="--", linewidth=0.8, alpha=0.6)
    ax.axhline(midpoint, color="gray", linestyle="--", linewidth=0.8, alpha=0.6)

    ax.set_xlim(-0.3, 5.3)
    ax.set_ylim(-0.3, 5.3)
    ax.set_xlabel(AXIS_LABELS[axis1], fontsize=12)
    ax.set_ylabel(AXIS_LABELS[axis2], fontsize=12)
    ax.set_title(f"Model Taste Eval — {AXIS_LABELS[axis1]} vs {AXIS_LABELS[axis2]}", fontsize=14)
    ax.grid(True, alpha=0.3)

    # Quadrant labels
    ax.text(1.2, 4.7, "Low / Low", fontsize=9, color="gray", alpha=0.7)
    ax.text(3.8, 4.7, "High / Low", fontsize=9, color="gray", alpha=0.7)
    ax.text(1.2, 0.3, "Low / High", fontsize=9, color="gray", alpha=0.7)
    ax.text(3.8, 0.3, "High / High", fontsize=9, color="gray", alpha=0.7)

    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=150)
    plt.close(fig)
    print(f"Saved {output_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Visualize behavioral scores as quadrant charts")
    parser.add_argument("axis1", choices=sorted(VALID_AXES), help="X-axis behavioral dimension")
    parser.add_argument("axis2", choices=sorted(VALID_AXES), help="Y-axis behavioral dimension")
    parser.add_argument(
        "--scores-file",
        default=str(SCORES_FILE),
        help="Path to scores.jsonl (default: results/scores.jsonl)",
    )
    parser.add_argument(
        "--output-dir",
        default=str(ROOT / "results"),
        help="Directory for output PNGs",
    )
    args = parser.parse_args()

    if args.axis1 == args.axis2:
        parser.error("axis1 and axis2 must be different")

    records = load_scores(Path(args.scores_file))
    output_path = Path(args.output_dir) / f"quadrant_{args.axis1}_vs_{args.axis2}.png"
    plot_quadrant(records, args.axis1, args.axis2, output_path)


if __name__ == "__main__":
    main()
