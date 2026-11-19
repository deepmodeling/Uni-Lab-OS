"""注册表与物料树向旧云端 Backend 的同步。

旧 Backend 自己维护一份实验室物料镜像：Edge 启动时全量上报（``POST
/edge/material``），运行期每次物料变更再增量推送（``PUT /edge/material`` /
``discard``）。微后端物料权威已经是唯一真源，这里只做「把权威的变化镜像给旧
Backend」：

- :func:`upload_registry_snapshot`  开机注册表上报（设备与物料模板分两批）；
- :class:`LegacyMaterialMirror`    开机全量上报 + 消费 ``inventory_ledger`` 的
  增量同步线程；旧后端的 uuid 与微后端保持一致（旧后端原样回显 ``cloud_uuid``）。
"""

from __future__ import annotations

import copy
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set

from unilabos.server.backend.legacy_adaptor.legacy.http import (
    LegacyBackendHTTPClient,
    LegacyBackendHTTPError,
)
from unilabos.utils.log import get_comm_logger

logger = get_comm_logger()

# 旧后端 Go 结构体不认识的新契约字段：上报时剥离，避免 ``parse parameter error``。
_LEGACY_UNSUPPORTED_ROOT_FIELDS = frozenset(
    {
        "display_name",
        "meta_data",
        "template_name",
        "resource_template_uuid",
        "joint_state",
        "sites",
        "sites_initialized",
        "substances",
        "liquid_history",
        "unknown_counter",
    }
)


@dataclass(frozen=True)
class RegistryUploadReport:
    device_count: int
    resource_count: int
    device_skipped: bool = False
    resource_skipped: bool = False
    #: 已上报给旧后端的模板 id（设备 + 物料），供物料镜像判断缺失模板。
    template_ids: frozenset = frozenset()


def _legacy_registry_entry(entry: Mapping[str, Any]) -> Dict[str, Any]:
    """把当前注册表条目投影成旧后端可解析的形状（``displayname`` 等旧字段名）。"""

    item = copy.deepcopy(dict(entry))
    display_name = item.pop("display_name", None)
    if display_name and not item.get("displayname"):
        item["displayname"] = display_name
    item.pop("available_sites", None)
    klass = item.get("class")
    if isinstance(klass, dict):
        klass.pop("supported_backends", None)
        klass.pop("status_policies", None)
        mappings = klass.get("action_value_mappings")
        if isinstance(mappings, dict):
            for action in mappings.values():
                if isinstance(action, dict):
                    action.pop("error_policy", None)
                    action_display = action.pop("display_name", None)
                    if action_display and not action.get("displayname"):
                        action["displayname"] = action_display
    return item


def upload_registry_snapshot(
    registry: Any,
    client: LegacyBackendHTTPClient,
) -> Optional[RegistryUploadReport]:
    """向旧 Backend 上报设备与物料模板（fail-open：失败只记日志）。"""

    from unilabos.app.register import collect_devices_and_resources

    devices, resources = collect_devices_and_resources(registry)
    report = {"device_skipped": False, "resource_skipped": False}
    for tag, entries in (("device", devices), ("resource", resources)):
        if not entries:
            continue
        started = time.time()
        try:
            data = client.upload_registry(
                [_legacy_registry_entry(item) for item in entries.values()]
            )
        except (LegacyBackendHTTPError, Exception) as exc:  # noqa: BLE001 - fail-open
            logger.error(
                "[LegacyRegistry] %s 注册表上报失败: %s", tag, exc
            )
            return None
        skipped = bool(data.get("skipped"))
        report[f"{tag}_skipped"] = skipped
        logger.info(
            "[LegacyRegistry] %s 注册表%s %d 个 (%.3fs)",
            tag,
            "跳过（内容未变化）" if skipped else "上报成功",
            len(entries),
            time.time() - started,
        )
    return RegistryUploadReport(
        device_count=len(devices),
        resource_count=len(resources),
        device_skipped=report["device_skipped"],
        resource_skipped=report["resource_skipped"],
        template_ids=frozenset(str(key) for key in (*devices, *resources)),
    )


