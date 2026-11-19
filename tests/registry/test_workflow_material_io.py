"""``ctx.material_io``：人工装入 / 取走 / 补量步骤的作者写法、编译、整图校验与节点模板。

确认、锁定与库存写入的运行时行为见 ``tests/server/test_material_io_scheduler.py``。
"""

from __future__ import annotations

import textwrap
from uuid import uuid4

import pytest

from unilabos.registry.workflow_ast import WorkflowSourceError, compile_workflow_source
from unilabos.registry.workflows import (
    DeviceCatalog,
    WorkflowBuildContext,
    build_workflow_template_payload,
    materialize_workflow_template,
    upsert_workflow,
)

MOUNT = "11111111-0000-4000-8000-0000000000aa"
BOTTLE = "11111111-0000-4000-8000-0000000000bb"

SOURCE = textwrap.dedent(f'''
    from unilabos.registry.workflows import workflow


    @workflow("换盒与补量")
    def flow(ctx, *, volume: float = 20.0):
        loaded = ctx.material_io(mode="load", mount="deck", site="A1", resource_template="tip_box",
                                 material_name="新吸头盒", barcode="TB-001")
        ctx.run("robot/pick", {{"box": loaded.material}})
        ctx.material_io(mode="refill", mount={{"uuid": "{MOUNT}"}}, site="B1", material="{BOTTLE}",
                        reagent={{"quantity": volume, "quantity_unit": "mL"}}, timeout_seconds=600,
                        assignee_user_ids=["alice", "alice", " bob "])
        ctx.material_io(mode="unload", mount="deck", site="A1", material=loaded.material,
                        disposition="soft_delete", scope="material_with_contents", name="丢弃旧盒")
''')


def _compile(source: str):
    return compile_workflow_source(source).ctx


def test_three_modes_compile_with_references_and_defaults() -> None:
    ctx = _compile(SOURCE)
    load, pick, refill, unload = ctx.steps
    assert [step.kind for step in ctx.steps] == ["material_io", "run", "material_io", "material_io"]
    assert (load.name, refill.name, unload.name) == ("人工装入", "人工补量", "丢弃旧盒")
    assert load.params == {
        "mode": "load", "mount": "deck", "site": "A1", "resource_template": "tip_box",
        "material_name": "新吸头盒", "barcode": "TB-001", "assignee_user_ids": [],
    }
    # 后面的动作与卸载步骤引用新物料，和动作结果引用一样是 node_output
    assert pick.params == {"box": {"kind": "node_output", "node_key": load.key, "data_key": "material"}}
    assert unload.params["material"] == {"kind": "node_output", "node_key": load.key, "data_key": "material"}
    assert (unload.params["disposition"], unload.params["scope"]) == ("soft_delete", "material_with_contents")
    # 补量的数量可以是工作流输入；物料 UUID 写成 {"uuid"}；确认人去空白去重
    assert refill.params["reagent"] == {"quantity": {"kind": "workflow_input", "parameter": "volume"},
                                        "quantity_unit": "mL"}
    assert refill.params["mount"] == {"uuid": MOUNT} and refill.params["material"] == BOTTLE
    assert (refill.params["timeout_seconds"], refill.params["assignee_user_ids"]) == (600, ["alice", "bob"])
    assert unload.dependencies == [refill.key] and refill.dependencies == [pick.key]


def test_materialized_nodes_are_material_io_with_uuid_references() -> None:
    ctx = _compile(SOURCE)
    definition = type("Definition", (), {
        "collect": lambda self: ctx, "source_path": "test:material_io", "uuid": str(uuid4()),
        "display_name": "换盒与补量", "description": "", "tags": [], "guide": None,
    })()
    catalog = DeviceCatalog()
    catalog.add("robot", "arm", str(uuid4()))
    payload = materialize_workflow_template(build_workflow_template_payload(definition), catalog)
    load, pick, refill, unload = payload["nodes"]
    assert [node["type"] for node in payload["nodes"]] == ["material_io", "device_action", "material_io", "material_io"]
    assert "material_uuid" not in load and load["meta_data"]["source_order"] == 0
    assert pick["param"]["box"] == {"kind": "node_output", "workflow_node_uuid": load["uuid"], "data_key": "material"}
    assert unload["param"]["material"]["workflow_node_uuid"] == load["uuid"]
    assert unload["execution_policy"]["depends_on"] == [refill["uuid"]]


