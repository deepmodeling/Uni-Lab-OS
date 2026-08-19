"""面向设备代码的物料创建 helper。"""

from __future__ import annotations

from typing import Any, Sequence
from uuid import uuid4

from unilabos.server.adapters.plr_materials import (
    CreatedPLRMaterials,
    MaterialGateway,
    create_plr_materials,
)
from unilabos.server.protocol.common import InventoryMutation


def _default_gateway() -> MaterialGateway:
    """按进程角色选择链路；Slave 永远经 HostLink，不直连 HTTP。"""

    from unilabos.config.config import BasicConfig, HTTPConfig

    if not BasicConfig.is_host_mode:
        from unilabos.hostlink.client import get_hostlink_client
        from unilabos.server.clients.materials import HostLinkMaterialsClient

        client = get_hostlink_client()
        if client is None:
            raise RuntimeError("Slave 尚未连接 HostLink，无法创建物料")
        return HostLinkMaterialsClient(client)

    from unilabos.server.scheduler.integration import get_materials_gateway

    gateway = get_materials_gateway()
    if gateway is not None:
        return gateway

    if HTTPConfig.material_microbackend_addr:
        from unilabos.server.clients.materials import HTTPMaterialsClient

        return HTTPMaterialsClient(HTTPConfig.material_microbackend_addr)

    from unilabos.server.clients.materials import LocalMaterialsClient
    from unilabos.server.composition import get_server_services

    services = get_server_services()
    if services is None:
        raise RuntimeError("Host 尚未配置 materials authority")
    return LocalMaterialsClient(services.materials)


def create(
    plr_resource: Any | Sequence[Any],
    *,
    mutation: InventoryMutation | None = None,
    gateway: MaterialGateway | None = None,
) -> CreatedPLRMaterials:
    """创建一棵 PLR 物料树并返回带权威 UUID 的新对象，不修改输入。"""

    resources = (
        list(plr_resource)
        if isinstance(plr_resource, (list, tuple))
        else [plr_resource]
    )
    if not resources or resources == [None]:
        raise ValueError("创建物料时至少需要一个 PLR resource")
    command_uuid = str(uuid4())
    request = mutation or InventoryMutation(
        command_uuid=command_uuid,
        effect_key="create_material_tree",
        operation="create_material_tree",
    )
    return create_plr_materials(gateway or _default_gateway(), request, resources)


__all__ = ["create"]
