"""F06 R1 已发布工作流（Published Workflow）目录与合同的公共 RED。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from unilabos.workflow.catalog import (
    PublishedSourceCatalog,
    PublishedSourceCatalogError,
)
from unilabos.workflow.composite import (
    PublishedWorkflowContractError,
    project_published_workflow_contract,
)
from unilabos.workflow.composite_compatibility import (
    published_workflow_projection_is_canonical,
)
from unilabos.workflow.store import WorkflowStore
from unilabos.workflow.template_projection_store import (
    RegistryTemplateProjectionStore,
)

WORKFLOW_UUID = "51000000-0000-4000-8000-000000000001"
HOST_RESOURCE_TEMPLATE_UUID = "52000000-0000-4000-8000-000000000001"
ACTION_RESOURCE_TEMPLATE_UUID = "52000000-0000-4000-8000-000000000002"
ACTION_TEMPLATE_UUID = "53000000-0000-4000-8000-000000000001"
ACTION_VALUE_TARGET_UUID = "54000000-0000-4000-8000-000000000003"
ACTION_VALUE_SOURCE_UUID = "54000000-0000-4000-8000-000000000004"
ACTION_NODE_UUID = "55000000-0000-4000-8000-000000000001"
APPLIED_SOURCE_HASH = "sha256:" + "3" * 64
DEFINITION_CONTENT_HASH = "sha256:" + "1" * 64
CONTRACT_DIGEST = (
    "sha256:689aaac733eba27d13279d242a71fc3c8bc41f0c144d41261dc160a52b46a1cf"
)


def _source_records() -> list[dict[str, str]]:
    """返回不依赖文件扫描或 Python import 的已发布源码记录。

    参数：无。返回：唯一子工作流的冻结来源记录列表。异常：无。
    """

    return [
        {
            "workflow_uuid": WORKFLOW_UUID,
            "definition_fqid": "c1_published_lab.workflows.prepare_sample",
            "module": "c1_published_lab.workflows.child",
            "symbol": "prepare_sample",
            "source_uri": "package://c1_published_lab/workflows/child.py",
            "definition_content_hash": DEFINITION_CONTENT_HASH,
        }
    ]


def _handle(
    handle_uuid: str,
    key: str,
    io_type: str,
) -> dict[str, Any]:
    """构造一个与数值工作流输入/输出相容的连接点（Handle）模板。

    参数：``handle_uuid`` 是模板身份，``key`` 是业务键，``io_type`` 是方向。
    返回：可供发布投影校验的数值连接点字典。异常：无。
    """

    return {
        "uuid": handle_uuid,
        "workflow_node_template_uuid": ACTION_TEMPLATE_UUID,
        "handle_key": key,
        "io_type": io_type,
        "display_name": key.title(),
        "description": "",
        "type": "number",
        "required": io_type == "target",
        "data_source": "executor",
        "data_key": key,
        "meta_data": {"unilab": {"value_schema": {"type": "number"}}},
    }


def _applied_snapshot() -> dict[str, Any]:
    """返回通过当前公共工作流输入/输出校验的已应用平面图快照。

    参数：无。返回：包含应用源码、单动作图与目录投影的冻结快照。异常：无。
    """

    timestamp = "2026-08-02T00:00:00Z"
    return {
        "workflow": {
            "uuid": WORKFLOW_UUID,
            "revision": 7,
            "name": "Published sample preparation",
            "tags": [],
            "description": "C1 R1 fixture",
            "create_time": timestamp,
            "update_time": timestamp,
            "meta_data": {
                "unilab": {
                    "input_contract": {
                        "version": 1,
                        "parameters": [
                            {
                                "name": "value",
                                "schema": {"type": "number"},
                                "required": True,
                            }
                        ],
                    },
                    "output_contract": {
                        "version": 1,
                        "outputs": [
                            {
                                "name": "result",
                                "schema": {"type": "number"},
                                "implicit": False,
                            }
                        ],
                    },
                    "output_bindings": {
                        "result": {
                            "kind": "node_output",
                            "workflow_node_uuid": ACTION_NODE_UUID,
                            "source_handle_uuid": ACTION_VALUE_SOURCE_UUID,
                        }
                    },
                }
            },
        },
        "applied_source": {
            "workflow_revision": 7,
            "source_hash": APPLIED_SOURCE_HASH,
            "python_source": "def prepare_sample(*, value: float): ...\n",
            "source_map": [],
            "compiler_version": "c1-fixture",
            "template_catalog_fingerprint": "sha256:" + "4" * 64,
        },
        "nodes": [
            {
                "uuid": ACTION_NODE_UUID,
                "workflow_uuid": WORKFLOW_UUID,
                "workflow_node_template_uuid": ACTION_TEMPLATE_UUID,
                "parent_uuid": None,
                "name": "measure",
                "status": "idle",
                "type": "device",
                "pose": {},
                "param": {},
                "execution_policy": {},
                "disabled": False,
                "minimized": False,
                "meta_data": {
                    "unilab": {
                        "input_bindings": {
                            ACTION_VALUE_TARGET_UUID: {"parameter": "value"}
                        }
                    }
                },
                "create_time": timestamp,
                "update_time": timestamp,
            }
        ],
        "edges": [],
        "node_templates": [
            {
                "uuid": ACTION_TEMPLATE_UUID,
                "resource_template_uuid": ACTION_RESOURCE_TEMPLATE_UUID,
                "name": "measure",
                "display_name": "Measure",
                "description": "",
                "meta_data": {},
                "goal": {},
                "goal_default": {},
                "feedback": {},
                "result": {},
                "schema": None,
                "type": "action",
                "node_type": "device",
            }
        ],
        "handle_templates": [
            _handle(ACTION_VALUE_TARGET_UUID, "value", "target"),
            _handle(ACTION_VALUE_SOURCE_UUID, "result", "source"),
        ],
    }


def _group_template() -> dict[str, Any]:
    """构造宿主节点（Host Node）所有的既有分组模板。

    参数：无。返回：框架所有的展示分组模板字典。异常：无。
    """

    return {
        "resource_template_uuid": HOST_RESOURCE_TEMPLATE_UUID,
        "name": "group",
        "display_name": "分组",
        "description": "fixture",
        "class": "unilabos.workflow.authoring:group",
        "goal": {},
        "goal_default": {},
        "feedback": {},
        "result": {},
        "schema": None,
        "type": "group",
        "node_type": "group",
        "meta_data": {
            "unilab": {"framework_owner_only": True},
            "resource_template": {
                "uuid": HOST_RESOURCE_TEMPLATE_UUID,
                "name": "host_node",
                "display_name": "Host Node",
            },
        },
    }


def _projected_contract() -> Any:
    """通过公共目录解析并投影一个有效已发布工作流合同。

    参数：无。返回：有效的已发布工作流合同投影。异常：来源或快照合同无效时
    抛出对应目录或 ``PublishedWorkflowContractError``。
    """

    catalog = PublishedSourceCatalog.from_records(_source_records())
    source = catalog.resolve("c1_published_lab.workflows.child", "prepare_sample")
    return project_published_workflow_contract(
        source=source,
        applied_snapshot=_applied_snapshot(),
        host_node_resource_template={
            "uuid": HOST_RESOURCE_TEMPLATE_UUID,
            "name": "host_node",
            "display_name": "Host Node",
        },
    )


def test_published_source_catalog_resolves_one_frozen_static_source() -> None:
    """已发布源码目录按绝对模块和静态符号唯一解析且顺序不影响摘要。

    参数：无。返回：无；断言来源身份与摘要稳定。异常：目录解析或断言失败时
    由 pytest 报告。
    """

    first = PublishedSourceCatalog.from_records(_source_records())
    second = PublishedSourceCatalog.from_records(list(reversed(_source_records())))

    source = first.resolve("c1_published_lab.workflows.child", "prepare_sample")

    assert source.workflow_uuid == WORKFLOW_UUID
    assert source.definition_fqid == "c1_published_lab.workflows.prepare_sample"
    assert source.module == "c1_published_lab.workflows.child"
    assert source.symbol == "prepare_sample"
    assert source.definition_content_hash == DEFINITION_CONTENT_HASH
    assert source.package_catalog_digest == first.digest == second.digest


def test_published_source_catalog_rejects_missing_duplicate_and_dynamic_identity() -> (
    None
):
    """目录对缺失、重复和相对模块身份稳定关闭失败。

    参数：无。返回：无；断言三类非法来源得到稳定错误码。异常：预期错误未抛
    或断言不成立时由 pytest 报告。
    """

    catalog = PublishedSourceCatalog.from_records(_source_records())
    with pytest.raises(PublishedSourceCatalogError) as missing:
        catalog.resolve("c1_published_lab.workflows.missing", "missing")
    assert missing.value.code == "published_source_not_found"

    with pytest.raises(PublishedSourceCatalogError) as duplicate:
        PublishedSourceCatalog.from_records(_source_records() * 2)
    assert duplicate.value.code == "published_source_duplicate"

    invalid = _source_records()
    invalid[0]["module"] = ".child"
    with pytest.raises(PublishedSourceCatalogError) as dynamic:
        PublishedSourceCatalog.from_records(invalid)
    assert dynamic.value.code == "published_source_invalid"


def test_applied_workflow_projects_exact_contract_digest_and_provenance() -> None:
    """已应用工作流投影封闭合同、稳定摘要和 package 来源证据。

    参数：无。返回：无；断言模板、来源与合同扩展逐字段一致。异常：投影或断言
    失败时由 pytest 报告。
    """

    projected = _projected_contract()
    assert projected is not None
    template = projected.template
    catalog = PublishedSourceCatalog.from_records(_source_records())

    assert template["resource_template_uuid"] == HOST_RESOURCE_TEMPLATE_UUID
    assert template["name"] == f"workflow:{WORKFLOW_UUID}"
    assert template["class"] == "c1_published_lab.workflows.child:prepare_sample"
    assert (template["type"], template["node_type"]) == ("workflow", "workflow")
    assert template["meta_data"]["unilab"] == {
        "framework_owner_only": True,
        "workflow_source": {
            "kind": "package",
            "definition_fqid": "c1_published_lab.workflows.prepare_sample",
            "module": "c1_published_lab.workflows.child",
            "symbol": "prepare_sample",
            "package_catalog_digest": catalog.digest,
            "definition_content_hash": DEFINITION_CONTENT_HASH,
        },
    }
    extension = template["schema"]["x-unilabos-workflow-contract"]
    assert extension == {
        "version": 1,
        "compatibility_version": 1,
        "workflow_uuid": WORKFLOW_UUID,
        "workflow_revision": 7,
        "applied_source_hash": APPLIED_SOURCE_HASH,
        "contract_digest": CONTRACT_DIGEST,
        "composition_allow_transparent": False,
        "input_order": ["value"],
        "output_order": ["result"],
    }


def test_optional_default_is_published_in_schema_and_remains_canonical(
    tmp_path: Path,
) -> None:
    """可选输入默认值同时进入 Schema 与聚合，且持久化后仍为规范合同。

    参数：``tmp_path`` 隔离模板投影存储。返回：无；通过公共投影和持久化读取
    断言默认值合同闭合。异常：投影、存储或断言失败时由 pytest 报告。
    """

    snapshot = _applied_snapshot()
    parameter = snapshot["workflow"]["meta_data"]["unilab"]["input_contract"][
        "parameters"
    ][0]
    parameter.update({"required": False, "default": 2.5})
    catalog = PublishedSourceCatalog.from_records(_source_records())
    projected = project_published_workflow_contract(
        source=catalog.resolve(
            "c1_published_lab.workflows.child", "prepare_sample"
        ),
        applied_snapshot=snapshot,
        host_node_resource_template={
            "uuid": HOST_RESOURCE_TEMPLATE_UUID,
            "name": "host_node",
            "display_name": "Host Node",
        },
    )
    assert projected is not None
    goal_schema = projected.template["schema"]["properties"]["goal"]
    assert goal_schema["properties"]["value"] == {
        "type": "number",
        "default": 2.5,
    }
    assert projected.template["goal_default"] == {"value": 2.5}
    assert projected.handles[0]["meta_data"]["unilab"]["value_schema"] == {
        "type": "number"
    }

    store = WorkflowStore(tmp_path / "workflow-default.db")
    try:
        generation = RegistryTemplateProjectionStore(store).replace_generation(
            authority_id="local",
            node_templates=[_group_template(), projected.template],
            handle_templates=projected.handles,
            resource_template_symbols={},
        )
        template = next(
            item for item in generation.node_templates if item["type"] == "workflow"
        )
        handles = [
            item
            for item in generation.handle_templates
            if item["workflow_node_template_uuid"] == template["uuid"]
        ]
        assert published_workflow_projection_is_canonical(template, handles)
    finally:
        store.close()


def test_projection_emits_business_handles_then_separate_ready_handles() -> None:
    """工作流边界按输入、输出和两个 ready 结构连接点的顺序发布。

    参数：无。返回：无；断言业务与结构连接点（Handle）顺序及语义。异常：投影
    或断言失败时由 pytest 报告。
    """

    projected = _projected_contract()
    assert projected is not None

    handles = [
        {
            "key": item["handle_key"],
            "io": item["io_type"],
            "type": item["type"],
            "required": item["required"],
            "data_source": item["data_source"],
            "data_key": item["data_key"],
            "meta_data": item["meta_data"],
        }
        for item in projected.handles
    ]
    assert handles == [
        {
            "key": "value",
            "io": "target",
            "type": "number",
            "required": True,
            "data_source": "goal",
            "data_key": "value",
            "meta_data": projected.handles[0]["meta_data"],
        },
        {
            "key": "result",
            "io": "source",
            "type": "number",
            "required": False,
            "data_source": "result",
            "data_key": "result",
            "meta_data": projected.handles[1]["meta_data"],
        },
        {
            "key": "ready",
            "io": "target",
            "type": "default",
            "required": False,
            "data_source": None,
            "data_key": None,
            "meta_data": {},
        },
        {
            "key": "ready",
            "io": "source",
            "type": "default",
            "required": False,
            "data_source": None,
            "data_key": None,
            "meta_data": {},
        },
    ]


def test_stale_contract_and_missing_host_owner_preserve_previous_generation(
    tmp_path: Path,
) -> None:
    """陈旧修订不发布，宿主所有者缺失也不改变既有模板代际。

    参数：``tmp_path`` 隔离投影存储。返回：无；断言失败发布不污染既有代际。
    异常：预期错误未抛、存储或断言失败时由 pytest 报告。
    """

    catalog = PublishedSourceCatalog.from_records(_source_records())
    source = catalog.resolve("c1_published_lab.workflows.child", "prepare_sample")
    stale = _applied_snapshot()
    stale["applied_source"] = {
        **stale["applied_source"],
        "workflow_revision": 6,
    }
    assert project_published_workflow_contract(
        source=source,
        applied_snapshot=stale,
        host_node_resource_template={
            "uuid": HOST_RESOURCE_TEMPLATE_UUID,
            "name": "host_node",
            "display_name": "Host Node",
        },
    ) is None

    store = WorkflowStore(tmp_path / "workflow.db")
    projection_store = RegistryTemplateProjectionStore(store)
    try:
        before = projection_store.replace_generation(
            authority_id="local",
            node_templates=[_group_template()],
            handle_templates=[],
            resource_template_symbols={},
        )
        before_uuid = before.node_templates[0]["uuid"]

        with pytest.raises(PublishedWorkflowContractError) as missing_host:
            project_published_workflow_contract(
                source=source,
                applied_snapshot=deepcopy(_applied_snapshot()),
                host_node_resource_template=None,
            )
        assert missing_host.value.code == "composite_catalog_mismatch"

        current = projection_store.load_generation(authority_id="local")
        assert current.generation == before.generation
        assert [item["uuid"] for item in current.node_templates] == [before_uuid]

        published = _projected_contract()
        after = projection_store.replace_generation(
            authority_id="local",
            node_templates=[_group_template(), published.template],
            handle_templates=published.handles,
            resource_template_symbols={},
        )
        after_by_name = {item["name"]: item["uuid"] for item in after.node_templates}
        assert after_by_name["group"] == before_uuid
        assert f"workflow:{WORKFLOW_UUID}" in after_by_name
    finally:
        store.close()
