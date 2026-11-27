"""用户导入与 API 编辑同步到领域包 Python 权威的端到端测试。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest
import yaml
from fastapi.testclient import TestClient

from tests.workflow.test_authoring_engine import (
    PREPARE_NODE_UUID,
    WORKFLOW_UUID,
    _engine,
    _handle,
    _source,
    _template,
)
from tests.workflow.test_repeat_until_authoring import (
    _repeat_engine,
    _repeat_source,
)
from tests.workflow.test_structured_condition_authoring import (
    _condition_engine,
    _condition_source,
)
from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow import domain_source_target, publication_catalog, source_publication
from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.authoring_kernel import AuthoringCatalogSnapshot
from unilabos.workflow.domain_source_target import DomainWorkflowSourceTarget
from unilabos.workflow.execution_plan import ExecutionPlanBuilder
from unilabos.workflow.service import WorkflowConflict, WorkflowError, WorkflowService
from unilabos.workflow.source_discovery import discover_editable_sources
from unilabos.workflow.store import WorkflowStore
from unilabos.workflow.workflow_spec_compiler import WorkflowSpecCompiler


def _empty_domain_package(selected_root: Path) -> Path:
    """建立只有包身份、尚无工作流的合法领域包。"""

    package_root = selected_root / "demo_domain"
    package_root.mkdir(parents=True)
    selected_root.joinpath("package.yaml").write_text(
        "package:\n  name: demo_domain\nworkflows: []\n",
        encoding="utf-8",
    )
    return package_root


def _service(
    *,
    database_path: Path,
    selected_root: Path,
    engine: WorkflowAuthoringEngine | None = None,
) -> tuple[WorkflowService, WorkflowStore, WorkflowStore]:
    """按生产启动顺序装配一套隔离 Local 工作流服务。

    参数：``database_path`` 是仅保存 Task/Job 等运行事实的 SQLite 文件；
    ``selected_root`` 是含 ``package.yaml`` 的授权领域包入口；``engine`` 是可选
    AST 编译器，省略时使用测试目录。返回：服务、运行事实库和进程内定义目录。
    异常：发现、Python 激活或发布合同恢复失败时原样传播，且不会把工作流定义
    写入运行 SQLite。启动顺序固定为“发现并激活 Python 定义→恢复发布合同”。
    """

    engine = engine or _engine()
    plan = discover_editable_sources((selected_root,))
    runtime_store = WorkflowStore(
        database_path,
        persist_workflow_definitions=False,
    )
    definition_store = WorkflowStore(":memory:")
    service = WorkflowService(
        runtime_store,
        definition_store=definition_store,
        compiler=engine,
        source_target=DomainWorkflowSourceTarget.from_discovery_plan(plan),
    )
    service.replace_discovered_source_authorizations(plan)
    if plan.registrations:
        service.activate_registered_sources_to_fixed_point()
    service.restore_published_workflow_contracts()
    return service, runtime_store, definition_store


def _project_compiled_catalog(definitions: WorkflowStore, compiled) -> None:
    """把编译产物中的节点/连接点模板写入进程内定义目录。"""

    assert compiled.graph is not None
    with definitions.transaction() as connection:
        definitions._ensure_authoring_catalog_projection(
            connection,
            node_templates=compiled.graph["node_templates"],
            handle_templates=compiled.graph["handle_templates"],
            authority_id=compiled.template_catalog_fingerprint,
            now="2026-08-28T00:00:00+00:00",
        )


def _legacy_graph_payload(compiled, *, name: str) -> dict[str, Any]:
    """把编译图转成测试导出 JSON：保留 type、连线和创作元数据。"""

    assert compiled.graph is not None
    workflow = compiled.graph["workflow"]
    return {
        "workflow_name": name,
        "tags": list(workflow.get("tags") or []),
        "description": workflow.get("description"),
        "meta_data": workflow.get("meta_data") or {},
        "workflow_type": workflow.get("workflow_type") or "normal",
        "nodes": list(compiled.graph["nodes"]),
        "edges": list(compiled.graph["edges"]),
    }


_CONTROL_FLOW_IMPORT_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "control_flow_import_payload.json"
)
_IMPORTED_DEVICE_UUID = "51000000-0000-4000-8000-000000000091"
_ACTION_CONTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "goal": {"type": "object", "additionalProperties": True},
    },
    "required": ["goal"],
}


def _imported_graph_can_run(imported: Mapping[str, Any]) -> dict[str, Any]:
    """导入后的应用图必须能建成执行计划、编成调度规格并提交本地调度器。

    参数：``imported`` 是 JSON 导入返回的完整图。返回：调度器首次提交摘要，
    便于线性图继续把已派发作业标完成。异常/断言：缺固定执行器、动作合同或
    控制区域不完整时，执行计划、规格编译或提交失败。
    """

    plan, jobs = ExecutionPlanBuilder().build(
        imported,
        run_mode="normal",
        target_node_uuid=None,
    )
    spec = WorkflowSpecCompiler().compile(
        {
            "uuid": "61000000-0000-4000-8000-000000000091",
            "workflow_uuid": imported["workflow"]["uuid"],
            "workflow_snapshot": imported,
            "execution_plan": plan,
        },
        jobs,
    )
    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    submitted = scheduler.submit_workflow(spec)
    assert submitted["state"]
    return submitted


class _RecordingAuthoringEngine:
    """记录 JSON 导入是否先尝试保留 ``workflow()`` 再退回内联。"""

    def __init__(self, inner: WorkflowAuthoringEngine) -> None:
        self._inner = inner
        self.inline_flags: list[bool] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def generate_python(self, **kwargs: Any) -> Any:
        self.inline_flags.append(bool(kwargs.get("inline_expanded_composites")))
        return self._inner.generate_python(**kwargs)


def _control_flow_import_catalog(
    payload: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按导出 JSON 的模板 UUID 和连线 Handle 构造最小可导入目录。

    参数：``payload`` 是含条件、循环和组合调用的完整导出图。返回：节点模板与
    连接点模板，供导入时按模板派生 type 并校验 Handle。异常：节点缺少模板
    UUID 时由断言暴露。
    """

    nodes = payload["nodes"]
    template_by_node = {
        str(node["uuid"]): str(node["workflow_node_template_uuid"])
        for node in nodes
    }
    specs: dict[str, tuple[str, str, str, str]] = {}
    for node in nodes:
        template_uuid = str(node["workflow_node_template_uuid"])
        node_type = str(node["type"])
        action_name = str(node.get("action_name") or node["name"])
        if node_type == "ILab":
            specs[template_uuid] = (
                action_name,
                "device_action",
                "lab.devices:Reactor",
                "UniLabJsonCommand",
            )
        elif node_type == "workflow":
            symbol = f"operation_{template_uuid.replace('-', '')[:8]}"
            specs[template_uuid] = (
                symbol,
                "workflow",
                f"demo_domain.workflows.sample:{symbol}",
                "workflow",
            )
        elif node_type == "condition":
            specs[template_uuid] = (
                "condition",
                "condition",
                "unilabos.workflow.authoring:condition",
                "condition",
            )
        elif node_type == "repeat_until":
            specs[template_uuid] = (
                "repeat_until",
                "repeat_until",
                "unilabos.workflow.authoring:repeat_until",
                "repeat_until",
            )
        elif node_type == "manual_confirm":
            specs[template_uuid] = (
                action_name,
                "manual_confirm",
                "lab.devices:Reactor",
                "UniLabJsonCommand",
            )
        else:
            raise AssertionError(f"未覆盖的导出节点类型 {node_type}")

    templates: list[dict[str, Any]] = []
    for template_uuid, (name, node_type, class_id, type_name) in specs.items():
        meta_data: dict[str, Any] = {"owner": "test"}
        if node_type == "workflow":
            meta_data = {
                "unilab": {
                    "workflow_source": {
                        "kind": "package",
                        "module": "demo_domain.workflows.sample",
                        "symbol": name,
                    }
                }
            }
        elif node_type in {"condition", "repeat_until"}:
            meta_data = {"unilab": {"framework_owner_only": True}}
        elif node_type in {"device_action", "manual_confirm"}:
            meta_data = {
                "unilab": {
                    "action_contract_schema": deepcopy(_ACTION_CONTRACT_SCHEMA),
                }
            }
        templates.append(
            {
                "uuid": template_uuid,
                "resource_template_uuid": "31000000-0000-4000-8000-000000000001",
                "name": name,
                "display_name": name,
                "class": class_id,
                "description": name,
                "meta_data": meta_data,
                "goal": {},
                "goal_default": {},
                "feedback": {},
                "result": {},
                "schema": None,
                "type": type_name,
                "node_type": node_type,
                "icon": None,
                "header": None,
                "footer": None,
            }
        )

    handles: list[dict[str, Any]] = []
    seen_handles: set[str] = set()
    seen_named_handles: set[tuple[str, str, str]] = set()

    def _add_named_handle(
        template_uuid: str,
        *,
        key: str,
        io_type: str,
        value_type: str,
        data_source: str = "executor",
    ) -> None:
        slot = (template_uuid, key, io_type)
        if slot in seen_named_handles:
            return
        seen_named_handles.add(slot)
        handle_uuid = str(uuid5(NAMESPACE_URL, f"{template_uuid}:{io_type}:{key}"))
        if handle_uuid in seen_handles:
            return
        seen_handles.add(handle_uuid)
        handles.append(
            _handle(
                handle_uuid,
                node_template_uuid=template_uuid,
                key=key,
                io_type=io_type,
                value_type=value_type,
                data_source=data_source,
            )
        )

    for template_uuid, (_name, node_type, _class_id, _type_name) in specs.items():
        if node_type in {"device_action", "manual_confirm"}:
            _add_named_handle(
                template_uuid,
                key="ready",
                io_type="source",
                value_type="object",
                data_source="dependency",
            )
            _add_named_handle(
                template_uuid,
                key="ready",
                io_type="target",
                value_type="object",
                data_source="dependency",
            )

    def _add_handle(
        handle_uuid: str,
        *,
        template_uuid: str,
        io_type: str,
        key: str,
    ) -> None:
        if handle_uuid in seen_handles:
            return
        seen_handles.add(handle_uuid)
        handles.append(
            _handle(
                handle_uuid,
                node_template_uuid=template_uuid,
                key=f"{key}_{handle_uuid[:8]}",
                io_type=io_type,
                value_type="object",
                data_source="dependency",
            )
        )

    def _condition_field_names(value: Any) -> list[str]:
        names: list[str] = []
        if isinstance(value, Mapping):
            if "name" in value and isinstance(value.get("field"), Mapping):
                names.append(str(value["name"]))
            for child in value.values():
                names.extend(_condition_field_names(child))
        elif isinstance(value, list):
            for child in value:
                names.extend(_condition_field_names(child))
        return names

    for edge in payload["edges"]:
        _add_handle(
            str(edge["source_handle_uuid"]),
            template_uuid=template_by_node[str(edge["source_node_uuid"])],
            io_type="source",
            key="source_port",
        )
        _add_handle(
            str(edge["target_handle_uuid"]),
            template_uuid=template_by_node[str(edge["target_node_uuid"])],
            io_type="target",
            key="target_port",
        )
    for node in nodes:
        template_uuid = template_by_node[str(node["uuid"])]
        unilab = ((node.get("meta_data") or {}).get("unilab") or {})
        for binding_field, io_type in (
            ("carry_bindings", "target"),
            ("input_bindings", "target"),
        ):
            raw_bindings = unilab.get(binding_field) or {}
            if not isinstance(raw_bindings, Mapping):
                continue
            for handle_uuid in raw_bindings:
                _add_handle(
                    str(handle_uuid),
                    template_uuid=template_uuid,
                    io_type=io_type,
                    key=binding_field,
                )
        composite = unilab.get("composite") or {}
        if not isinstance(composite, Mapping):
            continue
        for mapping_field, default_io in (
            ("target_mappings", "target"),
            ("source_mappings", "source"),
        ):
            raw_mappings = composite.get(mapping_field) or {}
            if not isinstance(raw_mappings, Mapping):
                continue
            for handle_uuid, targets in raw_mappings.items():
                _add_handle(
                    str(handle_uuid),
                    template_uuid=template_uuid,
                    io_type=default_io,
                    key=mapping_field,
                )
                for target in targets if isinstance(targets, list) else [targets]:
                    if not isinstance(target, Mapping):
                        continue
                    nested_node = str(target.get("workflow_node_uuid") or "")
                    nested_handle = target.get("target_handle_uuid") or target.get(
                        "source_handle_uuid"
                    )
                    if nested_node in template_by_node and nested_handle:
                        io_type = (
                            "source"
                            if target.get("source_handle_uuid")
                            else "target"
                        )
                        _add_handle(
                            str(nested_handle),
                            template_uuid=template_by_node[nested_node],
                            io_type=io_type,
                            key="composite_boundary",
                        )
        structural = composite.get("structural_mappings") or {}
        if isinstance(structural, Mapping):
            for group_name, io_type in (
                ("completion_sources", "source"),
                ("entry_targets", "target"),
            ):
                field = (
                    "source_handle_uuid"
                    if io_type == "source"
                    else "target_handle_uuid"
                )
                for item in structural.get(group_name) or []:
                    if not isinstance(item, Mapping):
                        continue
                    nested_node = str(item.get("workflow_node_uuid") or "")
                    nested_handle = item.get(field)
                    if nested_node in template_by_node and nested_handle:
                        _add_handle(
                            str(nested_handle),
                            template_uuid=template_by_node[nested_node],
                            io_type=io_type,
                            key=group_name,
                        )
    for node in nodes:
        params = node.get("param") or {}
        if not isinstance(params, Mapping):
            continue
        bindings = params.get("bindings") if isinstance(params, Mapping) else None
        bound_templates: list[str] = []
        if isinstance(bindings, Mapping):
            for binding in bindings.values():
                if not isinstance(binding, Mapping):
                    continue
                source_uuid = str(binding.get("node_uuid") or "")
                if source_uuid in template_by_node:
                    bound_templates.append(template_by_node[source_uuid])
        if str(node.get("type")) == "repeat_until":
            next_carry = params.get("next_carry") or {}
            if isinstance(next_carry, Mapping):
                for binding in next_carry.values():
                    if not isinstance(binding, Mapping):
                        continue
                    source_uuid = str(binding.get("node_uuid") or "")
                    path = binding.get("result_path") or []
                    if source_uuid in template_by_node and path:
                        _add_named_handle(
                            template_by_node[source_uuid],
                            key=str(path[0]),
                            io_type="source",
                            value_type="integer",
                        )
            for field_name in _condition_field_names(params.get("until")):
                for source_template in bound_templates:
                    _add_named_handle(
                        source_template,
                        key=field_name,
                        io_type="source",
                        value_type="boolean",
                    )
        if str(node.get("type")) == "condition":
            for field_name in _condition_field_names(params):
                for source_template in bound_templates:
                    _add_named_handle(
                        source_template,
                        key=field_name,
                        io_type="source",
                        value_type="boolean",
                    )
    return templates, handles


