"""@workflow 装饰器 + 模板载荷 + 按绑定实例化 + 端到端调度合同测试。"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import pytest

from unilabos.registry.ast_registry_scanner import _parse_file
from unilabos.registry.workflows import (
    DeviceCatalog,
    WorkflowBuildContext,
    WorkflowTemplateBindingError,
    build_workflow_template_payload,
    class_role_id,
    clear_registered_workflows,
    get_registered_workflows,
    instantiated_workflow_uuid,
    materialize_workflow_template,
    upsert_workflow,
    workflow,
    workflow_uuid_for,
    _step_node_uuid,
)
from unilabos.server.services.runtime.workflow.service import WorkflowService


@pytest.fixture(autouse=True)
def _isolated_workflow_registry():
    """每个用例独立的进程内注册表。"""

    snapshot = get_registered_workflows()
    clear_registered_workflows()
    yield
    clear_registered_workflows()
    for definition in snapshot.values():
        from unilabos.registry.workflows import _registered_workflows

        _registered_workflows[definition.uuid] = definition


def _catalog() -> DeviceCatalog:
    catalog = DeviceCatalog()
    catalog.add("device-1", "demo_class", str(uuid.uuid4()))
    catalog.add("dup-a", "dup_class", str(uuid.uuid4()))
    catalog.add("dup-b", "dup_class", str(uuid.uuid4()))
    return catalog


def test_workflow_uuid_is_stable_and_path_scoped() -> None:
    assert workflow_uuid_for("pkg.mod:flow") == workflow_uuid_for("pkg.mod:flow")
    assert workflow_uuid_for("pkg.mod:flow") != workflow_uuid_for("pkg.mod:other")


def test_workflow_decorator_registers_definition_with_display_name() -> None:
    @workflow(display_name="演示流", description="d", tags=["t1"])
    def sample_flow(ctx: WorkflowBuildContext) -> None:
        ctx.run("device-1/succeed", {"value": 1})

    definitions = get_registered_workflows()
    assert len(definitions) == 1
    definition = next(iter(definitions.values()))
    assert definition.display_name == "演示流"
    assert definition.tags == ["t1"]
    assert definition.source_path.endswith("sample_flow")
    assert definition.uuid == workflow_uuid_for(definition.source_path)

    with pytest.raises(ValueError, match="display_name"):
        workflow(display_name="  ")


def test_step_node_uuid_orders_lexicographically() -> None:
    wf_uuid = str(uuid.uuid4())
    node_uuids = [_step_node_uuid(wf_uuid, index) for index in range(20)]
    assert node_uuids == sorted(node_uuids)
    for value in node_uuids:
        uuid.UUID(value)  # 均为合法 uuid
    # 稳定：同 workflow 同步骤 => 同节点 uuid
    assert _step_node_uuid(wf_uuid, 3) == _step_node_uuid(wf_uuid, 3)


def test_template_payload_uses_roles_and_needs_no_device_graph() -> None:
    """@workflow → 注册表模板条目：run 的角色是设备 id，run_template 的角色是设备类，
    步骤按声明序用 edges 串成链；构建不依赖任何设备图（类无实例也能上报）。"""

    @workflow(display_name="双步流", description="d", tags=["t"])
    def two_steps(ctx: WorkflowBuildContext) -> None:
        ctx.run("external-device/do_thing", {"a": 1})
        ctx.run_template("nowhere_class/do_other", {"b": 2}, name="第二步")

    definition = next(iter(get_registered_workflows().values()))
    template = build_workflow_template_payload(definition)

    assert template["registry_type"] == "workflow"
    assert template["id"] == definition.source_path
    assert template["uuid"] == definition.uuid
    # 来源包 = 模块路径顶层包名（前端"设备包"徽标），module 为完整模块路径
    assert template["module"] == definition.source_path.partition(":")[0]
    assert template["package"] == template["module"].partition(".")[0]
    assert (template["display_name"], template["description"], template["tags"]) == ("双步流", "d", ["t"])
    assert template["roles"] == [
        {"role": "external-device", "label": "external-device", "kind": "device",
         "device_id": "external-device", "matches": ["external-device"]},
        {"role": class_role_id("nowhere_class"), "label": "nowhere_class", "kind": "class",
         "device_class": "nowhere_class", "matches": ["nowhere_class"]},
    ]
    assert [node["key"] for node in template["nodes"]] == ["step-0", "step-1"]
    assert template["nodes"][0] == {
        "key": "step-0", "kind": "action", "role": "external-device",
        "action_name": "do_thing", "name": "external-device.do_thing", "param": {"a": 1},
    }
    assert template["nodes"][1]["role"] == class_role_id("nowhere_class")
    assert template["nodes"][1]["name"] == "第二步"
    assert template["edges"] == [{"source": "step-0", "target": "step-1"}]


def test_template_payload_carries_guide_and_step_descriptions() -> None:
    """guide（准备 / 预期 / 注意）+ 每步 description 组成前端"全流程"说明；
    dict 形态与 WorkflowGuide 等价，空条目 / 未知键在声明时就拒绝。"""

    from unilabos.registry.workflows import WorkflowGuide

    @workflow(
        display_name="有指引的流",
        guide={
            "preparation": ["物料仓储出库一块板", "挂到 bench 的 T1 位"],
            "expected": ["台面报告里 T1 有板"],
        },
    )
    def guided(ctx: WorkflowBuildContext) -> None:
        ctx.run("bench/report", {}, name="台面报告", description="列出台面上每个位点的占用")
        ctx.run("bench/clear", {})

    template = build_workflow_template_payload(next(iter(get_registered_workflows().values())))
    assert template["guide"] == {
        "preparation": ["物料仓储出库一块板", "挂到 bench 的 T1 位"],
        "expected": ["台面报告里 T1 有板"],
        "notes": [],
    }
    assert template["nodes"][0]["description"] == "列出台面上每个位点的占用"
    assert "description" not in template["nodes"][1]

    clear_registered_workflows()

    @workflow(display_name="无指引", guide=WorkflowGuide())
    def plain(ctx: WorkflowBuildContext) -> None:
        ctx.run("bench/report", {})

    assert "guide" not in build_workflow_template_payload(next(iter(get_registered_workflows().values())))

    with pytest.raises(ValueError, match="preparation / expected / notes"):
        workflow(display_name="x", guide={"steps": ["a"]})
    with pytest.raises(ValueError, match="空条目"):
        workflow(display_name="x", guide={"expected": ["ok", "  "]})
    with pytest.raises(ValueError, match="字符串列表"):
        workflow(display_name="x", guide={"expected": "一句话"})


def test_materialize_resolves_roles_and_builds_serial_nodes() -> None:
    @workflow(display_name="双步流")
    def two_steps(ctx: WorkflowBuildContext) -> None:
        ctx.run("external-device/do_thing", {"a": 1})
        ctx.run_template("demo_class/do_other", {"b": 2}, name="第二步")

    definition = next(iter(get_registered_workflows().values()))
    template = build_workflow_template_payload(definition)
    catalog = _catalog()
    payload = materialize_workflow_template(template, catalog)

    assert payload["name"] == "双步流"
    assert payload["edges"] == []
    # 工作流 uuid 由模板 uuid + 绑定派生：同一组设备幂等，换设备则是另一个工作流
    assert payload["bindings"] == {"external-device": "external-device", class_role_id("demo_class"): "device-1"}
    assert payload["workflow_uuid"] == instantiated_workflow_uuid(definition.uuid, payload["bindings"])
    assert payload["workflow_uuid"] != definition.uuid
    other = materialize_workflow_template(template, catalog, {class_role_id("demo_class"): "dup-a"})
    assert other["workflow_uuid"] != payload["workflow_uuid"]

    nodes = payload["nodes"]
    assert [node["action_name"] for node in nodes] == ["do_thing", "do_other"]
    # 设备角色：显式 device_id；设备不在目录时 material_uuid 稳定占位
    assert nodes[0]["meta_data"]["target_device_id"] == "external-device"
    assert uuid.UUID(nodes[0]["material_uuid"])
    assert nodes[0]["param"] == {"a": 1}
    # 类角色：单实例自动填 device_id 与真实资源 uuid
    assert nodes[1]["meta_data"]["target_device_id"] == "device-1"
    assert nodes[1]["material_uuid"] == catalog.by_device_id["device-1"]["uuid"]
    assert nodes[1]["name"] == "第二步"
    # 节点 uuid 字典序 == 步骤序
    assert [node["uuid"] for node in nodes] == sorted(node["uuid"] for node in nodes)
    # 声明式步骤严格串行：第 i 步 execution_policy.depends_on 指向第 i-1 步
    assert nodes[0]["execution_policy"] == {}
    assert nodes[1]["execution_policy"] == {"depends_on": [nodes[0]["uuid"]]}


@pytest.mark.parametrize("compiler", ["builder", "ast"])
def test_device_id_rebinding_updates_actions_and_state_without_mutating_template(compiler: str) -> None:
    @workflow(display_name="通用反应釜流程", compiler=compiler)
    def flow(ctx):
        ctx.run("reactor/start_heating", {"target_c": 80})
        with ctx.loop_while(ctx.device_state("reactor", "temperature_c", "<", 80), interval_seconds=0.5):
            pass

    definition = next(iter(get_registered_workflows().values()))
    template = build_workflow_template_payload(definition)
    catalog = DeviceCatalog()
    catalog.add("reactor-a", "demo_class", str(uuid.uuid4()))
    catalog.add("reactor-b", "demo_class", str(uuid.uuid4()))
    assert [role["role"] for role in template["roles"]] == ["reactor"]
    for target in ("reactor-a", "reactor-b"):
        payload = materialize_workflow_template(template, catalog, {"reactor": target})
        action, loop = payload["nodes"]
        assert action["meta_data"]["target_device_id"] == target
        assert action["material_uuid"] == catalog.by_device_id[target]["uuid"]
        assert loop["param"]["condition"]["device_id"] == target
    assert template["nodes"][1]["param"]["condition"]["device_id"] == "reactor"


def test_state_only_device_is_a_bindable_id_role() -> None:
    @workflow(display_name="仅等待状态", compiler="ast")
    def flow(ctx):
        with ctx.loop_while(ctx.device_state("sensor", "ready", "==", False), interval_seconds=0.5):
            pass

    definition = next(iter(get_registered_workflows().values()))
    template = build_workflow_template_payload(definition)
    assert [role["role"] for role in template["roles"]] == ["sensor"]
    default = materialize_workflow_template(template, DeviceCatalog())
    bound = materialize_workflow_template(template, DeviceCatalog(), {"sensor": "slave-sensor-02"})
    assert default["nodes"][0]["param"]["condition"]["device_id"] == "sensor"
    assert bound["nodes"][0]["param"]["condition"]["device_id"] == "slave-sensor-02"


def test_class_role_needs_binding_unless_single_instance() -> None:
    @workflow(display_name="多实例流")
    def ambiguous(ctx: WorkflowBuildContext) -> None:
        ctx.run_template("dup_class/act", {})

    template = build_workflow_template_payload(next(iter(get_registered_workflows().values())))
    with pytest.raises(WorkflowTemplateBindingError, match="多个实例"):
        materialize_workflow_template(template, _catalog())
    # 显式绑定即可实例化；绑定到模板没有的角色被拒绝
    bound = materialize_workflow_template(template, _catalog(), {class_role_id("dup_class"): "dup-b"})
    assert bound["nodes"][0]["meta_data"]["target_device_id"] == "dup-b"
    with pytest.raises(WorkflowTemplateBindingError, match="没有这些角色"):
        materialize_workflow_template(template, _catalog(), {"ghost": "dup-b"})

    clear_registered_workflows()

    @workflow(display_name="零实例流")
    def missing(ctx: WorkflowBuildContext) -> None:
        ctx.run_template("nowhere_class/act", {})

    template = build_workflow_template_payload(next(iter(get_registered_workflows().values())))
    with pytest.raises(WorkflowTemplateBindingError, match="没有类"):
        materialize_workflow_template(template, _catalog())


def test_step_inventory_requirements_land_in_node_meta_data() -> None:
    """ctx.run(inventory=[...]) 声明时按 InventoryRequirement 校验，实例化后进 meta_data.inventory_requirements。"""

    requirement = {
        "key": "water",
        "kind": "lot",
        "lot_uuid": "30000000-0000-4000-8000-000000000001",
        "quantity": 40,
        "unit": "ml",
    }

    @workflow(display_name="库存步骤")
    def inventory_flow(ctx: WorkflowBuildContext) -> None:
        ctx.run("device-1/dispense", {"volume": 40}, inventory=[requirement])
        ctx.run("device-1/report", {})

    template = build_workflow_template_payload(next(iter(get_registered_workflows().values())))
    assert template["nodes"][0]["inventory_requirements"][0]["key"] == "water"
    assert "inventory_requirements" not in template["nodes"][1]
    nodes = materialize_workflow_template(template, _catalog())["nodes"]
    frozen = nodes[0]["meta_data"]["inventory_requirements"]
    assert len(frozen) == 1
    assert frozen[0]["key"] == "water" and frozen[0]["kind"] == "lot"
    assert frozen[0]["quantity"] == 40 and frozen[0]["unit"] == "ml"
    assert frozen[0]["lot_uuid"] == requirement["lot_uuid"]
    assert "inventory_requirements" not in nodes[1]["meta_data"]

    ctx = WorkflowBuildContext()
    with pytest.raises(ValueError):
        # lot 需求缺 quantity/unit：声明时即拒绝，而不是等到任务启动
        ctx.run("device-1/dispense", {}, inventory=[{"key": "water", "kind": "lot", "lot_uuid": "x"}])
    with pytest.raises(ValueError, match="key 不能重复"):
        ctx.run("device-1/dispense", {}, inventory=[requirement, requirement])


def test_step_target_must_contain_action() -> None:
    ctx = WorkflowBuildContext()
    with pytest.raises(ValueError, match="target"):
        ctx.run("only-device", {})
    with pytest.raises(ValueError, match="target"):
        ctx.run_template("only_class", {})


@pytest.mark.parametrize("misuse", [
    lambda ctx, step: bool(step),
    lambda ctx, step: bool(step.output("ok")),
    lambda ctx, step: step.output("ok") == False,  # noqa: E712 - 正是要拦住的写法
    lambda ctx, step: step.output("count") > 3,
    lambda ctx, step: f"S-{step.output('sample_id')}",
    lambda ctx, step: bool(ctx.step_output(step, "ok", "==", True)),
    lambda ctx, step: bool(ctx.device_state("reactor", "ready", "==", True)),
], ids=["step", "output", "output-eq", "output-gt", "output-fstring", "step-output-condition", "device-state-condition"])
def test_builder_mode_rejects_build_time_decisions_on_results(misuse) -> None:
    """builder 模式在注册时执行一次函数：对结果做判断只会得到固定答案（else 分支被静默丢弃），必须报错。"""

    @workflow(display_name="误用")
    def misuse_flow(ctx: WorkflowBuildContext) -> None:
        step = ctx.run("reactor/measure", {})
        if misuse(ctx, step):
            ctx.run("reactor/heat", {})
        else:
            ctx.run("reactor/cool", {})

    definition = next(iter(get_registered_workflows().values()))
    with pytest.raises(TypeError, match=r"compiler=|step\.output"):
        definition.collect()


def test_builder_references_are_stored_as_plain_json() -> None:
    """引用照常放进参数和条件；入库的是普通 dict，内部比较与序列化不受构建期保护影响。"""

    ctx = WorkflowBuildContext()
    first = ctx.run("reactor/measure", {})
    second = ctx.run("reactor/heat", {"target": first.output("temperature"), "nested": [{"ref": first.output()}]})
    with ctx.loop_while(ctx.step_output(second, "ok", "==", False), interval_seconds=0.5):
        ctx.run("reactor/heat", {})

    assert second.params["target"] == {"kind": "node_output", "node_key": first.key, "data_key": "temperature"}
    assert type(second.params["target"]) is dict and type(second.params["nested"][0]["ref"]) is dict
    condition = ctx.steps[2].params["condition"]
    assert type(condition) is dict and condition["node_key"] == second.key
    assert first.output("x") == {"kind": "node_output", "node_key": first.key, "data_key": "x"}
    assert json.loads(json.dumps(first.output("x"))) == first.output("x")


def test_materialized_workflow_upsert_is_idempotent() -> None:
    """同一模板 + 同一组设备反复实例化：uuid 不变、图被覆盖更新，不会堆出重复工作流。"""

    @workflow(display_name="上报流", tags=["demo"])
    def reported(ctx: WorkflowBuildContext) -> None:
        ctx.run("device-1/succeed", {"value": 5})
        ctx.run("device-1/succeed", {"value": 6})

    template = build_workflow_template_payload(next(iter(get_registered_workflows().values())))
    service = WorkflowService(":memory:")
    try:
        payload = materialize_workflow_template(template, _catalog())
        first = upsert_workflow(service, payload)
        assert first["uuid"] == payload["workflow_uuid"] and first["name"] == "上报流"
        assert len(service.get_graph(first["uuid"])["nodes"]) == 2

        second = upsert_workflow(service, materialize_workflow_template(template, _catalog()))
        assert second["uuid"] == first["uuid"]
        assert second["revision"] > first["revision"]
        assert len(service.get_graph(first["uuid"])["nodes"]) == 2
        assert service.list_workflows(page=1, page_size=10, name="上报流")["total"] == 1
    finally:
        service.close()


def test_upsert_over_http_like_client_survives_bodiless_get(monkeypatch) -> None:
    """upsert 经 HTTPWorkflowClient 走 POST 冲突 → GET → PUT → PUT graph。

    回归：HTTPClient 曾对无 body 的 GET 也带 ``Content-Type: application/json``，
    Workflow 路由把空 body 判为格式错误（1000），create 冲突后的查询失败被
    ``raise create_error from lookup_error`` 盖住，日志只剩误导性的 3003。
    """

    from fastapi.testclient import TestClient
    from types import SimpleNamespace

    from unilabos.client.http import HTTPClient, HTTPClientConfig
    from unilabos.client.runtime.workflow import HTTPWorkflowClient
    from unilabos.server.api.runtime.workflow import create_workflow_app

    @workflow(display_name="HTTP 上报流", tags=["demo"])
    def reported(ctx: WorkflowBuildContext) -> None:
        ctx.run("device-1/succeed", {"value": 5})

    template = build_workflow_template_payload(next(iter(get_registered_workflows().values())))
    service = WorkflowService(":memory:")
    base_url = "http://testserver/api/v1"
    http = HTTPClient(HTTPClientConfig(base_url=base_url))
    http._client.close()
    http._client = TestClient(create_workflow_app(service), base_url=base_url)
    reporter = HTTPWorkflowClient("http://testserver", http_client=http)
    try:
        payload = materialize_workflow_template(template, _catalog())
        # 程序化写图需要权威设备/动作声明，不能把无 schema 的夹具当成已校验。
        # 本用例的 value 是普通参数，不包含 Site；保持客户端默认 resolve 行为。
        authority_materials = SimpleNamespace(list_materials=lambda: [{"material": {
            "material_uuid": payload["nodes"][0]["material_uuid"],
            "resource_id": "device-1", "template_name": "demo_class",
        }}])
        authority_registry = SimpleNamespace(action_definition=lambda klass, action: {
            "schema": {"properties": {"goal": {"type": "object", "properties": {"value": {"type": "integer"}}}}},
        } if (klass, action) == ("demo_class", "succeed") else None)
        monkeypatch.setattr("unilabos.server.backend.composition.get_materials_service", lambda: authority_materials)
        monkeypatch.setattr("unilabos.server.services.runtime.registry.get_registry_service", lambda: authority_registry)
        upsert_workflow(reporter, payload)
        upsert_workflow(reporter, payload)
        assert service.get_workflow(payload["workflow_uuid"])["revision"] >= 2
        assert len(service.get_graph(payload["workflow_uuid"])["nodes"]) == 1
    finally:
        service.close()


def test_upsert_over_http_keeps_resource_scopes(monkeypatch) -> None:
    """回归：upsert 经 HTTPWorkflowClient 写图时须把资源作用域一并 PUT。

    ``upsert_workflow`` 向 ``save_graph`` 传 ``resource_scopes``；HTTP 客户端曾不接受该参数，
    Host 子进程向远端权威登记带作用域的 ``@workflow`` 模板时直接 TypeError。
    """

    from fastapi.testclient import TestClient
    from types import SimpleNamespace

    from unilabos.client.http import HTTPClient, HTTPClientConfig
    from unilabos.client.runtime.workflow import HTTPWorkflowClient
    from unilabos.server.api.runtime.workflow import create_workflow_app

    @workflow(display_name="HTTP 上报作用域流", tags=["demo"])
    def scoped(ctx: WorkflowBuildContext) -> None:
        with ctx.resource_scope("device-1", name="reaction"):
            ctx.run("device-1/succeed", {"value": 5})

    template = build_workflow_template_payload(next(iter(get_registered_workflows().values())))
    service = WorkflowService(":memory:")
    base_url = "http://testserver/api/v1"
    http = HTTPClient(HTTPClientConfig(base_url=base_url))
    http._client.close()
    http._client = TestClient(create_workflow_app(service), base_url=base_url)
    reporter = HTTPWorkflowClient("http://testserver", http_client=http)
    try:
        payload = materialize_workflow_template(template, _catalog())
        assert payload.get("resource_scopes"), "模板实例化应带出显式资源作用域"
        authority_materials = SimpleNamespace(list_materials=lambda: [{"material": {
            "material_uuid": payload["nodes"][0]["material_uuid"],
            "resource_id": "device-1", "template_name": "demo_class",
        }}])
        authority_registry = SimpleNamespace(action_definition=lambda klass, action: {
            "schema": {"properties": {"goal": {"type": "object", "properties": {"value": {"type": "integer"}}}}},
        } if (klass, action) == ("demo_class", "succeed") else None)
        monkeypatch.setattr("unilabos.server.backend.composition.get_materials_service", lambda: authority_materials)
        monkeypatch.setattr("unilabos.server.services.runtime.registry.get_registry_service", lambda: authority_registry)
        upsert_workflow(reporter, payload)
        stored = service.get_graph(payload["workflow_uuid"])["workflow"]["meta_data"]["unilab"]["resource_scopes"]
        assert [scope.get("scope_id") for scope in stored] == ["reaction"]
    finally:
        service.close()


def test_workflow_route_accepts_bodiless_get_with_json_content_type() -> None:
    """浏览器 / httpx 一类客户端默认给所有请求带 JSON Content-Type，空 body 不是格式错误。"""

    from fastapi.testclient import TestClient

    from unilabos.server.api.runtime.workflow import create_workflow_app

    service = WorkflowService(":memory:")
    client = TestClient(create_workflow_app(service))
    try:
        created = client.post("/api/v1/workflows", json={"name": "wf", "tags": []}).json()
        assert created["code"] == 0, created
        fetched = client.get(
            f"/api/v1/workflows/{created['data']['uuid']}",
            headers={"Content-Type": "application/json"},
        ).json()
        assert fetched["code"] == 0, fetched
        # 真正缺 body 的写请求仍按格式错误拒绝
        empty_post = client.post(
            "/api/v1/workflows", headers={"Content-Type": "application/json"}
        ).json()
        assert empty_post["code"] == 1000, empty_post
    finally:
        service.close()


def test_http_client_only_declares_content_type_with_body() -> None:
    import httpx

    from unilabos.client.http import HTTPClient, HTTPClientConfig

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"code": 0, "data": None})

    http = HTTPClient(HTTPClientConfig(base_url="http://testserver/api/v1"))
    http._client.close()
    http._client = httpx.Client(
        base_url="http://testserver/api/v1", transport=httpx.MockTransport(handler)
    )
    try:
        http.get("/workflows/x")
        http.post("/workflows", json={"name": "wf"})
    finally:
        http.close()
    assert "content-type" not in seen[0].headers
    assert seen[1].headers["content-type"] == "application/json"


def test_ast_scanner_discovers_module_level_workflow(tmp_path: Path) -> None:
    module_path = tmp_path / "wf_module.py"
    module_path.write_text(
        "\n".join(
            [
                "from unilabos.registry.workflows import workflow",
                "",
                "@workflow(display_name='扫描流', description='desc', tags=['x'])",
                "def scanned_flow(ctx):",
                "    ctx.run('dev/act', {})",
                "",
                "def not_a_workflow(ctx):",
                "    pass",
            ]
        ),
        encoding="utf-8",
    )
    _devices, _resources, workflows = _parse_file(module_path, tmp_path)
    assert len(workflows) == 1
    meta = workflows[0]
    assert meta["function"] == "scanned_flow"
    assert meta["display_name"] == "扫描流"
    assert meta["tags"] == ["x"]
    assert meta["module"] == "wf_module"


def test_action_display_name_flows_from_decorator_to_registry(tmp_path: Path) -> None:
    from unilabos.registry.registry import Registry

    module_path = tmp_path / "display_driver.py"
    module_path.write_text(
        "\n".join(
            [
                "from unilabos.registry.decorators import action, device",
                "",
                "@device(id='display_demo_device', category=['test'])",
                "class Driver:",
                "    @action(description='d', display_name='友好动作名')",
                "    def do_thing(self, value: int = 0) -> dict:",
                "        return {'value': value}",
            ]
        ),
        encoding="utf-8",
    )
    devices, _resources, _workflows = _parse_file(module_path, tmp_path)
    assert (
        devices[0]["actions"]["do_thing"]["action_args"]["display_name"]
        == "友好动作名"
    )
    entry = Registry()._build_device_entry_from_ast("display_demo_device", devices[0])
    mapping = entry["class"]["action_value_mappings"]["do_thing"]
    assert mapping["display_name"] == "友好动作名"


class OrderedDriver:
    """e2e 用最小驱动：记录动作参数以断言执行顺序（须为模块级类，供类路径实例化）。"""

    def __init__(self) -> None:
        self.calls: list[int] = []

    def record(self, value: int) -> dict:
        self.calls.append(value)
        return {"value": value}


def test_workflow_end_to_end_runs_via_local_scheduler() -> None:
    """@workflow -> 模板 -> 实例化 -> 创建任务 -> HostLink 执行栈真实跑通并按序执行。"""

    from unilabos.backend.hostlink.adapter_registry import clear_execution_adapter
    from unilabos.backend.hostlink.backend import HostLinkBackend
    from unilabos.backend.hostlink.local_runtime import (
        HostLinkDriverSpec,
        HostLinkLocalRuntime,
    )
    from unilabos.backend.hostlink.host_node import HostNode
    from unilabos.server.backend.execution import JobExecutionBackend
    from unilabos.server.backend.scheduler.service import BackendScheduler

    local = HostLinkLocalRuntime()
    node = local.add_driver(
        HostLinkDriverSpec(
            device_id="wf-device",
            driver_class=OrderedDriver,
            config={},
            action_names=("record",),
            action_value_mappings={"record": {"type": "UniLabJsonCommand"}},
        )
    )
    runtime = HostLinkBackend(local, is_slave=False)
    local.start()
    adapter = HostNode("host_node", runtime, bridges=[])
    microbackend = JobExecutionBackend(host_node_getter=lambda: adapter)
    adapter.bridges = [microbackend]
    microbackend.start()

    service = WorkflowService(":memory:")
    scheduler = BackendScheduler(service, microbackend)
    service.set_task_submitter(scheduler.submit)
    scheduler.start(recover=True)
    try:
        @workflow(display_name="端到端流")
        def e2e_flow(ctx: WorkflowBuildContext) -> None:
            ctx.run("wf-device/record", {"value": 1})
            ctx.run("wf-device/record", {"value": 2})
            ctx.run("wf-device/record", {"value": 3})

        definition = next(iter(get_registered_workflows().values()))
        catalog = DeviceCatalog()
        catalog.add("wf-device", "wf_demo_class", str(uuid.uuid4()))
        template = build_workflow_template_payload(definition)
        workflow_record = upsert_workflow(
            service, materialize_workflow_template(template, catalog)
        )

        task = service.create_workflow_task(
            workflow_uuid=workflow_record["uuid"],
            run_mode="normal",
            target_node_uuid=None,
            input_value={},
            description=None,
            meta_data={},
        )
        deadline = time.monotonic() + 5
        current = service.get_workflow_task(task["uuid"])
        while current["status"] not in {"succeeded", "failed"}:
            if time.monotonic() >= deadline:
                pytest.fail(f"workflow task 未在时限内结束: {current['status']}")
            time.sleep(0.02)
            current = service.get_workflow_task(task["uuid"])

        assert current["status"] == "succeeded"
        # execution_policy.depends_on 串行边 => 严格按声明序 1,2,3 执行
        assert node.driver.calls == [1, 2, 3]
        outputs = current["output"]
        assert {value["return_value"]["value"] for value in outputs.values()} == {1, 2, 3}
    finally:
        service.set_task_submitter(None)
        scheduler.stop()
        service.close()
        clear_execution_adapter(adapter)
        microbackend.stop()
        adapter.stop()
        HostNode.reset_state()
        runtime.stop()
