"""资源与 Site 共用的静态 2D/3D 几何和布局模型。"""

from __future__ import annotations

import copy
from typing import Any, Dict, Literal, Mapping, Optional

from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from unilabos.resources.objects.base import ResourceObject


def _copy_mapping_payload(value: Any) -> Any:
    """把只读/自定义 Mapping 递归转换成可校验的独立容器。"""

    if isinstance(value, Mapping):
        return {key: _copy_mapping_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy_mapping_payload(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_copy_mapping_payload(item) for item in value)
    return copy.deepcopy(value)


def normalize_site_pose_payload(value: Mapping[str, Any]) -> Dict[str, Any]:
    """把旧 Site 几何字段收敛到与物料共用的 ``pose`` 模型。"""

    site = _copy_mapping_payload(value)
    raw_pose = site.pop("pose", None)
    legacy_geometry_keys = {
        "position",
        "position3d",
        "rotation",
        "scale",
        "size",
        "position_x",
        "position_y",
        "position_z",
        "position3d_x",
        "position3d_y",
        "position3d_z",
        "rotation_x",
        "rotation_y",
        "rotation_z",
        "scale_x",
        "scale_y",
        "scale_z",
        "width",
        "length",
        "depth",
        "layout",
        "cross_section_type",
    }
    if isinstance(raw_pose, ResourceDictPosition) and legacy_geometry_keys.isdisjoint(
        site
    ):
        site["pose"] = raw_pose
        return site
    if isinstance(raw_pose, ResourceDictPosition):
        pose: Dict[str, Any] = raw_pose.model_dump()
    elif raw_pose is None:
        pose = {}
    elif isinstance(raw_pose, dict):
        pose = copy.deepcopy(raw_pose)
    else:
        raise ValueError("Site.pose 必须是对象")

    def merge_object(
        target: str, values: Dict[str, Any], mapping: Dict[str, str]
    ) -> None:
        current = pose.get(target)
        if isinstance(current, BaseModel):
            current = current.model_dump()
        if current is None:
            current = {}
        if not isinstance(current, dict):
            raise ValueError(f"Site.pose.{target} 必须是对象")
        current = copy.deepcopy(current)
        for source_key, target_key in mapping.items():
            if source_key not in values:
                continue
            incoming = values[source_key]
            if target_key in current and float(current[target_key]) != float(incoming):
                raise ValueError(f"Site.pose.{target}.{target_key} 与旧几何字段冲突")
            current.setdefault(target_key, incoming)
        pose[target] = current

    for source_key, target_key in (
        ("position", "position"),
        ("position3d", "position3d"),
        ("rotation", "rotation"),
        ("scale", "scale"),
    ):
        nested = site.pop(source_key, None)
        if nested is None:
            continue
        if not isinstance(nested, dict):
            raise ValueError(f"Site.{source_key} 必须是对象")
        merge_object(target_key, nested, {"x": "x", "y": "y", "z": "z"})

    size = site.pop("size", None)
    if size is not None:
        if not isinstance(size, dict):
            raise ValueError("Site.size 必须是对象")
        if (
            "height" in size
            and "length" in size
            and float(size["height"]) != float(size["length"])
        ):
            raise ValueError("Site.size.height 与 Site.size.length 冲突")
        merge_object(
            "size",
            size,
            {
                "width": "width",
                "height": "height",
                "length": "height",
                "depth": "depth",
            },
        )

    flat_groups = {
        "position": {"position_x": "x", "position_y": "y", "position_z": "z"},
        "position3d": {
            "position3d_x": "x",
            "position3d_y": "y",
            "position3d_z": "z",
        },
        "rotation": {"rotation_x": "x", "rotation_y": "y", "rotation_z": "z"},
        "scale": {"scale_x": "x", "scale_y": "y", "scale_z": "z"},
        "size": {"width": "width", "length": "height", "depth": "depth"},
    }
    for target, mapping in flat_groups.items():
        values = {key: site.pop(key) for key in mapping if key in site}
        if values:
            merge_object(target, values, mapping)
            # 扁平兼容字段可为缺项补零；嵌套对象必须提供完整坐标或尺寸。
            required_keys = (
                ("width", "height", "depth") if target == "size" else ("x", "y", "z")
            )
            for key in required_keys:
                pose[target].setdefault(key, 0.0)

    for field_name in ("layout", "cross_section_type"):
        if field_name not in site:
            continue
        incoming = site.pop(field_name)
        if field_name in pose and pose[field_name] != incoming:
            raise ValueError(f"Site.pose.{field_name} 与旧字段 {field_name} 冲突")
        pose.setdefault(field_name, incoming)

    if "position" in pose and "position3d" not in pose:
        pose["position3d"] = copy.deepcopy(pose["position"])
    elif "position3d" in pose and "position" not in pose:
        pose["position"] = copy.deepcopy(pose["position3d"])

    if pose:
        site["pose"] = pose
    return site


class ResourceDictPositionSizeType(TypedDict):
    depth: float
    width: float
    height: float


class ResourceDictPositionSize(ResourceObject):
    depth: float = Field(description="Depth", ge=0.0)  # z
    width: float = Field(description="Width", ge=0.0)  # x
    height: float = Field(description="Height", ge=0.0)  # y


class ResourceDictPositionScaleType(TypedDict):
    x: float
    y: float
    z: float


class ResourceDictPositionScale(ResourceObject):
    x: float = Field(description="x scale")
    y: float = Field(description="y scale")
    z: float = Field(description="z scale")


class ResourceDictPositionObjectType(TypedDict):
    x: float
    y: float
    z: float


class ResourceDictPositionObject(ResourceObject):
    x: float = Field(description="X coordinate")
    y: float = Field(description="Y coordinate")
    z: float = Field(description="Z coordinate")


def _zero_size() -> ResourceDictPositionSize:
    return ResourceDictPositionSize(depth=0.0, width=0.0, height=0.0)


def _zero_scale() -> ResourceDictPositionScale:
    return ResourceDictPositionScale(x=0.0, y=0.0, z=0.0)


def _zero_vector() -> ResourceDictPositionObject:
    return ResourceDictPositionObject(x=0.0, y=0.0, z=0.0)


class ResourceDictPositionType(TypedDict, total=False):
    """序列化/兼容输入形状；嵌套向量一旦出现就必须完整。"""

    size: ResourceDictPositionSizeType
    scale: ResourceDictPositionScaleType
    layout: Literal["2d", "x-y", "z-y", "x-z"]
    position: ResourceDictPositionObjectType
    position3d: ResourceDictPositionObjectType
    rotation: ResourceDictPositionObjectType
    cross_section_type: Literal["rectangle", "circle", "rounded_rectangle"]
    extra: Optional[Dict[str, Any]]


class ResourceDictPosition(ResourceObject):
    size: ResourceDictPositionSize = Field(
        description="Resource size", default_factory=_zero_size
    )
    scale: ResourceDictPositionScale = Field(
        description="Resource scale", default_factory=_zero_scale
    )
    layout: Literal["2d", "x-y", "z-y", "x-z"] = Field(
        description="Resource layout", default="x-y"
    )
    position: Optional[ResourceDictPositionObject] = Field(
        description="Actual position relative to the parent resource",
        default=None,
    )
    position3d: ResourceDictPositionObject = Field(
        description="3D layout/visualization position; not a second runtime position",
        default_factory=_zero_vector,
    )
    rotation: ResourceDictPositionObject = Field(
        description="Resource rotation", default_factory=_zero_vector
    )
    cross_section_type: Literal["rectangle", "circle", "rounded_rectangle"] = Field(
        description="Cross section type", default="rectangle"
    )
    extra: Optional[Dict[str, Any]] = Field(description="Extra data", default=None)


__all__ = [
    "ResourceDictPosition",
    "ResourceDictPositionObject",
    "ResourceDictPositionObjectType",
    "ResourceDictPositionScale",
    "ResourceDictPositionScaleType",
    "ResourceDictPositionSize",
    "ResourceDictPositionSizeType",
    "ResourceDictPositionType",
    "normalize_site_pose_payload",
]
