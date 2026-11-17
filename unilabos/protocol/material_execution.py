"""物理动作的冻结范围与实际回执；不以计划数量替代实际量。"""

from __future__ import annotations

from typing import Literal
from pydantic import Field, model_validator
from unilabos.protocol.base import NonEmptyStr, ServerObject


class PhysicalMove(ServerObject):
    material_uuid: NonEmptyStr
    source_owner_uuid: NonEmptyStr | None = None
    source_site_uuid: NonEmptyStr | None = None
    target_owner_uuid: NonEmptyStr
    target_site_uuid: NonEmptyStr


class PhysicalAliquot(ServerObject):
    source_material_uuid: NonEmptyStr
    target_material_uuids: list[NonEmptyStr] = Field(min_length=1)

    @model_validator(mode="after")
    def _targets(self):
        if (
            len(set(self.target_material_uuids)) != len(self.target_material_uuids)
            or self.source_material_uuid in self.target_material_uuids
        ):
            raise ValueError("分装来源与目标必须互异，目标不能重复")
        return self


class MaterialExecutionPrepare(ServerObject):
    job_uuid: NonEmptyStr
    task_uuid: NonEmptyStr
    claim_uuid: NonEmptyStr
    attempt_no: int = Field(strict=True, ge=1)
    parameter_hash: NonEmptyStr
    fences: dict[str, int]
    moves: list[PhysicalMove] = Field(default_factory=list)
    aliquot: PhysicalAliquot | None = None
    inventory_reservation_uuid: NonEmptyStr | None = None
    safety_device_ids: list[NonEmptyStr] = Field(default_factory=list)
    safety_material_uuids: list[NonEmptyStr] = Field(default_factory=list)
    reserved_site_uuids: list[NonEmptyStr] = Field(default_factory=list)

    @model_validator(mode="after")
    def _effects(self):
        if not self.moves and self.aliquot is None and not self.inventory_reservation_uuid:
            raise ValueError("物理结算至少声明搬运、分装或库存预留")
        identities = [item.material_uuid for item in self.moves]
        if len(identities) != len(set(identities)):
            raise ValueError("单次动作不能重复搬运同一物料")
        if any(type(value) is not int or value < 1 for value in self.fences.values()):
            raise ValueError("物理结算栅栏必须是正整数")
        return self


class AliquotReceipt(ServerObject):
    target_material_uuid: NonEmptyStr
    actual_quantity: float = Field(ge=0, allow_inf_nan=False, strict=True)
    quantity_unit: NonEmptyStr


class InventoryConsumption(ServerObject):
    key: NonEmptyStr
    lot_uuid: NonEmptyStr
    actual_quantity: float = Field(ge=0, allow_inf_nan=False, strict=True)
    quantity_unit: NonEmptyStr


class MaterialExecutionReceipt(ServerObject):
    job_uuid: NonEmptyStr
    claim_uuid: NonEmptyStr
    plan_hash: NonEmptyStr
    material_aliquot_receipts: list[AliquotReceipt] = Field(default_factory=list)
    material_transfer_receipts: list[PhysicalMove] = Field(default_factory=list)
    inventory_consumptions: list[InventoryConsumption] | None = None


class MaterialExecutionRelease(ServerObject):
    expected_version: int = Field(strict=True, ge=1)
    reason: NonEmptyStr
    outcome: Literal["not_started", "physically_verified"]
