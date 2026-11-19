"""资源设备的关节运行状态。"""

from __future__ import annotations

from typing import List

from pydantic import Field, model_validator
from typing_extensions import TypedDict

from unilabos.resources.objects.base import ResourceObject


class ResourceJointStateType(TypedDict):
    """与 ``sensor_msgs/msg/JointState`` 数组字段一致的传输形状。"""

    name: List[str]
    position: List[float]
    velocity: List[float]
    effort: List[float]


class ResourceJointState(ResourceObject):
    """设备关节的瞬时状态；不属于资源在父坐标系中的 ``pose``。"""

    name: List[str] = Field(description="Joint names", default_factory=list)
    position: List[float] = Field(
        description="Joint positions in the joint-defined unit", default_factory=list
    )
    velocity: List[float] = Field(
        description="Joint velocities; empty when unavailable", default_factory=list
    )
    effort: List[float] = Field(
        description="Joint efforts; empty when unavailable", default_factory=list
    )

    @model_validator(mode="after")
    def _validate_joint_arrays(self) -> "ResourceJointState":
        if len(set(self.name)) != len(self.name):
            raise ValueError("joint_state.name 不能包含重复关节名")
        for ordinal, joint_name in enumerate(self.name):
            if not joint_name:
                raise ValueError(f"joint_state.name[{ordinal}] 不能为空")
        for field_name in ("position", "velocity", "effort"):
            values = getattr(self, field_name)
            if values and len(values) != len(self.name):
                raise ValueError(
                    f"joint_state.{field_name} 长度必须为 0 或与 joint_state.name 一致"
                )
        return self


__all__ = ["ResourceJointState", "ResourceJointStateType"]