def _empty_applied_graph() -> dict[str, Any]:
    """返回编译器要求的空已应用图。"""

    return {
        "workflow": {
            "uuid": WORKFLOW_UUID,
            "name": "base",
            "tags": [],
            "description": None,
            "meta_data": {},
            "revision": 7,
        },
        "nodes": [],
        "edges": [],
        "node_templates": [],
        "handle_templates": [],
    }


def test_python_import_and_api_edits_survive_restart_via_domain_source(
    tmp_path: Path,
) -> None:
    """Python 导入及后续元数据/图修改都应回写同一文件并可冷启动重建。

    参数：``tmp_path`` 是本用例独占的领域包与运行事实目录。返回：无。异常：
    导入、API 编辑、原子写回或重启恢复丢失任一工作流事实时由断言暴露。
    """

    selected_root = tmp_path / "domain"
    package_root = _empty_domain_package(selected_root)
    database_path = tmp_path / "workflow_history.db"
    service, runtime_store, definitions = _service(
        database_path=database_path,
        selected_root=selected_root,
    )
    try:
        imported = service.import_python_workflow(
            file_name="sample_workflow.py",
            python_source=_source().replace(
                '    description="Prepare and analyze one sample.",\n',
                '    description="Prepare and analyze one sample.",\n'
                '    workflow_type="experiment_operation",\n',
            ),
        )
        assert imported["workflow"]["uuid"] == WORKFLOW_UUID
        source_path = package_root / "experiment_operations" / "sample_workflow.py"
        assert source_path.is_file()
        manifest = yaml.safe_load(
            selected_root.joinpath("package.yaml").read_text(encoding="utf-8")
        )
        assert manifest["workflows"] == [
            {
                "workflow_uuid": WORKFLOW_UUID,
                "source": ("demo_domain/experiment_operations/sample_workflow.py"),
            }
        ]

        updated_workflow = service.update_workflow(
            WORKFLOW_UUID,
            name="领域包工作流",
            tags=["production", "domain"],
            description="修改后仍以领域 Python 为准",
            meta_data={"owner": "lab"},
            workflow_type="experiment_operation",
        )
        graph = service.get_graph(WORKFLOW_UUID)
        edited_nodes = []
        for node in graph["nodes"]:
            edited = dict(node)
            if edited["uuid"] == PREPARE_NODE_UUID:
                edited["name"] = "领域包加样"
                edited["description"] = "由工作台修改并回写"
            edited_nodes.append(edited)
        edited_graph = service.save_graph(
            WORKFLOW_UUID,
            revision=int(updated_workflow["revision"]),
            nodes=edited_nodes,
            edges=graph["edges"],
        )
        assert edited_graph["workflow"]["revision"] == 3
        source = source_path.read_text(encoding="utf-8")
        assert "displayname='领域包工作流'" in source
        assert "tags=['production', 'domain']" in source
        assert "workflow_type='experiment_operation'" in source
        assert "meta_data={'owner': 'lab'}" in source
        assert "# [领域包加样]: 由工作台修改并回写" in source
        assert runtime_store.count_rows("workflow") == 0
        assert definitions.count_rows("workflow") == 1
    finally:
        service.close()

    reopened, reopened_runtime, reopened_definitions = _service(
        database_path=database_path,
        selected_root=selected_root,
    )
    try:
        rebuilt = reopened.get_graph(WORKFLOW_UUID)
        assert rebuilt["workflow"]["name"] == "领域包工作流"
        assert rebuilt["workflow"]["tags"] == ["production", "domain"]
        assert rebuilt["workflow"]["workflow_type"] == "experiment_operation"
        assert rebuilt["workflow"]["meta_data"]["owner"] == "lab"
        assert (
            next(
                node for node in rebuilt["nodes"] if node["uuid"] == PREPARE_NODE_UUID
            )["name"]
            == "领域包加样"
        )
        assert reopened_runtime.count_rows("workflow") == 0
        assert reopened_definitions.count_rows("workflow") == 1
    finally:
        reopened.close()


