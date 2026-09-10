"""库位选择（Site Selection）动作连接点进入可信工作流图的合同测试。"""

from __future__ import annotations

import ast
import textwrap
from typing import Any

import pytest

from unilabos.registry.action_contract_schema import parse_action_contract
from unilabos.registry.action_template_projection import (
    compile_action_template_handles,
)
from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.authoring_kernel import AuthoringCatalogSnapshot
from unilabos.workflow.schema import WorkflowSchemaError, parse_value_schema
from unilabos.workflow.workflow_io import (
    WorkflowIOValidationError,
    handle_value_schema,
)

# ``WORKFLOW_UUID`` 是本测试可信候选工作流（Workflow）的稳定身份。
WORKFLOW_UUID = "71000000-0000-4000-8000-000000000001"
# ``NODE_UUID`` 是候选图中 pick 动作节点的稳定身份。
NODE_UUID = "72000000-0000-4000-8000-000000000001"
# ``TEMPLATE_UUID`` 是注册表（Registry）pick 动作模板的稳定投影身份。
TEMPLATE_UUID = "73000000-0000-4000-8000-000000000001"
# ``RESOURCE_TEMPLATE_UUID`` 是承载 pick 动作设备类的资源模板身份。
RESOURCE_TEMPLATE_UUID = "74000000-0000-4000-8000-000000000001"

_HANDLE_UUIDS = {
    ("target", "ready"): "75000000-0000-4000-8000-000000000001",
    ("source", "ready"): "75000000-0000-4000-8000-000000000002",
    ("target", "resource"): "75000000-0000-4000-8000-000000000003",
    ("source", "resource"): "75000000-0000-4000-8000-000000000004",
    ("target", "site"): "75000000-0000-4000-8000-000000000005",
}


