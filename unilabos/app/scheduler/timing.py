"""按参数、设备和计时边界隔离的耗时预测，不授予执行权或硬时限保证。

公式只能由程序配置，不能执行工作流传入的表达式。历史样本按显式特征
分组；未声明特征时保守地使用全部参数，避免不同工作量混成同一 EMA。
拟合样本与余量校准样本分开，拟合数据变化会使旧校准失效。
"""
from __future__ import annotations

import hashlib
import json
import math
import statistics
import threading
from collections import deque
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional

from unilabos.app.scheduler.param_resolver import json_get_exists

DEFAULT_BOUNDARY = "dispatch_to_observed_completion"


def finite_seconds(value: Any, *, allow_zero: bool = False) -> Optional[float]:
    """拒绝布尔、NaN、无穷及非正时长；允许旧声明参数中的数值字符串。"""
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(result) or result < 0 or (result == 0 and not allow_zero):
        return None
    return result


def _fingerprint(value: Any) -> str:
    # 不使用 default=str：不支持的数据不能悄悄变成不同参数的同一组。
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _features(params: Any, paths: Optional[tuple[str, ...]]) -> Any:
    if paths is None:
        return {} if params is None else params
    result = {}
    for path in paths:
        exists, value = json_get_exists(params, path)
        if not exists:
            raise ValueError(f"缺少估时参数: {path}")
        result[path] = value
    return result


def _quantile(values: list[float], q: float) -> float:
    """经验分位取相应次序统计量，不声称具有未经验证的总体覆盖率。"""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def constant_speed_cycles(params: Any) -> float:
    """仅适用于目标圈数均在恒定转速下计数的公式，结果单位为秒。

    cycles 为圈数，rpm 为圈/分钟。本函数不猜测设备是否在加减速时计圈，
    也不追加未经声明的启动/停止时间；使用者必须配置适用范围和计时端点。
    """
    _, cycles = json_get_exists(params, "cycles")
    _, rpm = json_get_exists(params, "rpm")
    n, speed = finite_seconds(cycles), finite_seconds(rpm)
    if n is None or speed is None:
        raise ValueError("cycles 与 rpm 必须为有限正数")
    seconds = 60.0 * n / speed
    if finite_seconds(seconds) is None:
        raise ValueError("圈数公式产生无效时长")
    return seconds


@dataclass(frozen=True)
class ActionTimingSpec:
    """单个动作的估时规则；只有明确选择的参数参与历史分组。

    feature_paths=None 表示全部参数；空元组表示已确认参数不影响耗时。
    calibration_paths 默认同 feature_paths；只有明确验证残差可跨工作量
    共用时，才把对应参数从该分组中移除。更改规则必须变更 version。
    """
    version: str = "1"
    feature_paths: Optional[tuple[str, ...]] = None
    formula: Optional[Callable[[Any], float]] = field(default=None, repr=False, compare=False)
    calibration_paths: Optional[tuple[str, ...]] = None
    parameter_bounds: Mapping[str, tuple[float, float]] = field(default_factory=dict)
    timing_boundary: str = DEFAULT_BOUNDARY
    end_mode: str = "observed_completion"
    default_s: Optional[float] = None

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ActionTimingSpec:
        """只接受静态 JSON 配置及内置公式名称，不执行外部表达式。"""
        config = dict(value)
        formula = config.get("formula")
        if formula is not None:
            formulas = {"constant_speed_cycles": constant_speed_cycles}
            if not isinstance(formula, str) or formula not in formulas:
                raise ValueError("JSON 估时公式不在允许清单中")
            config["formula"] = formulas[formula]
        return cls(**config)

    def __post_init__(self) -> None:
        if any(not isinstance(value, str) or not value.strip()
               for value in (self.version, self.timing_boundary, self.end_mode)):
            raise ValueError("估时规则版本、结束模式和计时边界必须为非空字符串")
        if self.formula is not None and not callable(self.formula):
            raise ValueError("formula 必须是程序提供的可调用规则")
        for name in ("feature_paths", "calibration_paths"):
            paths = getattr(self, name)
            if paths is not None:
                if isinstance(paths, str) or any(not isinstance(p, str) or not p for p in paths):
                    raise ValueError(f"{name} 必须为非空路径列表")
                object.__setattr__(self, name, tuple(paths))
        frozen_bounds = {}
        for path, bounds in self.parameter_bounds.items():
            if not isinstance(path, str) or not path or len(bounds) != 2:
                raise ValueError("参数范围必须由路径和上下界组成")
            low, high = bounds
            if isinstance(low, bool) or isinstance(high, bool):
                raise ValueError("参数范围不能为布尔值")
            low, high = float(low), float(high)
            if not math.isfinite(low) or not math.isfinite(high) or low > high:
                raise ValueError("参数范围必须有限且下界不大于上界")
            frozen_bounds[path] = (low, high)
        object.__setattr__(self, "parameter_bounds", MappingProxyType(frozen_bounds))
        if self.default_s is not None and finite_seconds(self.default_s) is None:
            raise ValueError("临时默认时长必须为有限正数")