def test_legacy_import_remaps_control_region_references(
    tmp_path: Path,
) -> None:
    """含循环控制节点的 JSON 导入必须重映射参数内的节点身份引用。"""

    selected_root = tmp_path / "domain"
    _empty_domain_package(selected_root)
    engine = _repeat_engine()
    compiled = engine.compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        python_source=_repeat_source(),
        source_uri="package://demo_domain/workflows/repeat.py",
        applied_graph=_empty_applied_graph(),
    )
    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    service, _runtime_store, definitions = _service(
        database_path=tmp_path / "workflow_history.db",
        selected_root=selected_root,
        engine=engine,
    )
    _project_compiled_catalog(definitions, compiled)
    try:
        imported = service.import_legacy_workflow(
            payload=_legacy_graph_payload(compiled, name="导入循环流程")
        )
        imported_nodes = {node["uuid"]: node for node in imported["nodes"]}
        assert imported_nodes
        control = next(
            node for node in imported_nodes.values() if node["type"] == "repeat_until"
        )
        body_uuids = set(control["param"]["node_uuids"])
        assert body_uuids
        assert body_uuids.issubset(imported_nodes)
        assert all(
            node["parent_uuid"] == control["uuid"]
            for node in imported_nodes.values()
            if node["uuid"] in body_uuids
        )
        assert control["param"]["next_carry"]["dose"]["node_uuid"] in body_uuids
        assert imported["edges"]
        imported_identities = set(imported_nodes)
        for edge in imported["edges"]:
            assert edge["source_node_uuid"] in imported_identities
            assert edge["target_node_uuid"] in imported_identities
            assert edge["source_handle_uuid"]
            assert edge["target_handle_uuid"]
        copied = service.duplicate_workflow(imported["workflow"]["uuid"], name="复制循环流程")
        copied_nodes = {node["uuid"]: node for node in copied["nodes"]}
        assert set(copied_nodes).isdisjoint(imported_nodes)
        copied_control = next(node for node in copied_nodes.values() if node["type"] == "repeat_until")
        copied_body = set(copied_control["param"]["node_uuids"])
        assert copied_body and copied_body.issubset(copied_nodes)
        assert copied_control["param"]["next_carry"]["dose"]["node_uuid"] in copied_body
        assert all(copied_nodes[node_id]["parent_uuid"] == copied_control["uuid"] for node_id in copied_body)
    finally:
        service.close()


