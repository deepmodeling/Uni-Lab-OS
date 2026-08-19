"""PLR / ResourceTreeSet 与 ``materials.v1`` 的唯一转换边界。"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

from unilabos.resources.objects.resource import ResourceDict
from unilabos.resources.resource_tracker import ResourceTreeSet
from unilabos.server.protocol.common import InventoryMutation, MutationResult
from unilabos.server.protocol.materials import (
    MaterialAggregateRead,
    MaterialDataRead,
    MaterialDataWrite,
    MaterialIdentityRead,
    MaterialIdentityWrite,
    MaterialNodeCreate,
    MaterialPosition,
    MaterialSnapshot,
    MaterialSnapshotDiff,
    MaterialSubstance,
    MaterialTreeCreate,
    MaterialTreeRead,
    SiteCreate,
    SiteRead,
    SiteWrite,
)


SUBSTANCE_METADATA_EXTRA = "unilabos_substance_metadata"


class MaterialGateway(Protocol):
    def create_tree(
        self, mutation: InventoryMutation, value: MaterialTreeCreate
    ) -> MutationResult[MaterialTreeRead]: ...

    def get_material(self, material_uuid: str) -> MaterialAggregateRead: ...

    def get_tree(self, root_material_uuid: str) -> MaterialTreeRead: ...

    def compare_snapshot(self, value: MaterialSnapshot) -> MaterialSnapshotDiff: ...

    def apply_snapshot(
        self, mutation: InventoryMutation, value: MaterialSnapshot
    ) -> MutationResult[MaterialTreeRead]: ...


def _position_from_resource(resource: ResourceDict) -> MaterialPosition:
    pose = resource.pose
    return MaterialPosition(
        size_depth=pose.size.depth,
        size_width=pose.size.width,
        size_height=pose.size.height,
        scale_x=pose.scale.x,
        scale_y=pose.scale.y,
        scale_z=pose.scale.z,
        layout=pose.layout,
        position_x=pose.position.x if pose.position is not None else None,
        position_y=pose.position.y if pose.position is not None else None,
        position_z=pose.position.z if pose.position is not None else None,
        position3d_x=pose.position3d.x,
        position3d_y=pose.position3d.y,
        position3d_z=pose.position3d.z,
        rotation_x=pose.rotation.x,
        rotation_y=pose.rotation.y,
        rotation_z=pose.rotation.z,
        cross_section_type=pose.cross_section_type,
        extra=pose.extra or {},
    )


def _position_to_resource(value: MaterialPosition) -> dict[str, Any]:
    return {
        "size": {
            "depth": value.size_depth,
            "width": value.size_width,
            "height": value.size_height,
        },
        "scale": {"x": value.scale_x, "y": value.scale_y, "z": value.scale_z},
        "layout": value.layout,
        "position": (
            {
                "x": value.position_x,
                "y": value.position_y,
                "z": value.position_z,
            }
            if value.position_x is not None
            else None
        ),
        "position3d": {
            "x": value.position3d_x,
            "y": value.position3d_y,
            "z": value.position3d_z,
        },
        "rotation": {
            "x": value.rotation_x,
            "y": value.rotation_y,
            "z": value.rotation_z,
        },
        "cross_section_type": value.cross_section_type,
        "extra": value.extra or None,
    }


def _substances_from_resource(resource: ResourceDict) -> list[MaterialSubstance]:
    metadata = resource.extra.get(SUBSTANCE_METADATA_EXTRA, [])
    if not isinstance(metadata, list):
        metadata = []
    result: list[MaterialSubstance] = []
    for ordinal, item in enumerate(resource.substances or []):
        details = metadata[ordinal] if ordinal < len(metadata) else {}
        if not isinstance(details, dict):
            details = {}
        unit = item[2]
        result.append(
            MaterialSubstance(
                substance_uuid=details.get("substance_uuid"),
                name=item[0],
                quantity=item[1],
                quantity_unit=unit,
                physical_state=details.get(
                    "physical_state",
                    "solid" if unit.strip().lower() in {"ng", "ug", "mg", "g", "kg"} else "liquid",
                ),
                composition=details.get("composition", []),
                meta_data=details.get("meta_data", {}),
            )
        )
    return result


def _site_from_resource(value: Any) -> SiteWrite:
    return SiteWrite(
        site_uuid=value.uuid,
        schema_version=value.schema_version,
        template_name=value.template_name,
        site_index=value.index,
        label=value.label,
        visible=value.visible,
        occupied_material_uuid=value.occupied_material_uuid,
        pose=value.pose.model_dump(mode="json"),
        allowed_resource_categories=value.allowed_resource_categories,
        parent_link=value.parent_link,
        description=value.description,
        meta_data=value.meta_data,
        extra=value.extra,
    )


def _site_create_from_resource(
    value: Any, client_refs: Mapping[str, str]
) -> SiteCreate:
    occupied_client_ref = None
    if value.occupied_material_uuid is not None:
        occupied_client_ref = client_refs.get(value.occupied_material_uuid)
        if occupied_client_ref is None:
            raise ValueError(
                f"Site {value.label} 引用了创建树外的物料 UUID"
            )
    return SiteCreate(
        schema_version=value.schema_version,
        template_name=value.template_name,
        site_index=value.index,
        label=value.label,
        visible=value.visible,
        occupied_client_ref=occupied_client_ref,
        pose=value.pose.model_dump(mode="json"),
        allowed_resource_categories=value.allowed_resource_categories,
        parent_link=value.parent_link,
        description=value.description,
        meta_data=value.meta_data,
        extra=value.extra,
    )


def resource_tree_to_create(value: ResourceTreeSet) -> MaterialTreeCreate:
    """把一棵内部草稿转为不携带实例 UUID 的创建请求。"""

    if len(value.trees) != 1:
        raise ValueError("one create request must contain exactly one resource tree")
    instances = value.trees[0].get_all_nodes()
    client_refs = {
        instance.res_content.uuid: f"node-{ordinal}"
        for ordinal, instance in enumerate(instances)
    }
    nodes: list[MaterialNodeCreate] = []
    for instance in instances:
        resource = instance.res_content
        extra = copy.deepcopy(resource.extra)
        extra.pop(SUBSTANCE_METADATA_EXTRA, None)
        nodes.append(
            MaterialNodeCreate(
                client_ref=client_refs[resource.uuid],
                parent_client_ref=client_refs.get(resource.uuid_parent),
                identity=MaterialIdentityWrite(
                    resource_id=resource.id,
                    name=resource.name,
                    description=resource.description,
                    resource_type=resource.type,
                    class_name=(
                        resource.klass
                        or str(resource.config.get("type") or "Resource")
                    ),
                    machine_name=resource.machine_name,
                    barcode=resource.barcode,
                    barcode_symbology=resource.barcode_symbology,
                    template_name=resource.template_name,
                    resource_schema=resource.resource_schema,
                    model=resource.model,
                    icon_uri=resource.icon,
                    config=resource.config,
                    extra=extra,
                    meta_data=resource.meta_data,
                ),
                position=_position_from_resource(resource),
                data=MaterialDataWrite(
                    data=resource.data,
                    substances=_substances_from_resource(resource),
                    sites_initialized=resource.sites_initialized,
                    unknown_counter=resource.unknown_counter,
                ),
                sites=[
                    _site_create_from_resource(site, client_refs)
                    for site in (resource.sites or [])
                ],
            )
        )
    return MaterialTreeCreate(nodes=nodes)


def plr_resources_to_create(resources: Sequence[Any]) -> MaterialTreeCreate:
    """从无 UUID 的 PLR 树生成创建请求，不修改调用方对象。"""

    def validate_new(resource: Any) -> None:
        if getattr(resource, "unilabos_uuid", ""):
            raise ValueError(
                f"PLR 资源 {resource.name} 已有 UUID；已有物料应走更新而不是创建"
            )
        resource_sites = getattr(resource, "resource_sites", None)
        if resource_sites:
            raise ValueError(
                f"PLR 资源 {resource.name} 已有 canonical Site UUID；"
                "新物料 Site 应由已登记模板和微后端创建"
            )
        native_sites = getattr(resource, "sites", None)
        if isinstance(native_sites, dict):
            for site in native_sites.values():
                if site is not None and getattr(site, "unilabos_site_uuid", ""):
                    raise ValueError(
                        f"PLR 资源 {resource.name} 已有 Site UUID；"
                        "已有物料应走更新而不是创建"
                    )
        for child in getattr(resource, "children", []) or []:
            validate_new(child)

    for resource in resources:
        validate_new(resource)
    draft_resources = copy.deepcopy(list(resources))
    tree = ResourceTreeSet.from_plr_resources(
        draft_resources, known_random_uuid=True
    )
    return resource_tree_to_create(tree)


def material_tree_to_resource_tree(value: MaterialTreeRead) -> ResourceTreeSet:
    raw: list[dict[str, Any]] = []
    for node in value.nodes:
        material = node.material
        substance_metadata = [
            {
                "substance_uuid": item.substance_uuid,
                "physical_state": item.physical_state,
                "composition": item.composition,
                "meta_data": item.meta_data,
            }
            for item in node.data.substances
        ]
        extra = copy.deepcopy(material.extra)
        if substance_metadata:
            extra[SUBSTANCE_METADATA_EXTRA] = substance_metadata
        raw.append(
            {
                "id": material.resource_id,
                "uuid": material.material_uuid,
                "name": material.name,
                "description": material.description,
                "resource_schema": material.resource_schema,
                "model": material.model,
                "icon": material.icon_uri,
                "parent_uuid": material.parent_material_uuid,
                "parent": None,
                "type": material.resource_type,
                "class": material.class_name,
                "pose": _position_to_resource(node.position),
                "config": material.config,
                "data": node.data.data,
                "extra": extra,
                "meta_data": material.meta_data,
                "machine_name": material.machine_name,
                "barcode": material.barcode,
                "barcode_symbology": material.barcode_symbology,
                "template_name": material.template_name,
                "resource_template_uuid": material.template_uuid,
                "joint_state": None,
                "sites": [
                    {
                        "schema_version": site.schema_version,
                        "uuid": site.site_uuid,
                        "template_name": site.template_name,
                        "material_uuid": site.owner_material_uuid,
                        "index": site.site_index,
                        "label": site.label,
                        "visible": site.visible,
                        "occupied_material_uuid": site.occupied_material_uuid,
                        "pose": site.pose,
                        "allowed_resource_categories": site.allowed_resource_categories,
                        "parent_link": site.parent_link,
                        "description": site.description,
                        "meta_data": site.meta_data,
                        "extra": site.extra,
                    }
                    for site in node.sites
                ],
                "sites_initialized": node.data.sites_initialized,
                "substances": [
                    (item.name, item.quantity, item.quantity_unit)
                    for item in node.data.substances
                ],
                "liquid_history": None,
                "unknown_counter": node.data.unknown_counter,
            }
        )
    return ResourceTreeSet.from_raw_dict_list(raw)


def material_tree_to_plr_resources(value: MaterialTreeRead) -> list[Any]:
    return material_tree_to_resource_tree(value).to_plr_resources()


def resource_tree_to_snapshot(
    value: ResourceTreeSet,
    base: MaterialTreeRead,
    *,
    allow_partial: bool = False,
) -> MaterialSnapshot:
    """用运行时 ResourceTreeSet 覆盖下载基线。

    ``allow_partial`` 用于设备仅上报孔位或子树的场景；未上报的权威节点保持
    不变。无论是否局部更新，运行时都不能夹带权威树之外的 UUID。
    """

    runtime = {item.res_content.uuid: item.res_content for item in value.all_nodes}
    authoritative_uuids = {
        node.material.material_uuid for node in base.nodes
    }
    if not runtime.keys() <= authoritative_uuids:
        raise ValueError("runtime tree contains material UUIDs outside downloaded tree")
    if not allow_partial and runtime.keys() != authoritative_uuids:
        raise ValueError("runtime tree does not match downloaded material UUID set")
    nodes: list[MaterialAggregateRead] = []
    for node in base.nodes:
        resource = runtime.get(node.material.material_uuid)
        if resource is None:
            nodes.append(node.model_copy(deep=True))
            continue
        parent_material_uuid = resource.uuid_parent
        if parent_material_uuid not in authoritative_uuids:
            # 设备/Deck 挂载关系不属于 materials.db 的物料父子关系。
            parent_material_uuid = node.material.parent_material_uuid
        identity = MaterialIdentityRead.model_validate(
            {
                **node.material.model_dump(mode="json"),
                "name": resource.name,
                "description": resource.description,
                "machine_name": resource.machine_name,
                "barcode": resource.barcode,
                "barcode_symbology": resource.barcode_symbology,
                "resource_schema": resource.resource_schema,
                "model": resource.model,
                "icon_uri": resource.icon,
                "config": resource.config,
                "extra": {
                    key: item
                    for key, item in resource.extra.items()
                    if key != SUBSTANCE_METADATA_EXTRA
                },
                "meta_data": resource.meta_data,
                "parent_material_uuid": parent_material_uuid,
            }
        )
        substances = _substances_from_resource(resource)
        data = MaterialDataRead.model_validate(
            {
                **node.data.model_dump(mode="json"),
                "data": resource.data,
                "substances": substances,
                "sites_initialized": resource.sites_initialized,
                "unknown_counter": resource.unknown_counter,
            }
        )
        base_sites = {site.site_uuid: site for site in node.sites}
        sites: list[SiteRead] = []
        for resource_site in resource.sites or []:
            previous = base_sites.get(resource_site.uuid)
            if previous is None:
                raise ValueError(
                    f"runtime contains an unknown Site: {resource_site.uuid}"
                )
            sites.append(
                SiteRead.model_validate(
                    {
                        **previous.model_dump(mode="json"),
                        **_site_from_resource(resource_site).model_dump(
                            mode="json", exclude_none=False
                        ),
                        "site_uuid": resource_site.uuid,
                        "owner_material_uuid": resource_site.material_uuid,
                    }
                )
            )
        nodes.append(
            MaterialAggregateRead(
                material=identity,
                position=_position_from_resource(resource),
                position_version=node.position_version,
                data=data,
                sites=sites,
                state_hash=node.state_hash,
            )
        )
    return MaterialSnapshot(root_material_uuid=base.root_material_uuid, nodes=nodes)


@dataclass(frozen=True)
class CreatedPLRMaterials:
    result: MutationResult[MaterialTreeRead]
    tree: ResourceTreeSet
    resources: list[Any]


def create_plr_materials(
    gateway: MaterialGateway,
    mutation: InventoryMutation,
    resources: Sequence[Any],
) -> CreatedPLRMaterials:
    request = plr_resources_to_create(resources)
    result = gateway.create_tree(mutation, request)
    tree = material_tree_to_resource_tree(result.data)
    return CreatedPLRMaterials(
        result=result,
        tree=tree,
        resources=tree.to_plr_resources(),
    )


__all__ = [
    "CreatedPLRMaterials",
    "MaterialGateway",
    "SUBSTANCE_METADATA_EXTRA",
    "create_plr_materials",
    "material_tree_to_plr_resources",
    "material_tree_to_resource_tree",
    "plr_resources_to_create",
    "resource_tree_to_create",
    "resource_tree_to_snapshot",
]
