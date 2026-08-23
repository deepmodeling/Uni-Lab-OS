"""Validation helpers for action-level material-lock metadata."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def normalize_material_parameter_names(
    value: Any,
    *,
    action_parameter_names: Iterable[str] | None = None,
    action_name: str = "action",
) -> list[str]:
    """Validate and de-duplicate an ``@action`` material-lock declaration.

    ``materials_need_lock`` names driver method parameters, not ROS goal field
    aliases.  When the action signature is available, reject misspellings at
    declaration/AST-scan time instead of silently dispatching without a lock.
    """

    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise TypeError("materials_need_lock 必须是参数名列表")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("materials_need_lock 的每一项必须是非空参数名")
        name = item.strip()
        if name not in seen:
            result.append(name)
            seen.add(name)

    if action_parameter_names is not None:
        available = {
            str(name)
            for name in action_parameter_names
            if str(name) not in {"self", "cls"}
        }
        unknown = [name for name in result if name not in available]
        if unknown:
            raise ValueError(
                f"{action_name} 的 materials_need_lock 包含非动作入参: "
                f"{', '.join(unknown)}"
            )
    return result


__all__ = ["normalize_material_parameter_names"]