def test_legacy_import_remaps_condition_branch_references(
    tmp_path: Path,
) -> None:
    """含条件控制节点的 JSON 导入必须重映射分支内的节点身份引用。"""

    selected_root = tmp_path / "domain"
    _empty_domain_package(selected_root)
    engine = _condition_engine()
    compiled = engine.compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        python_source=_condition_source(),
        source_uri="package://demo_domain/workflows/condition.py",
        applied_graph=_empty_applied_graph(),
    )
    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    service, _runtime_store, definitions = _service(
        database_path=tmp_path / "workflow_history.db",
        selected_root=selected_root,
        engine=engine,
    )
    _project_compiled_catalog(definitions, compiled)
    try:
        imported = service.import_legacy_workflow(
            payload=_legacy_graph_payload(compiled, name="导入条件流程")
        )
        imported_nodes = {node["uuid"]: node for node in imported["nodes"]}
        control = next(
            node for node in imported_nodes.values() if node["type"] == "condition"
        )
        branch_uuids = {
            node_uuid
            for branch in control["param"]["branches"]
            for node_uuid in branch["node_uuids"]
        }
        assert branch_uuids
        assert branch_uuids.issubset(imported_nodes)
        assert all(
            node["parent_uuid"] == control["uuid"]
            for node in imported_nodes.values()
            if node["uuid"] in branch_uuids
        )
    finally:
        service.close()


def test_legacy_import_keeps_complete_data_edges(
    tmp_path: Path,
) -> None:
    """带完整 Handle 连线的 JSON 导入必须重建节点身份并保留连线端点。"""

    selected_root = tmp_path / "domain"
    _empty_domain_package(selected_root)
    engine = _engine()
    compiled = engine.compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        python_source=_source(),
        source_uri="package://demo_domain/workflows/connected.py",
        applied_graph=_empty_applied_graph(),
    )
    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    assert compiled.graph["edges"]
    service, _runtime_store, definitions = _service(
        database_path=tmp_path / "workflow_history.db",
        selected_root=selected_root,
        engine=engine,
    )
    _project_compiled_catalog(definitions, compiled)
    original_node_uuids = {node["uuid"] for node in compiled.graph["nodes"]}
    try:
        imported = service.import_legacy_workflow(
            payload=_legacy_graph_payload(compiled, name="导入连线流程")
        )
        imported_nodes = {node["uuid"]: node for node in imported["nodes"]}
        assert imported_nodes
        assert original_node_uuids.isdisjoint(imported_nodes)
        assert imported["edges"]
        for edge in imported["edges"]:
            assert edge["source_node_uuid"] in imported_nodes
            assert edge["target_node_uuid"] in imported_nodes
            assert edge["source_handle_uuid"]
            assert edge["target_handle_uuid"]
    finally:
        service.close()


def test_legacy_import_accepts_nested_composite_control_flow_json(
    tmp_path: Path,
) -> None:
    """含嵌套实验操作、条件、循环和完整连线的导出 JSON 必须能导入。"""

    payload = json.loads(_CONTROL_FLOW_IMPORT_FIXTURE.read_text(encoding="utf-8"))
    templates, handles = _control_flow_import_catalog(payload)
    engine = _RecordingAuthoringEngine(
        WorkflowAuthoringEngine(
            catalog=AuthoringCatalogSnapshot.from_entities(templates, handles)
        )
    )
    selected_root = tmp_path / "domain"
    _empty_domain_package(selected_root)
    service, _runtime_store, definitions = _service(
        database_path=tmp_path / "workflow_history.db",
        selected_root=selected_root,
        engine=engine,
    )

    class _CatalogProjection:
        graph = {"node_templates": templates, "handle_templates": handles}
        template_catalog_fingerprint = engine.template_catalog_fingerprint

    _project_compiled_catalog(definitions, _CatalogProjection)
    client = TestClient(create_workflow_app(service))
    try:
        response = client.post("/api/v1/workflows/import", json=payload)
        body = response.json()
        assert response.status_code == 201, body
        assert body["code"] == 0, body
        imported = body["data"]
        imported_nodes = {node["uuid"]: node for node in imported["nodes"]}
        assert any(node["type"] == "condition" for node in imported["nodes"])
        assert any(node["type"] == "repeat_until" for node in imported["nodes"])
        assert imported["edges"]
        for edge in imported["edges"]:
            assert edge["source_node_uuid"] in imported_nodes
            assert edge["target_node_uuid"] in imported_nodes
        assert engine.inline_flags[:2] == [False, True]
        bound_nodes = [
            node
            for node in imported["nodes"]
            if node["type"] in {"ILab", "manual_confirm"}
        ]
        assert bound_nodes
        assert all(
            ((node.get("meta_data") or {}).get("unilab") or {}).get(
                "executor_binding"
            )
            == {
                "mode": "fixed",
                "device_id": node["material_uuid"],
            }
            for node in bound_nodes
        )
        _imported_graph_can_run(imported)
        task = service.create_workflow_task(
            workflow_uuid=imported["workflow"]["uuid"],
            run_mode="normal",
            target_node_uuid=None,
            input_value={},
            description=None,
            meta_data={},
        )
        assert task["uuid"]
    except WorkflowError as error:
        raise AssertionError(f"{error.code}: {error.message}") from error
    finally:
        service.close()


def test_legacy_import_uniquifies_inlined_composite_result_names(
    tmp_path: Path,
) -> None:
    """内联导入时，跨组合重复的作者结果变量必须改成唯一 Python 名后仍能编译。"""

    payload = json.loads(_CONTROL_FLOW_IMPORT_FIXTURE.read_text(encoding="utf-8"))
    for node in payload["nodes"]:
        unilab = ((node.get("meta_data") or {}).get("unilab") or {})
        if unilab.get("authoring_result_name") in {
            "true_branch",
            "false_after_confirmation",
            "stirred",
        }:
            unilab["authoring_result_name"] = "observed"
    templates, handles = _control_flow_import_catalog(payload)
    engine = WorkflowAuthoringEngine(
        catalog=AuthoringCatalogSnapshot.from_entities(templates, handles)
    )
    selected_root = tmp_path / "domain"
    _empty_domain_package(selected_root)
    service, _runtime_store, definitions = _service(
        database_path=tmp_path / "workflow_history.db",
        selected_root=selected_root,
        engine=engine,
    )

    class _CatalogProjection:
        graph = {"node_templates": templates, "handle_templates": handles}
        template_catalog_fingerprint = engine.template_catalog_fingerprint

    _project_compiled_catalog(definitions, _CatalogProjection)
    try:
        imported = service.import_legacy_workflow(payload=payload)
        result_names = [
            ((node.get("meta_data") or {}).get("unilab") or {}).get(
                "authoring_result_name"
            )
            for node in imported["nodes"]
            if node["type"] not in {"condition", "repeat_until", "workflow"}
        ]
        assigned = [name for name in result_names if isinstance(name, str)]
        assert assigned
        assert len(assigned) == len(set(assigned))
        assert any(node["type"] == "condition" for node in imported["nodes"])
        assert any(node["type"] == "repeat_until" for node in imported["nodes"])
    finally:
        service.close()


def test_applied_graph_keeps_unexpanded_workflow_calls() -> None:
    """重编译前只去掉已展开组合调用，未展开的 ``workflow()`` 节点必须保留。"""

    graph = {
        "workflow": {"uuid": "00000000-0000-4000-8000-000000000001"},
        "nodes": [
            {"uuid": "unexpanded", "type": "workflow", "parent_uuid": None},
            {"uuid": "expanded", "type": "workflow", "parent_uuid": None},
            {
                "uuid": "child",
                "type": "ILab",
                "parent_uuid": "expanded",
            },
        ],
        "edges": [
            {
                "source_node_uuid": "unexpanded",
                "target_node_uuid": "expanded",
                "source_handle_uuid": "src",
                "target_handle_uuid": "dst",
            },
            {
                "source_node_uuid": "expanded",
                "target_node_uuid": "child",
                "source_handle_uuid": "ready",
                "target_handle_uuid": "ready",
            },
        ],
    }
    result = WorkflowService._applied_graph_without_inlined_composite_parents(graph)
    nodes = {node["uuid"]: node for node in result["nodes"]}
    assert "unexpanded" in nodes
    assert "expanded" not in nodes
    assert nodes["child"]["parent_uuid"] is None
    assert all(
        edge["source_node_uuid"] != "expanded"
        and edge["target_node_uuid"] != "expanded"
        for edge in result["edges"]
    )