@pytest.mark.parametrize(("arguments", "message"), [
    ('mode="move", mount="deck", site="A1"', "mode 必须是 load、unload 或 refill"),
    ('mode="load", site="A1", resource_template="tip_box"', "mount 必填"),
    ('mode="load", mount="deck", resource_template="tip_box"', "site 必填"),
    ('mode="load", mount="deck", site="A1"', "load 必须声明 resource_template"),
    ('mode="load", mount="deck", site="A1", resource_template="t", material="x"', "load 不能声明 material"),
    ('mode="unload", mount="deck", site="A1", disposition="detach", scope="material_only"', "unload 必须声明 material"),
    ('mode="unload", mount="deck", site="A1", material="x", scope="material_only"', "disposition 必须是 detach 或 soft_delete"),
    ('mode="unload", mount="deck", site="A1", material="x", disposition="detach", scope="material_with_contents"',
     "detach 只支持 material_only"),
    ('mode="unload", mount="deck", site="A1", material="x", disposition="detach", scope="material_only", '
     'reagent={"quantity": 1, "quantity_unit": "mL"}', "unload 不能声明 reagent"),
    ('mode="refill", mount="deck", site="A1", material="x"', "refill 必须声明 reagent"),
    ('mode="refill", mount="deck", site="A1", material="x", reagent={"quantity": 1, "quantity_unit": "mL", "name": "水"}',
     "refill 的 reagent 只能有 quantity 和 quantity_unit"),
    ('mode="refill", mount="deck", site="A1", material="x", reagent={"quantity": 0, "quantity_unit": "mL"}',
     "reagent.quantity 必须是正数"),
    ('mode="refill", mount="deck", site="A1", material="x", resource_template="t", '
     'reagent={"quantity": 1, "quantity_unit": "mL"}', "refill 不能声明 resource_template"),
    ('mode="load", mount="deck", site="A1", resource_template="t", reagent={"quantity": 1, "quantity_unit": "mL"}',
     "reagent_info_uuid、name 或 cas 之一"),
    ('mode="load", mount="deck", site="A1", resource_template="t", '
     f'reagent={{"quantity": 1, "quantity_unit": "mL", "name": "水", "reagent_info_uuid": "{BOTTLE}"}}',
     "reagent_info_uuid 不能与 name / cas 同时给出"),
    ('mode="load", mount="deck", site="A1", resource_template="t", barcode=" B1"', "barcode 必须是无首尾空白"),
    ('mode="load", mount="deck", site="A1", resource_template="t", timeout_seconds=0', "timeout_seconds 必须是 1..86400"),
    ('mode="load", mount="deck", site="A1", resource_template="t", timeout_seconds=86401', "1..86400"),
    ('mode="load", mount="deck", site="A1", resource_template="t", assignee_user_ids=[1]', "assignee_user_ids 必须是字符串列表"),
    ('mode="load", mount="deck", site="A1", resource_template="t", shelf="x"', "ctx.material_io 的参数不对"),
    ('"load", mount="deck", site="A1", resource_template="t"', "只接受命名参数"),
])
def test_publish_rules_per_mode_are_source_errors(arguments: str, message: str) -> None:
    source = f"def flow(ctx):\n    ctx.material_io({arguments})\n"
    with pytest.raises(WorkflowSourceError, match=message) as caught:
        compile_workflow_source(source, function_name="flow")
    assert caught.value.line == 2


def test_material_io_result_reference_must_come_from_an_earlier_step() -> None:
    ctx = WorkflowBuildContext()
    with pytest.raises(ValueError, match="并行分支不能读取兄弟分支的结果"):
        with ctx.parallel():
            loaded = ctx.material_io(mode="load", mount="deck", site="A1", resource_template="t")
            ctx.material_io(mode="unload", mount="deck", site="A1", material=loaded.output("material"),
                            disposition="detach", scope="material_only")


def _service():
    from unilabos.server.services.runtime.workflow.service import WorkflowService

    service = WorkflowService(":memory:")
    service.set_task_submitter(lambda _task_uuid: None)
    return service


def _node(**param) -> dict:
    return {"uuid": str(uuid4()), "name": "人工装入", "type": "material_io",
            "param": {"assignee_user_ids": [], **param}}


