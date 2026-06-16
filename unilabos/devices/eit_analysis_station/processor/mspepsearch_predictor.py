#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
功能:
    封装 SS-HM (Simple Search Hitlist Method) 和 iHS-HM (iterative Hybrid Search
    Hitlist Method) 两种基于 MSPepSearch 的分子量预测算法.
    参考论文: Moorthy et al. "Inferring the nominal molecular mass of an analyte
    from its electron ionization mass spectrum" (2021).
参数:
    无.
返回:
    无.
"""

import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from .molecular_mass_predictor import PIMPredictor
from .nist_library_reader import NistLibraryReader
from .report_generator import SSHMPrediction, iHSHMPrediction

logger = logging.getLogger(__name__)


class MSPepSearchPredictor:
    """
    功能:
        封装 SS-HM 和 iHS-HM 两种基于 MSPepSearch 谱库搜索的分子量预测算法.
        SS-HM: 简单搜索 → 命中修正 → 加权概率.
        iHS-HM: 候选质量遍历 → 混合搜索 → Omega 指标.
    参数:
        mspepsearch_exe: MSPepSearch64.exe 路径.
        lib_path: NIST 谱库路径 (如 mainlib 目录).
        lib_type: 库类型标识, "MAIN"/"REPL"/"LIB".
        pim_predictor: PIMPredictor 实例, 复用 PIM 核心算法.
        nist_lib_reader: NistLibraryReader 实例, 用于 SS-HM 库谱查找.
        work_dir: 临时文件工作目录.
        sshm_hits: SS-HM 命中深度.
        sshm_b_ss: SS-HM 指数缩放因子.
        ihshm_hits: iHS-HM 命中深度.
        ihshm_mEMF: iHS-HM 最低期望匹配因子.
        timeout: MSPepSearch 单次搜索超时(秒).
    返回:
        无.
    """

    def __init__(
        self,
        mspepsearch_exe: Path,
        lib_path: Path,
        lib_type: str = "MAIN",
        pim_predictor: Optional[PIMPredictor] = None,
        nist_lib_reader: Optional[NistLibraryReader] = None,
        work_dir: Optional[Path] = None,
        sshm_hits: int = 25,
        sshm_b_ss: int = 75,
        ihshm_hits: int = 25,
        ihshm_mEMF: int = 700,
        timeout: float = 120.0,
    ) -> None:
        self._exe = Path(mspepsearch_exe)
        self._lib_path = Path(lib_path)
        self._lib_type = lib_type.upper()
        self._pim = pim_predictor
        self._lib_reader = nist_lib_reader
        self._work_dir = Path(work_dir) if work_dir is not None else Path(tempfile.gettempdir())
        self._sshm_hits = sshm_hits
        self._sshm_b_ss = sshm_b_ss
        self._ihshm_hits = ihshm_hits
        self._ihshm_mEMF = ihshm_mEMF
        self._timeout = timeout

        self._available = self._exe.exists() and self._lib_path.exists()
        if not self._available:
            logger.warning(
                "MSPepSearch 不可用: exe=%s (存在=%s), lib=%s (存在=%s)",
                self._exe, self._exe.exists(),
                self._lib_path, self._lib_path.exists(),
            )

    @property
    def available(self) -> bool:
        """MSPepSearch 是否可用."""
        return self._available

    # ==================================================================
    # SS-HM: Simple Search Hitlist Method
    # ==================================================================

    def predict_sshm(
        self,
        mz_values: np.ndarray,
        intensities: np.ndarray,
    ) -> SSHMPrediction:
        """
        功能:
            完整 SS-HM 预测.
            1. PIM_core(query) → gamma.
            2. MSPepSearch 简单搜索 → hitlist.
            3. 对每个命中: correction = lib_MW - PIM(lib_spectrum), 库谱不可用时降级.
            4. 按 MF 加权计算各唯一 correction 的概率.
            5. sigma = gamma + best_correction.
        参数:
            mz_values: 查询质谱 m/z 数组.
            intensities: 查询质谱强度数组.
        返回:
            SSHMPrediction.
        """
        if not self._available:
            return SSHMPrediction(status="error", message="MSPepSearch 不可用")

        if self._pim is None:
            return SSHMPrediction(status="error", message="PIMPredictor 未配置")

        # 1. PIM 预测查询谱
        try:
            norm_mz, norm_ab = PIMPredictor._normalize_spectrum(mz_values, intensities)
            if len(norm_mz) == 0:
                return SSHMPrediction(status="no_spectrum", message="无有效质谱信号")
            gamma = self._pim._pim_core(norm_mz, norm_ab)
        except Exception as exc:
            return SSHMPrediction(status="error", message=f"PIM 预测失败: {exc}")

        # 2. 写入 MSP 并搜索
        msp_path = self._work_dir / "query_sshm.msp"
        out_path = self._work_dir / "hitlist_sshm.txt"
        self._write_query_msp("Query", norm_mz, norm_ab, msp_path)

        if not self._run_mspepsearch("Sf", msp_path, out_path):
            return SSHMPrediction(status="error", message="MSPepSearch 简单搜索失败")

        # 3. 解析 hitlist
        hits = self._parse_ss_hitlist(out_path)
        if len(hits) == 0:
            return SSHMPrediction(status="error", message="SS-HM 未获得有效命中")

        # 4. 计算修正因子
        corrections: List[int] = []
        match_factors: List[int] = []

        for hit_name, hit_id, lib_mw, mf in hits:
            correction = self._compute_sshm_correction(hit_name, lib_mw, gamma)
            corrections.append(correction)
            match_factors.append(mf)

        # 5. 加权概率计算
        sigma, i_sigma, best_correction = self._compute_sshm_probability(
            gamma, corrections, match_factors
        )

        return SSHMPrediction(
            predicted_mw=int(round(sigma)),
            confidence=round(i_sigma, 4),
            correction=best_correction,
            status="ok",
            message=f"SS-HM 预测成功, {len(hits)} 个命中",
        )

    def _compute_sshm_correction(
        self,
        hit_name: str,
        lib_mw: int,
        gamma_query: float,
    ) -> int:
        """
        功能:
            计算单个命中的 SS-HM 修正因子.
            完整版: correction = lib_MW - PIM(lib_spectrum).
            降级版: correction = lib_MW - gamma_query.
        参数:
            hit_name: 命中化合物名称.
            lib_mw: 命中化合物的库 MW.
            gamma_query: 查询谱的 PIM 预测.
        返回:
            int, 修正因子.
        """
        # 尝试从库中获取命中化合物的质谱并运行 PIM
        if self._lib_reader is not None and self._lib_reader.available:
            spectrum = self._lib_reader.get_spectrum(hit_name)
            if spectrum is not None:
                lib_mz, lib_ab, _ = spectrum
                try:
                    lib_norm_mz, lib_norm_ab = PIMPredictor._normalize_spectrum(lib_mz, lib_ab)
                    if len(lib_norm_mz) >= 2:
                        gamma_lib = self._pim._pim_core(lib_norm_mz, lib_norm_ab)
                        return int(round(lib_mw - gamma_lib))
                except Exception:
                    pass  # 降级到简化模式

        # 降级: 直接用 lib_MW - gamma_query
        return int(round(lib_mw - gamma_query))

    @staticmethod
    def _compute_sshm_probability(
        gamma: float,
        corrections: List[int],
        match_factors: List[int],
    ) -> Tuple[float, float, int]:
        """
        功能:
            按匹配因子加权计算各唯一修正值的概率.
            P(c) = sum(2^((MF_j - MF_best) / B_SS)) for j with correction_j == c
                 / sum(2^((MF_j - MF_best) / B_SS)) for all j
        参数:
            gamma: PIM 查询预测.
            corrections: 每个命中的修正因子列表.
            match_factors: 每个命中的匹配因子列表.
        返回:
            Tuple[float, float, int]: (sigma, confidence, best_correction).
        """
        if len(corrections) == 0:
            return gamma, 0.0, 0

        best_mf = match_factors[0]  # 已按 MF 降序排列
        b_ss = 75

        # 计算唯一修正值的概率
        unique_corrections: List[int] = []
        for c in corrections:
            if c not in unique_corrections:
                unique_corrections.append(c)

        correction_probs: List[float] = []
        denominator = 0.0
        for mf in match_factors:
            denominator += 2.0 ** ((mf - best_mf) / b_ss)

        for uc in unique_corrections:
            numerator = 0.0
            for j, c in enumerate(corrections):
                if c == uc:
                    numerator += 2.0 ** ((match_factors[j] - best_mf) / b_ss)
            correction_probs.append(numerator / denominator if denominator > 0 else 0.0)

        # 找最高概率的修正值
        best_idx = 0
        best_prob = 0.0
        for idx, prob in enumerate(correction_probs):
            if prob > best_prob:
                best_prob = prob
                best_idx = idx

        best_correction = unique_corrections[best_idx]
        sigma = gamma + best_correction
        return sigma, best_prob, best_correction

    # ==================================================================
    # iHS-HM: iterative Hybrid Search Hitlist Method
    # ==================================================================

    def predict_ihshm(
        self,
        mz_values: np.ndarray,
        intensities: np.ndarray,
    ) -> iHSHMPrediction:
        """
        功能:
            iHS-HM 预测.
            1. PIM_core → gamma, 确定候选范围 [gamma-18, gamma+basepeak_mz].
            2. 批量写入 MSP (每个候选质量一个条目).
            3. 单次 MSPepSearch Hf 混合搜索.
            4. 解析 hitlist, 逐块计算 Omega.
            5. theta = argmax(Omega), I_theta = (Omega1 - Omega2) / 999.
        参数:
            mz_values: 查询质谱 m/z 数组.
            intensities: 查询质谱强度数组.
        返回:
            iHSHMPrediction.
        """
        if not self._available:
            return iHSHMPrediction(status="error", message="MSPepSearch 不可用")

        if self._pim is None:
            return iHSHMPrediction(status="error", message="PIMPredictor 未配置")

        # 1. PIM 预测 + 确定候选范围
        try:
            norm_mz, norm_ab = PIMPredictor._normalize_spectrum(mz_values, intensities)
            if len(norm_mz) == 0:
                return iHSHMPrediction(status="no_spectrum", message="无有效质谱信号")
            gamma = self._pim._pim_core(norm_mz, norm_ab)
        except Exception as exc:
            return iHSHMPrediction(status="error", message=f"PIM 预测失败: {exc}")

        # 候选范围: [gamma-18, gamma+basepeak_mz] (参考 C++ 实现)
        bp_mz = 0
        bp_val = float(np.max(norm_ab))
        for i in range(len(norm_ab)):
            if float(norm_ab[i]) == bp_val:
                bp_mz = int(norm_mz[i])

        min_mw = int(gamma) - 18
        max_mw = int(gamma) + bp_mz
        if min_mw < 1:
            min_mw = 1
        candidate_masses = list(range(min_mw, max_mw + 1))

        if len(candidate_masses) == 0:
            return iHSHMPrediction(status="error", message="候选质量范围为空")

        logger.info(
            "iHS-HM 候选范围: %d-%d Da (%d 个候选), PIM=%.0f, basepeak_mz=%d",
            min_mw, max_mw, len(candidate_masses), gamma, bp_mz,
        )

        # 2. 批量写入 MSP
        msp_path = self._work_dir / "query_ihshm.msp"
        out_path = self._work_dir / "hitlist_ihshm.txt"
        self._write_batch_hs_msp(norm_mz, norm_ab, candidate_masses, msp_path)

        # 3. 单次混合搜索
        if not self._run_mspepsearch("Hf", msp_path, out_path, extra_flags=["/OutDeltaMW"]):
            return iHSHMPrediction(status="error", message="MSPepSearch 混合搜索失败")

        # 4. 解析 hitlist 并计算 Omega
        omega_values = self._parse_hs_hitlist_and_compute_omega(
            out_path, len(candidate_masses)
        )

        if len(omega_values) == 0:
            return iHSHMPrediction(status="error", message="iHS-HM 未获得有效 Omega 值")

        # 5. 找最大 Omega 对应的候选质量
        omega1 = 0.0
        omega2 = 0.0
        theta = float(candidate_masses[0])

        for i, omega_val in enumerate(omega_values):
            if omega_val > omega1:
                omega2 = omega1
                omega1 = omega_val
                theta = float(candidate_masses[i])
            elif omega_val > omega2:
                omega2 = omega_val

        confidence = (omega1 - omega2) / 999.0

        return iHSHMPrediction(
            predicted_mw=int(round(theta)),
            confidence=round(confidence, 6),
            status="ok",
            message=f"iHS-HM 预测成功, 范围 {min_mw}-{max_mw} Da",
        )

    # ==================================================================
    # MSP 文件写入
    # ==================================================================

    @staticmethod
    def _write_query_msp(
        name: str,
        mz_values: np.ndarray,
        intensities: np.ndarray,
        output_path: Path,
        mw: Optional[int] = None,
    ) -> None:
        """
        功能:
            写入单个查询质谱 MSP 文件.
        参数:
            name: 质谱名称.
            mz_values: m/z 数组 (已归一化).
            intensities: 强度数组 (已归一化).
            output_path: 输出文件路径.
            mw: 可选分子量 (用于混合搜索).
        返回:
            无.
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"Name: {name}"]
        if mw is not None:
            lines.append(f"MW: {mw}")
        lines.append(f"Num Peaks: {len(mz_values)}")

        # 峰数据: "mz ab; mz ab; ..."
        pairs = []
        for mz, ab in zip(mz_values, intensities):
            pairs.append(f"{int(round(mz))} {int(round(ab))}")
        # 每行最多 10 对
        for i in range(0, len(pairs), 10):
            lines.append("; ".join(pairs[i:i + 10]))
        lines.append("")

        output_path.write_text("\r\n".join(lines), encoding="ascii", errors="replace")

    @staticmethod
    def _write_batch_hs_msp(
        mz_values: np.ndarray,
        intensities: np.ndarray,
        candidate_masses: List[int],
        output_path: Path,
    ) -> None:
        """
        功能:
            批量写入 iHS-HM 混合搜索 MSP 文件.
            每个候选质量写入一个 MSP 条目, 共 N 个条目.
            参考 R 实现 (app.R:614-636).
        参数:
            mz_values: m/z 数组 (已归一化).
            intensities: 强度数组 (已归一化).
            candidate_masses: 候选质量列表.
            output_path: 输出文件路径.
        返回:
            无.
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # 预生成峰数据字符串
        pairs = []
        for mz, ab in zip(mz_values, intensities):
            pairs.append(f"{int(round(mz))} {int(round(ab))}")
        peak_lines = []
        for i in range(0, len(pairs), 10):
            peak_lines.append("; ".join(pairs[i:i + 10]))
        peak_block = "\r\n".join(peak_lines)

        all_lines = []
        for mass in candidate_masses:
            all_lines.append(f"Name: Query")
            all_lines.append(f"MW: {mass}")
            all_lines.append(f"Num Peaks: {len(mz_values)}")
            all_lines.append(peak_block)
            all_lines.append("")  # 空行分隔
            all_lines.append("")

        output_path.write_text("\r\n".join(all_lines), encoding="ascii", errors="replace")
        logger.debug("iHS-HM 批量 MSP 已写入: %s (%d 个候选)", output_path, len(candidate_masses))

    # ==================================================================
    # MSPepSearch 执行
    # ==================================================================

    def _run_mspepsearch(
        self,
        search_mode: str,
        input_msp: Path,
        output_tab: Path,
        extra_flags: Optional[List[str]] = None,
    ) -> bool:
        """
        功能:
            构建并执行 MSPepSearch 命令.
            通过 batch 文件调用以避免路径中 '/' 与选项冲突.
        参数:
            search_mode: 搜索模式 ("Sf" / "Hf" 等).
            input_msp: 输入 MSP 文件路径.
            output_tab: 输出 tab 文件路径.
            extra_flags: 额外的命令行标志 (如 ["/OutDeltaMW"]).
        返回:
            bool, 搜索成功返回 True.
        """
        # 确定库类型标志
        if self._lib_type == "MAIN":
            lib_flag = "/MAIN"
        elif self._lib_type == "REPL":
            lib_flag = "/REPL"
        else:
            lib_flag = "/LIB"

        # 确定命中数
        if search_mode.startswith("S"):
            hits = self._sshm_hits
        else:
            hits = self._ihshm_hits

        # 构建命令行各段
        cmd_parts = [
            f'"{self._exe}" {search_mode}^',
            f" {lib_flag} {self._lib_path}^",
            f" /HITS {hits}^",
            f" /INP {input_msp}^",
            " /OutNumMP^",
            " /OutMW^",
        ]
        if extra_flags is not None:
            for flag in extra_flags:
                cmd_parts.append(f" {flag}^")
        cmd_parts.append(f" /OUTTAB {output_tab}")

        # 写入 batch 文件
        bat_path = self._work_dir / "mspepsearch_run.bat"
        bat_content = "@echo off\r\n" + "\r\n".join(cmd_parts) + "\r\n"
        bat_path.write_text(bat_content, encoding="ascii")

        # 删除旧输出
        if output_tab.exists():
            output_tab.unlink()

        try:
            logger.info("启动 MSPepSearch: mode=%s, input=%s", search_mode, input_msp.name)
            result = subprocess.run(
                ["cmd.exe", "/c", str(bat_path)],
                capture_output=True,
                text=True,
                timeout=self._timeout,
                cwd=str(self._work_dir),
            )
            if result.returncode != 0:
                logger.warning(
                    "MSPepSearch 返回非零退出码: %d, stderr: %s",
                    result.returncode, result.stderr[:500],
                )
                # 即使退出码非零, 输出文件可能已生成
            if output_tab.exists():
                logger.info("MSPepSearch 搜索完成: %s", output_tab.name)
                return True
            else:
                logger.error("MSPepSearch 输出文件不存在: %s", output_tab)
                return False
        except subprocess.TimeoutExpired:
            logger.error("MSPepSearch 搜索超时 (%.0f 秒)", self._timeout)
            return False
        except Exception as exc:
            logger.error("MSPepSearch 执行失败: %s", exc)
            return False

    # ==================================================================
    # Hitlist 解析
    # ==================================================================

    @staticmethod
    def _parse_ss_hitlist(
        hitlist_path: Path,
    ) -> List[Tuple[str, int, int, int]]:
        """
        功能:
            解析 SS-HM 简单搜索命中列表.
            输出格式 (tab 分隔):
            Unknown  Rank  Library  Id  Mass  Lib MW  MF  Prob(%)  Name  ...
        参数:
            hitlist_path: hitlist 文件路径.
        返回:
            List[Tuple[str, int, int, int]]: [(name, id, lib_mw, mf), ...],
            按 Rank 升序 (MF 降序).
        """
        hits: List[Tuple[str, int, int, int]] = []

        if not hitlist_path.exists():
            logger.warning("SS hitlist 文件不存在: %s", hitlist_path)
            return hits

        try:
            content = hitlist_path.read_text(encoding="utf-8", errors="replace")
            for line in content.splitlines():
                line = line.strip()
                # 跳过注释行 (以 > 开头) 和表头行
                if line.startswith(">") or line.startswith("Unknown"):
                    continue
                if not line:
                    continue

                fields = line.split("\t")
                if len(fields) < 9:
                    continue

                try:
                    hit_name = fields[8].strip()     # Name 列
                    hit_id = int(fields[3].strip())   # Id 列
                    lib_mw = int(float(fields[5].strip()))  # Lib MW 列
                    mf = int(float(fields[6].strip()))      # MF 列
                    hits.append((hit_name, hit_id, lib_mw, mf))
                except (ValueError, IndexError):
                    continue

        except Exception as exc:
            logger.error("解析 SS hitlist 失败: %s", exc)

        logger.debug("SS hitlist 解析完成: %d 个命中", len(hits))
        return hits

    def _parse_hs_hitlist_and_compute_omega(
        self,
        hitlist_path: Path,
        n_candidates: int,
    ) -> List[float]:
        """
        功能:
            解析 iHS-HM 混合搜索命中列表并计算每个候选质量的 Omega 值.
            输出格式 (tab 分隔):
            Unknown  Rank  Library  Id  Mass  DeltaMW  Lib MW  MF(hMF)  Prob  NumMP
            o.NumMP  o.Match(sMF)  o.R.Match  Name  ...

            对每个候选质量, 取其 ≤ihshm_hits 个命中:
            - 过滤 hMF >= mEMF
            - DeltaMW==0 (isomer): isomerOmega += hMF * (hMF-mEMF)/(1000-mEMF)
            - DeltaMW!=0 (cognate): cognateOmega += (hMF-sMF) * (hMF-mEMF)/(1000-mEMF)
            - Omega = (isomerOmega + cognateOmega) / (N_isomers + N_cognates)
        参数:
            hitlist_path: hitlist 文件路径.
            n_candidates: 候选质量数.
        返回:
            List[float]: 每个候选质量的 Omega 值列表.
        """
        omega_values: List[float] = [0.0] * n_candidates

        if not hitlist_path.exists():
            logger.warning("HS hitlist 文件不存在: %s", hitlist_path)
            return omega_values

        mEMF = self._ihshm_mEMF
        hits_per_candidate = self._ihshm_hits

        try:
            content = hitlist_path.read_text(encoding="utf-8", errors="replace")
            data_lines: List[str] = []
            for line in content.splitlines():
                line = line.strip()
                if line.startswith(">") or line.startswith("Unknown") or not line:
                    continue
                data_lines.append(line)

            # 按候选质量分块处理
            # 每个候选最多 hits_per_candidate 行
            line_idx = 0
            for candidate_idx in range(n_candidates):
                n_cognates = 0
                n_isomers = 0
                cognate_omega = 0.0
                isomer_omega = 0.0

                hits_read = 0
                while line_idx < len(data_lines) and hits_read < hits_per_candidate:
                    fields = data_lines[line_idx].split("\t")
                    line_idx += 1

                    if len(fields) < 14:
                        continue

                    try:
                        rank = int(fields[1].strip())
                    except ValueError:
                        continue

                    # 检查 rank 是否连续, 若 rank != hits_read+1 说明该候选的命中已结束
                    if rank != hits_read + 1:
                        # 回退一行, 这行属于下一个候选
                        line_idx -= 1
                        break

                    hits_read += 1

                    try:
                        delta_mw = int(float(fields[5].strip()))  # DeltaMW 列
                        h_mf = int(float(fields[7].strip()))      # MF(hMF) 列
                        s_mf = int(float(fields[11].strip()))     # o.Match(sMF) 列
                    except (ValueError, IndexError):
                        continue

                    if h_mf < mEMF:
                        # 低于阈值的命中不参与计算, 但继续读取直到本候选结束
                        continue

                    weight = (h_mf - mEMF) / (1000.0 - mEMF)

                    if delta_mw == 0:
                        # isomer: 同质量匹配
                        n_isomers += 1
                        isomer_omega += h_mf * weight
                    else:
                        # cognate: 不同质量匹配
                        n_cognates += 1
                        cognate_omega += (h_mf - s_mf) * weight

                total = n_cognates + n_isomers
                if total > 0:
                    omega_values[candidate_idx] = (cognate_omega + isomer_omega) / total

        except Exception as exc:
            logger.error("解析 HS hitlist 失败: %s", exc)

        logger.debug(
            "iHS-HM Omega 计算完成: %d 个候选, 最大 Omega=%.2f",
            n_candidates, max(omega_values) if omega_values else 0.0,
        )
        return omega_values
