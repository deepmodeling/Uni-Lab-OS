"""OpenLab 加热演示的 provision/reset 协议。"""

from __future__ import annotations

from typing import Literal, Optional
from uuid import UUID

from pydantic import Field

from unilabos.server.database.tables.base import ServerObject


HeatingScenarioId = Literal[
    "single_sequential",
    "parallel_three_site",
    "cross_device_transfer",
]


class HeatingScenarioResetRequest(ServerObject):
    request_uuid: UUID
    source_device_id: str = "virtual-heater"
    target_device_id: str = "virtual-heater-target"


class HeatingScenarioEnvironment(ServerObject):
    scenario_id: HeatingScenarioId
    source_device_id: str
    target_device_id: str
    source_platform_uuid: str
    target_platform_uuid: Optional[str] = None
    transfer_material_uuid: Optional[str] = None
    transfer_target_site_uuid: Optional[str] = None
    assignments: dict[str, Optional[str]] = Field(default_factory=dict)
    material_versions: dict[str, int] = Field(default_factory=dict)


__all__ = [
    "HeatingScenarioEnvironment",
    "HeatingScenarioId",
    "HeatingScenarioResetRequest",
]