def test_graph_save_validates_material_io_and_plans_a_material_io_run() -> None:
    from unilabos.server.services.runtime.workflow.service import WorkflowError

    service = _service()
    try:
        bad = _node(mode="load", mount={"uuid": MOUNT}, site="A1")
        with pytest.raises(WorkflowError, match="load 必须声明 resource_template"):
            upsert_workflow(service, {"workflow_uuid": str(uuid4()), "name": "坏", "description": "", "tags": [],
                                      "nodes": [bad], "edges": []})
        missing_assignees = {**_node(mode="load", mount={"uuid": MOUNT}, site="A1", resource_template="t")}
        missing_assignees["param"].pop("assignee_user_ids")
        with pytest.raises(WorkflowError, match="assignee_user_ids 必须显式给出"):
            upsert_workflow(service, {"workflow_uuid": str(uuid4()), "name": "坏", "description": "", "tags": [],
                                      "nodes": [missing_assignees], "edges": []})
        good = _node(mode="load", mount={"uuid": MOUNT}, site="A1", resource_template="tip_box")
        record = upsert_workflow(service, {"workflow_uuid": str(uuid4()), "name": "好", "description": "", "tags": [],
                                           "nodes": [good], "edges": []})
        task = service.create_workflow_task(workflow_uuid=record["uuid"], run_mode="normal", target_node_uuid=None,
                                            input_value={}, description=None, meta_data={})
        [run] = service.list_workflow_node_runs(task["uuid"])
        assert run["executor_kind"] == "material_io" and run.get("material_uuid") is None
        assert run["param"]["resource_template"] == "tip_box"
    finally:
        service.close()


def test_structural_template_projects_material_io_with_ready_handles() -> None:
    from unilabos.registry.template_identity import action_template_uuid
    from unilabos.server.services.runtime.workflow.node_templates import DEFAULT_VISIBLE_NODE_TYPES, structural_templates

    templates, handles = structural_templates({"uuid": str(uuid4()), "name": "host_node", "display_name": "宿主"})
    [template] = [item for item in templates if item["name"] == "material_io"]
    assert template["uuid"] == action_template_uuid("host_node", "material_io")
    assert template["node_type"] == "material_io" and "material_io" in DEFAULT_VISIBLE_NODE_TYPES
    unilab = template["meta_data"]["unilab"]
    assert unilab["executor_kind"] == "material_io"
    assert unilab["parameter_schema"]["properties"]["mode"]["enum"] == ["load", "unload", "refill"]
    assert template["goal_default"] == {"mode": "load", "timeout_seconds": 3600, "assignee_user_ids": []}
    assert {(item["io_type"], item["handle_key"]) for item in handles
            if item["workflow_node_template_uuid"] == template["uuid"]} == {("target", "ready"), ("source", "ready")}


def test_generate_python_round_trips_material_io_nodes() -> None:
    from unilabos.server.services.runtime.workflow.authoring_compiler import WorkflowAuthoringCompiler

    service = _service()
    try:
        load = _node(mode="load", mount={"uuid": MOUNT}, site="A1", resource_template="tip_box", material_name="盒")
        unload = {"uuid": str(uuid4()), "name": "收走", "type": "material_io",
                  "execution_policy": {"depends_on": [load["uuid"]]},
                  "param": {"mode": "unload", "mount": {"uuid": MOUNT}, "site": "A1", "assignee_user_ids": ["alice"],
                            "material": {"kind": "node_output", "workflow_node_uuid": load["uuid"], "data_key": "material"},
                            "disposition": "detach", "scope": "material_only", "timeout_seconds": 120}}
        record = upsert_workflow(service, {"workflow_uuid": str(uuid4()), "name": "往返", "description": "", "tags": [],
                                           "nodes": [load, unload], "edges": []})
        graph = service.get_graph(record["uuid"])
        compiler = WorkflowAuthoringCompiler(catalog_provider=DeviceCatalog, registry_fingerprint=lambda: "r1")
        generated = compiler.generate_python(workflow_uuid=record["uuid"], workflow_revision=graph["workflow"]["revision"],
                                             graph=graph, source_uri="package://local/workflows/x.py")
        assert generated["diagnostics"] == [], generated["diagnostics"]
        assert generated["changeset"]["kind"] == "source_only"
        source = generated["normalized_python_source"]
        assert "step_1 = ctx.material_io(mode=\"load\"" in source
        assert "material=step_1.material" in source and 'name="收走"' in source
    finally:
        service.close()