def test_published_experiment_operation_survives_restart_in_domain_package(
    tmp_path: Path,
) -> None:
    """已发布实验操作的状态和合同须从领域包文件跨重启恢复。

    参数：``tmp_path`` 隔离领域包、内存定义目录和运行事实库。返回：无。异常：
    发布合同写入 SQLite、稳定身份变化或重启后降级为源码时由断言暴露。
    """

    selected_root = tmp_path / "domain"
    package_root = _empty_domain_package(selected_root)
    database_path = tmp_path / "workflow_history.db"
    service, runtime_store, definitions = _service(
        database_path=database_path,
        selected_root=selected_root,
    )
    try:
        imported = service.import_python_workflow(
            file_name="published_operation.py",
            python_source=_source().replace(
                '    description="Prepare and analyze one sample.",\n',
                '    description="Prepare and analyze one sample.",\n'
                '    workflow_type="experiment_operation",\n',
            ),
        )
        first_contract = service.publish_workflow_contract(
            WORKFLOW_UUID,
            revision=int(imported["workflow"]["revision"]),
        )
        repeated_contract = service.publish_workflow_contract(
            WORKFLOW_UUID,
            revision=int(imported["workflow"]["revision"]),
        )
        assert repeated_contract["uuid"] == first_contract["uuid"]
        updated = service.update_workflow(
            WORKFLOW_UUID,
            name="已更新实验操作",
            tags=[],
            description="第二个不可变发布版本",
            meta_data={},
            workflow_type="experiment_operation",
        )
        contract = service.publish_workflow_contract(
            WORKFLOW_UUID,
            revision=int(updated["revision"]),
        )
        assert contract["uuid"] != first_contract["uuid"]
        assert contract["version"] == 2
        assert service.get_workflow(WORKFLOW_UUID)["status"] == "published"
        publication_path = package_root / "workflow_publications.json"
        assert publication_path.is_file()
        publication_root = package_root / "workflow_publications" / WORKFLOW_UUID
        manifest_path = publication_root / "manifest.json"
        contract_path = publication_root / "contracts" / f"{contract['uuid']}.json"
        assert manifest_path.is_file()
        assert contract_path.is_file()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["workflow_uuid"] == WORKFLOW_UUID
        assert manifest["latest_contract_uuid"] == contract["uuid"]
        stored_entry = json.loads(contract_path.read_text(encoding="utf-8"))
        assert stored_entry["contract"]["uuid"] == contract["uuid"]
        assert stored_entry["contract"]["workflow_uuid"] == WORKFLOW_UUID
        assert stored_entry["contract"]["graph_snapshot"]["workflow"]["uuid"] == (
            WORKFLOW_UUID
        )
        assert "graph_snapshot" not in json.loads(
            publication_path.read_text(encoding="utf-8")
        )
        assert not (package_root / "catalog.sqlite").exists()
        assert (
            len(
                yaml.safe_load(publication_path.read_text(encoding="utf-8"))[
                    "publications"
                ]
            )
            == 2
        )
        assert runtime_store.count_rows("workflow") == 0
        # PostgreSQL 共用完整表结构，但定义仍由领域包文件保存，不应新增写入。
        from unilabos.persistence.database import SQLiteDatabase
        if isinstance(runtime_store.db, SQLiteDatabase):
            assert not runtime_store.db.table_exists("published_workflow_contract")
        elif runtime_store.db.table_exists("published_workflow_contract"):
            assert runtime_store.db.execute("SELECT COUNT(*) FROM published_workflow_contract").fetchone()[0] == 0
        # 运行时表不是领域包文件的持久化权威；模拟旧进程退出前表被清理，
        # 下次启动应由新目录合同重新建表并恢复。
        with definitions.transaction() as connection:
            connection.execute("DROP TABLE published_workflow_contract")
    finally:
        service.close()

    reopened, reopened_runtime, _reopened_definitions = _service(
        database_path=database_path,
        selected_root=selected_root,
    )
    try:
        assert reopened.get_workflow(WORKFLOW_UUID)["status"] == "published"
        listed = reopened.list_published_workflow_contracts()
        assert listed["items"][0]["uuid"] == contract["uuid"]
        assert (
            listed["items"][0]["node_template_uuid"] == contract["node_template_uuid"]
        )
        assert reopened_runtime.count_rows("workflow") == 0
        if isinstance(reopened_runtime.db, SQLiteDatabase):
            assert not reopened_runtime.db.table_exists("published_workflow_contract")
        elif reopened_runtime.db.table_exists("published_workflow_contract"):
            assert reopened_runtime.db.execute("SELECT COUNT(*) FROM published_workflow_contract").fetchone()[0] == 0
    finally:
        reopened.close()


def test_publication_marker_failure_keeps_authoritative_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """兼容索引写失败时，已提交的工作流合同仍应可冷启动恢复。"""

    selected_root = tmp_path / "domain"
    package_root = _empty_domain_package(selected_root)
    database_path = tmp_path / "workflow_history.db"
    service, _runtime_store, _definitions = _service(
        database_path=database_path,
        selected_root=selected_root,
    )

    def fail_marker(
        _catalog: publication_catalog.WorkflowPublicationCatalog,
        **_kwargs: object,
    ) -> None:
        """模拟非权威兼容索引写入失败。"""

        raise publication_catalog.WorkflowPublicationCatalogError("unavailable")

    monkeypatch.setattr(
        publication_catalog.WorkflowPublicationCatalog,
        "_write_legacy_marker",
        fail_marker,
    )
    try:
        imported = service.import_python_workflow(
            file_name="marker_failure.py",
            python_source=_source().replace(
                '    description="Prepare and analyze one sample.",\n',
                '    description="Prepare and analyze one sample.",\n'
                '    workflow_type="experiment_operation",\n',
            ),
        )
        published = service.publish_workflow_contract(
            WORKFLOW_UUID,
            revision=int(imported["workflow"]["revision"]),
        )
        assert published["workflow_uuid"] == WORKFLOW_UUID
        assert (
            package_root
            / "workflow_publications"
            / WORKFLOW_UUID
            / "manifest.json"
        ).is_file()
    finally:
        service.close()

    monkeypatch.undo()
    reopened, _reopened_runtime, _reopened_definitions = _service(
        database_path=database_path,
        selected_root=selected_root,
    )
    try:
        # 测试装配器的源码激活发生在合同恢复之前，会把单修订工作流推进一次
        # revision；这里验证的是合同确实从 manifest 恢复，而非兼容索引失败后
        # 被当作发布失败丢弃。
        assert reopened.list_published_workflow_contracts()["items"]
    finally:
        reopened.close()


