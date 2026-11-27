"""把设备图几何规范化为 Backend 物料（Material）相对位置合同。"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any


class InstanceGeometryError(ValueError):
    """设备图几何无法安全转换为物料相对位置。"""


def relative_position_from_graph_node(
    raw_node: Mapping[str, Any],
    config: Mapping[str, Any],
) -> dict[str, float] | None:
    """把设备图节点的坐标、尺寸和旋转映射为 Backend 写入合同。

    Args:
        raw_node: 保留 ``position``、``pose`` 或顶层 ``rotation`` 的设备图节点。
        config: 已规范化的节点配置，提供可选尺寸和旋转。

    Returns:
        至少声明一个几何轴时返回完整相对位置；完全没有几何信息时返回 ``None``。

    Raises:
        InstanceGeometryError: 几何对象结构非法、轴值不是有限实数或尺寸为负时抛出。
    """
    pose_value = raw_node.get("pose")
    if pose_value is not None and not isinstance(pose_value, Mapping):
        raise InstanceGeometryError("device graph node pose must be an object")
    pose = pose_value if isinstance(pose_value, Mapping) else {}

    position_value = raw_node.get("position")
    if position_value is None:
        position_value = pose.get("position")
    if position_value is not None and not isinstance(position_value, Mapping):
        raise InstanceGeometryError("device graph node position must be an object")
    position = position_value if isinstance(position_value, Mapping) else {}

    rotation_value = config.get("rotation")
    if rotation_value is None:
        rotation_value = pose.get("rotation")
    if rotation_value is None:
        rotation_value = raw_node.get("rotation")
    if rotation_value is not None and not isinstance(rotation_value, Mapping):
        raise InstanceGeometryError("device graph node rotation must be an object")
    rotation = rotation_value if isinstance(rotation_value, Mapping) else {}

    position_axes = {
        axis: _optional_finite_axis(position, axis, f"position.{axis}")
        for axis in ("x", "y", "z")
    }
    size_axes = {
        axis: _optional_finite_axis(
            config,
            f"size_{axis}",
            f"config.size_{axis}",
            minimum=0.0,
        )
        for axis in ("x", "y", "z")
    }
    rotation_axes = {
        axis: _optional_finite_axis(rotation, axis, f"rotation.{axis}")
        for axis in ("x", "y", "z")
    }
    if not any(
        value is not None
        for axes in (position_axes, size_axes, rotation_axes)
        for value in axes.values()
    ):
        return None

    return {
        "position_x": _zero_if_missing(position_axes["x"]),
        "position_y": _zero_if_missing(position_axes["y"]),
        "position_z": _zero_if_missing(position_axes["z"]),
        "width": _zero_if_missing(size_axes["x"]),
        "length": _zero_if_missing(size_axes["y"]),
        "depth": _zero_if_missing(size_axes["z"]),
        "scale_x": 1.0,
        "scale_y": 1.0,
        "scale_z": 1.0,
        "rotation_x": _zero_if_missing(rotation_axes["x"]),
        "rotation_y": _zero_if_missing(rotation_axes["y"]),
        "rotation_z": _zero_if_missing(rotation_axes["z"]),
    }


def default_host_relative_position() -> dict[str, float]:
    """返回自动 Host Node 的稳定世界坐标和最小非零包络。

    Returns:
        原点坐标、单位缩放、零旋转和 ``1 × 1 × 1`` 尺寸的独立字典。
    """
    return {
        "position_x": 0.0,
        "position_y": 0.0,
        "position_z": 0.0,
        "width": 1.0,
        "length": 1.0,
        "depth": 1.0,
        "scale_x": 1.0,
        "scale_y": 1.0,
        "scale_z": 1.0,
        "rotation_x": 0.0,
        "rotation_y": 0.0,
        "rotation_z": 0.0,
    }


def _optional_finite_axis(
    source: Mapping[str, Any],
    key: str,
    field_name: str,
    *,
    minimum: float | None = None,
) -> float | None:
    """读取一个可选几何轴并执行有限数和下界校验。

    Args:
        source: 几何轴所在对象。
        key: 待读取的对象键。
        field_name: 错误消息使用的完整字段路径。
        minimum: 可选闭区间下界；位置和旋转不设置下界。

    Returns:
        字段缺失时返回 ``None``，存在且有效时返回浮点值。

    Raises:
        InstanceGeometryError: 字段不是有限实数或小于允许下界时抛出。
    """
    if key not in source:
        return None
    value = source[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InstanceGeometryError(
            f"device graph node {field_name} must be a number"
        )
    number = float(value)
    if not math.isfinite(number):
        raise InstanceGeometryError(
            f"device graph node {field_name} must be finite"
        )
    if minimum is not None and number < minimum:
        raise InstanceGeometryError(
            f"device graph node {field_name} must be at least {minimum:g}"
        )
    return number


def _zero_if_missing(value: float | None) -> float:
    """把缺失几何轴规范化为零，同时保留显式负数与负零。

    Args:
        value: 已完成数值校验的可选几何轴。

    Returns:
        缺失时返回 ``0.0``，否则返回原浮点值。
    """
    return 0.0 if value is None else value
