"""机械臂物料转运的来源/目标库位与设备完整资源集解析。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from unilabos.app.scheduler.inventory.station_resource import (
    StationResourceError,
    StationResourceInventory,
    TransferResourceRequest,
)
from unilabos.app.scheduler.resource_lock import material_lock_key, site_lock_key
from unilabos.app.scheduler.site_target import ResolvedSiteTarget
from unilabos.workflow.resource_lock_key import device_lock_key


class TransferResourceSetError(ValueError):
    """转运动作完整资源集无法由库存事实证明。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        resources: Sequence[Mapping[str, str]] = (),
    ) -> None:
        """保存库存条件的稳定错误码和中文原因。

        参数：``code`` 是调度器用于等待/失败分类的错误码；``message`` 是展示
        原因；``resources`` 是库存权威给出的实际阻塞资源。返回：无。异常：无；
        调用点必须明确区分临时条件和永久合同错误。
        """

        super().__init__(message)
        self.code = code
        self.message = message
        self.resources = tuple(dict(resource) for resource in resources)


@dataclass(frozen=True, slots=True)
class TransferResourceSet:
    """已由库存权威证明的转运附加锁键与来源位置。"""

    lock_keys: tuple[str, ...]
    source_site_uuid: str
    source_site_name: str
    source_owner_material_uuid: str
    source_device_material_uuid: str
    target_device_material_uuid: str
    gripper_site_uuid: str


def resolve_transfer_resource_set(
    inventory: StationResourceInventory,
    *,
    resource_material_uuid: str,
    target: ResolvedSiteTarget,
    executor_material_uuid: str = "",
    gripper_site_role: str = "",
    require_device_owners: bool = False,
    allow_held_material: bool = False,
) -> TransferResourceSet:
    """解析机械臂转运一次性需要的物料、位置和设备完整资源集。

    参数：``inventory`` 是本站库存权威；``resource_material_uuid`` 是待搬物料；
    ``target`` 是已确认可接收的目标库位；``executor_material_uuid`` 是实际机械臂
    设备物料身份；``gripper_site_role`` 是部署在该机械臂下的夹爪库位角色；
    ``require_device_owners`` 为真时要求来源和目标位置都能向上追溯到设备。返回：
    包含待搬物料、来源/目标位置、来源/目标设备、机械臂和夹爪位置的稳定锁键。
    异常：任一位置、设备祖先或空夹爪事实无法证明时抛
    ``TransferResourceSetError``，不得部分派发。
    """

    try:
        facts = inventory.resolve_transfer_resources(
            TransferResourceRequest(
                resource_material_uuid=resource_material_uuid,
                target_site_uuid=target.uuid,
                target_owner_material_uuid=target.owner_material_uuid,
                executor_material_uuid=executor_material_uuid,
                gripper_site_role=gripper_site_role,
                require_device_owners=require_device_owners,
                allow_held_material=allow_held_material,
            )
        )
    except StationResourceError as error:
        raise TransferResourceSetError(
            error.code,
            error.message,
            resources=error.resources,
        ) from error
    keys = {
        material_lock_key(resource_material_uuid),
        site_lock_key(
            facts.source_owner_material_uuid,
            facts.source_site_uuid,
        ),
        site_lock_key(target.owner_material_uuid, target.uuid),
    }
    for owner_uuid in {
        facts.source_device_material_uuid,
        facts.target_device_material_uuid,
    } - {""}:
        keys.add(device_lock_key(owner_uuid))
    executor_uuid = str(executor_material_uuid or "").strip()
    gripper_role = str(gripper_site_role or "").strip()
    if gripper_role:
        keys.add(device_lock_key(executor_uuid))
        keys.add(site_lock_key(executor_uuid, facts.gripper_site_uuid))
    return TransferResourceSet(
        lock_keys=tuple(sorted(keys)),
        source_site_uuid=facts.source_site_uuid,
        source_site_name=facts.source_site_name,
        source_owner_material_uuid=facts.source_owner_material_uuid,
        source_device_material_uuid=facts.source_device_material_uuid,
        target_device_material_uuid=facts.target_device_material_uuid,
        gripper_site_uuid=facts.gripper_site_uuid,
    )


__all__ = [
    "TransferResourceSet",
    "TransferResourceSetError",
    "resolve_transfer_resource_set",
]