def _registry_action_projection(
    *,
    nullable: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """从真实 AST 动作合同生成 Backend 形状模板和连接点投影。

    参数说明：``nullable`` 决定库位（Site）参数是可空字符串还是必填字符串。
    返回：动作节点模板及其连接点（Handle）列表；静态解析或投影非法时传播公共
    合同异常，且不执行作者代码。
    """

    # ``site_annotation`` 和 ``site_default`` 联合决定 JSON Schema nullable 与
    # 连接点 required，禁止测试在投影后手写这两个结果。
    site_annotation = "str | None" if nullable else "str"
    site_default = " = None" if nullable else ""
    source = f"""
        from typing import Annotated
        from unilabos.registry.annotations import SiteSelector
        from unilabos.registry.placeholder_type import ResourceSlot

        def pick(
            resource: ResourceSlot,
            site: Annotated[
                {site_annotation},
                SiteSelector(owner="resource"),
            ]{site_default},
        ) -> None:
            pass
    """
    # ``module`` 是不执行作者代码的动作定义语法树。
    module = ast.parse(textwrap.dedent(source))
    # ``action`` 是本测试唯一的 pick 动作函数。
    action = next(
        node
        for node in ast.walk(module)
        if isinstance(node, ast.FunctionDef) and node.name == "pick"
    )
    # ``contract`` 是注册表静态解析器签发的规范动作合同（Action Contract）。
    contract = parse_action_contract(
        module,
        action,
        module_name="lab.devices.site_picker",
    )
    # ``action_schema`` 保留标准 JSON Schema nullable 与 UUID format。
    action_schema = contract.to_action_schema(action_name="pick")
    # ``projected_handles`` 是尚未补数据库 UUID 的注册表连接点候选。
    projected_handles = compile_action_template_handles(
        action_schema,
        node_business_key=("lab.devices:SitePicker", "pick"),
        resource_template_identity_resolver=None,
    )
    handles: list[dict[str, Any]] = []
    for projected in projected_handles:
        io_type = str(projected["io_type"])
        handle_key = str(projected["handle_key"])
        # ``handle`` 只补持久投影 UUID，不改注册表生成的值合同和必填语义。
        handle = dict(projected)
        handle.pop("node_business_key", None)
        handle["uuid"] = _HANDLE_UUIDS[(io_type, handle_key)]
        handle["workflow_node_template_uuid"] = TEMPLATE_UUID
        handles.append(handle)
    # ``template`` 只提供可信创作目录需要的动作业务身份和参数映射。
    template = {
        "uuid": TEMPLATE_UUID,
        "resource_template_uuid": RESOURCE_TEMPLATE_UUID,
        "name": "pick",
        "display_name": "Pick",
        "class": "lab.devices:SitePicker",
        "description": "按库位选择物料",
        "meta_data": {"owner": "test"},
        "goal": {"resource": "resource", "site": "site"},
        "goal_default": {"site": None} if nullable else {},
        "feedback": {},
        "result": {},
        # 节点 ``param`` 仍由连接点合同验证；动作执行 Schema 属于另一层 DTO，
        # 此最小候选图不重复测试其 ``goal`` 外壳。
        "schema": None,
        "type": "action",
        "node_type": "compute",
        "icon": None,
        "header": None,
        "footer": None,
    }
    return template, handles


def _site_handle(handles: list[dict[str, Any]]) -> dict[str, Any]:
    """取得唯一的库位（Site）目标连接点。

    参数说明：``handles`` 是真实注册表动作投影。返回：``target:site`` 连接点；
    缺失或重复时让 ``next``/断言直接暴露夹具漂移。
    """

    return next(
        handle
        for handle in handles
        if handle["io_type"] == "target" and handle["handle_key"] == "site"
    )


@pytest.mark.parametrize(
    ("nullable", "expected_required", "expected_schema"),
    [
        pytest.param(
            False,
            True,
            {
                "type": "string",
                "x-unilabos-editor-control": "site_selector",
                "x-unilabos-site-selector": {
                    "version": 1,
                    "owner": "resource",
                    "occupant": None,
                    "show_occupied": True,
                    "allow_occupied": False,
                },
            },
            id="required-string",
        ),
        pytest.param(
            True,
            False,
            {
                "anyOf": [
                    {
                        "type": "string",
                        "x-unilabos-editor-control": "site_selector",
                        "x-unilabos-site-selector": {
                            "version": 1,
                            "owner": "resource",
                            "occupant": None,
                            "show_occupied": True,
                            "allow_occupied": False,
                        },
                    },
                    {"type": "null"},
                ]
            },
            id="optional-nullable-string",
        ),
    ],
)
def test_registry_site_selector_handle_enters_workflow_io_without_semantic_drift(
    nullable: bool,
    expected_required: bool,
    expected_schema: dict[str, Any],
) -> None:
    """真实动作连接点必须规范进入工作流输入输出（Workflow IO）。

    参数说明：``nullable`` 选择动作 JSON Schema 形状；``expected_required`` 与
    ``expected_schema`` 固定工作流边界的必填性和值集合。返回：无；断言库位选择
    扩展不改变类型或 required 语义。
    """

    _template, handles = _registry_action_projection(nullable=nullable)
    site_handle = _site_handle(handles)
    # ``raw_schema`` 证明注册表继续发布标准 JSON Schema 的字符串或可空字符串。
    raw_schema = site_handle["meta_data"]["unilab"]["value_schema"]
    expected_raw_type: object = ["string", "null"] if nullable else "string"
    assert raw_schema["type"] == expected_raw_type
    assert raw_schema["format"] == "uuid"

    parsed = handle_value_schema(site_handle)

    assert site_handle["required"] is expected_required
    assert parsed.to_dict() == expected_schema


def test_registry_site_selector_handle_can_enter_trusted_candidate_graph() -> None:
    """合法库位选择动作必须进入可信工作流候选图。

    参数：无。返回：无；断言真实注册表合同、工作流输入绑定和静态空库位值共同
    通过创作编译，不再降级为 ``candidate_invalid``。
    """

    # ``template`` 与 ``handles`` 完全来自同一次真实动作合同投影。
    template, handles = _registry_action_projection(nullable=True)
    # ``catalog`` 是编译期间不可变的动作模板目录快照。
    catalog = AuthoringCatalogSnapshot.from_entities([template], handles)
    # ``source`` 是只静态编译、不执行设备动作的最小工作流作者源码。
    source = f'''from lab.devices import SitePicker
from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.workflow.authoring import device, workflow, workflow_output


picker: SitePicker = device()


@workflow(workflow_uuid="{WORKFLOW_UUID}", displayname="Site selector workflow")
def site_selector_workflow(*, resource: ResourceSlot):
    # unilab:node_uuid={NODE_UUID}
    picked = picker.pick(resource=resource, site=None)
    return workflow_output()
'''
    # ``compiled`` 是只经过可信静态编译、未创建工作流任务（WorkflowTask）的候选。
    compiled = WorkflowAuthoringEngine(catalog=catalog).compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=1,
        python_source=source,
        source_uri="package://lab/workflows/site_selector.py",
        applied_graph={
            "workflow": {
                "uuid": WORKFLOW_UUID,
                "name": "Persisted",
                "tags": [],
                "description": None,
                "meta_data": {},
                "revision": 1,
            },
            "nodes": [],
            "edges": [],
            "node_templates": [],
            "handle_templates": [],
        },
    )

    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    assert compiled.graph["nodes"][0]["param"]["site"] is None


