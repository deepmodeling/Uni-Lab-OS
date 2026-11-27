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

旧入口保持 EMA 兼容；新排程使用 estimate_timing 的参数/设备隔离模型。
线程安全：旧统计与新的分组模型各自加锁。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from unilabos.app.scheduler.param_resolver import json_get_exists
from unilabos.app.scheduler.timing import (
    ActionTimingSpec,
    TimingEstimate,
    TimingModel,
    finite_seconds,
)

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

_UNSET = object()


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
    timing_specs: Dict[str, ActionTimingSpec] = field(default_factory=dict)
    plan_quantile: float = 0.9
    # 一个完整的同类成功样本即可用于首轮排程；后续样本仍会继续校准。
    minimum_samples: int = 1
    sample_limit: int = 512

    def __post_init__(self) -> None:
        if self.mode not in ("declared", "historical", "auto"):
            raise ValueError(f"unknown estimate mode: {self.mode!r}")
        if finite_seconds(self.default_s) is None:
            raise ValueError("default_s 必须为有限正数")
        if finite_seconds(self.ema_alpha) is None or self.ema_alpha > 1:
            raise ValueError("ema_alpha 必须在 (0, 1] 内")
        self._timing = TimingModel(quantile=self.plan_quantile,
                                  min_samples=self.minimum_samples, sample_limit=self.sample_limit)
        configured_specs = dict(self.timing_specs)
        self.timing_specs = {}
        self._spec_lock = threading.Lock()
        for key, spec in configured_specs.items():
            self.register_timing_spec(key, spec)
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
            seconds = finite_seconds(value)
            if seconds is not None:
                return seconds
        return None

    # ── 历史统计 ──────────────────────────────────────────────

    def observe(self, device_action_key: str, actual_s: float, params: Any = _UNSET, *,
                device_id: str = "", action_version: str = "", outcome: str = "success",
                observation_id: Optional[str] = None, calibration: bool = False) -> bool:
        """记录完整成功样本；传 params 时参与新版参数分组，省略则仅兼容旧 EMA。

        observation_id 使用稳定 Attempt 身份去重。校准样本必须由调用方
        按时间/任务划分，不能把同一个执行既当拟合样本又当独立校准样本。
        """
        seconds = finite_seconds(actual_s)
        if seconds is None or outcome != "success":
            return False
        if params is not _UNSET:
            accepted = self._timing.observe(device_action_key, seconds, params,
                spec=self._spec_for(device_action_key), device_id=device_id,
                action_version=action_version, outcome=outcome, observation_id=observation_id,
                calibration=calibration, declared_s=self._declared(params),
                fallback_s=self._fallback(device_action_key))
            if not accepted:
                return False
        with self._lock:
            stats = self._stats.setdefault(device_action_key, _ActionStats())
            stats.observe(seconds, self.ema_alpha)
        return True

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
            fallback = finite_seconds(fallback)
            if fallback is not None:
                return fallback, SOURCE_DECLARED
            return float(self.default_s), SOURCE_DEFAULT
        return float(picked), source

    # ── 参数与设备敏感的新排程入口 ────────────────────────────

    def register_timing_spec(self, device_action_key: str,
                             spec: ActionTimingSpec | Mapping[str, Any]) -> None:
        """注册程序规则或 JSON 配置；公式名称仅能来自内置白名单。"""
        if not isinstance(spec, ActionTimingSpec):
            spec = ActionTimingSpec.from_dict(spec)
        with self._spec_lock:
            current = self.timing_specs.get(device_action_key)
            if current is not None and current.version == spec.version and current is not spec:
                raise ValueError("替换估时规则必须使用新的 version")
            self.timing_specs[device_action_key] = spec

    def _spec_for(self, device_action_key: str) -> ActionTimingSpec:
        with self._spec_lock:
            return self.timing_specs.get(device_action_key, ActionTimingSpec())

    def _fallback(self, device_action_key: str) -> float:
        return finite_seconds(self.static_defaults.get(device_action_key)) or float(self.default_s)

    def estimate_timing(self, device_action_key: str, params: Any = None, *,
                        device_id: str = "", action_version: str = "",
                        parameters_final: bool = True) -> TimingEstimate:
        """返回总耗时与软余量；不使用缺少参数/设备上下文的旧 EMA。

        未配置规则时按全部参数隔离历史；无足够数据时可使用低可信默认值。
        parameters_final=False 仅供预测，不能用来允许未确定参数的实际动作。
        """
        return self._timing.estimate(device_action_key, params,
            spec=self._spec_for(device_action_key), device_id=device_id,
            action_version=action_version, parameters_final=parameters_final,
            declared_s=self._declared(params), fallback_s=self._fallback(device_action_key),
            historical=self.mode != "declared")

    def estimate_remaining(self, device_action_key: str, params: Any = None, *,
                           elapsed_s: float, device_id: str = "", action_version: str = "",
                           progress_remaining_s: Optional[float] = None,
                           completed: bool = False, still_running: bool = True) -> TimingEstimate:
        """按可信进度或条件历史预测剩余时间；未知返回 None，不返回假零值。

        elapsed_s 与规则声明的计时边界必须一致；没有完成回报不等于
        still_running 的证据。资源释放必须由独立物理完成/交接事件决定。
        """
        return self._timing.remaining(device_action_key, params,
            spec=self._spec_for(device_action_key), elapsed_s=elapsed_s,
            device_id=device_id, action_version=action_version,
            progress_remaining_s=progress_remaining_s, completed=completed,
            still_running=still_running)

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
    "ActionTimingSpec",
    "TimingEstimate",
]
