"""Backend-neutral resource storage and device resource operations."""

from __future__ import annotations

import threading
from typing import Any, Iterable, Protocol

from unilabos.resources.resource_tracker import (
    DeviceNodeResourceTracker,
    ResourceDictInstance,
    ResourceTreeInstance,
    ResourceTreeSet,
)


class ResourceService(Protocol):
    """Operations a backend provides to one device node."""

    async def update_resources(
        self,
        device_id: str,
        device_uuid: str,
        resources: Any,
    ) -> dict[str, str]: ...

    async def get_resources(
        self,
        device_id: str,
        resources_uuid: list[str],
        with_children: bool,
    ) -> ResourceTreeSet: ...


def resources_to_tree_set(
    resources: Any,
    *,
    device_id: str,
    device_uuid: str,
) -> ResourceTreeSet:
    """Normalize PLR resources or a ResourceTreeSet for backend transport."""

    if isinstance(resources, ResourceTreeSet):
        tree_set = ResourceTreeSet.load(resources.dump())
    else:
        normalized = (
            list(resources)
            if isinstance(resources, (list, tuple))
            else [resources]
        )
        if not normalized or normalized == [None]:
            raise ValueError("更新物料时至少需要一个资源")
        tree_set = ResourceTreeSet.from_plr_resources(normalized)

    if device_id != "host_node":
        for root in tree_set.root_nodes:
            if not root.res_content.uuid_parent:
                root.res_content.parent_uuid = device_uuid or device_id
    return tree_set


def apply_uuid_mapping(resources: Any, uuid_mapping: dict[str, str]) -> None:
    """Apply a Host-assigned UUID mapping back to caller-owned PLR objects."""

    if not uuid_mapping or isinstance(resources, ResourceTreeSet):
        return
    DeviceNodeResourceTracker().loop_update_uuid(resources, uuid_mapping)


class ResourceStore:
    """Thread-safe canonical resource tree used by non-ROS backends."""

    def __init__(self, resources: ResourceTreeSet | None = None) -> None:
        if resources is not None and not isinstance(resources, ResourceTreeSet):
            raise TypeError("resources 必须是 ResourceTreeSet")
        self._resources = resources if resources is not None else ResourceTreeSet([])
        self._lock = threading.RLock()

    @property
    def resources(self) -> ResourceTreeSet:
        return self._resources

    @staticmethod
    def _find_parent(
        root: ResourceDictInstance,
        target_uuid: str,
    ) -> ResourceDictInstance | None:
        for child in root.children:
            if child.res_content.uuid == target_uuid:
                return root
            parent = ResourceStore._find_parent(child, target_uuid)
            if parent is not None:
                return parent
        return None

    def _detach(self, target_uuid: str) -> None:
        for index, tree in enumerate(tuple(self._resources.trees)):
            if tree.root_node.res_content.uuid == target_uuid:
                self._resources.trees.pop(index)
                return
            parent = self._find_parent(tree.root_node, target_uuid)
            if parent is None:
                continue
            parent.children = [
                child
                for child in parent.children
                if child.res_content.uuid != target_uuid
            ]
            return

    @staticmethod
    def _subtree_uuids(root: ResourceDictInstance) -> set[str]:
        result = {root.res_content.uuid}
        for child in root.children:
            result.update(ResourceStore._subtree_uuids(child))
        return result

    def apply_update(self, update: ResourceTreeSet) -> dict[str, str]:
        """Replace matching subtrees or mount new subtrees by parent UUID."""

        incoming = ResourceTreeSet.load(update.dump())
        uuid_mapping = {
            node.res_content.uuid: node.res_content.uuid
            for node in incoming.all_nodes
        }
        with self._lock:
            for tree in incoming.trees:
                root = tree.root_node
                root_uuid = root.res_content.uuid
                parent_uuid = root.res_content.uuid_parent
                if parent_uuid and parent_uuid in self._subtree_uuids(root):
                    raise ValueError(
                        f"物料 {root_uuid!r} 不能挂载到自己的子节点 "
                        f"{parent_uuid!r}"
                    )

                self._detach(root_uuid)
                parent = (
                    self._resources.find_by_uuid(parent_uuid)
                    if parent_uuid
                    else None
                )
                if parent is not None:
                    root.res_content.parent = parent.res_content
                    root.res_content.parent_uuid = parent.res_content.uuid
                    parent.children.append(root)
                else:
                    root.res_content.parent = None
                    self._resources.trees.append(ResourceTreeInstance(root))
        return uuid_mapping

    @staticmethod
    def _serialize_subtree(
        root: ResourceDictInstance,
        *,
        with_children: bool,
    ) -> list[dict[str, Any]]:
        result = [root.res_content.model_dump(by_alias=True)]
        if with_children:
            for child in root.children:
                result.extend(
                    ResourceStore._serialize_subtree(
                        child,
                        with_children=True,
                    )
                )
        return result

    def get_resources(
        self,
        resources_uuid: Iterable[str],
        *,
        with_children: bool = True,
    ) -> ResourceTreeSet:
        """Return independent copies of requested resource subtrees."""

        trees: list[ResourceTreeInstance] = []
        with self._lock:
            for resource_uuid in resources_uuid:
                node = self._resources.find_by_uuid(str(resource_uuid))
                if node is None:
                    continue
                raw_nodes = self._serialize_subtree(
                    node,
                    with_children=with_children,
                )
                trees.extend(ResourceTreeSet.from_raw_dict_list(raw_nodes).trees)
        return ResourceTreeSet(trees)

    def snapshot(self) -> ResourceTreeSet:
        with self._lock:
            return ResourceTreeSet.load(self._resources.dump())


class LocalResourceService:
    """Connect DeviceNode resource calls directly to a local ResourceStore."""

    def __init__(self, store: ResourceStore) -> None:
        self.store = store

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
        uuid_mapping = self.store.apply_update(tree_set)
        apply_uuid_mapping(resources, uuid_mapping)
        return uuid_mapping

    async def get_resources(
        self,
        device_id: str,
        resources_uuid: list[str],
        with_children: bool,
    ) -> ResourceTreeSet:
        del device_id
        return self.store.get_resources(
            resources_uuid,
            with_children=with_children,
        )


__all__ = [
    "LocalResourceService",
    "ResourceService",
    "ResourceStore",
    "apply_uuid_mapping",
    "resources_to_tree_set",
]
