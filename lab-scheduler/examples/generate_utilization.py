#!/usr/bin/env python3
"""Generate device utilization report from scheduler response JSON.

Usage:
    python generate_utilization.py --schedule-json response.json --output utilization.png
"""

import argparse
import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np


def load_schedule(json_path):
    """Load schedule response from JSON file."""
    with open(json_path) as f:
        return json.load(f)


def calculate_utilization(schedule_data):
    """Calculate utilization statistics per device."""
    steps = [entry for entry in schedule_data["schedule"] if "step_id" in entry]
    makespan = schedule_data["objective"]["total_makespan"]

    if not steps or makespan == 0:
        return {}, makespan

    # Group by device
    device_usage = {}
    for step in steps:
        device = step["resource"]
        if device not in device_usage:
            device_usage[device] = []
        device_usage[device].append((step["start"], step["end"]))

    # Calculate busy time per device
    utilization = {}
    for device, intervals in device_usage.items():
        # Merge overlapping intervals (shouldn't happen, but be safe)
        intervals.sort()
        merged = []
        for start, end in intervals:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))

        busy_time = sum(end - start for start, end in merged)
        idle_time = makespan - busy_time
        util_pct = (busy_time / makespan) * 100 if makespan > 0 else 0

        utilization[device] = {
            "busy_time": busy_time,
            "idle_time": idle_time,
            "utilization": util_pct
        }

    return utilization, makespan


def generate_utilization_chart(schedule_data, output_path):
    """Generate device utilization bar chart and summary table."""
    utilization, makespan = calculate_utilization(schedule_data)

    if not utilization:
        print("No utilization data to plot")
        return

    # Sort devices by utilization (descending)
    devices = sorted(utilization.keys(), key=lambda d: utilization[d]["utilization"], reverse=True)
    util_values = [utilization[d]["utilization"] for d in devices]

    # Create figure with two subplots
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 10), gridspec_kw={"height_ratios": [2, 1]})

    # --- Bar chart ---
    colors = ["#2ecc71" if u >= 70 else "#f39c12" if u >= 40 else "#e74c3c" for u in util_values]
    bars = ax1.barh(devices, util_values, color=colors, edgecolor="black", linewidth=0.5)

    # Add percentage labels on bars
    for i, (bar, util) in enumerate(zip(bars, util_values)):
        ax1.text(
            util + 1,
            bar.get_y() + bar.get_height() / 2,
            f"{util:.1f}%",
            va="center",
            fontsize=10,
            weight="bold"
        )

    ax1.set_xlabel("Utilization (%)", fontsize=12)
    ax1.set_ylabel("Device", fontsize=12)
    ax1.set_title("Device Utilization", fontsize=14, weight="bold")
    ax1.set_xlim(0, 110)
    ax1.grid(axis="x", alpha=0.3, linestyle="--")

    # Add reference lines
    ax1.axvline(70, color="green", linestyle="--", linewidth=1, alpha=0.5, label="Good (≥70%)")
    ax1.axvline(40, color="orange", linestyle="--", linewidth=1, alpha=0.5, label="Fair (≥40%)")
    ax1.legend(loc="lower right", fontsize=9)

    # --- Summary table ---
    ax2.axis("off")

    # Prepare table data
    table_data = []
    for device in devices:
        util = utilization[device]
        table_data.append([
            device,
            f"{util['busy_time']:.0f}",
            f"{util['idle_time']:.0f}",
            f"{util['utilization']:.1f}%"
        ])

    # Add overall average
    avg_util = np.mean(util_values)
    table_data.append([
        "OVERALL AVERAGE",
        "",
        "",
        f"{avg_util:.1f}%"
    ])

    # Create table
    table = ax2.table(
        cellText=table_data,
        colLabels=["Device", "Busy (min)", "Idle (min)", "Utilization"],
        cellLoc="center",
        loc="center",
        colWidths=[0.4, 0.2, 0.2, 0.2]
    )

    table.auto_set_font_size(False)
    table.set_fontsize(10)
    table.scale(1, 2)

    # Style header
    for i in range(4):
        table[(0, i)].set_facecolor("#34495e")
        table[(0, i)].set_text_props(weight="bold", color="white")

    # Style last row (average)
    for i in range(4):
        table[(len(table_data), i)].set_facecolor("#ecf0f1")
        table[(len(table_data), i)].set_text_props(weight="bold")

    # Add summary text
    summary_text = f"Total Makespan: {makespan} minutes\n"
    summary_text += f"Average Utilization: {avg_util:.1f}%\n"
    summary_text += f"Devices: {len(devices)}"

    ax2.text(
        0.5, -0.15,
        summary_text,
        ha="center",
        va="top",
        fontsize=11,
        transform=ax2.transAxes,
        bbox=dict(boxstyle="round,pad=0.5", facecolor="lightyellow", alpha=0.8)
    )

    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches="tight")
    print(f"Utilization report saved to {output_path}")

    # Print recommendations
    print("\n--- Recommendations ---")
    low_util = [d for d in devices if utilization[d]["utilization"] < 40]
    high_util = [d for d in devices if utilization[d]["utilization"] > 90]

    if low_util:
        print(f"⚠️  Low utilization devices: {', '.join(low_util)}")
        print("   Consider reducing device count or adding more tasks")

    if high_util:
        print(f"⚠️  High utilization devices: {', '.join(high_util)}")
        print("   Consider adding more devices or using batch processing")

    if not low_util and not high_util:
        print("✓ Utilization is well-balanced across all devices")


def main():
    parser = argparse.ArgumentParser(description="Generate device utilization report")
    parser.add_argument("--schedule-json", required=True, help="Path to schedule response JSON")
    parser.add_argument("--output", required=True, help="Output PNG path")

    args = parser.parse_args()

    schedule_data = load_schedule(args.schedule_json)
    generate_utilization_chart(schedule_data, args.output)


if __name__ == "__main__":
    main()