def test_uncommitted_contract_without_manifest_is_ignored_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """manifest 提交前进程退出留下的合同文件不能在重启时自行生效。"""

    selected_root = tmp_path / "domain"
    package_root = _empty_domain_package(selected_root)
    database_path = tmp_path / "workflow_history.db"
    service, _runtime_store, _definitions = _service(
        database_path=database_path,
        selected_root=selected_root,
    )

    def fail_manifest(
        _workflow_dir: Path,
        _workflow_identity: tuple[int, int],
        _workflow_uuid: str,
        _entries: list[dict[str, object]],
    ) -> None:
        """模拟合同文件已写入但 manifest 原子提交失败。"""

        raise publication_catalog.WorkflowPublicationCatalogError("unavailable")

    monkeypatch.setattr(
        publication_catalog.WorkflowPublicationCatalog,
        "_write_manifest",
        fail_manifest,
    )
    try:
        imported = service.import_python_workflow(
            file_name="manifest_failure.py",
            python_source=_source(),
        )
        with pytest.raises(WorkflowError) as failed:
            service.publish_workflow_contract(
                WORKFLOW_UUID,
                revision=int(imported["workflow"]["revision"]),
            )
        assert failed.value.code == "source_publication_failed"
        contracts_dir = (
            package_root
            / "workflow_publications"
            / WORKFLOW_UUID
            / "contracts"
        )
        assert list(contracts_dir.glob("*.json"))
        assert not (
            package_root
            / "workflow_publications"
            / WORKFLOW_UUID
            / "manifest.json"
        ).exists()
    finally:
        service.close()

    monkeypatch.undo()
    reopened, _reopened_runtime, _reopened_definitions = _service(
        database_path=database_path,
        selected_root=selected_root,
    )
    try:
        assert reopened.list_published_workflow_contracts()["items"] == []
    finally:
        reopened.close()


def test_http_create_persists_workflow_types_in_separate_domain_directories(
    tmp_path: Path,
) -> None:
    """证明接口创建的普通工作流与实验操作分别写入领域包并可重启恢复。

    参数：``tmp_path`` 隔离唯一领域包、运行事实库和进程内定义目录。返回：无。
    异常：HTTP 创建没有生成规范 Python、目录分类错误、清单缺项或冷启动丢失
    任一工作流时由断言暴露。
    """

    selected_root = tmp_path / "domain"
    package_root = _empty_domain_package(selected_root)
    database_path = tmp_path / "workflow_history.db"
    service, runtime_store, _definitions = _service(
        database_path=database_path,
        selected_root=selected_root,
    )
    # 两个响应 UUID 是工作流（Workflow）的稳定身份，同时决定默认 Python
    # 文件名；目录只表达源码类别，不参与身份计算。
    client = TestClient(create_workflow_app(service))
    try:
        normal_response = client.post(
            "/api/v1/workflows",
            json={
                "name": "普通工作流",
                "tags": [],
                "description": "接口创建的普通流程",
                "meta_data": {},
            },
        )
        operation_response = client.post(
            "/api/v1/workflows",
            json={
                "name": "实验操作",
                "workflow_type": "experiment_operation",
                "tags": [],
                "description": "接口创建的实验操作",
                "meta_data": {},
            },
        )
        assert normal_response.status_code == 201, normal_response.text
        assert operation_response.status_code == 201, operation_response.text
        normal = normal_response.json()["data"]
        operation = operation_response.json()["data"]
        normal_name = DomainWorkflowSourceTarget.default_file_name(normal["uuid"])
        operation_name = DomainWorkflowSourceTarget.default_file_name(operation["uuid"])
        normal_path = package_root / "workflows" / normal_name
        operation_path = package_root / "experiment_operations" / operation_name

        assert normal_path.is_file()
        assert operation_path.is_file()
        normal_source = normal_path.read_text(encoding="utf-8")
        operation_source = operation_path.read_text(encoding="utf-8")
        assert "@workflow(" in normal_source
        assert f'workflow_uuid="{normal["uuid"]}"' in normal_source
        assert "displayname='普通工作流'" in normal_source
        assert "description='接口创建的普通流程'" in normal_source
        assert "@workflow(" in operation_source
        assert f'workflow_uuid="{operation["uuid"]}"' in operation_source
        assert "displayname='实验操作'" in operation_source
        assert "description='接口创建的实验操作'" in operation_source
        assert "workflow_type='experiment_operation'" in operation_source

        manifest = yaml.safe_load(
            selected_root.joinpath("package.yaml").read_text(encoding="utf-8")
        )
        assert manifest["workflows"] == [
            {
                "workflow_uuid": normal["uuid"],
                "source": f"demo_domain/workflows/{normal_name}",
            },
            {
                "workflow_uuid": operation["uuid"],
                "source": (f"demo_domain/experiment_operations/{operation_name}"),
            },
        ]
        assert runtime_store.count_rows("workflow") == 0
    finally:
        service.close()

    reopened, reopened_runtime, _reopened_definitions = _service(
        database_path=database_path,
        selected_root=selected_root,
    )
    try:
        reopened_normal = reopened.get_workflow(normal["uuid"])
        reopened_operation = reopened.get_workflow(operation["uuid"])
        assert reopened_normal["workflow_type"] == "normal"
        assert reopened_normal["description"] == "接口创建的普通流程"
        assert reopened_operation["workflow_type"] == "experiment_operation"
        assert reopened_operation["description"] == "接口创建的实验操作"
        assert reopened_runtime.count_rows("workflow") == 0
    finally:
        reopened.close()


def test_http_create_rejects_duplicate_authoring_function_name(
    tmp_path: Path,
) -> None:
    """接口创建的工作流名称映射为作者函数名时必须保持包内唯一。"""

    selected_root = tmp_path / "domain"
    _empty_domain_package(selected_root)
    service, _runtime_store, _definitions = _service(
        database_path=tmp_path / "workflow_history.db",
        selected_root=selected_root,
    )
    client = TestClient(create_workflow_app(service))
    body = {
        "name": "重复函数名",
        "tags": [],
        "description": None,
        "meta_data": {},
    }
    try:
        first = client.post("/api/v1/workflows", json=body)
        second = client.post("/api/v1/workflows", json=body)
        assert first.status_code == 201, first.text
        assert second.status_code == 200
        assert second.json()["code"] == 3003
    finally:
        service.close()


def test_legacy_import_accepts_backend_string_template_schema(
    tmp_path: Path,
) -> None:
    """活环境文本 schema 的单动作 JSON 导入后必须能建成任务并派发。"""

    schema_text = json.dumps(
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "goal": {"type": "object", "additionalProperties": True},
                "result": {"type": "object", "additionalProperties": True},
            },
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    template, handles = _template(
        "30000000-0000-4000-8000-000000000091",
        name="noop",
        handles=[],
    )
    template["schema"] = schema_text
    template["type"] = "UniLabJsonCommand"
    template["node_type"] = "device_action"
    template["meta_data"] = {
        "unilab": {"action_contract_schema": deepcopy(_ACTION_CONTRACT_SCHEMA)}
    }
    engine = WorkflowAuthoringEngine(
        catalog=AuthoringCatalogSnapshot.from_entities([template], handles)
    )
    selected_root = tmp_path / "domain"
    _empty_domain_package(selected_root)
    service, _runtime_store, definitions = _service(
        database_path=tmp_path / "workflow_history.db",
        selected_root=selected_root,
        engine=engine,
    )

    class _CatalogProjection:
        graph = {"node_templates": [template], "handle_templates": handles}
        template_catalog_fingerprint = engine.template_catalog_fingerprint

    _project_compiled_catalog(definitions, _CatalogProjection)
    try:
        imported = service.import_legacy_workflow(
            payload={
                "name": "单动作导入",
                "tags": ["schema-regression"],
                "nodes": [
                    {
                        "uuid": "20000000-0000-4000-8000-000000000091",
                        "name": "noop",
                        "type": "ILab",
                        "workflow_node_template_uuid": template["uuid"],
                        "description": "单动作导入回归",
                        "param": {},
                        "material_uuid": _IMPORTED_DEVICE_UUID,
                    }
                ],
                "edges": [],
            }
        )
        node = imported["nodes"][0]
        assert node["material_uuid"] == _IMPORTED_DEVICE_UUID
        assert node["meta_data"]["unilab"]["executor_binding"] == {
            "mode": "fixed",
            "device_id": _IMPORTED_DEVICE_UUID,
        }
        submitted = _imported_graph_can_run(imported)
        assert submitted["dispatched"]
        task = service.create_workflow_task(
            workflow_uuid=imported["workflow"]["uuid"],
            run_mode="normal",
            target_node_uuid=None,
            input_value={},
            description=None,
            meta_data={},
        )
        assert task["uuid"]
        assert imported["workflow"]["name"] == "单动作导入"
    finally:
        service.close()


