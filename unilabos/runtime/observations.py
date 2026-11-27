"""设备包与 OS 共用的最小属性合同，不包含设备专有坐标或判据。"""

from __future__ import annotations

from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    model_validator,
)


class StateError(ValueError):
    """可机器判别的状态访问失败。"""

    def __init__(self, reason: str, message: str = "") -> None:
        self.reason = reason
        super().__init__(message or reason)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Selector(Contract):
    by: Literal["id", "name"] = "id"
    value: str = Field(min_length=1)


class Property(Contract):
    semantic: Literal["goal", "setpoint", "observed"]
    dtype: Literal["number", "integer", "boolean", "string", "matrix4"] = "number"
    unit: str
    frame: str
    lower: float | None = None
    upper: float | None = None
    ttl_seconds: float = Field(default=1.0, gt=0)

    @model_validator(mode="after")
    def ordered_limits(self) -> Property:
        if (
            self.lower is not None
            and self.upper is not None
            and self.lower > self.upper
        ):
            raise ValueError("属性范围下限不能大于上限")
        if self.dtype not in ("number", "integer") and (
            self.lower is not None or self.upper is not None
        ):
            raise ValueError("非数值属性不能声明数值范围")
        return self


class Descriptor(Contract):
    schema_version: Literal[1] = 1
    version: str = Field(min_length=1)
    properties: dict[str, Property] = Field(min_length=1)
    # 模型入口仍来自注册表 model；这里只声明逻辑属性到模型关节的映射。
    joints: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def known_bindings(self) -> Descriptor:
        if any(not name for name in self.properties):
            raise ValueError("属性名不能为空")
        if set(self.joints) - set(self.properties):
            raise ValueError("关节绑定引用了未声明属性")
        return self


class Sample(Contract):
    schema_version: Literal[1] = 1
    material_uuid: str = Field(min_length=1)
    generation: str = Field(min_length=1)
    descriptor_version: str = Field(min_length=1)
    group: str = Field(min_length=1)
    epoch: int = Field(ge=1, strict=True)
    runtime_session: str = Field(min_length=1)
    source_session: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    property: str = Field(min_length=1)
    seq: int = Field(ge=0, strict=True)
    sampled_at: float
    clock: str = Field(min_length=1)
    quality: Literal["good", "unknown", "bad", "stale"]
    # 值从哪来：observed = 设备读回；estimated = 孪生按真机命令推演（未读真机传感器）；
    # simulated = 纯仿真模拟端演出。与属性的 semantic（目标/设定/实际）是两回事。
    basis: Literal["observed", "estimated", "simulated"] = "observed"
    value: (
        StrictBool
        | StrictInt
        | StrictFloat
        | StrictStr
        | list[StrictInt | StrictFloat]
        | None
    )
    sample_group: str | None = None
    request_id: str | None = None
    task_id: str | None = None
    job_id: str | None = None

    @model_validator(mode="after")
    def known_value(self) -> Sample:
        if (self.quality == "unknown") != (self.value is None):
            raise ValueError("unknown 必须为空；其他质量必须保留观测值")
        return self
