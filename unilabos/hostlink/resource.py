"""Resource service carried over a HostLink client connection."""

from __future__ import annotations

import asyncio
from typing import Any

from unilabos.device_runtime.resource import (
    apply_uuid_mapping,
    resources_to_tree_set,
)
from unilabos.hostlink.client import HostLinkClient
from unilabos.hostlink.protocol import ActionType
from unilabos.resources.resource_tracker import ResourceTreeSet


class HostLinkResourceService:
    """Forward a Slave driver's resource operations to the Host."""

    def __init__(self, client: HostLinkClient) -> None:
        self.client = client

    async def update_resources(
        self,
        device_id: str,
        device_uuid: str,
        resources: Any,
    ) -> dict[str, str]:
        tree_set = resources_to_tree_set(
            resources,
            device_id=device_id,
            device_uuid=device_uuid,
        )
        response = await asyncio.to_thread(
            self.client.request,
            ActionType.RESOURCE_UPDATE,
            {
                "device_id": device_id,
                "resources": tree_set.dump(),
            },
        )
        raw_mapping = (response or {}).get("uuid_mapping")
        uuid_mapping = (
            {str(key): str(value) for key, value in raw_mapping.items()}
            if isinstance(raw_mapping, dict)
            else {}
        )
        apply_uuid_mapping(resources, uuid_mapping)
        return uuid_mapping

    async def get_resources(
        self,
        device_id: str,
        resources_uuid: list[str],
        with_children: bool,
    ) -> ResourceTreeSet:
        response = await asyncio.to_thread(
            self.client.request,
            ActionType.RESOURCE_GET,
            {
                "device_id": device_id,
                "resources_uuid": list(resources_uuid),
                "with_children": bool(with_children),
            },
        )
        raw_resources = (response or {}).get("resources")
        if not isinstance(raw_resources, list):
            raise TypeError("HostLink Host 返回了无效的物料树")
        return ResourceTreeSet.load(raw_resources)


__all__ = ["HostLinkResourceService"]