def test_json_import_is_canonicalized_to_domain_python(tmp_path: Path) -> None:
    """旧 JSON 图导入后应只留下规范 Python，并可从该文件冷启动重建。"""

    selected_root = tmp_path / "domain"
    package_root = _empty_domain_package(selected_root)
    template, handles = _template(
        "30000000-0000-4000-8000-000000000090",
        name="noop",
        handles=[],
    )
    engine = WorkflowAuthoringEngine(
        catalog=AuthoringCatalogSnapshot.from_entities([template], handles)
    )
    service, runtime_store, definitions = _service(
        database_path=tmp_path / "workflow_history.db",
        selected_root=selected_root,
        engine=engine,
    )
    source = f'''from lab.devices import Reactor
from unilabos.workflow.authoring import device, workflow, workflow_output

reactor: Reactor = device()

@workflow(workflow_uuid="{WORKFLOW_UUID}", displayname="JSON source")
def json_source():
    # unilab:node_uuid={PREPARE_NODE_UUID}
    completed = reactor.noop()
    return workflow_output()
'''
    compiled = engine.compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        python_source=source,
        source_uri="test://json-source",
        applied_graph={
            "workflow": {
                "uuid": WORKFLOW_UUID,
                "name": "base",
                "tags": [],
                "description": None,
                "meta_data": {},
                "revision": 7,
            },
            "nodes": [],
            "edges": [],
            "node_templates": [],
            "handle_templates": [],
        },
    )
    assert compiled.valid and compiled.graph is not None
    with definitions.transaction() as connection:
        definitions._ensure_authoring_catalog_projection(
            connection,
            node_templates=compiled.graph["node_templates"],
            handle_templates=compiled.graph["handle_templates"],
            authority_id=compiled.template_catalog_fingerprint,
            now="2026-08-28T00:00:00+00:00",
        )
    identity = ""
    source_path: Path | None = None
    try:
        imported = service.import_legacy_workflow(
            payload={
                "workflow_name": "JSON 导入工作流",
                "tags": ["json"],
                "description": "导入后转换为 Python",
                "meta_data": {"owner": "lab"},
                "nodes": list(compiled.graph["nodes"]),
                "edges": compiled.graph["edges"],
            }
        )
        identity = imported["workflow"]["uuid"]
        source_path = (
            package_root / "workflows" / (f"workflow_{identity.replace('-', '')}.py")
        )
        source = source_path.read_text(encoding="utf-8")
        assert "@workflow(" in source
        assert "displayname='JSON 导入工作流'" in source
        assert "tags=['json']" in source
        assert runtime_store.count_rows("workflow") == 0
    finally:
        service.close()

    assert identity and source_path is not None
    reopened, reopened_runtime, reopened_definitions = _service(
        database_path=tmp_path / "workflow_history.db",
        selected_root=selected_root,
        engine=engine,
    )
    try:
        rebuilt = reopened.get_graph(identity)
        assert rebuilt["workflow"]["name"] == "JSON 导入工作流"
        assert rebuilt["workflow"]["tags"] == ["json"]
        assert rebuilt["workflow"]["meta_data"]["owner"] == "lab"
        assert reopened_runtime.count_rows("workflow") == 0
        assert reopened_definitions.count_rows("workflow") == 1
    finally:
        reopened.close()


def test_imported_workflow_delete_removes_source_and_manifest(
    tmp_path: Path,
) -> None:
    """删除领域工作流应同时移除源码文件和 manifest 登记。"""

    selected_root = tmp_path / "domain"
    package_root = _empty_domain_package(selected_root)
    database_path = tmp_path / "workflow_history.db"
    service, _runtime_store, _definitions = _service(
        database_path=database_path,
        selected_root=selected_root,
    )
    source_path = package_root / "workflows" / "sample_workflow.py"
    try:
        service.import_python_workflow(
            file_name=source_path.name,
            python_source=_source(),
        )
        service.delete_workflow(WORKFLOW_UUID)
        assert not source_path.exists()
        assert (
            yaml.safe_load(
                selected_root.joinpath("package.yaml").read_text(encoding="utf-8")
            )["workflows"]
            == []
        )
        with pytest.raises(WorkflowError) as deleted:
            service.get_workflow(WORKFLOW_UUID)
        assert deleted.value.code == "not_found"
    finally:
        service.close()

    reopened, _reopened_runtime, reopened_definitions = _service(
        database_path=database_path,
        selected_root=selected_root,
    )
    try:
        with pytest.raises(WorkflowError) as deleted:
            reopened.get_workflow(WORKFLOW_UUID)
        assert deleted.value.code == "not_found"
        assert reopened_definitions.count_rows("workflow") == 0
    finally:
        reopened.close()


def test_deleted_python_workflow_can_be_imported_again_with_same_identity(
    tmp_path: Path,
) -> None:
    """删除后再次导入同一 Python 文件不得被旧 UUID/图记录阻塞。"""

    selected_root = tmp_path / "domain"
    package_root = _empty_domain_package(selected_root)
    service, _runtime_store, definitions = _service(
        database_path=tmp_path / "workflow_history.db",
        selected_root=selected_root,
    )
    try:
        first = service.import_python_workflow(
            file_name="same_workflow.py",
            python_source=_source(),
        )
        service.delete_workflow(first["workflow"]["uuid"])
        assert definitions.count_rows("workflow", include_deleted=True) == 0
        second = service.import_python_workflow(
            file_name="same_workflow.py",
            python_source=_source(),
        )
        assert second["workflow"]["uuid"] == first["workflow"]["uuid"]
        assert second["workflow"]["revision"] == 1
        assert definitions.count_rows("workflow") == 1
        assert package_root.joinpath(
            "workflows", "same_workflow.py"
        ).is_file()
    finally:
        service.close()


