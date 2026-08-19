"""Device status policies shared by registry and the Edge micro-backend.

A status policy turns a scalar ``@topic_config`` value into a device incident.
Policies are deliberately declarative and JSON-safe so the same metadata can be
discovered by the AST registry scanner and used at runtime without importing a
driver on the Host.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional


_SCALAR_TYPES = (bool, int, float, str)
_SEVERITIES = {"info", "warning", "error", "critical"}


@dataclass(frozen=True)
class StatusEvaluation:
    """Result of evaluating one scalar property value."""

    healthy: Optional[bool]
    incident: Optional[Dict[str, Any]] = None


def _scalar(value: Any, field: str) -> Any:
    if not isinstance(value, _SCALAR_TYPES):
        raise TypeError(f"{field} must be str/int/float/bool")
    return value


def _incident_config(
    value: Any,
    raw: Any,
    *,
    defaults: Mapping[str, Any],
) -> Dict[str, Any]:
    if isinstance(raw, str):
        raw = {"message": raw}
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise TypeError("status_policy incident config must be a mapping or message string")

    severity = str(raw.get("severity", defaults.get("severity", "error"))).strip().lower()
    if severity not in _SEVERITIES:
        raise ValueError("status_policy severity must be info/warning/error/critical")
    code = str(raw.get("code") or defaults.get("code") or f"status.{value}").strip()
    if not code:
        raise ValueError("status_policy incident code must be non-empty")
    message = str(
        raw.get("message")
        or defaults.get("message")
        or f"device status changed to {value!r}"
    )
    hold = raw.get("hold", defaults.get("hold", severity in {"error", "critical"}))
    if not isinstance(hold, bool):
        raise TypeError("status_policy incident hold must be bool")
    return {
        "value": _scalar(value, "status_policy incident value"),
        "code": code,
        "severity": severity,
        "message": message,
        "hold": hold,
    }


def normalize_status_policy(
    policy: Mapping[str, Any] | None,
) -> Optional[Dict[str, Any]]:
    """Validate and copy a status policy into its canonical JSON-safe form.

    Canonical input uses ``normal_values`` plus an ``incidents`` mapping::

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

    ``error_values`` is accepted as a compact migration form and uses the
    top-level code/severity/message/hold defaults. ``unknown_incident`` may be
    supplied when every value outside ``normal_values`` must open an incident.
    """

    if not policy:
        return None
    if not isinstance(policy, Mapping):
        raise TypeError("status_policy must be a mapping")

    raw_normal = policy.get("normal_values", policy.get("healthy_values", []))
    if not isinstance(raw_normal, (list, tuple)):
        raise TypeError("status_policy.normal_values must be a list")
    normal_values: List[Any] = []
    for value in raw_normal:
        value = _scalar(value, "status_policy normal value")
        if value not in normal_values:
            normal_values.append(value)

    defaults = {
        key: deepcopy(policy[key])
        for key in ("code", "severity", "message", "hold")
        if key in policy
    }
    incidents: List[Dict[str, Any]] = []
    raw_incidents = policy.get("incidents", {})
    if raw_incidents is None:
        raw_incidents = {}
    if isinstance(raw_incidents, Mapping):
        for value, config in raw_incidents.items():
            incidents.append(_incident_config(value, config, defaults=defaults))
    elif isinstance(raw_incidents, list):
        for item in raw_incidents:
            if not isinstance(item, Mapping) or "value" not in item:
                raise ValueError("status_policy.incidents list entries require value")
            incidents.append(
                _incident_config(item["value"], item, defaults=defaults)
            )
    else:
        raise TypeError("status_policy.incidents must be a mapping or list")

    raw_error_values = policy.get("error_values", [])
    if not isinstance(raw_error_values, (list, tuple)):
        raise TypeError("status_policy.error_values must be a list")
    existing_values = [item["value"] for item in incidents]
    for value in raw_error_values:
        value = _scalar(value, "status_policy error value")
        if value not in existing_values:
            incidents.append(_incident_config(value, {}, defaults=defaults))
            existing_values.append(value)

    if any(value in normal_values for value in existing_values):
        raise ValueError("a status value cannot be both normal and incident")

    normalized: Dict[str, Any] = {
        "normal_values": normal_values,
        "incidents": incidents,
    }
    unknown = policy.get("unknown_incident")
    if unknown is not None:
        normalized["unknown_incident"] = _incident_config(
            "*", unknown, defaults=defaults
        )
    if not normal_values and not incidents and unknown is None:
        raise ValueError("status_policy must define normal_values, incidents, or unknown_incident")
    return normalized


def evaluate_status(
    policy: Mapping[str, Any] | None,
    value: Any,
) -> StatusEvaluation:
    """Evaluate a scalar value without mutating the normalized policy."""

    normalized = normalize_status_policy(policy)
    if normalized is None:
        return StatusEvaluation(healthy=None)
    if not isinstance(value, _SCALAR_TYPES):
        return StatusEvaluation(healthy=None)

    for incident in normalized["incidents"]:
        if value == incident["value"]:
            matched = deepcopy(incident)
            matched["value"] = value
            return StatusEvaluation(healthy=False, incident=matched)

    normal_values = normalized["normal_values"]
    if value in normal_values:
        return StatusEvaluation(healthy=True)
    if "unknown_incident" in normalized:
        incident = deepcopy(normalized["unknown_incident"])
        incident["value"] = value
        return StatusEvaluation(healthy=False, incident=incident)
    # With an incident-only policy, all non-matching values are recovery values.
    # With an explicit normal allow-list, unknown values are neutral until the
    # driver publishes a value the policy understands.
    return StatusEvaluation(healthy=True if not normal_values else None)


__all__ = [
    "StatusEvaluation",
    "evaluate_status",
    "normalize_status_policy",
]
