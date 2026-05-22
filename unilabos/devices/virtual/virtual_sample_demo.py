"""虚拟样品演示设备 — 用于前端 sample tracking 功能的极简 demo"""

import asyncio
import logging
import random
import time
from typing import Any, Dict, List, Optional


class VirtualSampleDemo:
    """虚拟样品追踪演示设备，提供两种典型返回模式：
    - measure_samples: 等长输入输出 (前端按 index 自动对齐)
    - split_and_measure: 输出比输入长，附带 samples 列标注归属
    """

    def __init__(self, device_id: Optional[str] = None, config: Optional[Dict[str, Any]] = None, **kwargs):
        if device_id is None and "id" in kwargs:
            device_id = kwargs.pop("id")
        if config is None and "config" in kwargs:
            config = kwargs.pop("config")

        self.device_id = device_id or "unknown_sample_demo"
        self.config = config or {}
        self.logger = logging.getLogger(f"VirtualSampleDemo.{self.device_id}")
        self.data: Dict[str, Any] = {"status": "Idle"}

    # ------------------------------------------------------------------
    # Action 1: 等长输入输出，无 samples 列
    # ------------------------------------------------------------------
    async def measure_samples(self, concentrations: List[float]) -> Dict[str, Any]:
        """模拟光度测量。absorbance = concentration * 0.05 + noise

        入参和出参 list 长度相等，前端按 index 自动对齐。
        """
        self.logger.info(f"measure_samples: concentrations={concentrations}")
        absorbance = [round(c * 0.05 + random.gauss(0, 0.005), 4) for c in concentrations]
        return {"concentrations": concentrations, "absorbance": absorbance}

    # ------------------------------------------------------------------
    # Action 2: 输出比输入长，带 samples 列
    # ------------------------------------------------------------------
    async def split_and_measure(self, volumes: List[float], split_count: int = 3) -> Dict[str, Any]:
        """将每个样品均分为 split_count 份后逐份测量。

        返回的 list 长度 = len(volumes) * split_count，
        附带 samples 列标注每行属于第几个输入样品 (0-based index)。
        """
        self.logger.info(f"split_and_measure: volumes={volumes}, split_count={split_count}")
        out_volumes: List[float] = []
        readings: List[float] = []
        samples: List[int] = []

        for idx, vol in enumerate(volumes):
            split_vol = round(vol / split_count, 2)
            for _ in range(split_count):
                out_volumes.append(split_vol)
                readings.append(round(random.uniform(0.1, 1.0), 4))
                samples.append(idx)

        return {"volumes": out_volumes, "readings": readings, "unilabos_samples": samples}

    # ------------------------------------------------------------------
    # Action 3: 入参和出参都带 samples 列（不等长）
    # ------------------------------------------------------------------
    async def analyze_readings(self, readings: List[float], samples: List[int]) -> Dict[str, Any]:
        """对 split_and_measure 的输出做二次分析。

        入参 readings/samples 长度相同但 > 原始样品数，
        出参同样带 samples 列，长度与入参一致。
        """
        self.logger.info(f"analyze_readings: readings={readings}, samples={samples}")
        scores: List[float] = []
        passed: List[bool] = []
        threshold = 0.4

        for r in readings:
            score = round(r * 100 + random.gauss(0, 2), 2)
            scores.append(score)
            passed.append(r >= threshold)

        return {"scores": scores, "passed": passed, "unilabos_samples": samples}

    # ------------------------------------------------------------------
    # 状态属性
    # ------------------------------------------------------------------
    @property
    def status(self) -> str:
        return self.data.get("status", "Idle")