def legacy_material_node(node: Mapping[str, Any]) -> Dict[str, Any]:
    """把 ``ResourceDict.model_dump(by_alias=True)`` 投影成旧后端节点形状。

    旧后端把物料节点存成固定列的 Go 结构：未知根字段会被拒绝，``pose``
    子字段必须齐全，``parent_uuid`` 为空串表示根。
    """

    item = dict(node)
    # 旧后端按 ``class`` 关联已上报的注册表模板（22020 material resource template
    # not exist）；新契约里模板名在 ``template_name``，``class`` 常是 PLR 类名
    # （如 Resource），投影时以模板名为准。
    template_name = str(item.get("template_name") or "").strip()
    if template_name:
        item["class"] = template_name
    for key in _LEGACY_UNSUPPORTED_ROOT_FIELDS:
        item.pop(key, None)
    item.pop("parent", None)
    item.pop("children", None)
    item["parent_uuid"] = item.get("parent_uuid") or ""
    for key in ("description", "icon", "barcode", "barcode_symbology", "machine_name"):
        if item.get(key) is None:
            item[key] = ""
    for key in ("schema", "model", "config", "data", "extra"):
        if not isinstance(item.get(key), dict):
            item[key] = {}
    pose = dict(item.get("pose") or {})
    position = pose.get("position") or {"x": 0.0, "y": 0.0, "z": 0.0}
    pose["position"] = position
    pose.setdefault("position3d", dict(position))
    pose.setdefault("rotation", {"x": 0.0, "y": 0.0, "z": 0.0})
    pose.setdefault("scale", {"x": 1.0, "y": 1.0, "z": 1.0})
    pose.setdefault("size", {"width": 0.0, "height": 0.0, "depth": 0.0})
    pose.setdefault("layout", "x-y")
    pose.setdefault("cross_section_type", "rectangle")
    pose.setdefault("extra", None)
    item["pose"] = pose
    item.setdefault("class", "")
    return item


