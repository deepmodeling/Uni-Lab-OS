"""S07 天平调试数据的持续记录与多次测试叠加曲线。"""

from __future__ import annotations

import csv
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape


DEFAULT_BALANCE_HISTORY_DIR = (
    Path(__file__).resolve().parents[5] / "workflow_artifacts" / "s07_balance"
)
BALANCE_CURVES_FILENAME = "balance_curves.svg"
_FILE_LOCK = threading.RLock()
_CURVE_COLORS = [
    "#2563eb",
    "#dc2626",
    "#059669",
    "#7c3aed",
    "#d97706",
    "#0891b2",
    "#db2777",
    "#65a30d",
    "#4f46e5",
    "#9333ea",
]


@dataclass(frozen=True)
class BalanceSample:
    timestamp: str
    elapsed_s: float
    value_g: float


@dataclass
class BalanceCurve:
    run_id: str
    started_at: str
    target_weight: float
    recipe_name: str
    samples: list[BalanceSample] = field(default_factory=list)

    def relative_samples(self) -> list[BalanceSample]:
        if not self.samples:
            return []
        first_elapsed = self.samples[0].elapsed_s
        return [
            BalanceSample(
                timestamp=sample.timestamp,
                elapsed_s=max(sample.elapsed_s - first_elapsed, 0.0),
                value_g=sample.value_g,
            )
            for sample in self.samples
        ]


def estimate_coarse_transition(samples: list[BalanceSample], target_weight: float) -> BalanceSample | None:
    """按重量增速持续下降估算粗加转精加点，仅用于图中调试标记。"""
    if len(samples) < 7:
        return None
    slopes: list[float] = []
    for previous, current in zip(samples, samples[1:]):
        elapsed = current.elapsed_s - previous.elapsed_s
        slopes.append((current.value_g - previous.value_g) / elapsed if elapsed > 0 else 0.0)
    positive_slopes = sorted((slope for slope in slopes if slope > 0), reverse=True)
    if not positive_slopes:
        return None
    top_count = max(1, min(3, len(positive_slopes)))
    coarse_rate = sum(positive_slopes[:top_count]) / top_count
    low_rate = coarse_rate * 0.25
    peak_index = max(range(len(slopes)), key=slopes.__getitem__)
    for slope_index in range(peak_index + 1, len(slopes) - 2):
        window = slopes[slope_index: slope_index + 3]
        if (
            all(-coarse_rate * 0.05 <= slope <= low_rate for slope in window)
            and samples[slope_index].value_g < float(target_weight) * 0.98
        ):
            return samples[slope_index]
    return None


def _read_curves(samples_path: Path) -> list[BalanceCurve]:
    if not samples_path.exists() or samples_path.stat().st_size == 0:
        return []
    curves_by_id: dict[str, BalanceCurve] = {}
    with samples_path.open(newline="", encoding="utf-8") as csv_file:
        for row in csv.DictReader(csv_file):
            try:
                run_id = str(row["run_id"])
                sample = BalanceSample(
                    timestamp=str(row["timestamp"]),
                    elapsed_s=float(row["elapsed_s"]),
                    value_g=float(row["balance_g"]),
                )
                target_weight = float(row["target_weight_g"])
            except (KeyError, TypeError, ValueError):
                # 进程异常中断可能留下不完整末行，绘图时忽略该行。
                continue
            curve = curves_by_id.setdefault(
                run_id,
                BalanceCurve(
                    run_id=run_id,
                    started_at=str(row.get("started_at") or ""),
                    target_weight=target_weight,
                    recipe_name=str(row.get("recipe_name") or ""),
                ),
            )
            curve.samples.append(sample)
    return list(curves_by_id.values())


