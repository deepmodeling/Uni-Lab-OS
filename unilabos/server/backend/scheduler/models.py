"""Backend Scheduler 的资源锁合同。

展示名和动作参数名不作为锁身份。动作锁使用 ``device_id/action_name``，
物料锁使用 Materials Authority 分配的 ``material_uuid``。
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import Field, model_validator

from unilabos.server.database.tables.base import NonEmptyStr, ServerObject


LockKind = Literal["action", "material"]
LockRequestStatus = Literal["waiting", "held", "released", "canceled"]
LockHandoffStatus = Literal["pending", "completed", "canceled"]


class ActionLockClaim(ServerObject):
    """一个规范动作执行通道；同一 ``device_id/action_name`` 容量为一。"""

    device_id: NonEmptyStr
    action_name: NonEmptyStr


class MaterialLockClaim(ServerObject):
    """一个由 Materials Authority 分配身份的物料实例。"""

    material_uuid: NonEmptyStr


class ResourceLockIdentifier(ServerObject):
    """资源管理器内部和快照使用的规范锁身份。"""

    kind: LockKind
    device_id: Optional[NonEmptyStr] = None
    action_name: Optional[NonEmptyStr] = None
    material_uuid: Optional[NonEmptyStr] = None

    @model_validator(mode="after")
    def _validate_shape(self) -> "ResourceLockIdentifier":
        if self.kind == "action":
            if self.device_id is None or self.action_name is None:
                raise ValueError("action lock requires device_id and action_name")
            if self.material_uuid is not None:
                raise ValueError("action lock cannot carry material_uuid")
        else:
            if self.material_uuid is None:
                raise ValueError("material lock requires material_uuid")
            if self.device_id is not None or self.action_name is not None:
                raise ValueError("material lock cannot carry action fields")
        return self

    @classmethod
    def action(cls, value: ActionLockClaim) -> "ResourceLockIdentifier":
        return cls(
            kind="action",
            device_id=value.device_id,
            action_name=value.action_name,
        )

    @classmethod
    def material(cls, value: MaterialLockClaim) -> "ResourceLockIdentifier":
        return cls(kind="material", material_uuid=value.material_uuid)

    @property
    def canonical_key(self) -> tuple[str, ...]:
        if self.kind == "action":
            return ("action", self.device_id or "", self.action_name or "")
        return ("material", self.material_uuid or "")


class SchedulerResourceRequest(ServerObject):
    """一个 Job 在进入可执行态前登记的完整资源申请。

    ``always_free`` 只移除当前动作的隐式锁；显式 ``action_lock_claims`` 和
    ``material_lock_claims`` 仍然必须获取。
    """

    request_uuid: NonEmptyStr
    owner_uuid: NonEmptyStr
    task_uuid: NonEmptyStr
    current_action: Optional[ActionLockClaim] = None
    always_free: bool = False
    action_lock_claims: list[ActionLockClaim] = Field(default_factory=list)
    material_lock_claims: list[MaterialLockClaim] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_claims(self) -> "SchedulerResourceRequest":
        keys = [item.canonical_key for item in self.identifiers]
        if len(keys) != len(set(keys)):
            raise ValueError("resource request contains duplicate lock claims")
        return self

    @property
    def identifiers(self) -> list[ResourceLockIdentifier]:
        values: list[ResourceLockIdentifier] = []
        if self.current_action is not None and not self.always_free:
            values.append(ResourceLockIdentifier.action(self.current_action))
        values.extend(
            ResourceLockIdentifier.action(item) for item in self.action_lock_claims
        )
        values.extend(
            ResourceLockIdentifier.material(item) for item in self.material_lock_claims
        )
        return values


class MaterialHandleTransferProof(ServerObject):
    """物料锁沿工作流 output/input Handle 转移的冻结证据。"""

    material_uuid: NonEmptyStr
    workflow_edge_uuid: NonEmptyStr
    source_task_uuid: NonEmptyStr
    source_output_handle_uuid: NonEmptyStr
    target_task_uuid: NonEmptyStr
    target_input_handle_uuid: NonEmptyStr

    @model_validator(mode="after")
    def _different_handles(self) -> "MaterialHandleTransferProof":
        if self.source_output_handle_uuid == self.target_input_handle_uuid:
            raise ValueError("material lock handoff requires two distinct Handles")
        return self


class SchedulerLockHandoffRequest(ServerObject):
    """前序 Job 把已持有 claims 原子交给一个已登记的后继 Job。"""

    handoff_uuid: NonEmptyStr
    from_owner_uuid: NonEmptyStr
    to_owner_uuid: NonEmptyStr
    action_lock_claims: list[ActionLockClaim] = Field(default_factory=list)
    material_lock_claims: list[MaterialLockClaim] = Field(default_factory=list)
    material_handle_proofs: list[MaterialHandleTransferProof] = Field(
        default_factory=list
    )
    reason: str = ""

    @model_validator(mode="after")
    def _validate_handoff(self) -> "SchedulerLockHandoffRequest":
        if self.from_owner_uuid == self.to_owner_uuid:
            raise ValueError("lock handoff owners must differ")
        identifiers = self.identifiers
        if not identifiers:
            raise ValueError("lock handoff requires at least one claim")
        keys = [item.canonical_key for item in identifiers]
        if len(keys) != len(set(keys)):
            raise ValueError("lock handoff contains duplicate claims")
        claimed_materials = {item.material_uuid for item in self.material_lock_claims}
        proof_materials = {item.material_uuid for item in self.material_handle_proofs}
        if claimed_materials != proof_materials:
            raise ValueError(
                "every transferred material requires exactly one Handle proof"
            )
        if len(proof_materials) != len(self.material_handle_proofs):
            raise ValueError("material handoff contains duplicate Handle proofs")
        return self

    @property
    def identifiers(self) -> list[ResourceLockIdentifier]:
        return [
            *(ResourceLockIdentifier.action(item) for item in self.action_lock_claims),
            *(
                ResourceLockIdentifier.material(item)
                for item in self.material_lock_claims
            ),
        ]


class SchedulerResourceRequestRecord(ServerObject):
    request_uuid: NonEmptyStr
    owner_uuid: NonEmptyStr
    task_uuid: NonEmptyStr
    identifiers: list[ResourceLockIdentifier]
    status: LockRequestStatus
    release_requested: bool = False
    blockers: list[NonEmptyStr] = Field(default_factory=list)
    created_sequence: int = Field(ge=1)
    updated_sequence: int = Field(ge=1)
    version: int = Field(ge=1)


class SchedulerLockOwnership(ServerObject):
    identifier: ResourceLockIdentifier
    owner_uuid: NonEmptyStr
    acquired_sequence: int = Field(ge=1)
    version: int = Field(ge=1)


class SchedulerLockHandoffRecord(ServerObject):
    handoff_uuid: NonEmptyStr
    from_owner_uuid: NonEmptyStr
    to_owner_uuid: NonEmptyStr
    identifiers: list[ResourceLockIdentifier]
    material_handle_proofs: list[MaterialHandleTransferProof] = Field(
        default_factory=list
    )
    reason: str = ""
    status: LockHandoffStatus
    created_sequence: int = Field(ge=1)
    updated_sequence: int = Field(ge=1)
    version: int = Field(ge=1)


class SchedulerLockEvent(ServerObject):
    sequence: int = Field(ge=1)
    event_type: Literal[
        "lock.requested",
        "lock.acquired",
        "lock.handoff_pending",
        "lock.transferred",
        "lock.handoff_canceled",
        "lock.release_requested",
        "lock.released",
        "lock.owner_canceled",
    ]
    owner_uuid: Optional[NonEmptyStr] = None
    target_owner_uuid: Optional[NonEmptyStr] = None
    handoff_uuid: Optional[NonEmptyStr] = None
    identifiers: list[ResourceLockIdentifier] = Field(default_factory=list)
    reason: str = ""


class SchedulerResourceSnapshot(ServerObject):
    sequence: int = Field(ge=0)
    requests: list[SchedulerResourceRequestRecord] = Field(default_factory=list)
    ownerships: list[SchedulerLockOwnership] = Field(default_factory=list)
    handoffs: list[SchedulerLockHandoffRecord] = Field(default_factory=list)


__all__ = [
    "ActionLockClaim",
    "LockHandoffStatus",
    "LockKind",
    "LockRequestStatus",
    "MaterialHandleTransferProof",
    "MaterialLockClaim",
    "ResourceLockIdentifier",
    "SchedulerLockEvent",
    "SchedulerLockHandoffRecord",
    "SchedulerLockHandoffRequest",
    "SchedulerLockOwnership",
    "SchedulerResourceRequest",
    "SchedulerResourceRequestRecord",
    "SchedulerResourceSnapshot",
]
