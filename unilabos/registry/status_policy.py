"""设备状态策略的注册表规范化与运行时解析。

状态策略把 ``@topic_config`` 发布的标量值映射为设备 incident。规范化后的
结构只包含 JSON 安全的内建标量与容器，因此同一份元数据既可写入注册表，也可
由 Edge 在不导入设备驱动的情况下解析。
"""

from __future__ import annotations

import math
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, List, Literal, Mapping, Optional, TypeAlias

from typing_extensions import NotRequired, TypedDict


StatusScalar: TypeAlias = bool | int | float | str
StatusSeverity: TypeAlias = Literal["info", "warning", "error", "critical"]

_SCALAR_TYPES = (bool, int, float, str)
_SEVERITIES = frozenset({"info", "warning", "error", "critical"})


class StatusIncident(TypedDict):
    """一个规范化后的状态 incident 规则。"""

    value: StatusScalar
    code: str
    severity: StatusSeverity
    message: str
    hold: bool


class StatusPolicy(TypedDict):
    """写入注册表并供 Edge 消费的规范化状态策略。"""

    normal_values: List[StatusScalar]
    incidents: List[StatusIncident]
    unknown_incident: NotRequired[StatusIncident]


@dataclass(frozen=True)
class StatusEvaluation:
    """单个状态值的解析结果；``None`` 表示策略无法判断。"""

    healthy: Optional[bool]
    incident: Optional[StatusIncident] = None


def _scalar(value: Any, field: str) -> StatusScalar:
    """验证并转换为 JSON 安全的内建标量。"""

    if not isinstance(value, _SCALAR_TYPES):
        raise TypeError(f"{field} must be str/int/float/bool")
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{field} must be finite")
        return float(value)
    return str(value)


def _scalar_key(value: StatusScalar) -> tuple[type, StatusScalar]:
    """返回保留 JSON 标量类型的匹配键。

    Python 会把 ``True``、``1`` 和 ``1.0`` 当作相等值，但设备状态的 schema
    类型是规则的一部分，不能让三者互相命中。
    """

    if isinstance(value, bool):
        return bool, value
    if isinstance(value, int):
        return int, value
    if isinstance(value, float):
        return float, value
    return str, value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    result = value.strip()
    if not result:
        raise ValueError(f"{field} must be non-empty")
    return result


def _optional_text(
    raw: Mapping[str, Any],
    defaults: Mapping[str, Any],
    key: str,
    field: str,
) -> Optional[str]:
    """读取可选文本；兼容既有的 ``None``/空字符串回退语义。"""

    value = raw[key] if key in raw else defaults.get(key)
    if value is None or value == "":
        return None
    return _text(value, field)


def _incident_config(
    value: Any,
    raw: Any,
    *,
    defaults: Mapping[str, Any],
    field: str,
) -> StatusIncident:
    if isinstance(raw, str):
        raw = {"message": raw}
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise TypeError(f"{field} must be a mapping or message string")

    normalized_value = _scalar(value, f"{field}.value")

    severity_value = raw.get("severity", defaults.get("severity", "error"))
    if not isinstance(severity_value, str):
        raise TypeError(f"{field}.severity must be a string")
    severity = severity_value.strip().lower()
    if severity not in _SEVERITIES:
        raise ValueError(f"{field}.severity must be info/warning/error/critical")

    code = _optional_text(raw, defaults, "code", f"{field}.code")
    if code is None:
        code = f"status.{normalized_value}"

    message = _optional_text(raw, defaults, "message", f"{field}.message")
    if message is None:
        message = f"device status changed to {normalized_value!r}"

    hold = raw.get("hold", defaults.get("hold", severity in {"error", "critical"}))
    if not isinstance(hold, bool):
        raise TypeError(f"{field}.hold must be bool")

    return {
        "value": normalized_value,
        "code": code,
        "severity": severity,  # type: ignore[typeddict-item]
        "message": message,
        "hold": hold,
    }


