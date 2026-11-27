"""苏州补料回归：保存、作者编译与执行计划使用同一可执行性口径。

覆盖：
- ``assert_graph_executable`` 只拦截纯图结构类计划错误，模板目录/运行环境类错误留给建任务；
- 计划层新规则：条件互斥分支之间的数据边、条件表达式变量与 bindings 不一致；
- 补料来源输出作动态 mount 的子来源只能位于有料分支；
- 公开保存（无源码与源码托管两条路径）拒绝不可执行图，给出具体原因、业务码 1000，
  且当前图与修订不变；条件 bindings 形状损坏返回业务错误而不是 500；
- 作者编译诊断与保存一致；源码托管定义原样回存直接返回当前图。
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from tests.app.test_python_workflow_import_api import _client, _upload
from tests.workflow.test_f05_material_source_authoring import (
    MATERIAL_SOURCE_NODE_UUID,
    PLATE_SOURCE_SYMBOL,
)
from tests.workflow.test_quantity_runtime_fixes import HEADER, Harness, act, cond, nid, src
from tests.workflow.test_runtime_material_replenishment import (
    BACKUP,
    REFILL,
    _saved_refill_service,
    compiled_refill,
)
from tests.workflow.test_structured_condition_authoring import CONDITION_NODE_UUID
from unilabos.app.workflow_api import _business_code, create_workflow_app
from unilabos.workflow._execution_plan_graph import ExecutionPlanBuildError
from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.authoring_kernel import AuthoringCatalogSnapshot
from unilabos.workflow.execution_plan import ExecutionPlanBuilder
from unilabos.workflow.service import WorkflowError

DIRECT = "20000000-0000-4000-8000-000000000011"


def _node(graph: dict[str, Any], uuid: str) -> dict[str, Any]:
    return next(node for node in graph["nodes"] if node["uuid"] == uuid)


def _plain_condition(graph: dict[str, Any]) -> None:
    """关闭来源的支持补料，使条件成为普通条件区域（不受补料规则约束）。"""

    _node(graph, MATERIAL_SOURCE_NODE_UUID)["param"].pop("supports_replenishment", None)


def _cross_branch_edge(graph: dict[str, Any]) -> None:
    """有料分支动作改读缺料分支来源的输出（两条边互换目标，不产生多 Provider）。"""

    _plain_condition(graph)
    primary = next(e for e in graph["edges"] if e["source_node_uuid"] == MATERIAL_SOURCE_NODE_UUID)
    backup = next(e for e in graph["edges"] if e["source_node_uuid"] == BACKUP)
    primary["target_node_uuid"], backup["target_node_uuid"] = backup["target_node_uuid"], primary["target_node_uuid"]


def _extra_binding(graph: dict[str, Any]) -> None:
    """条件 bindings 多出表达式里没有的变量。"""

    _node(graph, CONDITION_NODE_UUID)["param"]["bindings"]["extra"] = {
        "kind": "node_result", "node_uuid": MATERIAL_SOURCE_NODE_UUID}


def _empty_bindings(graph: dict[str, Any]) -> None:
    """普通条件的表达式变量没有任何绑定。"""

    _plain_condition(graph)
    _node(graph, CONDITION_NODE_UUID)["param"]["bindings"] = {}


def _build(graph: dict[str, Any]) -> None:
    ExecutionPlanBuilder().build(deepcopy(graph), run_mode="normal", target_node_uuid=None)


def _put(service, workflow_uuid: str, revision: int, graph: dict[str, Any]):
    with TestClient(create_workflow_app(service), raise_server_exceptions=False) as client:
        return client.put(f"/api/v1/workflows/{workflow_uuid}/graph",
                          json={"revision": revision, "nodes": graph["nodes"], "edges": graph["edges"]})


# ---------------------------------------------------------------- 1. 结构类错误口径

@pytest.mark.parametrize("plan_code,public_code", [
    ("workflow_cycle", "graph_not_executable"),
    ("invalid_execution_graph", "graph_not_executable"),
    ("dynamic_material_source_mount_requires_runtime", "graph_not_executable"),
    ("invalid_control_region", "invalid_control_region"),
    ("MATERIAL_SHORTAGE_UNHANDLED", "material_shortage_unhandled"),
])
def test_structural_plan_errors_block_with_public_code(monkeypatch, plan_code: str, public_code: str) -> None:
    from unilabos.workflow.graph_executability import GraphNotExecutable, assert_graph_executable

    def reject(self, graph, **_kwargs):
        raise ExecutionPlanBuildError(plan_code, f"具体原因：{plan_code}")

    monkeypatch.setattr(ExecutionPlanBuilder, "build", reject)
    with pytest.raises(GraphNotExecutable) as raised:
        assert_graph_executable({"nodes": [{"uuid": "n"}]}, workflow_uuid="wf")
    assert raised.value.code == public_code
    assert raised.value.message == f"具体原因：{plan_code}"


@pytest.mark.parametrize("plan_code", [
    "invalid_param_schema", "invalid_resource_plan", "invalid_executor_binding",
    "invalid_device_selector", "invalid_action_contract", "composite_boundary_mapping_invalid",
])
def test_environment_dependent_plan_errors_do_not_block_save(monkeypatch, plan_code: str) -> None:
    """模板目录/运行环境/任务输入类错误由建任务与预检按真实环境判断，保存时不拦。"""

    from unilabos.workflow.graph_executability import assert_graph_executable

    def reject(self, graph, **_kwargs):
        raise ExecutionPlanBuildError(plan_code, "依赖环境的错误")

    monkeypatch.setattr(ExecutionPlanBuilder, "build", reject)
    assert assert_graph_executable({"nodes": [{"uuid": "n"}]}, workflow_uuid="wf") is None
    assert assert_graph_executable({"nodes": []}, workflow_uuid="wf") is None


# ---------------------------------------------------------------- 2. 计划层新规则

def test_plan_rejects_data_edge_between_mutually_exclusive_branches() -> None:
    _, compiled = compiled_refill()
    graph = deepcopy(compiled.graph)
    _build(graph)
    _cross_branch_edge(graph)
    with pytest.raises(ExecutionPlanBuildError) as raised:
        _build(graph)
    assert raised.value.code == "invalid_execution_graph"
    assert DIRECT in str(raised.value) and BACKUP in str(raised.value)
    assert "互斥分支" in str(raised.value)


@pytest.mark.parametrize("mutate", [_extra_binding, _empty_bindings], ids=["extra_binding", "empty_bindings"])
def test_plan_rejects_condition_bindings_that_differ_from_expression_variables(mutate) -> None:
    _, compiled = compiled_refill()
    graph = deepcopy(compiled.graph)
    mutate(graph)
    with pytest.raises(ExecutionPlanBuildError) as raised:
        _build(graph)
    assert raised.value.code == "invalid_control_region"
    assert "条件变量与绑定不一致" in str(raised.value)


# ---------------------------------------------------------------- 3. 补料来源作动态 mount

@pytest.mark.parametrize("child_node,allowed", [(DIRECT, True), (BACKUP, False)], ids=["ready_branch", "shortage_branch"])
def test_runtime_mount_child_of_replenishing_source_must_be_in_ready_branch(child_node: str, allowed: bool) -> None:
    from unilabos.workflow.runtime_material_source import validate_replenishment_plan

    _, compiled = compiled_refill()
    plan, _ = ExecutionPlanBuilder().build(compiled.graph, run_mode="normal", target_node_uuid=None)
    nodes = deepcopy(plan["nodes"])
    next(n for n in nodes if n["uuid"] == child_node)["runtime_mount_binding"] = {
        "source_node_uuid": MATERIAL_SOURCE_NODE_UUID}
    if allowed:
        validate_replenishment_plan(nodes)
        return
    with pytest.raises(ExecutionPlanBuildError) as raised:
        validate_replenishment_plan(nodes)
    assert raised.value.code == "MATERIAL_SHORTAGE_UNHANDLED"
    assert "sufficient=true 分支作为挂载位" in str(raised.value)


# ---------------------------------------------------------------- 1/2/5. 无源码的公开保存

@pytest.mark.parametrize("mutate,code,reason", [
    (_cross_branch_edge, "graph_not_executable", "互斥分支不会同时执行"),
    (_extra_binding, "invalid_control_region", "条件变量与绑定不一致"),
    (_empty_bindings, "invalid_control_region", "条件变量与绑定不一致"),
], ids=["cross_branch_edge", "extra_binding", "empty_bindings"])
def test_save_rejects_plan_invalid_graph_and_keeps_current_graph(tmp_path, mutate, code: str, reason: str) -> None:
    service, workflow_uuid, saved = _saved_refill_service(tmp_path)
    try:
        candidate = deepcopy(saved)
        mutate(candidate)
        revision = saved["workflow"]["revision"]
        with pytest.raises(WorkflowError) as raised:
            service.save_graph(workflow_uuid, revision=revision, nodes=candidate["nodes"], edges=candidate["edges"])
        assert raised.value.code == code
        assert reason in raised.value.message
        response = _put(service, workflow_uuid, revision, candidate)
        body = response.json()
        assert response.status_code == 200 and body["code"] == 1000, body
        assert reason in body["error"]["msg"]
        assert service.get_graph(workflow_uuid) == saved
        _build(service.get_graph(workflow_uuid))
    finally:
        service.close()


@pytest.mark.parametrize("bindings", ["x", {"plate": "x"}], ids=["bindings_not_object", "binding_not_object"])
def test_malformed_condition_bindings_on_refill_graph_is_business_rejection(tmp_path, bindings) -> None:
    """补料图条件 bindings 形状损坏：业务错误 invalid_control_region，HTTP 不是 500，零写入。"""

    service, workflow_uuid, saved = _saved_refill_service(tmp_path)
    try:
        candidate = deepcopy(saved)
        _node(candidate, CONDITION_NODE_UUID)["param"]["bindings"] = deepcopy(bindings)
        revision = saved["workflow"]["revision"]
        with pytest.raises(WorkflowError) as raised:
            service.save_graph(workflow_uuid, revision=revision, nodes=candidate["nodes"], edges=candidate["edges"])
        assert raised.value.code == "invalid_control_region"
        assert "条件节点的绑定或分支结构无效" in raised.value.message
        response = _put(service, workflow_uuid, revision, candidate)
        assert response.status_code == 200, response.text
        assert response.json()["code"] == 1000
        assert service.get_graph(workflow_uuid) == saved
    finally:
        service.close()


def test_generic_graph_validation_failure_reports_concrete_reason(tmp_path) -> None:
    """普通图校验失败映射为 invalid_input + 具体原因，不再是通用“请求参数不符合”。"""

    service, workflow_uuid, saved = _saved_refill_service(tmp_path)
    try:
        candidate = deepcopy(saved)
        candidate["edges"] = [e for e in candidate["edges"] if e["source_node_uuid"] != BACKUP]
        revision = saved["workflow"]["revision"]
        with pytest.raises(WorkflowError) as raised:
            service.save_graph(workflow_uuid, revision=revision, nodes=candidate["nodes"], edges=candidate["edges"])
        assert raised.value.code == "invalid_input"
        assert "'plate'" in raised.value.message
        body = _put(service, workflow_uuid, revision, candidate).json()
        assert body["code"] == 1000 and "'plate'" in body["error"]["msg"], body
        assert service.get_graph(workflow_uuid) == saved
    finally:
        service.close()


@pytest.mark.parametrize("code", [
    "graph_not_executable", "invalid_material_source", "material_flow_fan_out", "material_template_mismatch",
    "invalid_control_region", "material_shortage_unhandled",
])
def test_authoring_rejections_use_business_code_1000(code: str) -> None:
    assert _business_code(code) == 1000


# ---------------------------------------------------------------- 1/3. 作者编译与保存同口径

def _catalog_ids() -> dict[str, str]:
    return {key: str(uuid4()) for key in
            ("template", "container_template", "device_template", "WORK", "SPARE", "TARGET", "OTHER")}


def _engine(ids: dict[str, str]) -> WorkflowAuthoringEngine:
    """复用数量回归目录；模板 schema 以 JSON 文本给出，满足源码导入的读取合同。"""

    harness = Harness(None, None)
    harness.d = ids
    catalog = harness.engine()._catalog
    templates = [action.detached_template() for action in catalog.actions]
    handles = [handle for action in catalog.actions for handle in action.detached_handles()]
    for template in templates:
        if isinstance(template.get("schema"), dict):
            template["schema"] = json.dumps(template["schema"], ensure_ascii=False)
    return WorkflowAuthoringEngine(catalog=AuthoringCatalogSnapshot.from_entities(
        templates, handles, resource_template_symbols={
            PLATE_SOURCE_SYMBOL: ids["template"], "lab.resources:box_resource": ids["container_template"]}))


def _child_of_original(ids: dict[str, str]) -> str:
    return src("child", "child", ids["WORK"], level=2).replace(f'mount=resource_ref("{ids["WORK"]}")', "mount=original")


def _refill_source(ids: dict[str, str], workflow_uuid: str, variant: str, *, support: bool = True) -> str:
    body = src("original", "original", ids["WORK"], support=support)
    if variant == "shortage_consumes_original":
        body += src("spare0", "spare0", ids["SPARE"])
    body += cond("cond", "original.sufficient")
    body += {
        "legal": act("used", "used", "original", 2) + "    else:\n"
        + src("spare", "spare", ids["SPARE"], level=2) + act("alt", "alt", "spare", 2),
        "legal_child_in_ready_branch": _child_of_original(ids) + act("used", "used", "child", 2) + "    else:\n"
        + src("spare", "spare", ids["SPARE"], level=2) + act("alt", "alt", "spare", 2),
        "shortage_consumes_original": act("used", "used", "spare0", 2) + "    else:\n"
        + act("alt", "alt", "original", 2),
        "child_mounted_in_shortage_branch": act("used", "used", "original", 2) + "    else:\n"
        + _child_of_original(ids) + act("alt", "alt", "child", 2),
    }[variant]
    return HEADER.replace("WORKFLOW_UUID", workflow_uuid) + body + "    return workflow_output()\n"


def _compile(engine: WorkflowAuthoringEngine, text: str, workflow_uuid: str):
    applied = {"workflow": {"uuid": workflow_uuid, "name": "x", "tags": [], "description": None, "meta_data": {},
                            "revision": 1}, "nodes": [], "edges": [], "node_templates": [], "handle_templates": []}
    return engine.compile(workflow_uuid=workflow_uuid, workflow_revision=1, python_source=text,
                          source_uri="memory://suzhou.py", applied_graph=applied)


@pytest.mark.parametrize("variant,reason", [
    ("legal", None),
    ("legal_child_in_ready_branch", None),
    ("shortage_consumes_original", "只能在 sufficient=true 分支使用"),
    ("child_mounted_in_shortage_branch", "sufficient=true 分支作为挂载位"),
])
def test_authoring_compile_diagnostics_match_execution_plan(variant: str, reason: str | None) -> None:
    ids = _catalog_ids()
    workflow_uuid = str(uuid4())
    result = _compile(_engine(ids), _refill_source(ids, workflow_uuid, variant), workflow_uuid)
    if reason is None:
        assert result.valid, result.diagnostics
        _build(result.graph)
        return
    assert not result.valid
    errors = [d for d in result.diagnostics if d.get("severity") == "error"]
    assert [str(d.get("code")).lower() for d in errors] == ["material_shortage_unhandled"], errors
    assert reason in errors[0]["message"]


# ---------------------------------------------------------------- 1/7. 源码托管定义

class _SourceBacked:
    def __init__(self, tmp_path, *, support: bool = True, variant: str = "legal") -> None:
        self.ids = _catalog_ids()
        self.workflow_uuid = str(uuid4())
        self.client, self.service, _ = _client(tmp_path, compiler=_engine(self.ids))
        self.source = _refill_source(self.ids, self.workflow_uuid, variant, support=support)
        self.response = _upload(self.client, self.source, file_name="suzhou_refill.py")

    def source_state(self) -> tuple[Any, ...]:
        """源码托管定义的源码侧事实：草稿、已应用源码、候选与状态。"""

        authoring = self.service.get_authoring(self.workflow_uuid)
        return (authoring["draft"], authoring["applied_source"], authoring["candidate"], authoring["state"])

    def close(self) -> None:
        self.client.close()
        self.service.close()


@pytest.mark.parametrize("support", [True, False], ids=["refill", "plain"])
def test_source_backed_unchanged_resave_returns_current_graph(tmp_path, support: bool) -> None:
    scene = _SourceBacked(tmp_path, support=support)
    try:
        assert scene.response.json()["code"] == 0, scene.response.json()
        before = scene.service.get_graph(scene.workflow_uuid)
        authoring = scene.source_state()
        response = scene.client.put(f"/api/v1/workflows/{scene.workflow_uuid}/graph", json={
            "revision": before["workflow"]["revision"], "nodes": before["nodes"], "edges": before["edges"]})
        body = response.json()
        assert body["code"] == 0, body
        assert body["data"]["workflow"]["revision"] == before["workflow"]["revision"]
        assert scene.service.get_graph(scene.workflow_uuid) == before
        assert scene.source_state() == authoring
    finally:
        scene.close()


def test_source_backed_save_rejects_cross_branch_edge_with_reason(tmp_path) -> None:
    """源码托管图保存走预演事务：计划不可执行时拒绝并保留当前图与源码。"""

    scene = _SourceBacked(tmp_path, support=False)
    try:
        before = scene.service.get_graph(scene.workflow_uuid)
        authoring = scene.source_state()
        candidate = deepcopy(before)
        original = next(e for e in candidate["edges"] if e["source_node_uuid"] == nid("original")
                        and e["target_node_uuid"] == nid("used"))
        spare = next(e for e in candidate["edges"] if e["source_node_uuid"] == nid("spare")
                     and e["target_node_uuid"] == nid("alt"))
        original["target_node_uuid"], spare["target_node_uuid"] = spare["target_node_uuid"], original["target_node_uuid"]
        body = scene.client.put(f"/api/v1/workflows/{scene.workflow_uuid}/graph", json={
            "revision": before["workflow"]["revision"], "nodes": candidate["nodes"],
            "edges": candidate["edges"]}).json()
        assert body["code"] == 1000, body
        assert "互斥分支不会同时执行" in body["error"]["msg"]
        assert scene.service.get_graph(scene.workflow_uuid) == before
        assert scene.source_state() == authoring
    finally:
        scene.close()


@pytest.mark.parametrize("variant,reason", [
    ("shortage_consumes_original", "只能在 sufficient=true 分支使用"),
    ("child_mounted_in_shortage_branch", "sufficient=true 分支作为挂载位"),
])
def test_python_import_of_plan_invalid_source_leaves_no_workflow(tmp_path, variant: str, reason: str) -> None:
    scene = _SourceBacked(tmp_path, variant=variant)
    try:
        body = scene.response.json()
        assert body["code"] != 0, body
        assert reason in body["error"]["msg"], body
        assert scene.service.list_workflows(page=1, page_size=50)["total"] == 0
    finally:
        scene.close()


def test_saved_refill_fixture_is_plan_valid(tmp_path) -> None:
    """对照：上面各变体的基图本身可保存、可生成计划。"""

    service, workflow_uuid, saved = _saved_refill_service(tmp_path)
    try:
        _build(saved)
        assert {BACKUP, REFILL, DIRECT} <= {node["uuid"] for node in saved["nodes"]}
    finally:
        service.close()
