"""动作参数与结果中使用的样品公共类型。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional, TypeAlias

from typing_extensions import TypedDict

if TYPE_CHECKING:
    from pylabrobot.resources import Resource as PLRResource


SampleUUIDsType: TypeAlias = Dict[str, Optional["PLRResource"]]


class LabSample(TypedDict):
    sample_uuid: str
    oss_path: str
    extra: Dict[str, Any]


__all__ = ["LabSample", "SampleUUIDsType"]
