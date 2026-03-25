#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
功能:
    生成色谱图 (TIC / FID) 的积分结果可视化图.
    标注峰区域, 保留时间, 峰面积, 化合物名称等.
参数:
    无.
返回:
    无.
"""

import logging
import textwrap
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import matplotlib
matplotlib.use("Agg")  # 非交互式后端, 不依赖 GUI
import matplotlib.pyplot as plt
import numpy as np

plt.rcParams["axes.unicode_minus"] = False  # 负号正常显示

from .nist_matcher import CompoundMatch
from .peak_integrator import PeakResult

logger = logging.getLogger(__name__)

# 峰区域填充色板
_PEAK_COLORS = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
    "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
]


class ChromatogramPlotter:
    """
    功能:
        绘制色谱图并标注积分峰, 支持 TIC 和 FID.
    参数:
        chromatogram_ppi: TIC/FID 色谱图导出分辨率.
        ms_spectrum_ppi: 质谱图导出分辨率.
        figsize: 图片尺寸 (宽, 高) 英寸.
    返回:
        无.
    """

    def __init__(
        self,
        chromatogram_ppi: int = 150,
        ms_spectrum_ppi: int = 150,
        figsize: tuple = (14, 6),
    ):
        self._chromatogram_ppi = chromatogram_ppi
        self._ms_spectrum_ppi = ms_spectrum_ppi
        self._figsize = figsize

    def plot_chromatogram(
        self,
        times: np.ndarray,
        intensities: np.ndarray,
        peaks: List[PeakResult],
        compound_matches: Optional[Dict[float, List[CompoundMatch]]] = None,
        title: str = "",
        ylabel: str = "Intensity",
        output_path: Optional[Path] = None,
        rt_min: Optional[float] = None,
        rt_max: Optional[float] = None,
        y_range_min: float = 0,
        baseline: Optional[np.ndarray] = None,
        fill_baseline_mode: str = "local",
    ) -> Path:
        """
        功能:
            绘制色谱图, 标注每个峰的积分区域, 保留时间和峰面积.
            TIC 图额外标注化合物名称 (通过 compound_matches 参数).
            密集峰自动调整标注位置避免遮挡.
        参数:
            times: 保留时间数组 (min).
            intensities: 信号强度数组.
            peaks: 峰检测积分结果列表.
            compound_matches: 保留时间 -> 化合物匹配列表, None 表示不标注化合物名.
            title: 图片标题.
            ylabel: Y 轴标签.
            output_path: 输出图片路径.
            rt_min: X 轴最小保留时间 (min), None 使用数据范围.
            rt_max: X 轴最大保留时间 (min), None 使用数据范围.
            y_range_min: Y 轴最小显示范围, 确保小信号图不会过于压缩.
            baseline: 全局基线数组, 与 times/intensities 等长. 提供时用于峰区域填充,
                      None 时回退到峰端点连线基线.
            fill_baseline_mode: 填充基线模式, local 表示局部端点连线, global 表示优先使用传入 baseline.
        返回:
            Path: 保存的图片路径.
        """
        fig, ax = plt.subplots(1, 1, figsize=self._figsize)
        global_baseline_valid = (
            fill_baseline_mode == "global"
            and baseline is not None
            and len(baseline) == len(times)
        )
        if fill_baseline_mode == "global" and not global_baseline_valid:
            logger.warning("全局基线填充不可用, 自动回退到局部基线填充.")

        # 绘制色谱基线
        ax.plot(times, intensities, color="black", linewidth=0.6, label="Signal")

        for i, peak in enumerate(peaks):
            color = _PEAK_COLORS[i % len(_PEAK_COLORS)]

            # 填充峰区域: 从基线到信号, 与积分计算一致
            mask = (times >= peak.start_time) & (times <= peak.end_time)
            if mask.any():
                peak_times = times[mask]
                peak_intensities = intensities[mask]
                if global_baseline_valid:
                    peak_baseline = np.minimum(baseline[mask], peak_intensities)
                else:
                    # 默认使用局部端点连线, 与积分算法保持一致.
                    peak_baseline = np.linspace(
                        peak_intensities[0],
                        peak_intensities[-1],
                        len(peak_times),
                    )
                ax.fill_between(
                    peak_times, peak_baseline, peak_intensities,
                    alpha=0.3, color=color,
                )

        # X 轴范围: 受 rt_min / rt_max 控制
        x_lo = rt_min if rt_min is not None else float(times[0])
        x_hi = rt_max if rt_max is not None else float(times[-1])
        ax.set_xlim(x_lo, x_hi)

        # Y 轴范围: 基于可见区域内信号最大值, 留上方余量给标注
        visible_mask = (times >= x_lo) & (times <= x_hi)
        if visible_mask.any():
            y_max = float(intensities[visible_mask].max())
            # 确保最小显示范围
            y_max = max(y_max, y_range_min)
            # 留 30% 上方空间给标注文字
            ax.set_ylim(bottom=0, top=y_max * 1.30)

        # 标注峰: 按峰高降序排列, 高峰优先占据正上方位置
        sorted_peaks = sorted(enumerate(peaks), key=lambda x: x[1].height, reverse=True)
        annotations = self._annotate_peaks(
            ax, fig, sorted_peaks, compound_matches, x_lo, x_hi,
        )

        ax.set_xlabel("Retention Time (min)")
        ax.set_ylabel(ylabel)
        ax.set_title(title)
        ax.ticklabel_format(axis="y", style="sci", scilimits=(0, 0))

        plt.tight_layout()

        if output_path is None:
            output_path = Path("chromatogram.png")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(str(output_path), dpi=self._chromatogram_ppi, bbox_inches="tight")
        plt.close(fig)

        logger.info("色谱图已保存: %s", output_path)
        return output_path

    def _annotate_peaks(
        self,
        ax: plt.Axes,
        fig: plt.Figure,
        sorted_peaks: List[Tuple[int, PeakResult]],
        compound_matches: Optional[Dict[float, List[CompoundMatch]]],
        x_lo: float,
        x_hi: float,
    ) -> None:
        """
        功能:
            为所有峰添加标注 (RT + 面积 + 化合物名), 自动调整偏移避免遮挡.
            使用 display 坐标检测文字碰撞, 密集区域交替左右偏移.
        参数:
            ax: matplotlib 坐标轴.
            fig: matplotlib 图形 (用于坐标变换).
            sorted_peaks: 按峰高降序排列的 (原始索引, PeakResult) 列表.
            compound_matches: 化合物匹配字典.
            x_lo: X 轴显示下限.
            x_hi: X 轴显示上限.
        返回:
            无.
        """
        # 收集所有标注信息
        label_items = []
        for _, peak in sorted_peaks:
            # 跳过不在显示范围内的峰
            if peak.retention_time < x_lo or peak.retention_time > x_hi:
                continue

            # 构建标注文本: RT + 面积
            area_str = self._format_area(peak.area)
            label_text = f"RT {peak.retention_time:.2f}\nArea: {area_str}"

            # TIC 图标注化合物名称 (显示全名)
            if compound_matches is not None:
                match_list = self._find_match(peak.retention_time, compound_matches)
                if match_list is not None and len(match_list) > 0:
                    name = self._wrap_compound_name(match_list[0].compound_name)
                    label_text += f"\n{name}"

            label_items.append((peak, label_text))

        # 第一遍: 绘制所有标注, 默认正上方
        fig.canvas.draw()  # 需要先渲染才能获取文字 bbox
        placed_bboxes = []  # 已放置标注的 display 坐标 bbox 列表

        for peak, label_text in label_items:
            # 计算偏移: 检查是否与已有标注重叠
            offset_x, offset_y = self._compute_offset(
                ax, fig, peak, placed_bboxes, label_text,
            )

            ann = ax.annotate(
                label_text,
                xy=(peak.retention_time, peak.height),
                xytext=(offset_x, offset_y),
                textcoords="offset points",
                ha="center", va="bottom",
                fontsize=6,
                arrowprops=dict(arrowstyle="-", color="gray", lw=0.5),
            )

            # 记录已放置标注的 bbox
            fig.canvas.draw()
            bbox = ann.get_window_extent(renderer=fig.canvas.get_renderer())
            placed_bboxes.append(bbox)

    def _compute_offset(
        self,
        ax: plt.Axes,
        fig: plt.Figure,
        peak: PeakResult,
        placed_bboxes: list,
        label_text: str,
    ) -> Tuple[float, float]:
        """
        功能:
            计算标注的 (x_offset, y_offset) 偏移量 (单位: points).
            若正上方位置与已有标注重叠, 则交替向左/右偏移, 直到不重叠.
        参数:
            ax: 坐标轴.
            fig: 图形.
            peak: 当前峰.
            placed_bboxes: 已放置标注的 display bbox 列表.
            label_text: 标注文本.
        返回:
            Tuple[float, float]: (x_offset, y_offset) 偏移量, 单位 points.
        """
        if not placed_bboxes:
            return (0, 8)

        renderer = fig.canvas.get_renderer()
        # 创建临时标注来获取文字尺寸
        tmp = ax.annotate(
            label_text,
            xy=(peak.retention_time, peak.height),
            xytext=(0, 8),
            textcoords="offset points",
            ha="center", va="bottom",
            fontsize=6,
        )
        fig.canvas.draw()
        tmp_bbox = tmp.get_window_extent(renderer)
        tmp.remove()

        # 检查是否与已有标注重叠
        if not self._has_overlap(tmp_bbox, placed_bboxes):
            return (0, 8)

        # 仅沿 y 方向逐级上移, 避免标注重叠
        for shift in range(1, 8):
            y_off = 8 + shift * 12  # 同时逐级上移避免箭头交叉

            tmp2 = ax.annotate(
                label_text,
                xy=(peak.retention_time, peak.height),
                xytext=(0, y_off),
                textcoords="offset points",
                ha="center", va="bottom",
                fontsize=6,
            )
            fig.canvas.draw()
            bbox2 = tmp2.get_window_extent(renderer)
            tmp2.remove()

            if not self._has_overlap(bbox2, placed_bboxes):
                return (0, y_off)

        # 所有位置都重叠, 用最大偏移
        return (0, 8 + 7 * 12)

    @staticmethod
    def _has_overlap(bbox, placed_bboxes: list, padding: float = 2.0) -> bool:
        """
        功能:
            检查 bbox 是否与已放置的任一标注 bbox 重叠.
        参数:
            bbox: 待检测的 display 坐标 bbox.
            placed_bboxes: 已放置标注的 bbox 列表.
            padding: 额外间距 (display pixels).
        返回:
            bool: True 表示有重叠.
        """
        expanded = bbox.expanded(
            1 + padding / max(bbox.width, 1),
            1 + padding / max(bbox.height, 1),
        )
        for existing in placed_bboxes:
            if expanded.overlaps(existing):
                return True
        return False

    @staticmethod
    def _format_area(area: float) -> str:
        """
        功能:
            将峰面积格式化为紧凑的科学计数法字符串.
        参数:
            area: 峰面积.
        返回:
            str: 格式化字符串, 如 "1.23e6", "456".
        """
        if abs(area) >= 1e6:
            return f"{area:.2e}"
        elif abs(area) >= 1000:
            return f"{area:.0f}"
        elif abs(area) >= 1:
            return f"{area:.2f}"
        else:
            return f"{area:.4f}"

    @staticmethod
    def _wrap_compound_name(name: str, max_line_length: int = 22) -> str:
        """
        功能:
            对过长化合物名称自动换行, 减少峰标注的横向占位.
        参数:
            name: 原始化合物名称.
            max_line_length: 单行最大字符数.
        返回:
            str, 插入换行后的化合物名称.
        """
        normalized_name = " ".join(str(name).split())
        if len(normalized_name) <= max_line_length:
            return normalized_name

        # 优先按空格和连字符断行, 实在过长时再切分连续长串.
        return textwrap.fill(
            normalized_name,
            width=max_line_length,
            break_long_words=True,
            break_on_hyphens=True,
        )

    def plot_ms_spectrum(
        self,
        mz: np.ndarray,
        intensities: np.ndarray,
        title: str = "",
        output_path: Optional[Path] = None,
        top_n_labels: int = 10,
    ) -> Path:
        """
        功能:
            绘制单个峰的质谱棒状图 (m/z vs relative intensity).
            自动标注强度最高的 top_n_labels 个离子的 m/z 值.
        参数:
            mz: m/z 数组.
            intensities: 强度数组.
            title: 图片标题.
            output_path: 输出图片路径, None 使用默认路径.
            top_n_labels: 标注 m/z 值的最强峰数量.
        返回:
            Path: 保存的图片路径.
        """
        fig, ax = plt.subplots(1, 1, figsize=(12, 5))

        # 归一化为相对强度 (%)
        max_intensity = intensities.max() if len(intensities) > 0 else 1.0
        if max_intensity == 0:
            max_intensity = 1.0
        rel_intensities = intensities / max_intensity * 100.0

        # 棒状图绘制
        ax.vlines(mz, 0, rel_intensities, colors="#1f77b4", linewidth=0.8)

        # 标注最强的 top_n_labels 个峰的 m/z 值
        if len(mz) > 0:
            n = min(top_n_labels, len(mz))
            top_indices = np.argsort(rel_intensities)[-n:]
            for idx in top_indices:
                ax.annotate(
                    f"{mz[idx]:.1f}",
                    xy=(mz[idx], rel_intensities[idx]),
                    xytext=(0, 4),
                    textcoords="offset points",
                    ha="center", va="bottom",
                    fontsize=7,
                    color="#333333",
                )

        ax.set_xlabel("m/z")
        ax.set_ylabel("Relative Intensity (%)")
        ax.set_title(title)
        ax.set_ylim(bottom=0, top=110)

        # X 轴留边距
        if len(mz) > 0:
            margin = (mz.max() - mz.min()) * 0.05
            ax.set_xlim(mz.min() - max(margin, 5), mz.max() + max(margin, 5))

        plt.tight_layout()

        if output_path is None:
            output_path = Path("ms_spectrum.png")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(str(output_path), dpi=self._ms_spectrum_ppi, bbox_inches="tight")
        plt.close(fig)

        logger.info("质谱图已保存: %s", output_path)
        return output_path

    @staticmethod
    def _find_match(
        rt: float,
        matches: Dict[float, List[CompoundMatch]],
        tolerance: float = 0.05,
    ) -> Optional[List[CompoundMatch]]:
        """按保留时间查找最接近的化合物匹配."""
        if not matches:
            return None
        closest_rt = min(matches.keys(), key=lambda r: abs(r - rt))
        if abs(closest_rt - rt) <= tolerance:
            return matches[closest_rt]
        return None