def normalize_status_policy(
    policy: Mapping[str, Any] | None,
) -> Optional[StatusPolicy]:
    """验证并复制状态策略，生成稳定、JSON 安全的注册表结构。

    规范输入使用 ``normal_values`` 与 ``incidents``::

        {
            "normal_values": ["Idle", "Running"],
            "incidents": {
                "Error": {
                    "code": "stirrer.error",
                    "severity": "error",
                    "message": "stirrer entered error state",
                    "hold": True,
                }
            },
        }

    ``healthy_values`` 是 ``normal_values`` 的输入别名。``error_values`` 是
    incidents 的紧凑写法，并继承顶层 ``code/severity/message/hold`` 默认值。
    配置 ``unknown_incident`` 后，所有未被显式列出的合法标量都会触发它。

    ``None`` 与空映射表示未配置。其他 malformed 值会立即报错，避免把损坏的
    策略静默写入注册表。重复 normal 值保持首次出现顺序并去重；重复 incident
    值因语义有歧义而拒绝。
    """

    if policy is None:
        return None
    if not isinstance(policy, Mapping):
        raise TypeError("status_policy must be a mapping")
    if not policy:
        return None

    raw_normal = policy.get("normal_values", policy.get("healthy_values", []))
    if not isinstance(raw_normal, (list, tuple)):
        raise TypeError("status_policy.normal_values must be a list")

    normal_values: List[StatusScalar] = []
    normal_keys: set[tuple[type, StatusScalar]] = set()
    for index, raw_value in enumerate(raw_normal):
        value = _scalar(raw_value, f"status_policy.normal_values[{index}]")
        key = _scalar_key(value)
        if key not in normal_keys:
            normal_values.append(value)
            normal_keys.add(key)

    defaults = {
        key: deepcopy(policy[key])
        for key in ("code", "severity", "message", "hold")
        if key in policy
    }

    incidents: List[StatusIncident] = []
    incident_keys: set[tuple[type, StatusScalar]] = set()

    def append_incident(value: Any, config: Any, field: str) -> None:
        incident = _incident_config(value, config, defaults=defaults, field=field)
        key = _scalar_key(incident["value"])
        if key in incident_keys:
            raise ValueError(f"duplicate status incident value: {incident['value']!r}")
        incidents.append(incident)
        incident_keys.add(key)

    raw_incidents = policy.get("incidents", {})
    if raw_incidents is None:
        raw_incidents = {}
    if isinstance(raw_incidents, Mapping):
        for value, config in raw_incidents.items():
            append_incident(value, config, f"status_policy.incidents[{value!r}]")
    elif isinstance(raw_incidents, list):
        for index, item in enumerate(raw_incidents):
            field = f"status_policy.incidents[{index}]"
            if not isinstance(item, Mapping):
                raise TypeError(f"{field} must be a mapping")
            if "value" not in item:
                raise ValueError(f"{field} requires value")
            append_incident(item["value"], item, field)
    else:
        raise TypeError("status_policy.incidents must be a mapping or list")

    raw_error_values = policy.get("error_values", [])
    if not isinstance(raw_error_values, (list, tuple)):
        raise TypeError("status_policy.error_values must be a list")
    for index, raw_value in enumerate(raw_error_values):
        value = _scalar(raw_value, f"status_policy.error_values[{index}]")
        key = _scalar_key(value)
        # 紧凑写法允许与显式 incident 重叠，并以显式配置为准。
        if key not in incident_keys:
            append_incident(
                value,
                {},
                f"status_policy.error_values[{index}]",
            )

    conflicts = normal_keys.intersection(incident_keys)
    if conflicts:
        raise ValueError("a status value cannot be both normal and incident")

    normalized: StatusPolicy = {
        "normal_values": normal_values,
        "incidents": incidents,
    }
    unknown = policy.get("unknown_incident")
    if unknown is not None:
        normalized["unknown_incident"] = _incident_config(
            "*",
            unknown,
            defaults=defaults,
            field="status_policy.unknown_incident",
        )

    if not normal_values and not incidents and unknown is None:
        raise ValueError(
            "status_policy must define normal_values, incidents, or unknown_incident"
        )
    return normalized


def evaluate_status(
    policy: Mapping[str, Any] | None,
    value: Any,
) -> StatusEvaluation:
    """按类型精确解析一个状态值，且不修改调用方策略。

    malformed 策略仍由 :func:`normalize_status_policy` 抛错，让注册表生成阶段
    尽早失败。无法表示为 JSON 标量的观测值返回中性结果，不会误触发联锁。
    """

    normalized = normalize_status_policy(policy)
    if normalized is None:
        return StatusEvaluation(healthy=None)
    try:
        observed = _scalar(value, "observed status value")
    except (TypeError, ValueError):
        return StatusEvaluation(healthy=None)

    observed_key = _scalar_key(observed)
    for incident in normalized["incidents"]:
        if observed_key == _scalar_key(incident["value"]):
            matched = deepcopy(incident)
            matched["value"] = observed
            return StatusEvaluation(healthy=False, incident=matched)

    normal_values = normalized["normal_values"]
    if any(observed_key == _scalar_key(item) for item in normal_values):
        return StatusEvaluation(healthy=True)
    if "unknown_incident" in normalized:
        incident = deepcopy(normalized["unknown_incident"])
        incident["value"] = observed
        return StatusEvaluation(healthy=False, incident=incident)

    # 仅声明 incident 时，所有未命中值都代表恢复；显式 normal allow-list
    # 存在时，未知值保持中性，避免策略不完整导致误恢复。
    return StatusEvaluation(healthy=True if not normal_values else None)


__all__ = [
    "StatusEvaluation",
    "StatusIncident",
    "StatusPolicy",
    "StatusScalar",
    "StatusSeverity",
    "evaluate_status",
    "normalize_status_policy",
]
