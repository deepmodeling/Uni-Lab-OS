"""OpenLab 虚拟实验环境预设的稳定 HTTP 模型。"""

from __future__ import annotations

from typing import Literal, Optional
from uuid import UUID

from pydantic import Field

from unilabos.server.models.base import NonEmptyStr, ServerObject


VirtualEnvironmentId = Literal["organic", "biology", "materials"]


class VirtualEnvironmentPresetRead(ServerObject):
    preset_id: VirtualEnvironmentId
    name: NonEmptyStr
    description: NonEmptyStr
    material_count: int = Field(ge=1)
    featured_materials: list[str] = Field(default_factory=list)


class VirtualEnvironmentState(ServerObject):
    preset_id: Optional[VirtualEnvironmentId] = None
    setup_uuid: Optional[NonEmptyStr] = None
    root_material_uuid: Optional[NonEmptyStr] = None
    active_material_count: int = Field(ge=0)
    setup_at_ms: Optional[int] = Field(default=None, ge=0)


class VirtualEnvironmentCatalogRead(ServerObject):
    reset_allowed: bool
    reset_requires_test_mode: bool = True
    current: VirtualEnvironmentState
    presets: list[VirtualEnvironmentPresetRead]


class VirtualEnvironmentResetRequest(ServerObject):
    request_uuid: UUID
    confirmation: Literal["reset-virtual-materials"]


class VirtualEnvironmentResetResult(ServerObject):
    replayed: bool = False
    deleted_root_count: int = Field(ge=0)
    state: VirtualEnvironmentState


__all__ = [
    "VirtualEnvironmentCatalogRead",
    "VirtualEnvironmentId",
    "VirtualEnvironmentPresetRead",
    "VirtualEnvironmentResetRequest",
    "VirtualEnvironmentResetResult",
    "VirtualEnvironmentState",
]