def legacy_material_edge(edge: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """把 node-link 边投影成旧后端 ``/edge/material/edge`` 的形状。"""

    source_uuid = edge.get("source_uuid")
    target_uuid = edge.get("target_uuid")
    if not source_uuid or not target_uuid:
        return None
    return {
        "source_uuid": str(source_uuid),
        "target_uuid": str(target_uuid),
        "source_handle": str(
            edge.get("sourceHandle") or edge.get("source_handle") or ""
        ),
        "target_handle": str(
            edge.get("targetHandle") or edge.get("target_handle") or ""
        ),
        "type": str(edge.get("type") or ""),
    }


def legacy_template_from_authority(template: Any) -> Dict[str, Any]:
    """把微后端 ``ResourceTemplateRead`` 投影成旧 ``/lab/resource`` 注册表条目。

    设备运行期在微后端登记的模板（如虚拟加热平台的样品模板）不在 Edge
    Registry 里；旧后端按 ``class`` 关联模板，缺失即拒绝物料（22020）。
    """

    definition = dict(getattr(template, "definition", None) or {})
    name = str(template.name)
    return {
        "id": name,
        "displayname": str(getattr(template, "display_name", None) or name),
        "registry_type": str(getattr(template, "resource_type", None) or "resource"),
        "version": str(getattr(template, "template_version", None) or "1.0.0"),
        "description": str(definition.get("description") or ""),
        "icon": str(definition.get("icon") or ""),
        "category": list(getattr(template, "category", None) or []),
        "config_info": list(definition.get("config_info") or []),
        "handles": [
            handle.model_dump(mode="json") if hasattr(handle, "model_dump") else dict(handle)
            for handle in (getattr(template, "handles", None) or [])
        ],
        "init_param_schema": dict(definition.get("init_param_schema") or {}),
        "class": {
            "module": str(getattr(template, "module_name", None) or ""),
            "type": str(definition.get("class_type") or "pylabrobot"),
        },
    }


@dataclass
class LegacyMaterialMirror:
    """把微后端物料权威镜像到旧 Backend。

    - :meth:`upload_full`：开机把当前权威树全量 ``POST``（``first_add``）；
    - :meth:`start` / :meth:`stop`：后台线程消费 ``inventory_ledger``，把
      create / update / delete 聚合成 ``PUT`` / ``discard`` 推给旧后端；
    - 物料引用的模板若不在已上报的注册表里，先把权威模板补上报再推物料。
    """

    client: LegacyBackendHTTPClient
    gateway: Any
    poll_interval: float = 2.0
    known_templates: Set[str] = field(default_factory=set, repr=False)
    _thread: Optional[threading.Thread] = field(default=None, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _cursor: int = 0
    _uploaded_roots: Set[str] = field(default_factory=set, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    # ── 全量 ─────────────────────────────────────────────────

    def _dump_root(self, root_uuid: str) -> List[Dict[str, Any]]:
        from unilabos.resources.adapters.plr_materials import material_tree_to_resource_tree

        tree = material_tree_to_resource_tree(self.gateway.get_tree(root_uuid))
        return [
            legacy_material_node(node.res_content.model_dump(by_alias=True))
            for node in tree.all_nodes
        ]

    def _ensure_templates(self, nodes: Sequence[Mapping[str, Any]]) -> None:
        """把节点引用、但旧后端尚无的模板从权威补上报（幂等、fail-open）。"""

        missing = {
            str(node.get("class") or "")
            for node in nodes
            if node.get("class") and str(node["class"]) not in self.known_templates
        }
        if not missing or not callable(getattr(self.gateway, "list_templates", None)):
            return
        # 旧后端要的是 config_info / init_param_schema 正文，所以这里必须带
        # definition；但只按缺失的 name 逐个取，不为一两个模板拉整个注册表
        entries = []
        try:
            for name in sorted(missing):
                for template in self.gateway.list_templates(
                    name=name, include_definition=True
                ):
                    entries.append(legacy_template_from_authority(template))
        except Exception as exc:  # noqa: BLE001 - 模板列表失败只影响本次补报
            logger.warning("[LegacyMaterials] 读取权威模板失败: %s", exc)
            return
        if not entries:
            return
        try:
            self.client.upload_registry(entries)
        except LegacyBackendHTTPError as exc:
            logger.warning("[LegacyMaterials] 补报 %d 个物料模板失败: %s", len(entries), exc)
            return
        with self._lock:
            self.known_templates.update(entry["id"] for entry in entries)
        logger.info(
            "[LegacyMaterials] 补报权威模板 %d 个: %s",
            len(entries),
            ", ".join(entry["id"] for entry in entries),
        )

    def upload_full(
        self,
        root_uuids: Optional[Iterable[str]] = None,
        *,
        links: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> Dict[str, str]:
        """把给定根树（默认权威全部根）一次性 ``POST`` 给旧后端，并上报拓扑边。"""

        roots = list(root_uuids) if root_uuids is not None else [
            item.material.material_uuid
            for item in self.gateway.list_materials(roots_only=True)
        ]
        nodes: List[Dict[str, Any]] = []
        for root_uuid in roots:
            try:
                nodes.extend(self._dump_root(root_uuid))
            except Exception as exc:  # noqa: BLE001 - 单棵树失败不阻断其它
                logger.warning("[LegacyMaterials] 读取权威根树 %s 失败: %s", root_uuid, exc)
        if not nodes:
            logger.info("[LegacyMaterials] 权威中没有物料，跳过全量上报")
            return {}
        self._ensure_templates(nodes)
        started = time.time()
        mapping = self.client.upload_material_tree(nodes, mount_uuid="", first_add=True)
        drifted = {edge: cloud for edge, cloud in mapping.items() if edge != cloud}
        logger.info(
            "[LegacyMaterials] 全量物料上报 %d 节点 / %d 棵树 (%.3fs)%s",
            len(nodes),
            len(roots),
            time.time() - started,
            f"，旧后端改写了 {len(drifted)} 个 uuid（忽略，权威以微后端为准）" if drifted else "",
        )
        with self._lock:
            self._uploaded_roots.update(roots)
            # 全量之后只跟踪新增账本；启动前的历史不再回放
            self._cursor = self._latest_sequence()
        if links:
            edges = [item for item in (legacy_material_edge(link) for link in links) if item]
            if edges:
                try:
                    self.client.upload_material_edges(edges)
                    logger.info("[LegacyMaterials] 物料拓扑边上报 %d 条", len(edges))
                except LegacyBackendHTTPError as exc:
                    logger.warning("[LegacyMaterials] 物料拓扑边上报失败: %s", exc)
        return mapping

    def _latest_sequence(self) -> int:
        latest = 0
        cursor = 0
        while True:
            rows = self.gateway.changes(after_sequence=cursor, limit=1000)
            if not rows:
                return latest
            cursor = rows[-1].sequence
            latest = cursor

    # ── 增量 ─────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if not callable(getattr(self.gateway, "changes", None)):
            logger.info("[LegacyMaterials] 物料网关不提供账本，跳过增量同步")
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="LegacyMaterialMirror", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)

    def _run(self) -> None:
        while not self._stop.wait(self.poll_interval):
            try:
                self.sync_once()
            except Exception:  # noqa: BLE001 - 同步线程必须存活
                logger.exception("[LegacyMaterials] 增量同步失败，下轮重试")

    def _root_of(self, material_uuid: str) -> Optional[str]:
        seen: Set[str] = set()
        current = material_uuid
        while current and current not in seen:
            seen.add(current)
            try:
                aggregate = self.gateway.get_material(current)
            except Exception:  # noqa: BLE001 - 已删除节点无法回溯
                return None
            parent = aggregate.material.parent_material_uuid
            if not parent:
                return current
            current = str(parent)
        return None

    def sync_once(self) -> int:
        """消费一批账本；返回处理的账本条数。"""

        with self._lock:
            cursor = self._cursor
        rows = self.gateway.changes(after_sequence=cursor, limit=500)
        if not rows:
            return 0
        changed_roots: Set[str] = set()
        deleted: List[str] = []
        for row in rows:
            # Site 占用变化总是伴随同一 mutation 里的 material 行（move/
            # delete），只跟踪 material 聚合即可覆盖整棵根树的重发。
            if row.aggregate_type != "material":
                continue
            # delete 与快照退役被丢弃的枪头（retire）都是软删除：节点已不在任何根下
            if row.operation in {"delete", "retire"}:
                deleted.append(row.aggregate_uuid)
                continue
            root = self._root_of(row.aggregate_uuid)
            if root:
                changed_roots.add(root)
        # 被删除的物料不再属于任何根；只有仍存在的根才 PUT。
        for root in sorted(changed_roots):
            try:
                nodes = self._dump_root(root)
            except Exception as exc:  # noqa: BLE001 - 根在读取前被删
                logger.debug("[LegacyMaterials] 根 %s 读取失败，跳过: %s", root, exc)
                continue
            first_add = root not in self._uploaded_roots
            self._ensure_templates(nodes)
            try:
                self.client.upload_material_tree(nodes, mount_uuid="", first_add=first_add)
            except LegacyBackendHTTPError as exc:
                # 旧后端拒绝（模板缺失、字段不合法…）是该根树的数据问题：记录并
                # 推进游标，避免同一批账本每轮重放、把日志刷满。后续变更会再试。
                logger.warning(
                    "[LegacyMaterials] 根树 %s 镜像被旧后端拒绝，跳过本批: %s", root[:8], exc
                )
                continue
            with self._lock:
                self._uploaded_roots.add(root)
            logger.info(
                "[LegacyMaterials] %s 根树 %s (%d 节点)",
                "新增上报" if first_add else "增量更新",
                root[:8],
                len(nodes),
            )
        if deleted:
            try:
                self.client.discard_bench_materials(deleted)
                logger.info("[LegacyMaterials] 废弃 %d 个物料", len(deleted))
            except LegacyBackendHTTPError as exc:
                logger.warning("[LegacyMaterials] 废弃上报失败（旧后端可能已无该物料）: %s", exc)
        with self._lock:
            self._cursor = rows[-1].sequence
        return len(rows)


__all__ = [
    "LegacyMaterialMirror",
    "RegistryUploadReport",
    "legacy_material_edge",
    "legacy_material_node",
    "legacy_template_from_authority",
    "upload_registry_snapshot",
]
