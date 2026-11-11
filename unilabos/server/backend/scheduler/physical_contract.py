"""将动作资源合同解析为权威物料、完整库位和托管范围。"""

from __future__ import annotations

from typing import Any, Mapping

from unilabos.protocol.material_execution import PhysicalMove, PhysicalAliquot
from unilabos.protocol.runtime.resource_intervals import DeviceTenancyTransition


def resolve_physical_contract(
    schema: Mapping[str, Any] | None, values: dict[str, Any], device_id: str, materials: Any
) -> dict[str, Any]:
    contract = ((schema or {}).get("x-unilabos-action-contract") or {}).get("resource_contract") or {}
    if not any(contract.get(key) for key in ("transfer", "transfer_step", "aliquot")):
        return {}
    if materials is None:
        raise ValueError("物理动作需要材料权威")
    ids, devices, reserved_sites = set(), set(), set()

    def resource(name):
        value = values.get(name)
        if not isinstance(value, dict) or not isinstance(value.get("uuid"), str):
            raise ValueError(f"物理参数 {name} 缺少 ResourceSlot 身份")
        return value["uuid"]

    def aggregate(identity):
        result = materials.get_material(identity)
        ids.add(identity)
        return result

    def owner_device(identity):
        seen = set()
        while identity and identity not in seen:
            seen.add(identity)
            item = aggregate(identity).material
            if item.resource_type == "device":
                devices.add(item.resource_id)
                return item.resource_id
            identity = item.parent_material_uuid
        return None

    def site(owner, selected):
        matches = [
            item
            for item in aggregate(owner).sites
            if item.site_uuid == selected or item.label == selected or str(item.site_index) == str(selected)
        ]
        if len(matches) != 1 or not matches[0].visible:
            raise ValueError("库位选择必须唯一且可见")
        return matches[0]

    def location(identity):
        owner = aggregate(identity).material.parent_material_uuid
        if not owner:
            raise ValueError("待搬运物料没有权威来源容器")
        matches = [item for item in aggregate(owner).sites if item.occupied_material_uuid == identity]
        if len(matches) != 1:
            raise ValueError("待搬运物料没有唯一权威来源库位")
        owner_device(owner)
        return owner, matches[0].site_uuid

    def gripper(owners, role, material):
        matches = [
            (owner, item) for owner in owners for item in aggregate(owner).sites if item.meta_data.get("role") == role
        ]
        if (
            len(matches) != 1
            or not matches[0][1].visible
            or matches[0][1].occupied_material_uuid not in (None, material)
        ):
            raise ValueError("搬运器必须有唯一可用的夹爪库位角色")
        reserved_sites.add(matches[0][1].site_uuid)
        for owner in owners:
            owner_device(owner)
        return matches[0]

    for parameter in contract.get("resource_params", []):
        if parameter["role"] in {"device", "motion", "tool", "material"}:
            value = values.get(parameter["param"])
            identity = (
                materials.get_material_by_resource_id(value).material.material_uuid
                if isinstance(value, str)
                else resource(parameter["param"])
            )
            aggregate(identity)
            owner_device(identity)
    moves, aliquot, tenancy = [], None, None
    transfer = contract.get("transfer")
    if transfer:
        identity = resource(transfer["material_param"])
        source_owner, source_site = location(identity)
        ids.update(item.material.material_uuid for item in materials.get_tree(identity).nodes)
        target_owner = resource(transfer["target_owner_param"])
        target = site(
            target_owner,
            values.get(transfer.get("target_site_uuid_param")) or values.get(transfer.get("target_site_name_param")),
        )
        if transfer.get("source_owner_param"):
            requested_owner = resource(transfer["source_owner_param"])
            requested_site = site(
                requested_owner,
                values.get(transfer.get("source_site_uuid_param"))
                or values.get(transfer.get("source_site_name_param")),
            )
            if (requested_owner, requested_site.site_uuid) != (source_owner, source_site):
                raise ValueError("声明的搬运来源与权威库位不一致")
        executor = materials.get_material_by_resource_id(device_id)
        gripper([executor.material.material_uuid], transfer["gripper_site_role"], identity)
        owner_device(target_owner)
        moves.append(
            PhysicalMove(
                material_uuid=identity,
                source_owner_uuid=source_owner,
                source_site_uuid=source_site,
                target_owner_uuid=target_owner,
                target_site_uuid=target.site_uuid,
            )
        )
    step = contract.get("transfer_step")
    if step:
        if transfer:
            raise ValueError("一次动作不能同时声明 transfer 与 transfer_step")
        identity = resource(step["material_param"])
        source_owner, source_site = location(identity)
        ids.update(item.material.material_uuid for item in materials.get_tree(identity).nodes)
        end_owner = resource(step["owner_param"])
        end_site = site(end_owner, values.get(step["site_param"]))
        carriers = [resource(name) for name in step["carrier_params"]]
        carrier, grip = gripper(carriers, "gripper", identity)
        carrier_device = owner_device(carrier)
        if not carrier_device:
            raise ValueError("分步搬运的夹爪必须归属于设备，才能跨动作托管")
        if step["operation"] == "pick":
            if (source_owner, source_site) != (end_owner, end_site.site_uuid):
                raise ValueError("抓取来源与权威库位不一致")
            target_owner, target_site = carrier, grip.site_uuid
            tenancy = DeviceTenancyTransition(material_uuid=identity, acquire_device_id=carrier_device)
        else:
            if (source_owner, source_site) != (carrier, grip.site_uuid):
                raise ValueError("放置前样品必须仍托管在指定夹爪")
            target_owner, target_site = end_owner, end_site.site_uuid
            tenancy = DeviceTenancyTransition(material_uuid=identity, release_device_id=carrier_device)
        owner_device(end_owner)
        moves.append(
            PhysicalMove(
                material_uuid=identity,
                source_owner_uuid=source_owner,
                source_site_uuid=source_site,
                target_owner_uuid=target_owner,
                target_site_uuid=target_site,
            )
        )
    if contract.get("aliquot"):
        declaration = contract["aliquot"]
        source = resource(declaration["source_material_param"])
        targets = [resource(name) for name in declaration["target_material_params"]]
        aliquot = PhysicalAliquot(source_material_uuid=source, target_material_uuids=targets)
        for identity in [source, *targets]:
            aggregate(identity)
            owner_device(identity)
    return {
        "moves": moves,
        "aliquot": aliquot,
        "material_ids": sorted(ids),
        "device_ids": sorted(devices),
        "reserved_site_uuids": sorted(reserved_sites),
        "device_tenancy": tenancy,
    }