def _downsample(samples: list[BalanceSample], maximum_points: int = 1200) -> list[BalanceSample]:
    if len(samples) <= maximum_points:
        return samples
    step = max(len(samples) // maximum_points, 1)
    selected = samples[::step]
    if selected[-1] is not samples[-1]:
        selected.append(samples[-1])
    return selected


def _render_curves_svg(curves: list[BalanceCurve]) -> str:
    relative_curves = [(curve, curve.relative_samples()) for curve in curves if curve.samples]
    legend_height = max(len(relative_curves) * 34 + 100, 700)
    width, height = 1280, legend_height
    left, right, top, bottom = 86, 330, 64, 76
    plot_width = width - left - right
    plot_height = height - top - bottom
    all_samples = [sample for _curve, samples in relative_curves for sample in samples]
    all_values = [sample.value_g for sample in all_samples]
    all_targets = [curve.target_weight for curve, _samples in relative_curves]
    x_max = max((sample.elapsed_s for sample in all_samples), default=1.0)
    x_max = max(x_max, 1.0)
    if all_values:
        y_min = min([0.0, *all_values, *all_targets])
        y_max = max([0.0, *all_values, *all_targets])
    else:
        y_min, y_max = 0.0, 1.0
    y_padding = max((y_max - y_min) * 0.06, 0.01)
    y_min -= y_padding
    y_max += y_padding

    def x(value: float) -> float:
        return left + value / x_max * plot_width

    def y(value: float) -> float:
        return top + (y_max - value) / max(y_max - y_min, 1e-9) * plot_height

    grid_parts: list[str] = []
    for index in range(7):
        ratio = index / 6
        grid_y = top + ratio * plot_height
        grid_value = y_max - ratio * (y_max - y_min)
        grid_parts.append(
            f'<line x1="{left}" y1="{grid_y:.2f}" x2="{left + plot_width}" y2="{grid_y:.2f}" '
            'stroke="#e5e7eb" stroke-width="1"/>'
            f'<text x="{left - 10}" y="{grid_y + 4:.2f}" text-anchor="end" '
            f'fill="#4b5563" font-size="12">{grid_value:.3f}</text>'
        )
    for index in range(7):
        ratio = index / 6
        grid_x = left + ratio * plot_width
        grid_value = ratio * x_max
        grid_parts.append(
            f'<line x1="{grid_x:.2f}" y1="{top}" x2="{grid_x:.2f}" y2="{top + plot_height}" '
            'stroke="#f3f4f6" stroke-width="1"/>'
            f'<text x="{grid_x:.2f}" y="{top + plot_height + 22}" text-anchor="middle" '
            f'fill="#4b5563" font-size="12">{grid_value:.1f}</text>'
        )

    zero_y = y(0.0)
    zero_line = (
        f'<line x1="{left}" y1="{zero_y:.2f}" x2="{left + plot_width}" y2="{zero_y:.2f}" '
        'stroke="#dc2626" stroke-width="1.8" stroke-dasharray="8 5"/>'
        f'<text x="{left + 8}" y="{zero_y - 7:.2f}" fill="#b91c1c" font-size="12" '
        'font-weight="700">0 g 基线</text>'
    )

    curve_parts: list[str] = []
    legend_parts: list[str] = []
    for index, (curve, samples) in enumerate(relative_curves):
        color = _CURVE_COLORS[index % len(_CURVE_COLORS)]
        plotted_samples = _downsample(samples)
        points = " ".join(
            f"{x(sample.elapsed_s):.2f},{y(sample.value_g):.2f}"
            for sample in plotted_samples
        )
        curve_parts.append(
            f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2.2" '
            'stroke-linejoin="round" stroke-linecap="round"/>'
        )
        final_sample = samples[-1]
        curve_parts.append(
            f'<circle cx="{x(final_sample.elapsed_s):.2f}" cy="{y(final_sample.value_g):.2f}" '
            f'r="4" fill="{color}"/>'
        )
        transition = estimate_coarse_transition(samples, curve.target_weight)
        if transition is not None:
            curve_parts.append(
                f'<circle cx="{x(transition.elapsed_s):.2f}" cy="{y(transition.value_g):.2f}" '
                'r="6" fill="#ffffff" stroke="#d97706" stroke-width="3">'
                "<title>粗加→精加（估算）</title></circle>"
            )
        legend_y = 82 + index * 34
        test_time = curve.started_at[5:19].replace("T", " ") if curve.started_at else curve.run_id
        legend_parts.append(
            f'<line x1="{left + plot_width + 28}" y1="{legend_y}" '
            f'x2="{left + plot_width + 58}" y2="{legend_y}" stroke="{color}" stroke-width="3"/>'
            f'<text x="{left + plot_width + 68}" y="{legend_y + 4}" fill="#111827" font-size="12">'
            f"{escape(test_time)} · 目标 {curve.target_weight:.4f} g · "
            f"最终 {final_sample.value_g:.4f} g</text>"
        )

    empty_message = ""
    if not relative_curves:
        empty_message = (
            f'<text x="{left + plot_width / 2:.2f}" y="{top + plot_height / 2:.2f}" '
            'text-anchor="middle" fill="#9ca3af" font-size="18">等待 S07 天平采样</text>'
        )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">'
        f'<rect width="{width}" height="{height}" fill="#ffffff"/>'
        f'<text x="{left}" y="34" fill="#111827" font-size="22" font-weight="700">'
        "S07 注粉重量—相对时间叠加曲线</text>"
        f'<text x="{left + plot_width + 28}" y="34" fill="#374151" font-size="14">'
        f"测试次数：{len(relative_curves)}</text>"
        + "".join(grid_parts)
        + zero_line
        + f'<line x1="{left}" y1="{top + plot_height}" x2="{left + plot_width}" '
        f'y2="{top + plot_height}" stroke="#374151" stroke-width="1.5"/>'
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_height}" '
        'stroke="#374151" stroke-width="1.5"/>'
        + "".join(curve_parts)
        + empty_message
        + "".join(legend_parts)
        + f'<text x="{left + plot_width / 2:.2f}" y="{height - 22}" text-anchor="middle" '
        'fill="#374151" font-size="14">相对时间（秒，每次测试从 0 开始）</text>'
        f'<text x="24" y="{top + plot_height / 2:.2f}" text-anchor="middle" '
        f'transform="rotate(-90 24 {top + plot_height / 2:.2f})" '
        'fill="#374151" font-size="14">天平重量（g）</text>'
        f'<circle cx="{left + plot_width + 35}" cy="{height - 35}" r="6" '
        'fill="#ffffff" stroke="#d97706" stroke-width="3"/>'
        f'<text x="{left + plot_width + 50}" y="{height - 30}" fill="#92400e" font-size="12">'
        "粗加→精加（估算）</text>"
        "</svg>"
    )


