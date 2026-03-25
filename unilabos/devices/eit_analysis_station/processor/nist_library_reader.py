#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
功能:
    从 lib2nist 导出的 MSP 文本文件中按名称查找库谱.
    用于 SS-HM 算法对命中化合物运行 PIM 修正.
参数:
    无.
返回:
    无.
"""

import logging
import pickle
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)


class NistLibraryReader:
    """
    功能:
        从 lib2nist 导出的 MSP 文本文件中按化合物名称查找质谱数据.
        首次加载时扫描整个 MSP 文件构建 {name: [byte_offset, ...]} 索引, 并缓存为 pickle 文件.
        同名化合物可能存在多条质谱记录, 索引保留全部偏移, 查找时返回第一条.
    参数:
        msp_path: lib2nist 导出的 MSP 文件路径.
        index_cache_path: 索引缓存路径, None 时自动推导为 <msp_path>.idx.pkl.
    返回:
        无.
    """

    def __init__(
        self,
        msp_path: Path,
        index_cache_path: Optional[Path] = None,
    ) -> None:
        self._msp_path = Path(msp_path)
        if index_cache_path is None:
            self._index_cache_path = self._msp_path.with_suffix(".idx.pkl")
        else:
            self._index_cache_path = Path(index_cache_path)

        # 名称 -> 文件字节偏移列表 (同名化合物可能有多条质谱)
        self._index: Dict[str, List[int]] = {}
        self._total_spectra = 0  # MSP 文件中的质谱总数
        self._loaded = False

        if self._msp_path.exists():
            self._load_or_build_index()
        else:
            logger.warning("NIST 库 MSP 文件不存在: %s", self._msp_path)

    @property
    def available(self) -> bool:
        """MSP 文件和索引是否可用."""
        return self._loaded and self._total_spectra > 0

    @property
    def spectrum_count(self) -> int:
        """索引中的质谱总数 (含同名重复)."""
        return self._total_spectra

    @property
    def unique_name_count(self) -> int:
        """索引中的唯一化合物名称数."""
        return len(self._index)

    # ------------------------------------------------------------------
    # 索引管理
    # ------------------------------------------------------------------

    def _load_or_build_index(self) -> None:
        """
        功能:
            加载或构建索引. 优先从缓存加载, 缓存不存在或过期时重建.
        参数:
            无.
        返回:
            无.
        """
        if self._try_load_cache():
            return
        self._build_index()
        self._save_cache()

    def _try_load_cache(self) -> bool:
        """
        功能:
            尝试从 pickle 缓存加载索引.
            缓存有效条件: 文件存在且修改时间晚于 MSP 文件.
        参数:
            无.
        返回:
            bool, 加载成功返回 True.
        """
        if not self._index_cache_path.exists():
            return False

        # 缓存修改时间必须晚于 MSP 文件
        cache_mtime = self._index_cache_path.stat().st_mtime
        msp_mtime = self._msp_path.stat().st_mtime
        if cache_mtime < msp_mtime:
            logger.info("索引缓存已过期, 将重建: %s", self._index_cache_path)
            return False

        try:
            with self._index_cache_path.open("rb") as f:
                cached = pickle.load(f)

            # 兼容旧格式 (Dict[str, int]) 和新格式 (Dict[str, List[int]])
            if isinstance(cached, dict):
                sample_val = next(iter(cached.values()), None) if cached else None
                if isinstance(sample_val, list):
                    # 新格式: Dict[str, List[int]]
                    self._index = cached
                else:
                    # 旧格式: Dict[str, int] -> 转换为新格式
                    logger.info("检测到旧格式索引缓存, 将重建")
                    return False
            else:
                logger.warning("索引缓存格式异常, 将重建")
                return False

            self._total_spectra = sum(len(offsets) for offsets in self._index.values())
            self._loaded = True
            logger.info(
                "已从缓存加载 NIST 库索引: %d 个质谱, %d 个唯一名称, 来源: %s",
                self._total_spectra, len(self._index), self._index_cache_path,
            )
            return True
        except Exception as exc:
            logger.warning("加载索引缓存失败, 将重建: %s", exc)
            return False

    def _build_index(self) -> None:
        """
        功能:
            扫描整个 MSP 文件, 记录每个 "Name: xxx" 行的字节偏移.
            同名化合物的多条记录全部保留.
            以二进制模式读取以获取精确字节偏移.
        参数:
            无.
        返回:
            无.
        """
        logger.info("开始构建 NIST 库索引, MSP 文件: %s", self._msp_path)
        self._index.clear()
        total = 0

        try:
            with self._msp_path.open("rb") as f:
                while True:
                    offset = f.tell()
                    raw_line = f.readline()
                    if raw_line == b"":
                        break  # EOF

                    # 检测 "Name: " 开头行
                    stripped = raw_line.strip()
                    if stripped.startswith(b"Name:") or stripped.startswith(b"NAME:"):
                        # 提取名称
                        name = stripped.split(b":", 1)[1].strip().decode(
                            "utf-8", errors="replace"
                        )
                        if name:
                            self._index.setdefault(name, []).append(offset)
                            total += 1

            self._total_spectra = total
            self._loaded = True
            logger.info(
                "NIST 库索引构建完成: %d 个质谱, %d 个唯一名称",
                total, len(self._index),
            )
        except Exception as exc:
            logger.error("构建 NIST 库索引失败: %s", exc)
            self._loaded = False

    def _save_cache(self) -> None:
        """
        功能:
            将索引保存为 pickle 缓存文件.
        参数:
            无.
        返回:
            无.
        """
        try:
            self._index_cache_path.parent.mkdir(parents=True, exist_ok=True)
            with self._index_cache_path.open("wb") as f:
                pickle.dump(self._index, f, protocol=pickle.HIGHEST_PROTOCOL)
            logger.info("索引缓存已保存: %s", self._index_cache_path)
        except Exception as exc:
            logger.warning("保存索引缓存失败: %s", exc)

    # ------------------------------------------------------------------
    # 质谱查找
    # ------------------------------------------------------------------

    def get_spectrum(
        self,
        name: str,
    ) -> Optional[Tuple[np.ndarray, np.ndarray, int]]:
        """
        功能:
            按化合物名称查找质谱, 返回 (mz, abundance, mw).
        参数:
            name: 化合物名称 (与 MSP 文件中 Name 字段匹配).
        返回:
            Optional[Tuple[np.ndarray, np.ndarray, int]]:
                (mz 数组, 强度数组, 分子量).
                未找到或解析失败返回 None.
        """
        if not self._loaded:
            return None

        offsets = self._index.get(name)
        if offsets is None or len(offsets) == 0:
            return None

        # 返回第一条同名质谱
        return self._read_spectrum_at_offset(offsets[0])

    def _read_spectrum_at_offset(
        self,
        offset: int,
    ) -> Optional[Tuple[np.ndarray, np.ndarray, int]]:
        """
        功能:
            从 MSP 文件指定偏移处读取一个质谱记录.
            MSP 格式:
            Name: xxx
            MW: 383
            Num Peaks: 176
            41 91; 42 15; 43 16; ...
        参数:
            offset: 文件字节偏移 (Name 行起始位置).
        返回:
            Optional[Tuple[np.ndarray, np.ndarray, int]]:
                (mz, abundance, mw), 解析失败返回 None.
        """
        try:
            mw = 0
            num_peaks = 0
            reading_peaks = False
            mz_list: List[float] = []
            ab_list: List[float] = []

            with self._msp_path.open("r", encoding="utf-8", errors="replace") as f:
                f.seek(offset)
                for line in f:
                    stripped = line.strip()

                    # 空行表示记录结束
                    if stripped == "" and reading_peaks:
                        break

                    upper = stripped.upper()
                    if upper.startswith("NAME:"):
                        continue

                    if upper.startswith("MW:"):
                        try:
                            mw = int(stripped.split(":", 1)[1].strip())
                        except ValueError:
                            pass
                        continue

                    if upper.startswith("NUM PEAKS:"):
                        try:
                            num_peaks = int(stripped.split(":", 1)[1].strip())
                        except ValueError:
                            pass
                        reading_peaks = True
                        continue

                    # 跳过其他元数据行 (如 Formula, InChIKey 等)
                    if not reading_peaks:
                        continue

                    # 解析峰数据行, 支持两种格式:
                    # 1. "mz ab; mz ab; ..." (分号分隔)
                    # 2. "mz ab\nmz ab\n..." (每行一对)
                    self._parse_peak_line(stripped, mz_list, ab_list)

                    # 遇到下一个 Name 行则停止
                    if upper.startswith("NAME:"):
                        break

            if len(mz_list) == 0:
                return None

            return (
                np.array(mz_list, dtype=float),
                np.array(ab_list, dtype=float),
                mw,
            )
        except Exception as exc:
            logger.warning("读取偏移 %d 处质谱失败: %s", offset, exc)
            return None

    @staticmethod
    def _parse_peak_line(
        line: str,
        mz_list: List[float],
        ab_list: List[float],
    ) -> None:
        """
        功能:
            解析单行峰数据, 支持分号分隔和空格分隔两种格式.
            "41 91; 42 15; 43 16" → [(41, 91), (42, 15), (43, 16)]
            "41 91" → [(41, 91)]
            "(41,91) (42,15)" → [(41, 91), (42, 15)]
        参数:
            line: 峰数据行文本.
            mz_list: m/z 值累加列表 (输出参数).
            ab_list: 强度值累加列表 (输出参数).
        返回:
            无.
        """
        if not line:
            return

        # 分号分隔的情况
        if ";" in line:
            segments = line.split(";")
        else:
            segments = [line]

        for seg in segments:
            seg = seg.strip()
            if not seg:
                continue

            # 支持 "mz ab" 和 "mz\tab" 格式
            parts = seg.replace("\t", " ").split()
            if len(parts) >= 2:
                try:
                    mz_val = float(parts[0])
                    ab_val = float(parts[1])
                    mz_list.append(mz_val)
                    ab_list.append(ab_val)
                except ValueError:
                    continue
