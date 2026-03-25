#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
功能:
    读取 Agilent .D 目录中的色谱/质谱数据.
    支持 TIC (总离子流色谱), FID (火焰离子化检测器) 和单扫描质谱.
    TIC 优先从智达软件自动导出的 tic_front.csv 读取,
    备选使用 rainbow-api 解析 data.ms 和 FID*.ch 二进制文件.
参数:
    无.
返回:
    无.
"""

import csv
import logging
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)


class GCMSDataReader:
    """
    功能:
        读取 .D 目录中的色谱/质谱数据.
        优先使用 tic_front.csv (智达自动导出),
        备选使用 rainbow-api 解析 data.ms 和 FID1A.ch 二进制文件.
    参数:
        无.
    返回:
        无.
    """

    def read_tic(self, d_dir: Path) -> Tuple[np.ndarray, np.ndarray]:
        """
        功能:
            读取 TIC (总离子流色谱) 数据.
            优先从 tic_front.csv 解析, 不存在时用 rainbow-api 解析 data.ms.
        参数:
            d_dir: .D 目录路径.
        返回:
            Tuple[np.ndarray, np.ndarray]: (保留时间数组, 强度数组).
        """
        csv_path = d_dir / "tic_front.csv"
        if csv_path.exists():
            return self._read_tic_from_csv(csv_path)

        logger.info("tic_front.csv 不存在, 使用 rainbow-api 解析 data.ms")
        return self._read_tic_from_ms(d_dir)

    def _read_tic_from_csv(self, csv_path: Path) -> Tuple[np.ndarray, np.ndarray]:
        """
        功能:
            从智达软件导出的 tic_front.csv 解析 TIC 数据.
            文件格式: 第1行为路径信息, 第2行为 "Start of data points",
            后续每行为 "retention_time,intensity".
        参数:
            csv_path: tic_front.csv 文件路径.
        返回:
            Tuple[np.ndarray, np.ndarray]: (保留时间数组, 强度数组).
        """
        times = []
        intensities = []

        with csv_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                # 跳过头部信息行
                if not line or line.startswith("Start of") or not line[0].isdigit():
                    continue
                parts = line.split(",")
                if len(parts) >= 2:
                    try:
                        times.append(float(parts[0]))
                        intensities.append(float(parts[1]))
                    except ValueError:
                        continue

        logger.info("从 tic_front.csv 读取 %d 个数据点", len(times))
        return np.array(times), np.array(intensities)

    def _read_tic_from_ms(self, d_dir: Path) -> Tuple[np.ndarray, np.ndarray]:
        """
        功能:
            使用 rainbow-api 解析 data.ms, 计算 TIC (各扫描全离子强度之和).
        参数:
            d_dir: .D 目录路径.
        返回:
            Tuple[np.ndarray, np.ndarray]: (保留时间数组, TIC 强度数组).
        """
        import rainbow as rb

        datadir = rb.read(str(d_dir))
        ms_file = datadir.get_file("data.ms")
        if ms_file is None:
            raise FileNotFoundError(f"未找到 data.ms: {d_dir}")

        times = ms_file.xlabels          # shape: (n_scans,)
        tic = ms_file.data.sum(axis=1)   # 每次扫描的总离子强度

        logger.info("从 data.ms 读取 %d 个扫描, TIC 范围: %.0f - %.0f",
                     len(times), tic.min(), tic.max())
        return times, tic

    def read_fid(self, d_dir: Path) -> Tuple[np.ndarray, np.ndarray]:
        """
        功能:
            使用 rainbow-api 解析 FID .ch 文件, 读取 FID 检测器信号.
            兼容 FID1A.ch, FID1B.ch 等不同通道命名.
            优先使用 FID1A.ch, 若不存在则自动查找目录中其他 FID*.ch 文件.
        参数:
            d_dir: .D 目录路径.
        返回:
            Tuple[np.ndarray, np.ndarray]: (保留时间数组, FID 强度数组).
        """
        import rainbow as rb

        # 先扫描目录中所有 FID*.ch 文件, 确定实际可用的文件名
        fid_candidates = sorted(d_dir.glob("FID*.ch"))
        if len(fid_candidates) == 0:
            raise FileNotFoundError(f"未找到任何 FID*.ch 文件: {d_dir}")

        # 优先使用 FID1A.ch, 不存在则取排序后的第一个
        fid_name = "FID1A.ch"
        has_default = any(f.name == fid_name for f in fid_candidates)
        if has_default is False:
            fid_name = fid_candidates[0].name
            if len(fid_candidates) > 1:
                candidate_names = [f.name for f in fid_candidates]
                logger.warning(
                    "未找到 FID1A.ch, 目录中存在多个 FID 文件 %s, 使用 %s",
                    candidate_names, fid_name,
                )
            else:
                logger.info("未找到 FID1A.ch, 使用备选文件: %s", fid_name)

        datadir = rb.read(str(d_dir))
        fid_file = datadir.get_file(fid_name)

        times = fid_file.xlabels              # shape: (n_points,)
        intensities = fid_file.data[:, 0]     # shape: (n_points,), 取第一列

        logger.info("从 %s 读取 %d 个数据点, 信号范围: %.2f - %.2f",
                     fid_name, len(times), intensities.min(), intensities.max())
        return times, intensities

    def read_ms_spectra_at_rt(
        self, d_dir: Path, retention_time: float, tolerance: float = 0.02
    ) -> Tuple[np.ndarray, np.ndarray]:
        """
        功能:
            读取指定保留时间处的质谱数据 (m/z vs intensity).
            在 tolerance 范围内找到最近的扫描.
        参数:
            d_dir: .D 目录路径.
            retention_time: 目标保留时间 (min).
            tolerance: 保留时间匹配容差 (min).
        返回:
            Tuple[np.ndarray, np.ndarray]: (m/z 数组, 强度数组).
        """
        import rainbow as rb

        datadir = rb.read(str(d_dir))
        ms_file = datadir.get_file("data.ms")
        if ms_file is None:
            raise FileNotFoundError(f"未找到 data.ms: {d_dir}")

        # 找到最近的扫描索引
        idx = int(np.argmin(np.abs(ms_file.xlabels - retention_time)))
        actual_rt = ms_file.xlabels[idx]

        if abs(actual_rt - retention_time) > tolerance:
            logger.warning(
                "最近扫描 RT=%.3f 与目标 RT=%.3f 偏差 %.3f min, 超出容差 %.3f",
                actual_rt, retention_time, abs(actual_rt - retention_time), tolerance
            )

        spectrum = ms_file.data[idx]          # shape: (n_mz,)
        mz_values = ms_file.ylabels           # shape: (n_mz,)

        # 过滤零强度离子
        nonzero = spectrum > 0
        return mz_values[nonzero], spectrum[nonzero]

    def read_ms_spectra_at_peak(
        self,
        d_dir: Path,
        start_time: float,
        end_time: float,
        avg_scans: int = 3,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """
        功能:
            读取峰边界范围内 TIC 强度最高的质谱 (apex), 并对附近扫描取平均以提升信噪比.
            相比 read_ms_spectra_at_rt 只取最近单次扫描, 本方法:
            1. 在 [start_time, end_time] 内找到 TIC 最大的扫描 (真正的 apex).
            2. 以 apex 为中心, 平均 avg_scans 个扫描, 降低噪声.
        参数:
            d_dir: .D 目录路径.
            start_time: 峰起始时间 (min).
            end_time: 峰结束时间 (min).
            avg_scans: 以 apex 为中心的平均扫描数 (奇数, 默认 3).
        返回:
            Tuple[np.ndarray, np.ndarray]: (m/z 数组, 平均强度数组).
        """
        import rainbow as rb

        datadir = rb.read(str(d_dir))
        ms_file = datadir.get_file("data.ms")
        if ms_file is None:
            raise FileNotFoundError(f"未找到 data.ms: {d_dir}")

        scan_times = ms_file.xlabels  # shape: (n_scans,)

        # 找到峰边界内的扫描索引范围
        mask = (scan_times >= start_time) & (scan_times <= end_time)
        boundary_indices = np.where(mask)[0]

        if len(boundary_indices) == 0:
            # 降级: 使用原始最近扫描方法
            logger.warning(
                "峰范围 [%.3f, %.3f] 内无扫描, 降级为最近扫描",
                start_time, end_time,
            )
            mid_rt = (start_time + end_time) / 2.0
            return self.read_ms_spectra_at_rt(d_dir, mid_rt)

        # 计算范围内每个扫描的 TIC, 找到最大值 (apex)
        tic_in_range = ms_file.data[boundary_indices].sum(axis=1)
        apex_local_idx = int(np.argmax(tic_in_range))
        apex_idx = boundary_indices[apex_local_idx]

        # 以 apex 为中心取 avg_scans 个扫描做平均
        half = avg_scans // 2
        avg_start = max(0, apex_idx - half)
        avg_end = min(ms_file.data.shape[0], apex_idx + half + 1)

        avg_spectrum = ms_file.data[avg_start:avg_end].mean(axis=0)  # shape: (n_mz,)
        mz_values = ms_file.ylabels  # shape: (n_mz,)

        # 过滤零强度离子
        nonzero = avg_spectrum > 0

        logger.debug(
            "峰 apex 扫描: idx=%d, RT=%.3f, 平均 %d 个扫描 [%d:%d]",
            apex_idx, scan_times[apex_idx], avg_end - avg_start, avg_start, avg_end,
        )

        return mz_values[nonzero], avg_spectrum[nonzero]

    def read_sample_info(self, d_dir: Path) -> Dict:
        """
        功能:
            从 AcqData/sample_info.xml 读取样品元数据.
        参数:
            d_dir: .D 目录路径.
        返回:
            Dict: 包含 sample_name, acq_time, method, data_file 等键.
        """
        info_path = d_dir / "AcqData" / "sample_info.xml"
        result = {}

        if not info_path.exists():
            logger.warning("sample_info.xml 不存在: %s", info_path)
            return result

        tree = ET.parse(str(info_path))
        root = tree.getroot()

        # 字段名到键名的映射
        field_map = {
            "Sample Name": "sample_name",
            "Sample Position": "sample_position",
            "Data File": "data_file",
            "Method": "method",
            "AcqTime": "acq_time",
            "RunCompletedFlag": "run_completed",
            "InstrumentName": "instrument_name",
            "Inj Vol (µl)": "inj_vol",
        }

        for field_elem in root.findall("Field"):
            name = field_elem.findtext("Name", "").strip()
            value = field_elem.findtext("Value", "").strip()
            if name in field_map:
                result[field_map[name]] = value

        logger.info("样品信息: %s", result.get("sample_name", "未知"))
        return result