def test_domain_workflow_status_returns_source_after_editing_published_revision(
    tmp_path: Path,
) -> None:
    """领域包发布旧修订后，图被修改时当前定义必须回到 source。"""

    selected_root = tmp_path / "domain"
    _empty_domain_package(selected_root)
    service, _runtime_store, _definitions = _service(
        database_path=tmp_path / "workflow_history.db",
        selected_root=selected_root,
    )
    try:
        imported = service.import_python_workflow(
            file_name="published_then_edited.py",
            python_source=_source(),
        )
        service.publish_workflow_contract(
            WORKFLOW_UUID,
            revision=int(imported["workflow"]["revision"]),
        )
        graph = service.get_graph(WORKFLOW_UUID)
        edited_nodes = [
            {**node, "name": "编辑后的节点"} for node in graph["nodes"]
        ]
        edited = service.save_graph(
            WORKFLOW_UUID,
            revision=int(imported["workflow"]["revision"]),
            nodes=edited_nodes,
            edges=graph["edges"],
        )
        assert edited["workflow"]["revision"] == int(imported["workflow"]["revision"]) + 1
        assert service.get_workflow(WORKFLOW_UUID)["status"] == "source"
        assert service.list_workflows(
            workflow_type="experiment_operation",
            status="published",
        )["items"] == []
    finally:
        service.close()


def test_delete_hides_published_contract_when_history_cleanup_is_unavailable(
    tmp_path: Path,
) -> None:
    """定义删除完成后，暂时无法清理的发布历史不得继续对外可见。

    参数：``tmp_path`` 隔离领域包、运行事实库和故障文件。返回：无。异常：发布
    文件故障让删除表面失败、合同仍可查询或重启后恢复孤儿定义时由断言暴露。
    """

    selected_root = tmp_path / "domain"
    package_root = _empty_domain_package(selected_root)
    database_path = tmp_path / "workflow_history.db"
    service, _runtime_store, _definitions = _service(
        database_path=database_path,
        selected_root=selected_root,
    )
    publication_path = package_root / "workflow_publications.json"
    try:
        imported = service.import_python_workflow(
            file_name="published_operation.py",
            python_source=_source().replace(
                '    description="Prepare and analyze one sample.",\n',
                '    description="Prepare and analyze one sample.",\n'
                '    workflow_type="experiment_operation",\n',
            ),
        )
        service.publish_workflow_contract(
            WORKFLOW_UUID,
            revision=int(imported["workflow"]["revision"]),
        )
        stale_publications = publication_path.read_bytes()
        publication_path.unlink()
        publication_path.mkdir()

        service.delete_workflow(WORKFLOW_UUID)

        with pytest.raises(WorkflowError) as deleted:
            service.get_workflow(WORKFLOW_UUID)
        assert deleted.value.code == "not_found"
        assert service.list_published_workflow_contracts()["items"] == []
        assert (
            yaml.safe_load(
                selected_root.joinpath("package.yaml").read_text(encoding="utf-8")
            )["workflows"]
            == []
        )
    finally:
        service.close()

    publication_path.rmdir()
    publication_path.write_bytes(stale_publications)
    reopened, _reopened_runtime, reopened_definitions = _service(
        database_path=database_path,
        selected_root=selected_root,
    )
    try:
        with pytest.raises(WorkflowError) as deleted:
            reopened.get_workflow(WORKFLOW_UUID)
        assert deleted.value.code == "not_found"
        assert reopened.list_published_workflow_contracts()["items"] == []
        assert reopened_definitions.count_rows("workflow") == 0
    finally:
        reopened.close()


def test_python_import_identity_conflict_does_not_publish_manifest(
    tmp_path: Path,
) -> None:
    """内存定义已占用 UUID 时，失败导入不得提前登记领域源码。"""

    selected_root = tmp_path / "domain"
    package_root = _empty_domain_package(selected_root)
    service, _runtime_store, definitions = _service(
        database_path=tmp_path / "workflow_history.db",
        selected_root=selected_root,
    )
    definitions.create_workflow(
        workflow_uuid=WORKFLOW_UUID,
        name="既有内存工作流",
        tags=[],
        description=None,
        meta_data={},
    )
    try:
        with pytest.raises(WorkflowConflict):
            service.import_python_workflow(
                file_name="conflicting_workflow.py",
                python_source=_source(),
            )
        assert (
            yaml.safe_load(
                selected_root.joinpath("package.yaml").read_text(encoding="utf-8")
            )["workflows"]
            == []
        )
        assert not package_root.joinpath(
            "workflows", "conflicting_workflow.py"
        ).exists()
    finally:
        service.close()


@pytest.mark.parametrize(
    ("publication_error", "expected_code"),
    [
        (source_publication.SourcePublicationError, "source_publication_failed"),
        (source_publication.SourcePublicationConflict, "source_identity_conflict"),
    ],
)
def test_python_import_manifest_failure_rolls_back_new_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    publication_error: type[RuntimeError],
    expected_code: str,
) -> None:
    """manifest 发布失败时必须清理本次新建源码且保留旧 manifest。

    参数：``tmp_path`` 隔离领域包与运行事实；``monkeypatch`` 注入 manifest
    发布故障；``publication_error`` 是基础设施故障或并发冲突；``expected_code``
    是导入接口的稳定错误码。返回：无。异常：若失败后留下 Python 源码、修改
    manifest 或把冲突误报为未定义异常，由断言暴露。
    """

    selected_root = tmp_path / "domain"
    package_root = _empty_domain_package(selected_root)
    service, _runtime_store, definitions = _service(
        database_path=tmp_path / "workflow_history.db",
        selected_root=selected_root,
    )
    original_manifest = selected_root.joinpath("package.yaml").read_bytes()

    def fail_manifest_publish(**_kwargs: object) -> None:
        """仅拒绝 manifest 的最终替换，模拟源码已写入后的失败窗口。"""

        raise publication_error("injected_manifest_failure")

    monkeypatch.setattr(
        domain_source_target,
        "atomic_publish_source",
        fail_manifest_publish,
    )
    source_path = package_root / "workflows" / "failed_manifest.py"
    try:
        with pytest.raises(WorkflowError) as failed:
            service.import_python_workflow(
                file_name="failed_manifest.py",
                python_source=_source(),
            )
        assert failed.value.code == expected_code
        assert selected_root.joinpath("package.yaml").read_bytes() == original_manifest
        assert not source_path.exists()
        assert definitions.count_rows("workflow") == 0
    finally:
        service.close()


def test_python_import_rolls_back_manifest_when_activation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """源码发布后的激活失败应补偿 manifest 和内存定义，不留下假导入。"""

    selected_root = tmp_path / "domain"
    package_root = _empty_domain_package(selected_root)
    service, _runtime_store, definitions = _service(
        database_path=tmp_path / "workflow_history.db",
        selected_root=selected_root,
    )
    publish = service._publish_imported_domain_workflow

    def fail_activation(**_kwargs: object) -> dict[str, object]:
        raise WorkflowError("internal_error")

    monkeypatch.setattr(
        service,
        "_publish_imported_domain_workflow",
        fail_activation,
    )
    try:
        with pytest.raises(WorkflowError) as failed:
            service.import_python_workflow(
                file_name="failed_activation.py",
                python_source=_source(),
            )
        assert failed.value.code == "internal_error"
        assert (
            yaml.safe_load(
                selected_root.joinpath("package.yaml").read_text(encoding="utf-8")
            )["workflows"]
            == []
        )
        assert definitions.count_rows("workflow") == 0
        assert package_root.joinpath("workflows", "failed_activation.py").is_file()

        monkeypatch.setattr(
            service,
            "_publish_imported_domain_workflow",
            publish,
        )
        retried = service.import_python_workflow(
            file_name="failed_activation.py",
            python_source=_source(),
        )
        assert retried["workflow"]["uuid"] == WORKFLOW_UUID
        assert retried["workflow"]["revision"] == 1
        assert service.get_authoring(WORKFLOW_UUID)["state"] == "applied"
    finally:
        service.close()