@dataclass(frozen=True)
class TimingEstimate:
    """可序列化的预测结果；planned_s=None 表示未知，不能按零占用处理。"""
    point_s: Optional[float]
    planned_s: Optional[float]
    margin_s: Optional[float]
    source: str
    kind: str
    confidence: str
    samples: int
    calibration_samples: int
    quantile: float
    model_version: str
    parameter_fingerprint: str
    device_id: str
    action_version: str
    timing_boundary: str
    parameters_final: bool
    reason: str = ""
    hard_bound_s: Optional[float] = None

    @property
    def known(self) -> bool:
        return self.planned_s is not None


@dataclass
class _Samples:
    actual: deque[float]
    residual: deque[float]
    calibration_errors: deque[float]
    generation: int = 0


class TimingModel:
    """线程安全的分组预测器；默认策略参数可配置，不代表已通过工艺校验。"""

    def __init__(self, *, quantile: float = 0.9, min_samples: int = 1,
                 sample_limit: int = 512) -> None:
        if isinstance(quantile, bool) or not math.isfinite(quantile) or not 0.5 <= quantile < 1:
            raise ValueError("计划分位必须在 [0.5, 1) 内")
        if isinstance(min_samples, bool) or not isinstance(min_samples, int) or min_samples < 1:
            raise ValueError("min_samples 必须为正整数")
        if isinstance(sample_limit, bool) or not isinstance(sample_limit, int) or sample_limit < min_samples:
            raise ValueError("sample_limit 必须不小于 min_samples")
        self.quantile = float(quantile)
        self.min_samples = min_samples
        self.sample_limit = sample_limit
        self._groups: dict[str, _Samples] = {}
        self._seen: dict[str, str] = {}
        self._lock = threading.RLock()

    def _context(self, action_key: str, params: Any, spec: ActionTimingSpec,
                 device_id: str, action_version: str) -> tuple[str, str]:
        for path, (low, high) in spec.parameter_bounds.items():
            exists, value = json_get_exists(params, path)
            try:
                numeric = float(value)
            except (TypeError, ValueError, OverflowError):
                raise ValueError(f"参数 {path} 不是有限数值") from None
            if not exists or isinstance(value, bool) or not math.isfinite(numeric) or not low <= numeric <= high:
                raise ValueError(f"参数 {path} 超出估时规则适用范围")
        features = _features(params, spec.feature_paths)
        parameter_fingerprint = _fingerprint(features)
        calibration_paths = spec.calibration_paths
        if calibration_paths is None:
            calibration_paths = spec.feature_paths
        grouping = _features(params, calibration_paths if spec.formula else spec.feature_paths)
        key = _fingerprint([action_key, device_id, action_version, spec.version,
                            spec.timing_boundary, spec.end_mode, grouping])
        return key, parameter_fingerprint

    def _formula(self, spec: ActionTimingSpec, params: Any) -> Optional[float]:
        if spec.formula is None:
            return None
        # 只执行程序注册规则；失败不能回退到不适用的历史数字。
        result = finite_seconds(spec.formula(params))
        if result is None:
            raise ValueError("公式产生非正或非有限时长")
        return result

    def estimate(self, action_key: str, params: Any, *, spec: ActionTimingSpec,
                 device_id: str = "", action_version: str = "", parameters_final: bool = True,
                 declared_s: Optional[float] = None, fallback_s: Optional[float] = None,
                 historical: bool = True) -> TimingEstimate:
        fingerprint = ""
        reason = ""
        point = planned = None
        source, confidence = "unknown", "unknown"
        count = calibration_count = generation = 0
        try:
            key, fingerprint = self._context(action_key, params, spec, device_id, action_version)
            formula = self._formula(spec, params)
            with self._lock:
                group = self._groups.get(key) if historical else None
                count = len(group.actual) if group else 0
                generation = group.generation if group else 0
                correction = statistics.median(group.residual) if formula is not None and group and count >= self.min_samples else 0.0
                if formula is not None:
                    point = formula + correction
                    source = "formula_calibrated" if count >= self.min_samples else "formula"
                elif declared_s is not None:
                    # 参数中明确给出的时长优先，不让旧参数组均值覆盖工作量。
                    point, source = finite_seconds(declared_s), "declared"
                elif group and count >= self.min_samples:
                    point = statistics.median(group.actual)
                    source = "historical_group"
                else:
                    point = finite_seconds(spec.default_s if spec.default_s is not None else fallback_s)
                    source = "default" if point is not None else "unknown"
                if point is not None:
                    if finite_seconds(point) is None:
                        raise ValueError("校准后产生非正或非有限时长")
                    planned = point
                    confidence = "low"
                    if group:
                        calibration_count = len(group.calibration_errors)
                    if group and calibration_count >= self.min_samples:
                        planned = point + max(0.0, _quantile(list(group.calibration_errors), self.quantile))
                        confidence = "empirical_calibrated"
                    elif group and count >= self.min_samples:
                        # 生产训练样本也可形成经验余量，但始终标低可信，不能冒充
                        # 独立留出校准。公式跨工作量只共享残差，不共享总耗时。
                        if formula is not None:
                            residual_q = _quantile(list(group.residual), self.quantile)
                            planned = max(point, formula + residual_q)
                        else:
                            planned = max(point, _quantile(list(group.actual), self.quantile))
                    if finite_seconds(planned) is None:
                        raise ValueError("计划时长无效")
                    reason = "统计预测或临时默认值不构成硬时限上界"
                else:
                    reason = "没有适用公式、声明时长或足够的同组历史"
        except (TypeError, ValueError, OverflowError, ZeroDivisionError) as error:
            point = planned = None
            source, confidence, reason = "unknown", "unknown", str(error)
        if not parameters_final and point is not None:
            confidence = "tentative"
        return TimingEstimate(point, planned, None if point is None else planned - point,
                              source, "total", confidence, count, calibration_count,
                              self.quantile, f"{spec.version}:{generation}", fingerprint,
                              device_id, action_version, spec.timing_boundary, parameters_final, reason)

    def observe(self, action_key: str, actual_s: Any, params: Any, *, spec: ActionTimingSpec,
                device_id: str = "", action_version: str = "", outcome: str = "success",
                observation_id: Optional[str] = None, calibration: bool = False,
                declared_s: Optional[float] = None, fallback_s: Optional[float] = None) -> bool:
        """仅接纳完整成功样本；失败、取消、暂停和删失不能当完整耗时。

        calibration=True 表示调用方已经按时间/任务划分的独立校准集。
        observation_id 应使用稳定 Attempt 身份；同 ID 异内容直接报错。
        """
        actual = finite_seconds(actual_s)
        if actual is None or outcome != "success":
            return False
        try:
            key, fingerprint = self._context(action_key, params, spec, device_id, action_version)
            formula = self._formula(spec, params)
        except (TypeError, ValueError, OverflowError, ZeroDivisionError):
            return False
        signature = _fingerprint([key, fingerprint, actual, calibration])
        with self._lock:
            if observation_id is not None and observation_id in self._seen:
                if self._seen[observation_id] != signature:
                    raise ValueError("同一耗时样本身份收到不同内容")
                return False
            group = self._groups.setdefault(key, _Samples(deque(maxlen=self.sample_limit),
                deque(maxlen=self.sample_limit), deque(maxlen=self.sample_limit)))
            if calibration:
                estimate = self.estimate(action_key, params, spec=spec, device_id=device_id,
                    action_version=action_version, declared_s=declared_s, fallback_s=fallback_s)
                if estimate.point_s is None or estimate.source == "default":
                    return False
                group.calibration_errors.append(actual - estimate.point_s)
            else:
                group.actual.append(actual)
                if formula is not None:
                    group.residual.append(actual - formula)
                group.generation += 1
                # 拟合模型变化后，不再宣称旧误差属于当前模型的独立校准。
                group.calibration_errors.clear()
            if observation_id is not None:
                self._seen[observation_id] = signature
            return True

    def remaining(self, action_key: str, params: Any, *, spec: ActionTimingSpec,
                  elapsed_s: Any, device_id: str = "", action_version: str = "",
                  progress_remaining_s: Optional[float] = None, completed: bool = False,
                  still_running: bool = True) -> TimingEstimate:
        """返回剩余预测；still_running 必须来自可信证据，而非仅未收到完成消息。"""
        base = self.estimate(action_key, params, spec=spec, device_id=device_id,
                             action_version=action_version)
        elapsed = finite_seconds(elapsed_s, allow_zero=True)
        point = planned = None
        source, confidence, reason = "unknown", "unknown", "缺少可信剩余时间依据"
        if elapsed is None:
            reason = "已运行时间必须为有限非负数"
        elif completed:
            point = planned = 0.0
            source, confidence, reason = "completed", "observed", "真实完成证据，不代替资源交接"
        elif not still_running:
            reason = "未确认设备仍在运行，须核实物理状态"
        elif progress_remaining_s is not None:
            point = finite_seconds(progress_remaining_s)
            if point is not None:
                planned = point
                source, confidence, reason = "progress", "low", "按可信剩余进度预测，未校准硬上界"
            else:
                reason = "运行中的进度预测须为有限正数；零不能证明完成"
        else:
            try:
                key, _ = self._context(action_key, params, spec, device_id, action_version)
                # 使用同工作量的完整历史条件分布，禁止跨公式残差组共享总耗时。
                if spec.formula and spec.calibration_paths is not None and spec.calibration_paths != spec.feature_paths:
                    raise ValueError("跨参数残差组不能用于总耗时的条件剩余预测")
                with self._lock:
                    group = self._groups.get(key)
                    surviving = [t - elapsed for t in group.actual if t > elapsed] if group else []
                if len(surviving) >= self.min_samples:
                    point = statistics.median(surviving)
                    planned = max(point, _quantile(surviving, self.quantile))
                    source, confidence = "conditional_history", "low"
                    reason = "仅同组已完成样本中超过当前时长的经验条件分布；不提供硬保证"
            except (TypeError, ValueError, OverflowError) as error:
                reason = str(error)
        return TimingEstimate(point, planned, None if point is None else planned - point,
            source, "remaining", confidence, base.samples, 0, self.quantile, base.model_version,
            base.parameter_fingerprint, device_id, action_version, spec.timing_boundary, True, reason)
