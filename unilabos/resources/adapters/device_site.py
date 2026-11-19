"""设备装配时的权威状态：注册表 ``available_sites`` ↔ 实例 ``sites``，以及设备持有的物料。

设备持有 Site 以及挂在 Site 上 / 直接挂在设备上的物料，但 Site 与物料的身份（uuid /
父子关系 / 占用）只由物料权威发放。设备装配时按「权威优先」取：

- 权威已有该设备（开机图对齐 ``materials.ensure`` 已落库）→ 直接采用权威的 Site
  快照与下挂物料树（图只是创建入口，运行以权威为准）；
- 权威没有 → 以注册表 ``available_sites`` 定义请权威创建（adopt 图中 uuid，连同
  设备下挂的物料子树一起）再取回；
- 权威不可达（``--slave_no_host`` 离线启动、Host 掉线时动态加设备、微后端未装配）→
  只能核验本地已有的权威快照并沿用图中子节点，Edge 仍不本地补齐。

三种进程都在权威可达之后才装配设备：Host 与 ROS2 Slave 先 ``materials.ensure`` 再建
设备；HostLink Slave 由 ``HostLinkBackend.start(populate=...)`` 先建链再装配。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Mapping, Optional, Sequence
from uuid import uuid4

from unilabos.resources.resource_tracker import (
    ResourceDictInstance,
    ResourceTreeInstance,
    ResourceTreeSet,
)
from unilabos.resources.objects.site import (
    ResourceSite,
    SiteDefinition,
    normalize_available_sites,
    site_definition_extra,
    site_instance_definition,
    validate_instantiated_sites,
)
from unilabos.utils import logger


def _normalized_definitions(
    device_config: ResourceDictInstance,
    registry_entry: Dict[str, Any],
    registry_name: str,
) -> List[Dict[str, Any]]:
    resource = device_config.res_content
    if resource.type != "device":
        raise ValueError(
            f"available_sites 只能应用于设备，{resource.id} 的 type={resource.type!r}"
        )

    definitions = normalize_available_sites(registry_entry.get("available_sites"))
    registry_entry["available_sites"] = definitions

    if resource.template_name != registry_name:
        raise ValueError(
            f"设备 {resource.id} 的 template_name={resource.template_name!r} "
            f"与注册表 {registry_name!r} 冲突"
        )
    return definitions


def _validate_local_snapshot(
    resource: Any, definitions: Sequence[Mapping[str, Any]], registry_name: str
) -> None:
    current_sites = (
        [site.model_dump() for site in resource.sites]
        if resource.sites is not None
        else None
    )
    validate_instantiated_sites(
        definitions,
        owner_uuid=resource.uuid,
        template_name=registry_name,
        current_sites=current_sites,
        sites_initialized=resource.sites_initialized,
    )


def validate_device_sites(
    device_config: ResourceDictInstance,
    registry_entry: Dict[str, Any],
    registry_name: str,
) -> None:
    """只核验设备携带的 Site 快照与 Registry 模板定义一致（不取、不建、不改）。

    Registry ``available_sites`` 不写入实例；Edge 不生成或修改 Site 身份。
    """

    definitions = _normalized_definitions(device_config, registry_entry, registry_name)
    _validate_local_snapshot(device_config.res_content, definitions, registry_name)


def _resolve_optional_gateway() -> Any:
    from unilabos.resources.materials import resolve_materials_gateway

    try:
        gateway = resolve_materials_gateway()
    except RuntimeError:
        # 微后端未装配 / Slave 尚未连上 HostLink：本次装配只能核验本地快照
        return None
    link = getattr(gateway, "client", None)
    if link is not None and getattr(link, "online", True) is False:
        # Slave 链路已建但离线（--slave_no_host、Host 暂不可达）：同样视为权威不可达
        return None
    return gateway


def _fetch_device_tree(gateway: Any, device_uuid: str) -> Any:
    """设备物料及其下挂的全部物料（``MaterialTreeRead``）；权威没有该设备时 None。"""

    try:
        return gateway.get_tree(device_uuid)
    except Exception:  # noqa: BLE001 - 与 materials.ensure 一致：查不到即视为权威尚无该设备
        return None


def _site_create_payload(
    definition: Mapping[str, Any], template_name: str
) -> Dict[str, Any]:
    site = SiteDefinition.model_validate(dict(definition)).model_dump(mode="json")
    return {
        "schema_version": 1,
        "template_name": template_name,
        "site_index": site["index"],
        "label": site["label"],
        "visible": site["visible"],
        "pose": site["pose"],
        "allowed_resource_categories": site["allowed_resource_categories"],
        "parent_link": site["parent_link"],
        "description": site["description"],
        "meta_data": site["meta_data"],
        "extra": site_definition_extra(site),
    }


def registry_device_site_templates() -> Dict[str, List[Dict[str, Any]]]:
    """本进程注册表目录的 ``template_name -> available_sites``；目录不可用时为空。

    调度权威读 Host 上报的生效设备条目，Host 读本地扫描（``registry/catalog.py``）。
    """

    from unilabos.registry.catalog import device_entries

    return {
        device_id: list((entry or {}).get("available_sites") or [])
        for device_id, entry in device_entries().items()
    }


def fill_device_site_creates(
    request: Any,
    device_site_templates: Mapping[str, Sequence[Any]],
) -> int:
    """给创建请求里尚未声明 Site 的设备节点补上注册表槽位定义，返回补齐的节点数。

    权威没有设备模板（注册表只把资源条目同步成模板），空 ``sites`` 会让设备落库
    成 0 个位点且无法再补。这里补的只是定义（label / pose / 分类），uuid 仍由权威
    在创建时发放——这不是 Edge 本地补齐身份。
    """

    from unilabos.protocol.materials import SiteCreate

    filled = 0
    for node in request.nodes:
        if node.sites or str(node.identity.resource_type).lower() != "device":
            continue
        definitions = normalize_available_sites(
            device_site_templates.get(node.identity.template_name)
        )
        if not definitions:
            continue
        node.sites = [
            SiteCreate.model_validate(
                _site_create_payload(item, node.identity.template_name)
            )
            for item in definitions
        ]
        node.data.sites_initialized = True
        filled += 1
    return filled


def _create_device_in_authority(
    gateway: Any,
    device_config: ResourceDictInstance,
    *,
    registry_name: str,
    definitions: Sequence[Mapping[str, Any]],
) -> Any:
    """以图中 uuid 在权威创建设备（连同其下挂物料子树），返回创建后的权威树。

    Site 定义来自注册表 ``available_sites``（本地快照已带 Site 时沿用其定义）；
    uuid 一律由权威发放。按根整棵创建与 ``materials.ensure`` 的对齐语义一致：
    之后 ensure 看到根已存在直接采用权威树，不会漏掉设备下的子物料。
    """

    from unilabos.protocol.materials import ACTOR_DEVICE, InventoryMutation
    from unilabos.resources.adapters.plr_materials import resource_tree_to_create

    resource = device_config.res_content
    request = resource_tree_to_create(
        ResourceTreeSet([ResourceTreeInstance(device_config)]), adopt_uuid=True
    )
    # 本设备按传入的注册表定义补；子树里的其它设备（工作站子设备）按全注册表补
    fill_device_site_creates(
        request, {**registry_device_site_templates(), registry_name: list(definitions)}
    )
    device_node = next(
        node for node in request.nodes if node.material_uuid == resource.uuid
    )
    device_node.data.sites_initialized = True

    command_uuid = str(uuid4())
    created = gateway.create_tree(
        InventoryMutation(
            command_uuid=command_uuid,
            effect_key=f"ensure_device_material:{resource.uuid}:{command_uuid}",
            operation="create_material_tree",
            actor_type=ACTOR_DEVICE,
            actor_uuid=str(resource.uuid),
            observed_at_ms=int(time.time() * 1000),
        ),
        request,
    )
    return created.data


def _adopt_authority_children(device_config: ResourceDictInstance, tree_read: Any) -> None:
    """用权威树里挂在设备下的物料替换图中的非设备子节点。

    Site 上的占用物和直接挂在设备上的台面在权威里都是 ``parent = 设备``，一次
    ``get_tree`` 全部拿到。子设备不在这里处理：它们由图声明、各自装配自己持有的
    物料。图里有、权威该设备下没有的物料（已被移走 / 从未登记）以权威为准不装载。
    """

    from unilabos.resources.adapters.plr_materials import material_tree_to_resource_tree

    resource = device_config.res_content
    tree_set = material_tree_to_resource_tree(tree_read)
    root = next(
        (
            tree.root_node
            for tree in tree_set.trees
            if tree.root_node.res_content.uuid == resource.uuid
        ),
        None,
    )
    if root is None:
        raise ValueError(f"权威返回的物料树不含设备 {resource.id} ({resource.uuid})")

    held = [child for child in root.children if child.res_content.type != "device"]
    held_uuids = {child.res_content.uuid for child in held}
    sub_devices = [
        child for child in device_config.children if child.res_content.type == "device"
    ]
    orphaned = [
        child.res_content
        for child in device_config.children
        if child.res_content.type != "device"
        and child.res_content.uuid not in held_uuids
    ]
    if orphaned:
        logger.warning(
            "[DeviceSite] 设备 %s 图中的物料 %s 不在权威的该设备下（已移走或从未登记），"
            "以权威为准不装载；要按图新增请先经 materials.ensure 对齐",
            resource.id,
            [f"{item.id}({item.uuid})" for item in orphaned],
        )
    for child in held:
        child.res_content.parent = resource
    device_config.children = sub_devices + held


def _definition_of(site: Mapping[str, Any]) -> Dict[str, Any]:
    return site_instance_definition(site)


def _check_authority_sites(
    resource: Any,
    sites: Sequence[Dict[str, Any]],
    definitions: Sequence[Mapping[str, Any]],
) -> None:
    """权威 Site 快照必须与注册表槽位一一对应；定义字段漂移只告警。

    身份（label / index / owner / uuid）不一致是配置错误，必须拒绝：工作流按
    label 或 uuid 指位点。pose / 描述等定义字段以权威首次实例化时的注册表为准，
    之后注册表修改不会自动回写权威，这里只提示，不阻断设备启动。
    """

    for site in sites:
        if site["material_uuid"] != resource.uuid:
            raise ValueError(
                f"权威返回的 Site {site['label']} 归属 {site['material_uuid']!r}，"
                f"与设备 {resource.id} ({resource.uuid}) 不一致"
            )
        if site["template_name"] != resource.template_name:
            raise ValueError(
                f"权威返回的 Site {site['label']} 模板 {site['template_name']!r} "
                f"与设备模板 {resource.template_name!r} 不一致"
            )
        if not site.get("uuid"):
            raise ValueError(f"权威返回的 Site {site['label']} 缺少 uuid")

    by_label = {str(site["label"]).casefold(): site for site in sites}
    by_index = {
        (type(site["index"]).__name__, site["index"]): site for site in sites
    }
    matched: set[int] = set()
    drifted: List[str] = []
    for definition in definitions:
        site = by_label.get(str(definition["label"]).casefold())
        if site is None:
            site = by_index.get(
                (type(definition["index"]).__name__, definition["index"])
            )
        if site is None:
            raise ValueError(
                f"物料权威中设备 {resource.id} 缺少注册表声明的 Site "
                f"{definition['label']}；权威不支持给既有物料补 Site，请删除该设备"
                "物料后重启，让权威按注册表模板重新实例化"
            )
        matched.add(id(site))
        if _definition_of(site) != definition:
            drifted.append(str(definition["label"]))

    unused = [site for site in sites if id(site) not in matched]
    if unused:
        raise ValueError(
            f"物料权威中设备 {resource.id} 存在注册表 available_sites 未声明的 Site: "
            f"{[site['label'] for site in unused]}"
        )
    if drifted:
        logger.warning(
            "[DeviceSite] 设备 %s 的 Site %s 在权威中的定义与注册表 available_sites "
            "不一致（权威保留首次实例化时的定义）；运行以权威为准",
            resource.id,
            drifted,
        )


def apply_device_authority_state(
    device_config: ResourceDictInstance,
    registry_entry: Dict[str, Any],
    registry_name: str,
    *,
    gateway: Any = None,
) -> None:
    """按权威优先为设备装配 Site 快照和它持有的物料。

    权威已有该设备则采用其 Site 与下挂物料树；没有则以注册表 ``available_sites``
    请权威创建（连同图中下挂的物料）再取回；权威不可达时退化为只核验本地快照、
    沿用图中子节点。任何路径下 Edge 都不生成 Site / 物料 uuid。
    """

    resource = device_config.res_content
    definitions = _normalized_definitions(device_config, registry_entry, registry_name)

    if gateway is None:
        gateway = _resolve_optional_gateway()
    if gateway is None:
        _validate_local_snapshot(resource, definitions, registry_name)
        return

    device_uuid = str(resource.uuid or "").strip()
    if not device_uuid:
        raise ValueError(f"设备 {resource.id} 缺少 uuid，无法向物料权威查询 Site")

    tree_read = _fetch_device_tree(gateway, device_uuid)
    created = tree_read is None
    if created:
        tree_read = _create_device_in_authority(
            gateway,
            device_config,
            registry_name=registry_name,
            definitions=definitions,
        )
    aggregate = next(
        node for node in tree_read.nodes if node.material.material_uuid == device_uuid
    )
    if created:
        logger.info(
            "[DeviceSite] 设备 %s 不在物料权威中，已按注册表 available_sites 创建"
            "（%d 个 Site，%d 个下挂物料）",
            resource.id,
            len(aggregate.sites),
            len(tree_read.nodes) - 1,
        )
    elif str(aggregate.material.template_name).casefold() != registry_name.casefold():
        raise ValueError(
            f"物料权威中 uuid={device_uuid} 是模板 "
            f"{aggregate.material.template_name!r} 的物料，不是设备 {resource.id} "
            f"({registry_name!r})"
        )

    from unilabos.resources.adapters.plr_materials import site_read_to_resource_site

    sites = [site_read_to_resource_site(site) for site in aggregate.sites]
    _check_authority_sites(resource, sites, definitions)
    resource.sites = [ResourceSite.model_validate(site) for site in sites]
    resource.sites_initialized = True
    _adopt_authority_children(device_config, tree_read)


def prepare_devices_for_report(
    resources: ResourceTreeSet,
    device_registry: Optional[Mapping[str, Dict[str, Any]]] = None,
) -> int:
    """在设备启动/上报前校验微后端返回的 Site 权威快照。

    该入口不会生成 UUID、复制 available_sites 或修改实例。
    """

    if device_registry is None:
        from unilabos.registry.registry import lab_registry

        device_registry = lab_registry.device_type_registry

    prepared = 0
    for node in resources.all_nodes:
        resource = node.res_content
        if resource.type != "device":
            continue
        registry_name = resource.template_name
        if not isinstance(registry_name, str) or not registry_name:
            raise ValueError(f"设备 {resource.id} 的 template_name 不能为空")
        registry_entry = device_registry.get(registry_name)
        if registry_entry is None:
            raise ValueError(
                f"设备 {resource.id} 的 template_name={registry_name!r} 不在注册表中"
            )
        validate_device_sites(node, registry_entry, registry_name)
        prepared += 1
    return prepared


__all__ = [
    "apply_device_authority_state",
    "fill_device_site_creates",
    "prepare_devices_for_report",
    "registry_device_site_templates",
    "validate_device_sites",
]
