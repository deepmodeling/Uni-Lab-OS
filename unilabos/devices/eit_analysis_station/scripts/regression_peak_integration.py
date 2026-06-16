#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
功能:
    对 robust_v3 积分方案执行快速回归检查.
    覆盖合成肩峰场景, 合成真实双峰场景, 以及本地 725/760 样本的 TIC/FID 场景.
参数:
    --data-root: 样本根目录, 默认 eit_analysis_station/data.
返回:
    进程退出码, 0 表示通过, 1 表示失败.
"""

import argparse
import logging
import sys
from pathlib import Path
from typing import Callable, List, Optional

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from unilabos.devices.eit_analysis_station.processor.data_reader import GCMSDataReader
from unilabos.devices.eit_analysis_station.processor.peak_integrator import PeakIntegrator, PeakResult

logger = logging.getLogger(__name__)


def _build_integrator(
    prominence: float,
    min_distance: int,
    *,
    integration_mode: str = "robust_v3",
    shoulder_filter_enable: bool = True,
    tail_artifact_filter_enable: bool = True,
    tail_monotonic_filter_enable: bool = True,
    tail_monotonic_ratio_max: float = 0.25,
    max_peak_width_min: float = 0.5,
    leading_edge_filter_enable: bool = True,
    leading_edge_relative_prominence_max: float = 0.25,
    leading_edge_monotonic_ratio_min: float = 0.65,
) -> PeakIntegrator:
    """
    功能:
        构造指定模式的积分器.
    参数:
        prominence: 峰检测 prominence.
        min_distance: 峰最小间距.
        integration_mode: 积分模式.
        shoulder_filter_enable: 是否启用肩峰过滤.
        tail_artifact_filter_enable: 是否启用三条件拖尾假峰过滤.
        tail_monotonic_filter_enable: 是否启用平滑信号单调下降拖尾过滤.
        tail_monotonic_ratio_max: 单调下降判定上升步占比上限.
        max_peak_width_min: 峰最大边界宽度(min), 超出视为基线抬升假峰, 设0关闭.
        leading_edge_filter_enable: 是否启用前沿假峰过滤.
        leading_edge_relative_prominence_max: 前沿假峰相对后峰显著性上限.
        leading_edge_monotonic_ratio_min: 前沿假峰判定上升步占比下限.
    返回:
        PeakIntegrator.
    """
    return PeakIntegrator(
        smoothing_window=11,
        prominence=prominence,
        min_distance=min_distance,
        integration_mode=integration_mode,
        baseline_method="rolling_quantile",
        baseline_quantile=20.0,
        baseline_window_min=0.9,
        boundary_sigma_factor=3.0,
        boundary_edge_ratio=0.005,
        boundary_expand_factor=6.0,
        boundary_min_span_min=0.08,
        boundary_max_span_min=2.0,
        shoulder_filter_enable=shoulder_filter_enable,
        shoulder_filter_width_max_min=0.035,
        shoulder_filter_gap_max_min=0.09,
        shoulder_filter_relative_prominence_max=0.15,
        tail_artifact_filter_enable=tail_artifact_filter_enable,
        tail_artifact_gap_max_min=0.20,
        tail_artifact_relative_prominence_max=0.15,
        tail_artifact_half_width_asymmetry_min=2.0,
        tail_monotonic_filter_enable=tail_monotonic_filter_enable,
        tail_monotonic_ratio_max=tail_monotonic_ratio_max,
        max_peak_width_min=max_peak_width_min,
        leading_edge_filter_enable=leading_edge_filter_enable,
        leading_edge_relative_prominence_max=leading_edge_relative_prominence_max,
        leading_edge_monotonic_ratio_min=leading_edge_monotonic_ratio_min,
    )


def _gaussian(times: np.ndarray, center: float, sigma: float, amplitude: float) -> np.ndarray:
    """
    功能:
        生成高斯峰信号.
    参数:
        times: 时间数组.
        center: 中心时间.
        sigma: 峰宽参数.
        amplitude: 峰高.
    返回:
        np.ndarray.
    """
    return amplitude * np.exp(-0.5 * ((times - center) / sigma) ** 2)


def _has_peak(peaks: List[PeakResult], target_rt: float, tolerance: float) -> bool:
    """
    功能:
        判断峰列表中是否存在命中目标保留时间的峰.
    参数:
        peaks: 峰列表.
        target_rt: 目标保留时间.
        tolerance: 容差(min).
    返回:
        bool, True 表示命中.
    """
    for peak in peaks:
        if abs(peak.retention_time - target_rt) <= tolerance:
            return True
    return False


def _has_peak_in_window(peaks: List[PeakResult], rt_min: float, rt_max: float) -> bool:
    """
    功能:
        判断峰列表中是否存在落入窗口范围的峰.
    参数:
        peaks: 峰列表.
        rt_min: 保留时间下限.
        rt_max: 保留时间上限.
    返回:
        bool, True 表示存在峰.
    """
    for peak in peaks:
        if rt_min <= peak.retention_time <= rt_max:
            return True
    return False


def _rounded_rts(peaks: List[PeakResult]) -> List[float]:
    """
    功能:
        提取峰列表的保留时间并做固定精度归一化.
    参数:
        peaks: 峰列表.
    返回:
        List[float], 四舍五入后的保留时间列表.
    """
    return [round(item.retention_time, 4) for item in peaks]


def _closest_peak(peaks: List[PeakResult], target_rt: float) -> PeakResult:
    """
    功能:
        找到最接近目标保留时间的峰.
    参数:
        peaks: 峰列表.
        target_rt: 目标保留时间.
    返回:
        PeakResult.
    """
    return min(peaks, key=lambda item: abs(item.retention_time - target_rt))


def run_synthetic_doublet_regression() -> bool:
    """
    功能:
        验证 robust_v3 不会误伤真实双峰.
    参数:
        无.
    返回:
        bool, True 表示通过.
    """
    rng = np.random.default_rng(77)
    times = np.arange(4.0, 10.0, 0.01)
    baseline = (
        2.8e4
        + 4.2e3 * np.sin((times - 4.0) * 0.9)
        + 1.5e4 * np.exp(-0.5 * ((times - 7.0) / 1.2) ** 2)
    )
    first_peak = _gaussian(times, center=6.85, sigma=0.006, amplitude=2.8e6)
    second_peak = _gaussian(times, center=6.98, sigma=0.012, amplitude=1.5e5)
    signal = baseline + first_peak + second_peak + rng.normal(0.0, 1200.0, size=len(times))

    integrator = _build_integrator(prominence=50000.0, min_distance=5)
    peaks = integrator.integrate(times, signal)
    if _has_peak(peaks, 6.85, 0.03) is False:
        logger.error("合成真双峰回归失败: 未保留 6.85 min 主峰.")
        return False
    if _has_peak(peaks, 6.98, 0.04) is False:
        logger.error("合成真双峰回归失败: 未保留 6.98 min 近邻峰.")
        return False

    logger.info("合成真双峰回归通过: peaks=%s", _rounded_rts(peaks))
    return True


def run_synthetic_shoulder_regression() -> bool:
    """
    功能:
        验证 robust_v3 可以过滤拖尾肩峰, 且关闭肩峰过滤时退化为 robust_v2.
    参数:
        无.
    返回:
        bool, True 表示通过.
    """
    rng = np.random.default_rng(123)
    times = np.arange(8.0, 9.2, 0.01)
    baseline = 2.2e4 + 3.5e3 * np.sin((times - 8.0) * 1.7)
    first_peak = _gaussian(times, center=8.66, sigma=0.012, amplitude=3.2e7)
    shoulder_peak = _gaussian(times, center=8.74, sigma=0.004, amplitude=4.0e5)
    second_peak = _gaussian(times, center=8.83, sigma=0.011, amplitude=5.5e6)
    signal = baseline + first_peak + shoulder_peak + second_peak + rng.normal(0.0, 2500.0, size=len(times))

    peaks_v2 = _build_integrator(
        prominence=10000.0,
        min_distance=5,
        integration_mode="robust_v2",
        shoulder_filter_enable=False,
    ).integrate(times, signal)
    peaks_v3 = _build_integrator(
        prominence=10000.0,
        min_distance=5,
        integration_mode="robust_v3",
        shoulder_filter_enable=True,
    ).integrate(times, signal)
    peaks_v3_disabled = _build_integrator(
        prominence=10000.0,
        min_distance=5,
        integration_mode="robust_v3",
        shoulder_filter_enable=False,
        tail_artifact_filter_enable=False,
        tail_monotonic_filter_enable=False,
        leading_edge_filter_enable=False,
        max_peak_width_min=0.0,
    ).integrate(times, signal)

    if _has_peak_in_window(peaks_v2, 8.72, 8.77) is False:
        logger.error("合成肩峰回归失败: robust_v2 未检出预期肩峰, 当前样本构造失效.")
        return False
    if _has_peak_in_window(peaks_v3, 8.72, 8.77) is True:
        logger.error("合成肩峰回归失败: robust_v3 仍保留 8.72-8.77 min 肩峰.")
        return False
    if _has_peak(peaks_v3, 8.66, 0.03) is False or _has_peak(peaks_v3, 8.83, 0.03) is False:
        logger.error("合成肩峰回归失败: robust_v3 误删了主峰.")
        return False
    merged_peak = _closest_peak(peaks_v3, 8.66)
    if merged_peak.end_time < 8.74:
        logger.error(
            "合成肩峰回归失败: robust_v3 未将肩峰面积并入前峰. 前峰结束时间=%.4f",
            merged_peak.end_time,
        )
        return False
    if _rounded_rts(peaks_v2) != _rounded_rts(peaks_v3_disabled):
        logger.error(
            "合成肩峰回归失败: robust_v3 关闭肩峰过滤后未退化为 robust_v2. v2=%s, v3_disabled=%s",
            _rounded_rts(peaks_v2),
            _rounded_rts(peaks_v3_disabled),
        )
        return False

    logger.info(
        "合成肩峰回归通过: robust_v2=%s, robust_v3=%s",
        _rounded_rts(peaks_v2),
        _rounded_rts(peaks_v3),
    )
    return True


def run_synthetic_tail_artifact_regression() -> bool:
    """
    功能:
        验证 robust_v3 可以过滤强峰拖尾后的单侧假峰.
    参数:
        无.
    返回:
        bool, True 表示通过.
    """
    rng = np.random.default_rng(7)
    times = np.arange(8.0, 9.2, 0.01)
    baseline = 1.8e4 + 2.5e3 * np.sin((times - 8.0) * 1.1)
    main_peak = _gaussian(times, center=8.67, sigma=0.006, amplitude=1.8e7)
    tail_signal = np.where(times > 8.67, 2.5e5 * np.exp(-(times - 8.67) / 0.06), 0.0)
    signal = baseline + main_peak + tail_signal + rng.normal(0.0, 1800.0, size=len(times))

    peaks_disabled = _build_integrator(
        prominence=10000.0,
        min_distance=5,
        integration_mode="robust_v3",
        shoulder_filter_enable=True,
        tail_artifact_filter_enable=False,
    ).integrate(times, signal)
    peaks_enabled = _build_integrator(
        prominence=10000.0,
        min_distance=5,
        integration_mode="robust_v3",
        shoulder_filter_enable=True,
        tail_artifact_filter_enable=True,
    ).integrate(times, signal)

    if len(peaks_disabled) < 2 or _has_peak_in_window(peaks_disabled, 8.72, 8.78) is False:
        logger.error(
            "合成拖尾假峰回归失败: 关闭拖尾假峰过滤时, 未保留预期尾部伪峰. peaks=%s",
            _rounded_rts(peaks_disabled),
        )
        return False
    if len(peaks_enabled) != 1 or _has_peak(peaks_enabled, 8.67, 0.03) is False:
        logger.error(
            "合成拖尾假峰回归失败: 启用拖尾假峰过滤后, 主峰保留结果异常. peaks=%s",
            _rounded_rts(peaks_enabled),
        )
        return False

    merged_peak = _closest_peak(peaks_enabled, 8.67)
    if merged_peak.end_time < 8.74:
        logger.error(
            "合成拖尾假峰回归失败: 启用拖尾假峰过滤后, 主峰未吸收尾部面积. end_time=%.4f",
            merged_peak.end_time,
        )
        return False

    logger.info(
        "合成拖尾假峰回归通过: disabled=%s, enabled=%s",
        _rounded_rts(peaks_disabled),
        _rounded_rts(peaks_enabled),
    )
    return True


def run_synthetic_monotonic_tail_regression() -> bool:
    """
    功能:
        验证 robust_v3 可以过滤强峰长距离拖尾产生的基线校正假峰.
        模拟 771-5 场景: 大峰 RT 6.10, 单调指数衰减尾部在 ~6.19 产生假峰.
    参数:
        无.
    返回:
        bool, True 表示通过.
    """
    rng = np.random.default_rng(771)
    times = np.arange(5.5, 7.0, 0.01)
    # 缓变背景基线
    baseline = 1.5e4 + 2.0e3 * np.sin((times - 5.5) * 0.8)
    # 主峰: 强且窄, 中心 6.10
    main_peak = _gaussian(times, center=6.10, sigma=0.008, amplitude=2.3e6)
    # 长指数拖尾: 从 6.10 开始衰减, 时间常数 0.08 min
    tail_signal = np.where(
        times > 6.10,
        3.5e5 * np.exp(-(times - 6.10) / 0.08),
        0.0,
    )
    signal = baseline + main_peak + tail_signal + rng.normal(0.0, 1500.0, size=len(times))

    # 启用全部过滤: 拖尾假峰应被合并
    peaks_enabled = _build_integrator(
        prominence=10000.0,
        min_distance=5,
        integration_mode="robust_v3",
        shoulder_filter_enable=True,
        tail_artifact_filter_enable=True,
        tail_monotonic_filter_enable=True,
    ).integrate(times, signal)

    # 关闭全部过滤: 验证假峰确实产生(确认测试构造有效)
    peaks_disabled = _build_integrator(
        prominence=10000.0,
        min_distance=5,
        integration_mode="robust_v3",
        shoulder_filter_enable=True,
        tail_artifact_filter_enable=False,
        tail_monotonic_filter_enable=False,
    ).integrate(times, signal)

    if _has_peak_in_window(peaks_disabled, 6.15, 6.25) is False:
        logger.warning(
            "合成单调拖尾回归: 关闭过滤时未检出拖尾区域假峰, 测试构造可能需调整. peaks=%s",
            _rounded_rts(peaks_disabled),
        )

    if _has_peak_in_window(peaks_enabled, 6.15, 6.25) is True:
        logger.error(
            "合成单调拖尾回归失败: 启用过滤后, 拖尾区域仍保留假峰. peaks=%s",
            _rounded_rts(peaks_enabled),
        )
        return False

    if _has_peak(peaks_enabled, 6.10, 0.03) is False:
        logger.error(
            "合成单调拖尾回归失败: 主峰 RT=6.10 未保留. peaks=%s",
            _rounded_rts(peaks_enabled),
        )
        return False

    logger.info(
        "合成单调拖尾回归通过: enabled=%s, disabled=%s",
        _rounded_rts(peaks_enabled),
        _rounded_rts(peaks_disabled),
    )
    return True


def _load_detector_signal(
    reader: GCMSDataReader,
    d_dir: Path,
    detector: str,
) -> Optional[tuple]:
    """
    功能:
        读取指定检测器的色谱信号.
    参数:
        reader: 数据读取器.
        d_dir: 样本目录.
        detector: 检测器类型, 仅支持 tic/fid.
    返回:
        Optional[tuple], 成功时返回(times, intensities), 失败时返回 None.
    """
    try:
        if detector == "tic":
            return reader.read_tic(d_dir)
        if detector == "fid":
            return reader.read_fid(d_dir)
    except Exception as exc:
        logger.error("样本 %s %s 读取失败: %s", d_dir.name, detector.upper(), exc)
        return None

    logger.error("未知检测器类型: %s", detector)
    return None


def _run_real_case(
    reader: GCMSDataReader,
    d_dir: Path,
    detector: str,
    prominence: float,
    min_distance: int,
    required_rts: List[float],
    forbidden_window: Optional[tuple] = None,
) -> bool:
    """
    功能:
        执行单个真实样本回归检查.
    参数:
        reader: 数据读取器.
        d_dir: 样本目录.
        detector: 检测器类型.
        prominence: 峰检测 prominence.
        min_distance: 峰最小间距.
        required_rts: 必须保留的 RT 列表.
        forbidden_window: 禁止出现峰的 RT 窗口.
    返回:
        bool, True 表示通过.
    """
    signal = _load_detector_signal(reader, d_dir, detector)
    if signal is None:
        return False

    times, intensities = signal
    peaks = _build_integrator(
        prominence=prominence,
        min_distance=min_distance,
        integration_mode="robust_v3",
        shoulder_filter_enable=True,
    ).integrate(times, intensities)
    peaks = [item for item in peaks if 4.0 <= item.retention_time <= 10.0]
    if len(peaks) == 0:
        logger.error("样本 %s %s 回归失败: 4-10 min 未检出峰.", d_dir.name, detector.upper())
        return False

    for target_rt in required_rts:
        if _has_peak(peaks, target_rt, 0.03) is False:
            logger.error(
                "样本 %s %s 回归失败: 未保留目标峰 RT=%.3f. peaks=%s",
                d_dir.name,
                detector.upper(),
                target_rt,
                _rounded_rts(peaks),
            )
            return False

    if detector == "tic" and d_dir.name == "760-2.D":
        merged_peak = _closest_peak(peaks, 8.6666)
        if merged_peak.end_time < 8.76:
            logger.error(
                "样本 %s %s 回归失败: 8.6666 min 前峰未吸收肩峰面积. end_time=%.4f, peaks=%s",
                d_dir.name,
                detector.upper(),
                merged_peak.end_time,
                _rounded_rts(peaks),
            )
            return False

    if forbidden_window is not None:
        window_min, window_max = forbidden_window
        if _has_peak_in_window(peaks, window_min, window_max) is True:
            logger.error(
                "样本 %s %s 回归失败: 禁止窗口 %.3f-%.3f min 仍存在峰. peaks=%s",
                d_dir.name,
                detector.upper(),
                window_min,
                window_max,
                _rounded_rts(peaks),
            )
            return False

    logger.info("真实样本回归通过: sample=%s, detector=%s, peaks=%s", d_dir.name, detector.upper(), _rounded_rts(peaks))
    return True


def run_real_sample_regression(data_root: Path) -> bool:
    """
    功能:
        对 725/760 真实样本执行 TIC 和 FID 回归检查.
    参数:
        data_root: 样本根目录.
    返回:
        bool, True 表示通过.
    """
    reader = GCMSDataReader()
    cases = [
        {
            "d_dir": data_root / "760" / "760-2.D",
            "detector": "tic",
            "prominence": 10000.0,
            "min_distance": 5,
            "required_rts": [8.6666, 8.8273],
            "forbidden_window": (8.72, 8.77),
        },
        {
            "d_dir": data_root / "725" / "725-1.D",
            "detector": "tic",
            "prominence": 50000.0,
            "min_distance": 5,
            "required_rts": [6.8492, 6.9797],
            "forbidden_window": None,
        },
        {
            "d_dir": data_root / "760" / "760-2.D",
            "detector": "fid",
            "prominence": 0.5,
            "min_distance": 50,
            "required_rts": [6.8403, 6.9683, 8.6597, 8.8140],
            "forbidden_window": None,
        },
        {
            "d_dir": data_root / "725" / "725-1.D",
            "detector": "fid",
            "prominence": 0.5,
            "min_distance": 50,
            "required_rts": [6.8427, 6.9723],
            "forbidden_window": None,
        },
    ]

    all_ok = True
    for case in cases:
        d_dir = case["d_dir"]
        if d_dir.is_dir() is False:
            logger.warning("样本目录不存在, 跳过真实样本回归: %s", d_dir)
            continue
        case_ok = _run_real_case(
            reader=reader,
            d_dir=d_dir,
            detector=case["detector"],
            prominence=case["prominence"],
            min_distance=case["min_distance"],
            required_rts=case["required_rts"],
            forbidden_window=case["forbidden_window"],
        )
        if case_ok is False:
            all_ok = False

    return all_ok


def run_robust_v2_compat_smoke(data_root: Path) -> bool:
    """
    功能:
        验证显式选择 robust_v2, 或在 robust_v3 中关闭肩峰过滤时, 行为保持一致.
    参数:
        data_root: 样本根目录.
    返回:
        bool, True 表示通过.
    """
    reader = GCMSDataReader()
    cases = [
        (data_root / "760" / "760-2.D", "tic", 10000.0, 5),
        (data_root / "760" / "760-2.D", "fid", 0.5, 50),
    ]

    all_ok = True
    for d_dir, detector, prominence, min_distance in cases:
        if d_dir.is_dir() is False:
            logger.warning("样本目录不存在, 跳过 robust_v2 兼容烟雾测试: %s", d_dir)
            continue

        signal = _load_detector_signal(reader, d_dir, detector)
        if signal is None:
            all_ok = False
            continue

        times, intensities = signal
        peaks_v2 = _build_integrator(
            prominence=prominence,
            min_distance=min_distance,
            integration_mode="robust_v2",
            shoulder_filter_enable=False,
            max_peak_width_min=0.0,
        ).integrate(times, intensities)
        peaks_v3_disabled = _build_integrator(
            prominence=prominence,
            min_distance=min_distance,
            integration_mode="robust_v3",
            shoulder_filter_enable=False,
            tail_artifact_filter_enable=False,
            tail_monotonic_filter_enable=False,
            leading_edge_filter_enable=False,
            max_peak_width_min=0.0,
        ).integrate(times, intensities)

        if _rounded_rts(peaks_v2) != _rounded_rts(peaks_v3_disabled):
            logger.error(
                "robust_v2 兼容烟雾测试失败: sample=%s, detector=%s, v2=%s, v3_disabled=%s",
                d_dir.name,
                detector.upper(),
                _rounded_rts(peaks_v2),
                _rounded_rts(peaks_v3_disabled),
            )
            all_ok = False
            continue

        logger.info(
            "robust_v2 兼容烟雾测试通过: sample=%s, detector=%s, peaks=%s",
            d_dir.name,
            detector.upper(),
            _rounded_rts(peaks_v2),
        )

    return all_ok


def run_synthetic_leading_edge_regression() -> bool:
    """
    功能:
        验证 robust_v3 可以丢弃强峰上升沿(前沿)上的假峰.
        模拟 771-7 场景: 大峰 RT 11.00 的上升沿在 ~10.89 产生假峰.
    参数:
        无.
    返回:
        bool, True 表示通过.
    """
    rng = np.random.default_rng(7717)
    times = np.arange(10.0, 12.0, 0.01)
    # 缓变背景基线
    baseline = 2.0e4 + 3.0e3 * np.sin((times - 10.0) * 1.2)
    # 主峰: 强且窄, 中心 11.00
    main_peak = _gaussian(times, center=11.00, sigma=0.010, amplitude=2.0e6)
    # 前沿小扰动: 在上升沿 10.89 处叠加一个微弱的窄高斯
    leading_bump = _gaussian(times, center=10.89, sigma=0.005, amplitude=6.0e4)
    signal = baseline + main_peak + leading_bump + rng.normal(0.0, 1500.0, size=len(times))

    # 启用前沿过滤: 假峰应被丢弃
    peaks_enabled = _build_integrator(
        prominence=10000.0,
        min_distance=5,
        integration_mode="robust_v3",
        leading_edge_filter_enable=True,
    ).integrate(times, signal)

    # 关闭前沿过滤: 验证假峰确实被检出(确认测试构造有效)
    peaks_disabled = _build_integrator(
        prominence=10000.0,
        min_distance=5,
        integration_mode="robust_v3",
        leading_edge_filter_enable=False,
    ).integrate(times, signal)

    if _has_peak_in_window(peaks_disabled, 10.85, 10.93) is False:
        logger.warning(
            "合成前沿假峰回归: 关闭过滤时未检出前沿区域假峰, 测试构造可能需调整. peaks=%s",
            _rounded_rts(peaks_disabled),
        )

    # 验证启用过滤后前沿假峰被丢弃
    if _has_peak_in_window(peaks_enabled, 10.85, 10.93) is True:
        logger.error(
            "合成前沿假峰回归失败: 启用过滤后, 前沿区域仍保留假峰. peaks=%s",
            _rounded_rts(peaks_enabled),
        )
        return False

    # 验证主峰保留
    if _has_peak(peaks_enabled, 11.00, 0.03) is False:
        logger.error(
            "合成前沿假峰回归失败: 主峰 RT=11.00 未保留. peaks=%s",
            _rounded_rts(peaks_enabled),
        )
        return False

    logger.info(
        "合成前沿假峰回归通过: enabled=%s, disabled=%s",
        _rounded_rts(peaks_enabled),
        _rounded_rts(peaks_disabled),
    )
    return True


def run_synthetic_leading_edge_doublet_safety_regression() -> bool:
    """
    功能:
        验证前沿过滤不会误伤真实近邻双峰(两峰 prominence 接近的情况).
    参数:
        无.
    返回:
        bool, True 表示通过.
    """
    rng = np.random.default_rng(8888)
    times = np.arange(10.0, 12.0, 0.01)
    baseline = 2.0e4 + 2.0e3 * np.sin((times - 10.0) * 0.8)
    # 两个 prominence 接近的真实峰
    peak_a = _gaussian(times, center=10.80, sigma=0.010, amplitude=1.0e6)
    peak_b = _gaussian(times, center=11.00, sigma=0.012, amplitude=1.5e6)
    signal = baseline + peak_a + peak_b + rng.normal(0.0, 1500.0, size=len(times))

    peaks = _build_integrator(
        prominence=10000.0,
        min_distance=5,
        integration_mode="robust_v3",
        leading_edge_filter_enable=True,
    ).integrate(times, signal)

    if _has_peak(peaks, 10.80, 0.03) is False:
        logger.error(
            "前沿过滤安全回归失败: RT=10.80 的真实峰被误删. peaks=%s",
            _rounded_rts(peaks),
        )
        return False

    if _has_peak(peaks, 11.00, 0.03) is False:
        logger.error(
            "前沿过滤安全回归失败: RT=11.00 的真实峰被误删. peaks=%s",
            _rounded_rts(peaks),
        )
        return False

    logger.info(
        "前沿过滤安全回归通过: peaks=%s",
        _rounded_rts(peaks),
    )
    return True


def run_synthetic_wide_baseline_peak_regression() -> bool:
    """
    功能:
        验证 robust_v3 可以过滤由柱流失基线抬升产生的超宽假峰.
        模拟 771 系列场景: 渐增基线上的宽驼峰被误检为峰.
    参数:
        无.
    返回:
        bool, True 表示通过.
    """
    rng = np.random.default_rng(7715)
    times = np.arange(4.0, 15.0, 0.01)

    # 缓变背景基线 + 后段渐增模拟柱流失
    baseline = 2.0e4 + 5.0e4 * np.exp(0.15 * (times - 4.0))
    # 宽驼峰: 模拟基线抬升残差在 RT 12 附近形成超宽"峰"
    wide_hump = 8.0e4 * np.exp(-0.5 * ((times - 12.0) / 0.8) ** 2)

    # 真实窄峰
    peak_612 = _gaussian(times, center=6.12, sigma=0.008, amplitude=5.0e6)
    peak_685 = _gaussian(times, center=6.85, sigma=0.007, amplitude=8.0e6)
    peak_774 = _gaussian(times, center=7.74, sigma=0.006, amplitude=1.6e7)

    signal = (
        baseline + wide_hump + peak_612 + peak_685 + peak_774
        + rng.normal(0.0, 2000.0, size=len(times))
    )

    # 启用超宽峰过滤
    peaks_enabled = _build_integrator(
        prominence=20000.0,
        min_distance=5,
        integration_mode="robust_v3",
        max_peak_width_min=0.5,
    ).integrate(times, signal)

    # 关闭超宽峰过滤
    peaks_disabled = _build_integrator(
        prominence=20000.0,
        min_distance=5,
        integration_mode="robust_v3",
        max_peak_width_min=0.0,
    ).integrate(times, signal)

    # 验证关闭时超宽峰存在(确认测试构造有效)
    wide_peaks_disabled = [p for p in peaks_disabled if p.width > 0.5]
    if len(wide_peaks_disabled) == 0:
        logger.warning(
            "合成超宽基线峰回归: 关闭过滤时未检出超宽峰, 测试构造可能需调整. peaks=%s",
            _rounded_rts(peaks_disabled),
        )

    # 验证启用时超宽峰被过滤
    wide_peaks_enabled = [p for p in peaks_enabled if p.width > 0.5]
    if len(wide_peaks_enabled) > 0:
        logger.error(
            "合成超宽基线峰回归失败: 启用过滤后仍存在超宽峰. peaks=%s, widths=%s",
            _rounded_rts(peaks_enabled),
            [round(p.width, 4) for p in peaks_enabled],
        )
        return False

    # 验证真实窄峰保留
    for target_rt in [6.12, 6.85, 7.74]:
        if _has_peak(peaks_enabled, target_rt, 0.04) is False:
            logger.error(
                "合成超宽基线峰回归失败: 真实峰 RT=%.2f 被误删. peaks=%s",
                target_rt, _rounded_rts(peaks_enabled),
            )
            return False

    logger.info(
        "合成超宽基线峰回归通过: enabled=%s, disabled=%s",
        _rounded_rts(peaks_enabled),
        _rounded_rts(peaks_disabled),
    )
    return True


def run_synthetic_real_peak_on_elevated_baseline_regression() -> bool:
    """
    功能:
        验证 robust_v3 不会误删位于抬升基线上的真实窄峰.
        模拟 771-8 RT 11.00 场景: 高基线上存在真实的尖锐色谱峰.
    参数:
        无.
    返回:
        bool, True 表示通过.
    """
    rng = np.random.default_rng(9999)
    times = np.arange(8.0, 14.0, 0.01)

    # 渐增基线模拟柱流失
    baseline = 3.0e4 + 5.0e4 * np.exp(0.12 * (times - 8.0))

    # 真实窄峰: 虽在高基线上但峰本身是尖锐的
    real_peak_1 = _gaussian(times, center=10.0, sigma=0.015, amplitude=8.0e5)
    real_peak_2 = _gaussian(times, center=12.0, sigma=0.020, amplitude=1.5e6)

    signal = baseline + real_peak_1 + real_peak_2 + rng.normal(0.0, 2000.0, size=len(times))

    peaks = _build_integrator(
        prominence=20000.0,
        min_distance=5,
        integration_mode="robust_v3",
        max_peak_width_min=0.5,
    ).integrate(times, signal)

    if _has_peak(peaks, 10.0, 0.05) is False:
        logger.error(
            "合成高基线真实峰回归失败: RT=10.0 真实峰被误删. peaks=%s",
            _rounded_rts(peaks),
        )
        return False

    if _has_peak(peaks, 12.0, 0.05) is False:
        logger.error(
            "合成高基线真实峰回归失败: RT=12.0 真实峰被误删. peaks=%s",
            _rounded_rts(peaks),
        )
        return False

    logger.info(
        "合成高基线真实峰回归通过: peaks=%s",
        _rounded_rts(peaks),
    )
    return True


def main() -> int:
    """
    功能:
        程序入口.
    参数:
        无.
    返回:
        int, 进程退出码.
    """
    parser = argparse.ArgumentParser(description="robust_v3 积分回归检查")
    parser.add_argument(
        "--data-root",
        type=Path,
        default=PROJECT_ROOT / "eit_analysis_station" / "data",
        help="样本根目录路径",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )

    checks: List[Callable[[], bool]] = [
        run_synthetic_doublet_regression,
        run_synthetic_shoulder_regression,
        run_synthetic_tail_artifact_regression,
        run_synthetic_monotonic_tail_regression,
        run_synthetic_leading_edge_regression,
        run_synthetic_leading_edge_doublet_safety_regression,
        run_synthetic_wide_baseline_peak_regression,
        run_synthetic_real_peak_on_elevated_baseline_regression,
        lambda: run_real_sample_regression(args.data_root),
        lambda: run_robust_v2_compat_smoke(args.data_root),
    ]

    all_ok = True
    for check in checks:
        if check() is False:
            all_ok = False

    if all_ok is True:
        logger.info("回归检查全部通过.")
        return 0

    logger.error("回归检查失败.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
