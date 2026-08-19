"""节点执行时长预估：声明式（gjson）与历史统计（EMA）两种计算模式。

两种模式：

- **declared（声明式）**：用 gjson 语义（``param_resolver.json_get``）从节点
  参数里按路径清单取声明时长。参数是 sjson 覆写后的 resolved param 时，
  预估自动反映父节点传参的真实值（例如上游把 ``time`` 写进等待节点）。
- **historical（历史统计）**：按 device_action_key 维护实际执行时长的
  指数滑动平均（EMA），每个 job 完成时 ``observe()`` 一次。

模式选择（``mode``）：

- ``"declared"``：只用声明式（查不到用 default_s）
- ``"historical"``：只用历史（无样本回退声明式）
- ``"auto"``（默认）：有历史样本用历史，否则声明式

线程安全：EdgeScheduler 在自身 RLock 内调用，本模块不额外加锁。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from unilabos.server.scheduler.param_resolver import json_get_exists

# 声明式预估默认查找路径（gjson dot path，按序取第一个命中的数值）
DEFAULT_DECLARED_PATHS: Tuple[str, ...] = (
    "estimated_duration_s",
    "estimated_duration",
    "duration_s",
    "duration",
    "time",
)

# 预估来源标记（timeline / 前端展示用）
SOURCE_DECLARED = "declared"      # 命中 gjson 声明路径
SOURCE_HISTORICAL = "historical"  # 历史 EMA
SOURCE_DEFAULT = "default"        # 都没有 → 兜底默认值


@dataclass
class _ActionStats:
    """单 device_action_key 的历史统计。"""

    ema_s: float = 0.0
    samples: int = 0
    last_s: float = 0.0

    def observe(self, seconds: float, alpha: float) -> None:
        self.last_s = seconds
        if self.samples == 0:
            self.ema_s = seconds
        else:
            self.ema_s = alpha * seconds + (1.0 - alpha) * self.ema_s
        self.samples += 1


@dataclass
class DurationEstimator:
    """两种计算模式的时长预估器（秒）。"""

    mode: str = "auto"                      # declared / historical / auto
    default_s: float = 60.0                 # 兜底默认时长
    ema_alpha: float = 0.35                 # EMA 平滑系数（越大越跟随最新样本）
    declared_paths: Tuple[str, ...] = DEFAULT_DECLARED_PATHS
    # device_action_key → 静态默认表（装配时可注入 action 元数据）
    static_defaults: Dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.mode not in ("declared", "historical", "auto"):
            raise ValueError(f"unknown estimate mode: {self.mode!r}")
        self._stats: Dict[str, _ActionStats] = {}
        self._lock = threading.Lock()

    # ── 声明式 ────────────────────────────────────────────────

    def _declared(self, params: Any) -> Optional[float]:
        """按 gjson 路径清单从参数取声明时长；返回 None = 未声明。"""
        if not isinstance(params, (dict, list)):
            return None
        for path in self.declared_paths:
            exists, value = json_get_exists(params, path)
            if not exists:
                continue
            try:
                seconds = float(value)
            except (TypeError, ValueError):
                continue
            if seconds > 0:
                return seconds
        return None

    # ── 历史统计 ──────────────────────────────────────────────

    def observe(self, device_action_key: str, actual_s: float) -> None:
        """job 完成回调：记录实际执行时长。"""
        # Windows 的 wall clock 在极短模拟动作上可能同 tick，0 秒仍是有效样本；
        # 仅拒绝不可能的负时长。
        if actual_s < 0:
            return
        with self._lock:
            stats = self._stats.setdefault(device_action_key, _ActionStats())
            stats.observe(actual_s, self.ema_alpha)

    def _historical(self, device_action_key: str) -> Optional[float]:
        with self._lock:
            stats = self._stats.get(device_action_key)
            if stats is None or stats.samples == 0:
                return None
            return stats.ema_s

    # ── 统一入口 ──────────────────────────────────────────────

    def estimate(self, device_action_key: str, params: Any = None) -> Tuple[float, str]:
        """返回 ``(预估秒数, 来源)``；来源 ∈ declared / historical / default。"""
        declared = self._declared(params)
        historical = self._historical(device_action_key)

        if self.mode == "declared":
            picked, source = declared, SOURCE_DECLARED
        elif self.mode == "historical":
            picked, source = historical, SOURCE_HISTORICAL
            if picked is None:
                picked, source = declared, SOURCE_DECLARED
        else:  # auto：历史优先（越跑越准），无样本用声明
            if historical is not None:
                picked, source = historical, SOURCE_HISTORICAL
            else:
                picked, source = declared, SOURCE_DECLARED

        if picked is None:
            fallback = self.static_defaults.get(device_action_key)
            if fallback is not None and fallback > 0:
                return float(fallback), SOURCE_DECLARED
            return float(self.default_s), SOURCE_DEFAULT
        return float(picked), source

    # ── 观测面 ────────────────────────────────────────────────

    def stats(self) -> List[Dict[str, Any]]:
        """各 device_action_key 的历史统计（timeline API 暴露）。"""
        with self._lock:
            return [
                {
                    "device_action_key": key,
                    "ema_s": round(s.ema_s, 3),
                    "samples": s.samples,
                    "last_s": round(s.last_s, 3),
                }
                for key, s in sorted(self._stats.items())
            ]


__all__ = [
    "DEFAULT_DECLARED_PATHS",
    "SOURCE_DECLARED",
    "SOURCE_DEFAULT",
    "SOURCE_HISTORICAL",
    "DurationEstimator",
]
