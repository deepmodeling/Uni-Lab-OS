#!/usr/bin/env python3
"""Generate Gantt chart from scheduler response JSON.

Usage:
    python generate_gantt.py --schedule-json response.json --output gantt.png --title "Lab Schedule"
"""

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.dates import DateFormatter
import numpy as np


def load_schedule(json_path):
    """Load schedule response from JSON file."""
    with open(json_path) as f:
        return json.load(f)


def generate_gantt(schedule_data, output_path, title="Lab Schedule"):
    """Generate Gantt chart from schedule data."""

    # Extract schedule entries (steps only, not transfers)
    steps = [entry for entry in schedule_data["schedule"] if "step_id" in entry]

    if not steps:
        print("No steps found in schedule")
        return

    # Group by device (resource)
    devices = {}
    for step in steps:
        device = step["resource"]
        if device not in devices:
            devices[device] = []
        devices[device].append(step)

    # Sort devices by name for consistent ordering
    device_names = sorted(devices.keys())

    # Create figure
    fig, ax = plt.subplots(figsize=(14, max(6, len(device_names) * 0.5)))

    # Color map for tasks
    task_ids = sorted(set(step["task_id"] for step in steps))
    colors = plt.cm.tab20(np.linspace(0, 1, len(task_ids)))
    task_colors = dict(zip(task_ids, colors))

    # Plot each step as a horizontal bar
    y_pos = 0
    y_labels = []
    y_ticks = []

    for device in device_names:
        device_steps = sorted(devices[device], key=lambda s: s["start"])

        for step in device_steps:
            start = step["start"]
            duration = step["end"] - step["start"]
            task_id = step["task_id"]
            step_id = step["step_id"]

            # Draw bar
            ax.barh(
                y_pos,
                duration,
                left=start,
                height=0.8,
                color=task_colors[task_id],
                edgecolor="black",
                linewidth=0.5,
                alpha=0.8
            )

            # Add step label
            label = f"{task_id}.{step_id}"
            ax.text(
                start + duration / 2,
                y_pos,
                label,
                ha="center",
                va="center",
                fontsize=8,
                color="white" if duration > 5 else "black",
                weight="bold"
            )

        y_labels.append(device)
        y_ticks.append(y_pos)
        y_pos += 1

    # Configure axes
    ax.set_yticks(y_ticks)
    ax.set_yticklabels(y_labels)
    ax.set_xlabel("Time (minutes)", fontsize=12)
    ax.set_ylabel("Device", fontsize=12)
    ax.set_title(title, fontsize=14, weight="bold")
    ax.grid(axis="x", alpha=0.3, linestyle="--")

    # Add legend for tasks
    legend_patches = [
        mpatches.Patch(color=task_colors[task_id], label=task_id)
        for task_id in task_ids
    ]
    ax.legend(
        handles=legend_patches,
        loc="upper right",
        fontsize=9,
        title="Tasks"
    )

    # Add makespan annotation
    makespan = schedule_data["objective"]["total_makespan"]
    ax.axvline(makespan, color="red", linestyle="--", linewidth=1.5, alpha=0.7)
    ax.text(
        makespan,
        len(device_names) - 0.5,
        f"Makespan: {makespan} min",
        ha="right",
        va="top",
        fontsize=10,
        color="red",
        weight="bold",
        bbox=dict(boxstyle="round,pad=0.3", facecolor="white", alpha=0.8)
    )

    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches="tight")
    print(f"Gantt chart saved to {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Generate Gantt chart from schedule JSON")
    parser.add_argument("--schedule-json", required=True, help="Path to schedule response JSON")
    parser.add_argument("--output", required=True, help="Output PNG path")
    parser.add_argument("--title", default="Lab Schedule", help="Chart title")

    args = parser.parse_args()

    schedule_data = load_schedule(args.schedule_json)
    generate_gantt(schedule_data, args.output, args.title)


if __name__ == "__main__":
    main()
