"""V1 增量定义编辑（单节点 / 单连线 / 批量删除 / 复制）在 dev Workflow Authority 上的实现。

每个操作都读当前完整图、做纯变换（``definition_edit``），再走一次带 revision CAS 的完整图保存，
校验、修订推进与事件与整图 ``PUT /graph`` 完全一致；失败时图保持不变。
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional
from uuid import uuid4

from pydantic import ValidationError

from unilabos.protocol.runtime.workflow import WorkflowEdgeWrite, validate_uuid
from unilabos.server.services.runtime.workflow.definition_edit import (
    WorkflowDefinitionInvalid,
    create_edge,
    create_node,
    definition_graph,
    duplicate_graph,
    duplicate_node,
    patch_node,
    restore_private_subgraphs,
)


class WorkflowDefinitionEditing:
    """混入 ``WorkflowService``：依赖其 ``get_graph`` / ``save_graph`` / 锁与错误类型。"""

    # ── 定位 ──────────────────────────────────────────────────────────

    def _entity_owner(self, table: str, entity_uuid: str) -> tuple[str, str]:
        from unilabos.server.services.runtime.workflow.service import WorkflowError

        try:
            identity = validate_uuid(entity_uuid)
        except (TypeError, ValueError):
            raise WorkflowError("invalid_input") from None
        with self.write_lock:  # type: ignore[attr-defined]
            row = self.connection.execute(  # type: ignore[attr-defined]
                f"SELECT {table}.workflow_uuid FROM {table} JOIN workflow ON workflow.uuid = {table}.workflow_uuid "
                f"WHERE {table}.uuid = ? AND {table}.deleted_at IS NULL AND workflow.deleted_at IS NULL",
                (identity,),
            ).fetchone()
        if row is None:
            raise WorkflowError("not_found")
        return str(row["workflow_uuid"]), identity

    @staticmethod
    def _graph_entity(graph: Mapping[str, Any], collection: str, entity_uuid: str) -> Dict[str, Any]:
        from unilabos.server.services.runtime.workflow.service import WorkflowError

        for entity in graph[collection]:
            if entity["uuid"] == entity_uuid:
                return entity
        raise WorkflowError("not_found")

    def get_workflow_node_owner(self, node_uuid: str) -> str:
        return self._entity_owner("workflow_node", node_uuid)[0]

    def get_workflow_node(self, node_uuid: str) -> Dict[str, Any]:
        workflow_uuid, identity = self._entity_owner("workflow_node", node_uuid)
        return self._graph_entity(self.get_graph(workflow_uuid), "nodes", identity)  # type: ignore[attr-defined]

    def list_workflow_nodes(
        self,
        workflow_uuid: str,
        *,
        page: int = 1,
        page_size: int = 20,
        workflow_node_template_uuid: Optional[str] = None,
        material_uuid: Optional[str] = None,
    ) -> Dict[str, Any]:
        """当前层节点分页（组合调用的私有子图不列出），可按模板与物料筛选。"""

        from unilabos.server.services.runtime.workflow.service import WorkflowError

        graph = definition_graph(self.get_graph(workflow_uuid))  # type: ignore[attr-defined]
        page, page_size = self._normalize_page(page, page_size)  # type: ignore[attr-defined]
        try:
            template_uuid = validate_uuid(workflow_node_template_uuid) if workflow_node_template_uuid else None
            material = validate_uuid(material_uuid) if material_uuid else None
        except ValueError:
            raise WorkflowError("invalid_input") from None
        items = [
            node
            for node in graph["nodes"]
            if (template_uuid is None or node.get("workflow_node_template_uuid") == template_uuid)
            and (material is None or node.get("material_uuid") == material)
        ]
        offset = (page - 1) * page_size
        return {"items": items[offset: offset + page_size], "total": len(items), "page": page, "page_size": page_size}

    # ── 写 ──────────────────────────────────────────────────────────

    def create_workflow_node(self, workflow_uuid: str, *, payload: Mapping[str, Any]) -> Dict[str, Any]:
        """增加节点；模板节点的类型 / 动作名 / 默认参数由模板派生，返回保存后的整图。"""

        from unilabos.server.services.runtime.workflow.service import WorkflowError

        identity = self.get_workflow(workflow_uuid)["uuid"]  # type: ignore[attr-defined]
        with self._authoring_lock(identity):  # type: ignore[attr-defined]
            graph = self.get_graph(identity)  # type: ignore[attr-defined]
            template = None
            template_uuid = payload.get("workflow_node_template_uuid")
            if template_uuid is not None:
                try:
                    template = self.get_node_template_record(validate_uuid(str(template_uuid)))  # type: ignore[attr-defined]
                except ValueError:
                    raise WorkflowError("invalid_input") from None
            try:
                node = create_node(payload=payload, template=template)
            except (TypeError, ValueError, WorkflowDefinitionInvalid) as exc:
                raise WorkflowError("invalid_input", detail=str(exc)) from None
            # 公共写接口不能改写 ``unilab`` 保留元数据；只接受可编辑的 input_bindings，
            # 固定执行设备由服务端按物料身份生成（dev 调度按 target_device_id 解析设备）。
            submitted = (node.get("meta_data") or {}).get("unilab") or {}
            editable: Dict[str, Any] = {}
            if isinstance(submitted, Mapping) and isinstance(submitted.get("input_bindings"), Mapping):
                editable["input_bindings"] = dict(submitted["input_bindings"])
            meta_data = {key: value for key, value in (node.get("meta_data") or {}).items() if key != "unilab"}
            device_id = self._device_id_for_material(node.get("material_uuid"), template)
            if device_id:
                meta_data.setdefault("target_device_id", device_id)
            orders = [
                ((item.get("meta_data") or {}).get("unilab") or {}).get("authoring_source_order")
                for item in graph["nodes"]
            ]
            orders = [value for value in orders if type(value) is int]
            editable["authoring_source_order"] = max(orders) + 1 if orders else len(graph["nodes"])
            meta_data["unilab"] = editable
            node["meta_data"] = meta_data
            saved = self._save_reserved_graph(  # type: ignore[attr-defined]
                identity,
                revision=graph["workflow"]["revision"],
                workflow_meta_data=graph["workflow"]["meta_data"],
                nodes=[*graph["nodes"], node],
                edges=graph["edges"],
            )
            return saved

    def _device_id_for_material(self, material_uuid: Any, template: Optional[Mapping[str, Any]]) -> str:
        """固定执行器：物料 UUID → 设备 ID（物料权威里设备根物料的 resource_id）；解析不到返回空串。"""

        if not material_uuid or template is None:
            return ""
        try:
            from unilabos.server.backend.composition import get_materials_service

            materials = get_materials_service()
            if materials is None:
                return ""
            aggregate = materials.get_material(str(material_uuid))
        except Exception:  # noqa: BLE001 - 解析不到时调度仍按 material_uuid 兜底
            return ""
        material = getattr(aggregate, "material", aggregate)
        return str(getattr(material, "resource_id", "") or "")

    def patch_workflow_node(self, node_uuid: str, *, patch: Mapping[str, Any]) -> Dict[str, Any]:
        from unilabos.server.services.runtime.workflow.service import WorkflowError

        workflow_uuid, identity = self._entity_owner("workflow_node", node_uuid)
        with self._authoring_lock(workflow_uuid):  # type: ignore[attr-defined]
            graph = self.get_graph(workflow_uuid)  # type: ignore[attr-defined]
            current = self._graph_entity(graph, "nodes", identity)
            try:
                updated_node = patch_node(current, patch)
            except (TypeError, ValueError, WorkflowDefinitionInvalid) as exc:
                raise WorkflowError("invalid_input", detail=str(exc)) from None
            nodes = [updated_node if node["uuid"] == identity else node for node in graph["nodes"]]
            updated = self.save_graph(  # type: ignore[attr-defined]
                workflow_uuid, revision=graph["workflow"]["revision"], nodes=nodes, edges=graph["edges"],
            )
            return self._graph_entity(updated, "nodes", identity)

    def duplicate_workflow_node(self, node_uuid: str, *, name: Optional[str]) -> Dict[str, Any]:
        from unilabos.server.services.runtime.workflow.service import WorkflowError

        workflow_uuid, identity = self._entity_owner("workflow_node", node_uuid)
        with self._authoring_lock(workflow_uuid):  # type: ignore[attr-defined]
            graph = self.get_graph(workflow_uuid)  # type: ignore[attr-defined]
            current = self._graph_entity(graph, "nodes", identity)
            try:
                duplicate = duplicate_node(current, name=name)
            except (TypeError, ValueError, WorkflowDefinitionInvalid) as exc:
                raise WorkflowError("invalid_input", detail=str(exc)) from None
            updated = self._save_reserved_graph(  # type: ignore[attr-defined]
                workflow_uuid,
                revision=graph["workflow"]["revision"],
                workflow_meta_data=graph["workflow"]["meta_data"],
                nodes=[*graph["nodes"], duplicate],
                edges=graph["edges"],
            )
            return self._graph_entity(updated, "nodes", duplicate["uuid"])

    def delete_workflow_node(self, node_uuid: str) -> None:
        """删除节点及关联连线；仍有子节点时冲突（3003）。"""

        from unilabos.server.services.runtime.workflow.service import WorkflowConflict

        workflow_uuid, identity = self._entity_owner("workflow_node", node_uuid)
        with self._authoring_lock(workflow_uuid):  # type: ignore[attr-defined]
            graph = self.get_graph(workflow_uuid)  # type: ignore[attr-defined]
            self._graph_entity(graph, "nodes", identity)
            if any(node.get("parent_uuid") == identity for node in graph["nodes"]):
                raise WorkflowConflict("conflict", detail="节点仍有子节点，请先删除子节点")
            self.save_graph(  # type: ignore[attr-defined]
                workflow_uuid,
                revision=graph["workflow"]["revision"],
                nodes=[node for node in graph["nodes"] if node["uuid"] != identity],
                edges=[
                    edge for edge in graph["edges"]
                    if identity not in {edge["source_node_uuid"], edge["target_node_uuid"]}
                ],
            )

    def create_workflow_edge(self, workflow_uuid: str, *, payload: Mapping[str, Any]) -> Dict[str, Any]:
        """增加连线（复用完整图的引用与环路校验）；返回保存后的整图。"""

        from unilabos.server.services.runtime.workflow.service import WorkflowError

        identity = self.get_workflow(workflow_uuid)["uuid"]  # type: ignore[attr-defined]
        with self._authoring_lock(identity):  # type: ignore[attr-defined]
            graph = self.get_graph(identity)  # type: ignore[attr-defined]
            try:
                edge = WorkflowEdgeWrite.model_validate(create_edge(payload))
            except (TypeError, ValueError, ValidationError, WorkflowDefinitionInvalid) as exc:
                raise WorkflowError("invalid_input", detail=str(exc)) from None
            return self.save_graph(  # type: ignore[attr-defined]
                identity, revision=graph["workflow"]["revision"], nodes=graph["nodes"], edges=[*graph["edges"], edge],
            )

    def delete_workflow_edge(self, edge_uuid: str) -> None:
        workflow_uuid, identity = self._entity_owner("workflow_edge", edge_uuid)
        with self._authoring_lock(workflow_uuid):  # type: ignore[attr-defined]
            graph = self.get_graph(workflow_uuid)  # type: ignore[attr-defined]
            self._graph_entity(graph, "edges", identity)
            self.save_graph(  # type: ignore[attr-defined]
                workflow_uuid,
                revision=graph["workflow"]["revision"],
                nodes=graph["nodes"],
                edges=[edge for edge in graph["edges"] if edge["uuid"] != identity],
            )

    def batch_delete_workflow_graph(
        self, workflow_uuid: str, *, node_uuids: List[str], edge_uuids: List[str],
    ) -> Dict[str, Any]:
        """一次原子删除指定节点（连带其连线）与连线。"""

        from unilabos.server.services.runtime.workflow.service import WorkflowConflict, WorkflowError

        identity = self.get_workflow(workflow_uuid)["uuid"]  # type: ignore[attr-defined]
        try:
            node_set = {validate_uuid(value) for value in node_uuids}
            edge_set = {validate_uuid(value) for value in edge_uuids}
        except (TypeError, ValueError):
            raise WorkflowError("invalid_input") from None
        if not node_set and not edge_set:
            raise WorkflowError("invalid_input", detail="至少指定一个节点或连线")
        with self._authoring_lock(identity):  # type: ignore[attr-defined]
            graph = self.get_graph(identity)  # type: ignore[attr-defined]
            if not node_set <= {node["uuid"] for node in graph["nodes"]} or not edge_set <= {
                edge["uuid"] for edge in graph["edges"]
            }:
                raise WorkflowError("invalid_input", detail="节点或连线不属于该工作流")
            if any(node.get("parent_uuid") in node_set and node["uuid"] not in node_set for node in graph["nodes"]):
                raise WorkflowConflict("conflict", detail="被删除的节点仍有子节点")
            return self.save_graph(  # type: ignore[attr-defined]
                identity,
                revision=graph["workflow"]["revision"],
                nodes=[node for node in graph["nodes"] if node["uuid"] not in node_set],
                edges=[
                    edge for edge in graph["edges"]
                    if edge["uuid"] not in edge_set
                    and edge["source_node_uuid"] not in node_set
                    and edge["target_node_uuid"] not in node_set
                ],
            )

    def duplicate_workflow(self, workflow_uuid: str, *, name: Optional[str]) -> Dict[str, Any]:
        """复制工作流与完整图（新身份、修订 1→2）；来源没有节点时拒绝。"""

        from unilabos.server.services.runtime.workflow.service import WorkflowError

        source = self.get_graph(workflow_uuid)  # type: ignore[attr-defined]
        if not source["nodes"]:
            raise WorkflowError("invalid_input", detail="空工作流不能复制")
        copied_name = name.strip() if isinstance(name, str) else f"{source['workflow']['name']} copy"
        if not copied_name:
            raise WorkflowError("invalid_input")
        nodes, edges, _requirements = duplicate_graph(source)
        workflow_type, category = self.operation_categories.workflow_kind(source["workflow"]["uuid"])  # type: ignore[attr-defined]
        record = self.create_workflow(  # type: ignore[attr-defined]
            workflow_uuid=str(uuid4()),
            name=copied_name,
            tags=list(source["workflow"].get("tags", [])),
            description=source["workflow"].get("description"),
            meta_data={key: value for key, value in source["workflow"].get("meta_data", {}).items() if key != "unilab"},
            workflow_type=workflow_type,
            operation_category_uuid=category,
        )
        try:
            return self._save_reserved_graph(  # type: ignore[attr-defined]
                record["uuid"],
                revision=record["revision"],
                workflow_meta_data=dict(source["workflow"].get("meta_data", {})),
                nodes=nodes,
                edges=edges,
            )
        except Exception:
            # 新身份只属于本次请求：复制失败不留下空壳
            self.delete_workflow(record["uuid"])  # type: ignore[attr-defined]
            raise

    def save_definition_graph(
        self,
        workflow_uuid: str,
        *,
        revision: int,
        nodes: List[Dict[str, Any]],
        edges: List[Dict[str, Any]],
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """V1 ``PUT /graph``：客户端只提交本层时恢复组合调用的私有子图后整图保存。"""

        applied = self.get_graph(workflow_uuid)  # type: ignore[attr-defined]
        nodes, edges = restore_private_subgraphs(applied=applied, nodes=nodes, edges=edges)
        return self.save_graph(workflow_uuid, revision=revision, nodes=nodes, edges=edges, **kwargs)  # type: ignore[attr-defined]


__all__ = ["WorkflowDefinitionEditing"]
