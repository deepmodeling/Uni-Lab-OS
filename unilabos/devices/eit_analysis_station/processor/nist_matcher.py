#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
功能:
    对峰的质谱数据进行 NIST 谱库匹配.
    实现策略 (按优先级):
    1. 调用本地 NIST MS Search 程序的文件自动化接口 (AUTOIMP.MSD 协议).
    2. 解析 .D/Results/Qual/ 中 MassHunter 已生成的定性结果 (降级方案).
参数:
    无.
返回:
    无.
"""

import logging
import os
import re
import shutil
import subprocess
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class CompoundMatch:
    """
    功能:
        存储单个化合物的谱库匹配结果.
    参数:
        compound_name: 化合物名称.
        inchikey: InChIKey, 用于结构回退时优先精确定位 PubChem 记录.
        cas_number: CAS 注册号.
        match_score: 匹配度 (0-100), 取 MF (Match Factor) 的百分制值.
        reverse_match_score: 反向匹配度 (0-100), 取 RMF.
        probability: NIST 概率匹配度 (0-100).
        formula: 分子式.
        mw: 分子量.
        library: 匹配来源谱库名称.
        nist_id: NIST 库命中编号 (Id 字段).
    返回:
        CompoundMatch.
    """
    compound_name: str = ""
    inchikey: str = ""
    cas_number: str = ""
    match_score: float = 0.0
    reverse_match_score: float = 0.0
    probability: float = 0.0
    formula: str = ""
    mw: float = 0.0
    library: str = ""
    nist_id: Optional[int] = None


class NISTMatcher:
    """
    功能:
        对峰的质谱数据进行 NIST 谱库匹配.
        核心方案: 通过 NIST MS Search 的文件自动化接口批量搜索质谱.
        降级方案: 从 MassHunter 导出的 CSV 报告中提取已有定性结果.
    参数:
        nist_path: NIST MS Search 安装目录 (含 nistms$.exe 的目录).
        max_hits: 每个质谱返回的最大匹配数.
        search_timeout: 单次搜索等待超时时间(秒).
    返回:
        无.
    """

    def __init__(
        self,
        nist_path: Optional[Path] = None,
        max_hits: int = 5,
        search_timeout: float = 60.0,
    ) -> None:
        self._nist_path = nist_path
        self._max_hits = max_hits
        self._search_timeout = search_timeout

        # 验证 NIST MS Search 安装
        if self._nist_path is not None:
            self._nistms_exe = self._nist_path / "nistms$.exe"
            self._autoimp_path = self._nist_path / "AUTOIMP.MSD"
            self._srcreslt_path = self._nist_path / "SRCRESLT.TXT"
            self._srcready_path = self._nist_path / "SRCREADY.TXT"

            if not self._nistms_exe.exists():
                logger.warning("NIST MS Search 自动化程序不存在: %s", self._nistms_exe)
                self._nist_path = None
            elif not self._autoimp_path.exists():
                logger.warning("AUTOIMP.MSD 不存在: %s", self._autoimp_path)
                self._nist_path = None
            else:
                # 读取 AUTOIMP.MSD 获取第二级定位文件路径
                self._filespec_path = Path(
                    self._autoimp_path.read_text(encoding="ascii", errors="replace").strip()
                )
                logger.info(
                    "NIST MS Search 就绪: %s, FILESPEC: %s",
                    self._nistms_exe, self._filespec_path
                )

    @property
    def nist_available(self) -> bool:
        """NIST MS Search 自动化接口是否可用."""
        return self._nist_path is not None

    # ------------------------------------------------------------------
    # 核心方案: NIST MS Search 文件自动化接口
    # ------------------------------------------------------------------

    def _write_msp_file(
        self,
        spectra: List[Tuple[str, np.ndarray, np.ndarray]],
        output_path: Path,
    ) -> None:
        """
        功能:
            将多个质谱写入 NIST MSP 文本文件格式.
            每个质谱包含 Name, Num Peaks, 以及 m/z-intensity 对.
        参数:
            spectra: [(名称, m/z数组, intensity数组), ...].
            output_path: MSP 文件输出路径.
        返回:
            无.
        """
        lines = []
        for name, mz_values, intensities in spectra:
            # 归一化强度到 0-999 (NIST 标准范围)
            max_int = intensities.max() if len(intensities) > 0 else 1.0
            if max_int <= 0:
                max_int = 1.0
            norm_intensities = (intensities / max_int * 999).astype(int)

            lines.append(f"Name: {name}")
            lines.append(f"Num Peaks: {len(mz_values)}")

            # 每行最多 5 个 m/z-intensity 对
            pairs = []
            for mz, inten in zip(mz_values, norm_intensities):
                pairs.append(f"{int(round(mz))} {max(inten, 1)}")
            # 每行 5 对
            for i in range(0, len(pairs), 5):
                lines.append("; ".join(pairs[i:i + 5]))

            lines.append("")  # 空行分隔不同质谱

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("\r\n".join(lines), encoding="ascii", errors="replace")
        logger.info("MSP 文件已写入: %s (%d 个质谱)", output_path, len(spectra))

    def _write_filespec(self, msp_path: Path) -> None:
        """
        功能:
            写入第二级定位文件 (FILESPEC.TXT), 指向 MSP 数据文件.
            格式: "<msp_path> OVERWRITE"
            OVERWRITE 表示替换 Spec List 中已有的质谱.
        参数:
            msp_path: MSP 文件的完整路径.
        返回:
            无.
        """
        content = f"{msp_path} OVERWRITE"
        self._filespec_path.parent.mkdir(parents=True, exist_ok=True)
        self._filespec_path.write_text(content, encoding="ascii", errors="replace")
        logger.debug("FILESPEC 写入: %s", content)

    def _cleanup_result_files(self) -> None:
        """
        功能:
            清理上一次搜索的结果文件, 避免读取到旧数据.
        参数:
            无.
        返回:
            无.
        """
        for path in (self._srcreslt_path, self._srcready_path):
            if path.exists():
                path.unlink()
                logger.debug("已清理: %s", path)

    def _normalize_max_hits(self) -> int:
        """
        功能:
            归一化 max_hits 配置值, 确保用于 NIST 配置同步的命中数量至少为 1.
        参数:
            无.
        返回:
            int, 归一化后的命中数量.
        """
        normalized_hits = self._max_hits
        if normalized_hits < 1:
            logger.warning(
                "NIST 命中数量配置无效: max_hits=%s, 已强制修正为 1",
                self._max_hits,
            )
            normalized_hits = 1
        return normalized_hits

    def _limit_match_list(self, hits: List[CompoundMatch]) -> List[CompoundMatch]:
        """
        功能:
            按当前 max_hits 配置裁剪单个质谱的命中列表.
        参数:
            hits: 单个质谱对应的命中列表.
        返回:
            List[CompoundMatch], 裁剪后的命中列表.
        """
        normalized_hits = self._normalize_max_hits()
        return list(hits[:normalized_hits])

    def _limit_match_dict(
        self,
        results: Dict,
    ) -> Dict:
        """
        功能:
            按当前 max_hits 配置裁剪多组命中结果.
        参数:
            results: 任意键 -> 命中列表 的字典.
        返回:
            Dict, 裁剪后的结果字典.
        """
        limited_results: Dict = {}
        for key, hits in results.items():
            limited_results[key] = self._limit_match_list(hits)
        return limited_results

    @staticmethod
    def _find_section_range(
        lines: List[str],
        section_name: str,
    ) -> Tuple[Optional[int], Optional[int]]:
        """
        功能:
            在 INI 行列表中定位指定 section 的起止下标.
            返回区间为左闭右开形式 [start, end).
        参数:
            lines: INI 文件按行拆分后的文本列表.
            section_name: 目标 section 名称, 不包含方括号.
        返回:
            Tuple[Optional[int], Optional[int]]:
                section 起始行下标和结束行下标.
                当 section 不存在时, 两个值均为 None.
        """
        section_start: Optional[int] = None
        section_end: Optional[int] = None

        for line_index, raw_line in enumerate(lines):
            stripped_line = raw_line.strip()
            if not stripped_line.startswith("["):
                continue
            if not stripped_line.endswith("]"):
                continue

            current_section = stripped_line[1:-1].strip()
            if section_start is None:
                if current_section == section_name:
                    section_start = line_index
                continue

            section_end = line_index
            break

        if section_start is None:
            return None, None
        if section_end is None:
            section_end = len(lines)
        return section_start, section_end

    def _set_ini_key_value(
        self,
        lines: List[str],
        section_name: str,
        key_name: str,
        key_value: int,
    ) -> Tuple[List[str], bool, bool]:
        """
        功能:
            在指定 section 中精确写入 key=value.
            1. key 存在时更新值.
            2. key 不存在时追加到 section 末尾.
            3. section 不存在时不改动内容.
        参数:
            lines: INI 文件按行拆分后的文本列表.
            section_name: 目标 section 名称.
            key_name: 目标 key 名称.
            key_value: 目标 key 值.
        返回:
            Tuple[List[str], bool, bool]:
                更新后的行列表, 是否发生变更, section 是否存在.
        """
        section_start, section_end = self._find_section_range(lines, section_name)
        if section_start is None:
            return lines, False, False
        if section_end is None:
            return lines, False, False

        target_value = str(key_value)
        for line_index in range(section_start + 1, section_end):
            raw_line = lines[line_index]
            stripped_line = raw_line.strip()

            if not stripped_line:
                continue
            if stripped_line.startswith(";"):
                continue
            if stripped_line.startswith("#"):
                continue
            if "=" not in raw_line:
                continue

            left_part, _, right_part = raw_line.partition("=")
            if left_part.strip() != key_name:
                continue

            current_value = right_part.strip()
            if current_value == target_value:
                return lines, False, True

            leading_spaces = raw_line[: len(raw_line) - len(raw_line.lstrip())]
            lines[line_index] = f"{leading_spaces}{key_name}={target_value}"
            return lines, True, True

        lines.insert(section_end, f"{key_name}={target_value}")
        return lines, True, True

    def _sync_single_ini_hits(self, ini_path: Path, normalized_hits: int) -> None:
        """
        功能:
            将单个 INI 文件中与命中条数相关的配置同步为指定值.
            同步目标:
            1. [Search Options] Hits to Print.
            2. [REPORT] First Hits Number.
            3. [Compare List] Hits.
        参数:
            ini_path: 目标 INI 文件路径.
            normalized_hits: 归一化后的命中数量.
        返回:
            无.
        """
        if not ini_path.exists():
            logger.warning("NIST 配置文件不存在, 跳过同步: %s", ini_path)
            return

        try:
            ini_text = ini_path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            logger.warning("读取 NIST 配置文件失败, 已跳过: %s, 错误: %s", ini_path, exc)
            return

        line_ending = "\n"
        if "\r\n" in ini_text:
            line_ending = "\r\n"
        keep_trailing_newline = ini_text.endswith("\n") or ini_text.endswith("\r")

        ini_lines = ini_text.splitlines()
        has_changed = False

        target_keys = [
            ("Search Options", "Hits to Print"),
            ("REPORT", "First Hits Number"),
            ("Compare List", "Hits"),
        ]
        for section_name, key_name in target_keys:
            ini_lines, key_changed, section_exists = self._set_ini_key_value(
                lines=ini_lines,
                section_name=section_name,
                key_name=key_name,
                key_value=normalized_hits,
            )
            if section_exists is False:
                logger.warning(
                    "NIST 配置文件缺少 [%s], 跳过键 %s: %s",
                    section_name,
                    key_name,
                    ini_path,
                )
            if key_changed is True:
                has_changed = True

        if has_changed is False:
            logger.debug("NIST 配置文件无需更新: %s", ini_path)
            return

        new_text = line_ending.join(ini_lines)
        if keep_trailing_newline:
            new_text = f"{new_text}{line_ending}"

        try:
            ini_path.write_text(new_text, encoding="utf-8")
            logger.info("已同步 NIST 命中配置: %s, hits=%d", ini_path.name, normalized_hits)
        except Exception as exc:
            logger.warning("写入 NIST 配置文件失败, 已跳过: %s, 错误: %s", ini_path, exc)

    def _update_ini_hits(self) -> None:
        """
        功能:
            同步 NIST 命中条数配置到多个 INI 文件.
            同步目标文件:
            1. nistms.INI.
            2. settings_EI.INI.
            3. settings_MSMS.INI.
            4. settings_PEP.INI.
            同步目标键:
            1. [Search Options] Hits to Print.
            2. [REPORT] First Hits Number.
            3. [Compare List] Hits.
        参数:
            无.
        返回:
            无.
        """
        if self._nist_path is None:
            logger.warning("NIST 路径不可用, 跳过命中条数配置同步")
            return

        normalized_hits = self._normalize_max_hits()
        ini_names = [
            "nistms.INI",
            "settings_EI.INI",
            "settings_MSMS.INI",
            "settings_PEP.INI",
        ]
        for ini_name in ini_names:
            ini_path = self._nist_path / ini_name
            self._sync_single_ini_hits(ini_path=ini_path, normalized_hits=normalized_hits)

    def _launch_nist_search(self) -> bool:
        """
        功能:
            启动 nistms$.exe 并等待搜索完成.
            使用 /PAR=2 参数启动自动搜索模式, 程序会:
            1. 读取 AUTOIMP.MSD -> FILESPEC.TXT -> MSP 文件
            2. 导入质谱到 Spec List
            3. 对每个质谱执行谱库搜索
            4. 将结果写入 SRCRESLT.TXT
            5. 创建 SRCREADY.TXT 表示搜索完成
        参数:
            无.
        返回:
            bool: True 表示搜索完成, False 表示超时或失败.
        """
        try:
            # 启动 nistms$.exe /PAR=2 (自动搜索模式)
            cmd = [str(self._nistms_exe), "/PAR=2"]
            logger.info("启动 NIST MS Search: %s", " ".join(cmd))
            subprocess.Popen(
                cmd,
                cwd=str(self._nist_path),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            logger.error("启动 NIST MS Search 失败: %s", e)
            return False

        # 等待 SRCREADY.TXT 出现, 表示搜索完成
        start_time = time.time()
        while time.time() - start_time < self._search_timeout:
            if self._srcready_path.exists():
                elapsed = time.time() - start_time
                logger.info("NIST 搜索完成, 耗时 %.1f 秒", elapsed)
                return True
            time.sleep(0.5)

        logger.warning("NIST 搜索超时 (%.0f 秒)", self._search_timeout)
        return False

    def _parse_srcreslt(self) -> Dict[str, List[CompoundMatch]]:
        """
        功能:
            解析 NIST MS Search 输出的 SRCRESLT.TXT 文件.
            文件格式 (每行一个 Hit):
            Hit 1 : <<compound_name>>; <<formula>>; MF: 824; RMF: 856;
                     Prob: 98.10; CAS: 74-95-3; Mw: 172; Lib: <<replib>>; Id: 10054; RI: 0.2
            Unknown 行分隔不同查询质谱:
            Unknown: <<spectrum_name>>
        参数:
            无.
        返回:
            Dict[str, List[CompoundMatch]]: 质谱名称 -> 匹配结果列表.
        """
        results: Dict[str, List[CompoundMatch]] = {}

        if not self._srcreslt_path.exists():
            logger.warning("SRCRESLT.TXT 不存在: %s", self._srcreslt_path)
            return results

        content = self._srcreslt_path.read_text(encoding="utf-8", errors="replace")
        current_name = ""

        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue

            # 检测 Unknown 行: "Unknown: <<name>>" 或 "Unknown: name  Compound in Library Factor = ..."
            unknown_match = re.match(r"Unknown:\s*(?:<<)?(.+?)(?:>>)?\s*(?:Compound\s+in\s+Library.*)?$", line)
            if unknown_match:
                current_name = unknown_match.group(1).strip()
                if current_name not in results:
                    results[current_name] = []
                continue

            # 检测 Hit 行
            hit_match = re.match(r"Hit\s+\d+\s*:", line)
            if hit_match is None:
                continue

            # 提取字段
            compound = self._extract_field(line, r":\s*<<(.+?)>>")
            formula = self._extract_field(line, r">>\s*;\s*<<(.+?)>>")
            mf = self._extract_float_field(line, r"MF:\s*([\d.]+)")
            rmf = self._extract_float_field(line, r"RMF:\s*([\d.]+)")
            prob = self._extract_float_field(line, r"Prob:\s*([\d.]+)")
            cas_raw = self._extract_field(line, r"CAS:\s*([\d-]+)")
            mw = self._extract_float_field(line, r"Mw:\s*([\d.]+)")
            lib = self._extract_field(line, r"Lib:\s*<<(.+?)>>")
            nist_id = self._extract_int_field(line, r"Id:\s*(\d+)")

            # CAS=0 表示无有效 CAS, 统一按空字符串处理.
            cas = cas_raw or ""
            if cas in ("0", "0-00-0"):
                logger.debug(
                    "NIST 命中 CAS 无效(CAS=0), Unknown=%s, 化合物=%s, 行=%s",
                    current_name or "(未知)",
                    compound or "(未知)",
                    line,
                )
                cas = ""

            # Id 缺失时记录可追踪日志, 便于后续结构链路排查.
            if nist_id is None:
                logger.debug(
                    "NIST 命中缺少 Id 字段, Unknown=%s, 化合物=%s, 行=%s",
                    current_name or "(未知)",
                    compound or "(未知)",
                    line,
                )

            match = CompoundMatch(
                compound_name=compound or "",
                cas_number=cas or "",
                match_score=mf / 10.0 if mf else 0.0,  # MF 0-1000 转为 0-100
                reverse_match_score=rmf / 10.0 if rmf else 0.0,
                probability=prob or 0.0,
                formula=formula or "",
                mw=mw or 0.0,
                library=lib or "",
                nist_id=nist_id,
            )

            if current_name not in results:
                results[current_name] = []
            results[current_name].append(match)

        logger.info("SRCRESLT 解析完成: %d 个质谱, 共 %d 个匹配",
                     len(results), sum(len(v) for v in results.values()))
        return results

    @staticmethod
    def _extract_field(text: str, pattern: str) -> Optional[str]:
        """用正则从文本中提取字符串字段."""
        m = re.search(pattern, text)
        return m.group(1).strip() if m else None

    @staticmethod
    def _extract_float_field(text: str, pattern: str) -> Optional[float]:
        """用正则从文本中提取浮点字段."""
        m = re.search(pattern, text)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
        return None

    @staticmethod
    def _extract_int_field(text: str, pattern: str) -> Optional[int]:
        """用正则从文本中提取整数字段."""
        m = re.search(pattern, text)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                pass
        return None

    def search_spectra(
        self,
        spectra: List[Tuple[str, np.ndarray, np.ndarray]],
        work_dir: Optional[Path] = None,
    ) -> Dict[str, List[CompoundMatch]]:
        """
        功能:
            通过 NIST MS Search 文件自动化接口批量搜索多个质谱.
            完整流程:
            1. 将质谱写入 MSP 文件.
            2. 写入 FILESPEC.TXT 指向 MSP 文件.
            3. 清理旧的结果文件.
            4. 启动 nistms$.exe /PAR=2 自动搜索.
            5. 等待 SRCREADY.TXT 信号.
            6. 解析 SRCRESLT.TXT 获取匹配结果.
        参数:
            spectra: [(名称, m/z数组, intensity数组), ...].
            work_dir: 工作目录, 用于存放临时 MSP 文件. 默认使用 NIST 目录.
        返回:
            Dict[str, List[CompoundMatch]]: 质谱名称 -> 匹配结果列表.
        """
        if not self.nist_available:
            logger.warning("NIST MS Search 不可用, 无法执行搜索")
            return {}

        if not spectra:
            return {}

        # 确定 MSP 文件路径
        if work_dir is None:
            work_dir = self._nist_path
        msp_path = work_dir / "autosearch_spectra.msp"

        # 写入 MSP 文件
        self._write_msp_file(spectra, msp_path)

        # 写入 FILESPEC
        self._write_filespec(msp_path)

        # 清理旧结果
        self._cleanup_result_files()

        # 确保 NIST 返回足够数量的匹配结果
        self._update_ini_hits()

        # 启动搜索并等待
        if not self._launch_nist_search():
            logger.warning("NIST 搜索未完成, 尝试读取已有结果")

        # 解析结果
        return self._limit_match_dict(self._parse_srcreslt())

    def search_single_spectrum(
        self,
        name: str,
        mz_values: np.ndarray,
        intensities: np.ndarray,
    ) -> List[CompoundMatch]:
        """
        功能:
            搜索单个质谱, 返回匹配结果列表.
        参数:
            name: 质谱标识名称.
            mz_values: m/z 值数组.
            intensities: 强度数组.
        返回:
            List[CompoundMatch]: 匹配结果, 按匹配度降序排列.
        """
        results = self.search_spectra([(name, mz_values, intensities)])
        return results.get(name, [])

    # ------------------------------------------------------------------
    # 批量匹配: 对 .D 目录中所有 TIC 峰进行 NIST 搜索
    # ------------------------------------------------------------------

    def match_peaks_with_nist(
        self,
        d_dir: Path,
        peak_results: List["PeakResult"],
        reader: "GCMSDataReader",
        avg_scans: int = 3,
    ) -> Dict[float, List[CompoundMatch]]:
        """
        功能:
            对 .D 目录中指定峰逐一提取 apex 质谱并通过 NIST 搜索.
            使用峰边界范围内 TIC 最大的扫描, 并平均周围扫描以提升信噪比.
        参数:
            d_dir: .D 目录路径.
            peak_results: 峰检测积分结果列表 (含 start_time/end_time/retention_time).
            reader: GCMSDataReader 实例.
            avg_scans: 以 apex 为中心的平均扫描数.
        返回:
            Dict[float, List[CompoundMatch]]: 保留时间 -> 按 max_hits 裁剪后的匹配结果列表.
        """
        if not self.nist_available:
            logger.info("NIST 不可用, 跳过峰匹配")
            return {}

        # 收集所有峰的质谱
        spectra: List[Tuple[str, np.ndarray, np.ndarray]] = []
        rt_name_map: Dict[str, float] = {}

        for peak in peak_results:
            try:
                mz_values, intensities = reader.read_ms_spectra_at_peak(
                    d_dir, peak.start_time, peak.end_time, avg_scans
                )
                if len(mz_values) == 0:
                    continue
                # 用 RT 作为标识名
                name = f"RT_{peak.retention_time:.3f}"
                spectra.append((name, mz_values, intensities))
                rt_name_map[name] = peak.retention_time
            except Exception as e:
                logger.warning("提取 RT=%.3f 处质谱失败: %s", peak.retention_time, e)

        if not spectra:
            logger.info("无有效质谱数据, 跳过 NIST 搜索")
            return {}

        logger.info("准备搜索 %d 个峰的质谱", len(spectra))

        # 批量搜索
        all_results = self.search_spectra(spectra, work_dir=d_dir.parent)

        # 保存 NIST 结果文件副本, 避免后续样品搜索覆盖
        self._saved_srcreslt: Optional[Path] = None
        if self._srcreslt_path.exists():
            saved_path = d_dir.parent / f"SRCRESLT_{d_dir.stem}.TXT"
            try:
                shutil.copy2(str(self._srcreslt_path), str(saved_path))
                self._saved_srcreslt = saved_path
                logger.debug("NIST 结果已备份: %s", saved_path)
            except Exception as e:
                logger.warning("备份 SRCRESLT.TXT 失败: %s", e)

        # 映射回保留时间, 每个峰保留 setting.py 配置的命中上限
        matches: Dict[float, List[CompoundMatch]] = {}
        for name, hits in all_results.items():
            if name in rt_name_map and hits:
                rt = rt_name_map[name]
                matches[rt] = hits

        logger.info("NIST 峰匹配完成: %d/%d 个峰有匹配结果",
                     len(matches), len(spectra))
        return matches

    # ------------------------------------------------------------------
    # 降级方案: MassHunter 报告文件
    # ------------------------------------------------------------------

    def match_from_qual_results(self, d_dir: Path) -> Dict[float, List[CompoundMatch]]:
        """
        功能:
            从 MassHunter 定性结果中提取化合物匹配信息.
            MassHunter 的 QualResult.bin 为二进制格式,
            当前采用降级策略: 若 MassHunter 已在 .D 目录下
            生成了 CSV 或 TXT 格式的报告文件, 则解析这些文件.
            否则返回空字典, 由上层调用者决定后续处理.
        参数:
            d_dir: .D 目录路径.
        返回:
            Dict[float, List[CompoundMatch]]: 保留时间 -> 化合物匹配结果列表.
                返回空字典表示无可用的定性结果.
        """
        results: Dict[float, List[CompoundMatch]] = {}

        # 策略1: 尝试读取 MassHunter 导出的报告文件
        report_files = list(d_dir.glob("*.report.csv")) + list(d_dir.glob("Report*.csv"))
        if report_files:
            results = self._limit_match_dict(self._parse_masshunter_report_csv(report_files[0]))
            if results:
                logger.info("从 MassHunter 报告文件提取 %d 个化合物匹配", len(results))
                return results

        # 策略2: 检查 Results/Qual 目录是否存在
        qual_dir = d_dir / "Results" / "Qual"
        if qual_dir.exists():
            logger.info(
                "MassHunter 定性结果目录存在但 QualResult.bin 为二进制格式, "
                "建议使用 NIST MS Search 自动化接口进行定性."
            )

        return results

    def _parse_masshunter_report_csv(self, csv_path: Path) -> Dict[float, List[CompoundMatch]]:
        """
        功能:
            解析 MassHunter 导出的 CSV 格式报告文件.
            常见列名: RT, Name, CAS#, Match Score, Formula, MW 等.
        参数:
            csv_path: CSV 报告文件路径.
        返回:
            Dict[float, List[CompoundMatch]]: 保留时间 -> 化合物匹配结果列表.
        """
        import csv

        results: Dict[float, List[CompoundMatch]] = {}

        try:
            with csv_path.open("r", encoding="utf-8", errors="replace") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    # MassHunter 报告的列名可能因版本而异, 逐一尝试
                    rt = self._extract_float_from_row(row, ["RT", "R.T.", "Retention Time", "RetTime"])
                    if rt is None:
                        continue

                    name = self._extract_str_from_row(row, ["Name", "Compound Name", "Hit Name", "Library Hit"])
                    inchikey = self._extract_str_from_row(row, ["InChIKey", "InChI Key"])
                    cas = self._extract_str_from_row(row, ["CAS#", "CAS", "CAS Number"])
                    score = self._extract_float_from_row(row, ["Match Score", "Score", "Match", "Quality"])
                    formula = self._extract_str_from_row(row, ["Formula", "Molecular Formula"])
                    mw = self._extract_float_from_row(row, ["MW", "Molecular Weight", "Mol. Weight"])

                    results.setdefault(rt, []).append(CompoundMatch(
                        compound_name=name or "",
                        inchikey=inchikey or "",
                        cas_number=cas or "",
                        match_score=score or 0.0,
                        formula=formula or "",
                        mw=mw or 0.0,
                    ))

        except Exception as e:
            logger.warning("解析 MassHunter 报告文件失败: %s - %s", csv_path, e)

        return results

    @staticmethod
    def _extract_str_from_row(row: Dict, keys: List[str]) -> Optional[str]:
        """从 CSV 行中按多个候选列名提取字符串值."""
        for key in keys:
            if key in row and row[key].strip():
                return row[key].strip()
        return None

    @staticmethod
    def _extract_float_from_row(row: Dict, keys: List[str]) -> Optional[float]:
        """从 CSV 行中按多个候选列名提取浮点值."""
        for key in keys:
            if key in row and row[key].strip():
                try:
                    return float(row[key].strip())
                except ValueError:
                    continue
        return None

    def find_closest_match(
        self,
        target_rt: float,
        matches: Dict[float, List[CompoundMatch]],
        tolerance: float = 0.05,
    ) -> Optional[List[CompoundMatch]]:
        """
        功能:
            在已有的化合物匹配字典中, 按保留时间查找最接近的匹配列表.
        参数:
            target_rt: 目标保留时间 (min).
            matches: 保留时间 -> 化合物匹配列表字典.
            tolerance: 保留时间容差 (min).
        返回:
            Optional[List[CompoundMatch]]: 最接近的匹配列表, 超出容差返回 None.
        """
        if not matches:
            return None

        closest_rt = min(matches.keys(), key=lambda rt: abs(rt - target_rt))
        if abs(closest_rt - target_rt) <= tolerance:
            return matches[closest_rt]

        return None