def render_balance_curves_svg(
    output_dir: str | Path = DEFAULT_BALANCE_HISTORY_DIR,
) -> Path:
    """从共享 samples.csv 重新生成多次测试叠加图。"""
    output_path = Path(output_dir).resolve()
    output_path.mkdir(parents=True, exist_ok=True)
    samples_path = output_path / "samples.csv"
    chart_path = output_path / BALANCE_CURVES_FILENAME
    with _FILE_LOCK:
        curves = _read_curves(samples_path)
        temporary_path = chart_path.with_suffix(".svg.tmp")
        temporary_path.write_text(_render_curves_svg(curves), encoding="utf-8")
        temporary_path.replace(chart_path)
    return chart_path


class S07BalanceHistoryRecorder:
    """将每次注粉样本追加到共享 CSV，并实时刷新多次测试叠加曲线。"""

    SAMPLE_HEADER = [
        "run_id",
        "started_at",
        "timestamp",
        "elapsed_s",
        "target_weight_g",
        "balance_g",
        "recipe_name",
        "coarse_position",
        "fine_position",
    ]

    def __init__(
        self,
        *,
        output_dir: str | Path = DEFAULT_BALANCE_HISTORY_DIR,
        target_weight: float,
        recipe_name: str,
        coarse_position: int,
        fine_position: int,
    ) -> None:
        now = datetime.now().astimezone()
        self.run_id = f"{now:%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}"
        self.started_at = now.isoformat(timespec="milliseconds")
        self.output_dir = Path(output_dir).resolve()
        self.samples_path = self.output_dir / "samples.csv"
        self.chart_path = self.output_dir / BALANCE_CURVES_FILENAME
        self.target_weight = float(target_weight)
        self.recipe_name = str(recipe_name)
        self.coarse_position = int(coarse_position)
        self.fine_position = int(fine_position)
        self.samples: list[BalanceSample] = []
        self._first_sample_monotonic: float | None = None
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def record(self, value_g: float) -> BalanceSample:
        now_monotonic = time.monotonic()
        if self._first_sample_monotonic is None:
            self._first_sample_monotonic = now_monotonic
        sample = BalanceSample(
            timestamp=datetime.now().astimezone().isoformat(timespec="milliseconds"),
            elapsed_s=round(now_monotonic - self._first_sample_monotonic, 3),
            value_g=float(value_g),
        )
        self.samples.append(sample)
        self._append_csv(
            [
                self.run_id,
                self.started_at,
                sample.timestamp,
                sample.elapsed_s,
                self.target_weight,
                sample.value_g,
                self.recipe_name,
                self.coarse_position,
                self.fine_position,
            ]
        )
        render_balance_curves_svg(self.output_dir)
        return sample

    def finish(self, *, status: str, final_weight: float | None = None) -> dict[str, Any]:
        del status, final_weight
        render_balance_curves_svg(self.output_dir)
        return self.artifact_info()

    def artifact_info(self) -> dict[str, Any]:
        return {
            "balance_run_id": self.run_id,
            "balance_samples_path": str(self.samples_path),
            "balance_chart_path": str(self.chart_path),
        }

    def _append_csv(self, row: list[Any]) -> None:
        with _FILE_LOCK:
            needs_header = not self.samples_path.exists() or self.samples_path.stat().st_size == 0
            with self.samples_path.open("a", newline="", encoding="utf-8") as csv_file:
                writer = csv.writer(csv_file)
                if needs_header:
                    writer.writerow(self.SAMPLE_HEADER)
                writer.writerow(row)
                csv_file.flush()
