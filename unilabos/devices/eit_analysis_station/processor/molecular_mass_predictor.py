#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
功能:
    基于 EI 质谱实现 PIM (Peak Interpretation Method) 分子量预测.
    输入单个峰的 m/z-强度谱图后, 输出预测分子离子峰和置信指数.
参数:
    无.
返回:
    无.
"""

import logging
import math
from pathlib import Path
from typing import Dict, Optional, Set, Tuple

import numpy as np

from .report_generator import PIMPrediction

logger = logging.getLogger(__name__)


class PIMPredictor:
    """
    功能:
        使用 PIM 算法对 EI 质谱进行分子量预测.
        算法流程:
        1. 光谱归一化.
        2. 计算 PIM 核心预测质量.
        3. 计算 PIM 置信指数.
    参数:
        ab_m: 分子离子峰最小相对丰度阈值, 单位 %.
        beta: 置信指数公式中的斜率参数.
        epsilon_f: 置信指数公式中的噪声修正参数.
        illogical_losses_path: 非逻辑损失列表文件路径.
    返回:
        无.
    """

    def __init__(
        self,
        ab_m: float = 0.3,
        beta: float = 5.0,
        epsilon_f: float = 0.0,
        illogical_losses_path: Optional[Path] = None,
    ) -> None:
        self._ab_m = float(ab_m)
        self._beta = float(beta)
        self._epsilon_f = float(epsilon_f)
        if illogical_losses_path is None:
            self._illogical_losses_path = Path(__file__).parent / "data" / "illogical_losses.txt"
        else:
            self._illogical_losses_path = illogical_losses_path
        self._illogical_losses = self._load_illogical_losses(self._illogical_losses_path)

    @staticmethod
    def _load_illogical_losses(file_path: Path) -> Set[int]:
        """
        功能:
            读取非逻辑损失列表文件.
        参数:
            file_path: 损失列表文件路径, 每行一个整数.
        返回:
            Set[int], 非逻辑损失集合.
        """
        losses: Set[int] = set()
        if file_path.exists() is False:
            logger.warning("PIM 非逻辑损失文件不存在: %s", file_path)
            return losses

        lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
        for line_no, raw_line in enumerate(lines, start=1):
            text = raw_line.strip()
            if text == "":
                continue

            try:
                loss_value = int(text)
            except ValueError:
                logger.warning("PIM 非逻辑损失文件含非法行, 已忽略: %s:%d -> %s", file_path, line_no, text)
                continue

            losses.add(loss_value)

        logger.info("PIM 非逻辑损失加载完成: %d 项", len(losses))
        return losses

    @staticmethod
    def _normalize_spectrum(
        mz_values: np.ndarray,
        intensities: np.ndarray,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """
        功能:
            归一化输入光谱:
            1. 过滤无效值和非正强度.
            2. m/z 四舍五入为整数质量.
            3. 合并重复 m/z, 取最大强度.
            4. 按 m/z 升序输出.
        参数:
            mz_values: m/z 数组.
            intensities: 强度数组.
        返回:
            Tuple[np.ndarray, np.ndarray], 归一化后的 m/z 和强度数组.
        """
        mz_array = np.asarray(mz_values, dtype=float)
        intensity_array = np.asarray(intensities, dtype=float)

        if len(mz_array) != len(intensity_array):
            raise ValueError("m/z 与强度数组长度不一致")

        if len(mz_array) == 0:
            return np.array([], dtype=float), np.array([], dtype=float)

        valid_mask = np.isfinite(mz_array) & np.isfinite(intensity_array) & (intensity_array > 0)
        mz_valid = mz_array[valid_mask]
        intensity_valid = intensity_array[valid_mask]

        if len(mz_valid) == 0:
            return np.array([], dtype=float), np.array([], dtype=float)

        merged_intensity: Dict[int, float] = {}
        rounded_mz = np.rint(mz_valid).astype(int)
        for mz_item, intensity_item in zip(rounded_mz, intensity_valid):
            mz_key = int(mz_item)
            intensity_value = float(intensity_item)
            old_value = merged_intensity.get(mz_key)

            if old_value is None:
                merged_intensity[mz_key] = intensity_value
                continue

            if intensity_value > old_value:
                merged_intensity[mz_key] = intensity_value

        sorted_mz = sorted(merged_intensity.keys())
        normalized_mz = np.array(sorted_mz, dtype=float)
        normalized_intensity = np.array([merged_intensity[mz_key] for mz_key in sorted_mz], dtype=float)
        return normalized_mz, normalized_intensity

    def _pim_core(
        self,
        mz_values: np.ndarray,
        intensities: np.ndarray,
    ) -> float:
        """
        功能:
            计算 PIM 核心预测质量.
        参数:
            mz_values: 归一化后的 m/z 数组, 升序.
            intensities: 归一化后的强度数组.
        返回:
            float, 预测的分子离子质量.
        """
        if len(mz_values) == 0:
            raise ValueError("光谱为空, 无法执行 PIM 预测")

        if len(mz_values) < 2:
            return float(mz_values[0])

        base_peak = float(np.max(intensities))
        lowest_ab = (self._ab_m / 100.0) * base_peak
        n_peaks = len(mz_values)

        for peak_index in range(n_peaks - 1, 1, -1):
            m0 = float(mz_values[peak_index])
            a0 = float(intensities[peak_index])
            m1 = float(mz_values[peak_index - 1])
            a1 = float(intensities[peak_index - 1])
            md1 = m0 - m1

            if peak_index - 2 < 1:
                md2 = 0.0
                a2 = 0.0
            else:
                m2 = float(mz_values[peak_index - 2])
                a2 = float(intensities[peak_index - 2])
                md2 = m0 - m2

            if a0 < lowest_ab:
                continue

            if md1 > 2.0:
                return m0

            if abs(md1 - 2.0) <= 1e-8:
                if a1 < int(a0 / 2.0):
                    return m0
            elif abs(md1 - 1.0) <= 1e-8:
                isocalc = (a1 * 0.011 * m1) / 14.0

                if int(3.0 * isocalc) > a0 or (a0 - int(isocalc)) < lowest_ab:
                    if peak_index == 2:
                        return m1
                    continue

                if md2 > 2.0:
                    return m0

                if peak_index - 2 >= 1:
                    if a2 < int(a0 / 2.0):
                        return m0

        return float(mz_values[n_peaks - 1])

    def _compute_confidence_index(
        self,
        gamma: float,
        mz_values: np.ndarray,
        intensities: np.ndarray,
    ) -> Optional[float]:
        """
        功能:
            计算 PIM 置信指数.
            I = 2 - 2 / (1 + exp(-beta * r_tau)).
        参数:
            gamma: PIM 预测质量.
            mz_values: 归一化后的 m/z 数组.
            intensities: 归一化后的强度数组.
        返回:
            Optional[float], 置信指数. 无法计算时返回 None.
        """
        if len(mz_values) == 0:
            return None

        nearest_index = int(np.argmin(np.abs(mz_values - gamma)))
        p_ab = float(intensities[nearest_index])
        if p_ab <= 0:
            return None

        max_ab = float(np.max(intensities))
        lambda_values = gamma - mz_values

        numerator = 0.0
        for idx, loss_candidate in enumerate(lambda_values):
            loss_key = int(round(float(loss_candidate)))
            if loss_key in self._illogical_losses:
                penalty = max(float(intensities[idx]) - self._epsilon_f * max_ab, 0.0)
                numerator += penalty

        r_tau = numerator / p_ab
        try:
            confidence_index = 2.0 - 2.0 / (1.0 + math.exp(-1.0 * self._beta * r_tau))
        except OverflowError:
            confidence_index = 2.0
        return confidence_index

    def predict(
        self,
        mz_values: np.ndarray,
        intensities: np.ndarray,
    ) -> PIMPrediction:
        """
        功能:
            执行 PIM 预测并返回结构化结果.
        参数:
            mz_values: 原始 m/z 数组.
            intensities: 原始强度数组.
        返回:
            PIMPrediction, 预测结果对象.
        """
        try:
            normalized_mz, normalized_intensity = self._normalize_spectrum(mz_values, intensities)
        except Exception as exc:
            logger.error("PIM 光谱归一化失败: %s", exc)
            return PIMPrediction(
                status="error",
                message=f"光谱归一化失败: {exc}",
            )

        if len(normalized_mz) == 0:
            return PIMPrediction(
                status="no_spectrum",
                message="峰内无有效质谱信号",
            )

        try:
            gamma = self._pim_core(normalized_mz, normalized_intensity)
        except Exception as exc:
            logger.error("PIM 预测失败: %s", exc)
            return PIMPrediction(
                status="error",
                message=f"PIM 核心预测失败: {exc}",
            )

        predicted_mass = int(round(gamma))
        confidence_index = self._compute_confidence_index(gamma, normalized_mz, normalized_intensity)

        if confidence_index is None:
            return PIMPrediction(
                predicted_mz=predicted_mass,
                predicted_mw=predicted_mass,
                confidence_index=None,
                status="error",
                message="PIM 置信指数计算失败",
            )

        return PIMPrediction(
            predicted_mz=predicted_mass,
            predicted_mw=predicted_mass,
            confidence_index=confidence_index,
            status="ok",
            message="PIM 预测成功",
        )