@pytest.mark.parametrize(
    ("raw_schema", "error_path"),
    [
        pytest.param(
            {
                "type": "string",
                "x-unilabos-editor-control": "site_selector",
            },
            "/x-unilabos-site-selector",
            id="missing-extension",
        ),
        pytest.param(
            {
                "type": "string",
                "x-unilabos-site-selector": {
                    "version": 1,
                    "owner": "resource",
                    "occupant": None,
                    "show_occupied": True,
                    "allow_occupied": False,
                },
            },
            "/x-unilabos-editor-control",
            id="missing-control",
        ),
        pytest.param(
            {
                "type": "string",
                "x-unilabos-editor-control": "site_selector",
                "x-unilabos-site-selector": "resource",
            },
            "/x-unilabos-site-selector",
            id="selector-not-object",
        ),
        pytest.param(
            {
                "type": "string",
                "x-unilabos-editor-control": "site_selector",
                "x-unilabos-site-selector": {
                    "version": 1,
                    "owner": "resource",
                    "occupant": None,
                    "show_occupied": True,
                },
            },
            "/x-unilabos-site-selector/allow_occupied",
            id="missing-selector-field",
        ),
        pytest.param(
            {
                "type": "string",
                "x-unilabos-editor-control": "site_selector",
                "x-unilabos-site-selector": {
                    "version": 2,
                    "owner": "resource",
                    "occupant": None,
                    "show_occupied": True,
                    "allow_occupied": False,
                },
            },
            "/x-unilabos-site-selector/version",
            id="unsupported-version",
        ),
        pytest.param(
            {
                "type": "string",
                "x-unilabos-editor-control": "site_selector",
                "x-unilabos-site-selector": {
                    "version": 1,
                    "owner": "resource",
                    "occupant": None,
                    "show_occupied": True,
                    "allow_occupied": False,
                    "fallback": "A1",
                },
            },
            "/x-unilabos-site-selector/fallback",
            id="unknown-selector-field",
        ),
        pytest.param(
            {
                "type": "integer",
                "x-unilabos-editor-control": "site_selector",
                "x-unilabos-site-selector": {
                    "version": 1,
                    "owner": "resource",
                    "occupant": None,
                    "show_occupied": True,
                    "allow_occupied": False,
                },
            },
            "/type",
            id="non-string-site-value",
        ),
        pytest.param(
            {
                "type": ["string", "integer", "null"],
                "x-unilabos-editor-control": "site_selector",
                "x-unilabos-site-selector": {
                    "version": 1,
                    "owner": "resource",
                    "occupant": None,
                    "show_occupied": True,
                    "allow_occupied": False,
                },
            },
            "/type",
            id="open-nullable-shape",
        ),
        pytest.param(
            {
                "type": "string",
                "x-unilabos-editor-control": "site_selector",
                "x-unilabos-site-selector": {
                    "version": 1,
                    "owner": "resource",
                    "occupant": None,
                    "show_occupied": True,
                    "allow_occupied": False,
                },
                "x-unilabos-unknown": True,
            },
            "/x-unilabos-unknown",
            id="unknown-root-extension",
        ),
    ],
)
def test_workflow_value_schema_rejects_invalid_site_selector_contracts(
    raw_schema: dict[str, Any],
    error_path: str,
) -> None:
    """工作流第 1 版值 Schema 必须关闭式拒绝非法库位选择合同。

    参数说明：``raw_schema`` 隔离扩展与 nullable 形状反例；``error_path`` 是稳定
    JSON Pointer。返回：无；断言未知键、缺失配对、非法字段和版本均不被放行。
    """

    with pytest.raises(WorkflowSchemaError) as caught:
        parse_value_schema(raw_schema)
    assert caught.value.code == "invalid_schema"
    assert caught.value.path == error_path


def test_workflow_handle_rejects_invalid_nullable_axis_before_candidate_graph() -> None:
    """动作连接点的开放 nullable 类型数组必须在候选图前失败关闭。

    参数：无。返回：无；断言即使库位选择扩展本身合法，三成员 ``type`` 数组仍
    不能被投影成工作流值集合。
    """

    _template, handles = _registry_action_projection(nullable=True)
    site_handle = _site_handle(handles)
    # ``raw_schema`` 只破坏 nullable 轴，保持五字段库位选择合同不变。
    raw_schema = site_handle["meta_data"]["unilab"]["value_schema"]
    raw_schema["type"] = ["string", "integer", "null"]

    with pytest.raises(WorkflowIOValidationError):
        handle_value_schema(site_handle)
