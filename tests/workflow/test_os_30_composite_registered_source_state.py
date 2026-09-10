"""OS#30 组合工作流（Composite Workflow）子来源三态合同。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from unilabos.workflow.authoring_kernel import AuthoringCatalogSnapshot
from unilabos.workflow.composite import CompositeAuthoring, CompositeExpansion
from unilabos.workflow.published_workflow_runtime import (
    PublishedWorkflowGeneration,
    build_published_workflow_generation,
)

from .test_c1_r2_static_expansion_contract import (
    APPLIED_SOURCE_HASH,
    CHILD_TEMPLATE_UUID,
    CHILD_WORKFLOW_UUID,
    HOST_RESOURCE_TEMPLATE_UUID,
    INVOCATION_UUID,
    PARENT_WORKFLOW_UUID,
    MemorySnapshotProvider,
    _world_components,
)

_MODULE = "c1_published_lab.workflows.child"
_SYMBOL = "prepare_sample"
_CATALOG_SOURCE_HASH = "sha256:" + "4" * 64


def _registration() -> dict[str, str]:
    """构造同一冻结包目录（PackageCatalog）交付的完整工作流源码身份。

    参数：无。
    返回：包含模块、符号、内容哈希和既有持久来源字段的独立字典。
    异常：无；夹具只包含静态常量。
    """

    return {
        "workflow_uuid": CHILD_WORKFLOW_UUID,
        "package_id": "c1_published_lab",
        "relative_path": "workflows/child.py",
        "source_uri": "package://c1_published_lab/workflows/child.py",
        "module": _MODULE,
        "symbol": _SYMBOL,
        "definition_content_hash": _CATALOG_SOURCE_HASH,
    }


def _host_template() -> dict[str, Any]:
    """构造发布模板投影需要的唯一宿主节点（Host Node）摘要。

    参数：无。
    返回：只包含宿主资源模板稳定身份的节点模板字典。
    异常：无；夹具不读取外部状态。
    """

    return {
        "meta_data": {
            "unilab": {
                "resource_template": {
                    "uuid": HOST_RESOURCE_TEMPLATE_UUID,
                    "name": "host_node",
                    "display_name": "宿主节点",
                }
            }
        }
    }


def _generation(
    *,
    registrations: tuple[dict[str, str], ...],
    provider: MemorySnapshotProvider,
) -> PublishedWorkflowGeneration:
    """通过生产构造入口冻结一代工作流来源与模板投影。

    参数：``registrations`` 是活动包目录来源；``provider`` 是同视图工作流快照
    读取端口。
    返回：同一来源摘要下的解析目录、节点模板和连接点（Handle）模板。
    异常：来源或快照违反发布合同时由生产入口原样抛出。
    """

    return build_published_workflow_generation(
        registrations=registrations,
        snapshot_provider=provider,
        base_node_templates=(_host_template(),),
    )


def _compile(
    *,
    generation: PublishedWorkflowGeneration,
    provider: MemorySnapshotProvider,
    catalog: AuthoringCatalogSnapshot,
) -> CompositeExpansion:
    """用指定冻结代际编译一次组合工作流调用（Composite Workflow Invocation）。

    参数：``generation`` 提供来源解析；``provider`` 提供子图快照；``catalog``
    提供创作模板投影。
    返回：成功展开或带稳定诊断的关闭式结果。
    异常：生产公共入口未收敛的编程错误原样传播。
    """

    # ``authoring`` 只消费传入的冻结来源和模板，不导入或执行作者源码。
    authoring = CompositeAuthoring(
        snapshot_provider=provider,
        catalog=catalog,
        resolver=generation.source_catalog,
    )
    return authoring.compile_invocation(
        parent_workflow_uuid=PARENT_WORKFLOW_UUID,
        invocation_uuid=INVOCATION_UUID,
        module=_MODULE,
        symbol=_SYMBOL,
        keyword_arguments={"value": 7.5},
    )


def test_unregistered_child_source_remains_not_found() -> None:
    """未被活动包目录登记的子来源保持 ``composite_child_not_found``。

    参数：无。
    返回：无；断言未登记来源没有解析身份且不读取子工作流快照。
    异常：生产诊断或读取边界漂移时由 pytest 报告。
    """

    _authoring, provider, catalog, _source_catalog = _world_components()
    provider.read_count = 0
    # ``generation`` 是明确不含该模块/符号的活动包目录代际。
    generation = _generation(registrations=(), provider=provider)

    expansion = _compile(
        generation=generation,
        provider=provider,
        catalog=catalog,
    )

    assert [item["code"] for item in expansion.diagnostics] == [
        "composite_child_not_found"
    ]
    assert provider.read_count == 0


def test_registered_but_unapplied_child_source_is_distinguished() -> None:
    """已登记但没有同修订应用快照的子来源返回 ``composite_child_unapplied``。

    参数：无。
    返回：无；断言来源仍可解析、没有发布模板，并读取一次持久快照后准确诊断。
    异常：来源被错误过滤或诊断退化时由 pytest 报告。
    """

    _authoring, original_provider, catalog, _source_catalog = _world_components()
    # ``unapplied_snapshot`` 保留已登记工作流定义，只移除同修订应用事实。
    unapplied_snapshot = deepcopy(original_provider.snapshots[CHILD_WORKFLOW_UUID])
    unapplied_snapshot["applied_source"] = None
    # ``provider`` 是本测试唯一持久快照读取端口。
    provider = MemorySnapshotProvider({CHILD_WORKFLOW_UUID: unapplied_snapshot})
    generation = _generation(
        registrations=(_registration(),),
        provider=provider,
    )

    expansion = _compile(
        generation=generation,
        provider=provider,
        catalog=catalog,
    )

    resolved = generation.source_catalog.resolve(_MODULE, _SYMBOL)
    assert resolved.workflow_uuid == CHILD_WORKFLOW_UUID
    assert resolved.definition_content_hash == _CATALOG_SOURCE_HASH
    assert generation.node_templates == ()
    assert generation.handle_templates == ()
    assert [item["code"] for item in expansion.diagnostics] == [
        "composite_child_unapplied"
    ]
    assert provider.read_count == 2


def test_registered_and_applied_child_source_expands_statically() -> None:
    """已登记且具有同修订应用快照的子来源完成静态展开（Static Expansion）。

    参数：无。
    返回：无；断言来源、模板和组合解析来自同一代际并产生一个内部动作节点。
    异常：发布合同、模板投影或静态展开失败时由 pytest 报告。
    """

    _authoring, provider, catalog, _source_catalog = _world_components()
    generation = _generation(
        registrations=(_registration(),),
        provider=provider,
    )
    # ``published_template`` 保留测试创作目录已分配的稳定模板 UUID。
    published_template = {
        **generation.node_templates[0],
        "uuid": CHILD_TEMPLATE_UUID,
    }
    # ``base_templates`` 去掉旧夹具工作流模板，只保留普通动作模板。
    base_templates = [
        action.detached_template()
        for action in catalog.actions
        if action.template["uuid"] != CHILD_TEMPLATE_UUID
    ]
    # ``base_handles`` 同步去掉旧工作流边界连接点，避免目录重复身份。
    base_handles = [
        handle
        for action in catalog.actions
        if action.template["uuid"] != CHILD_TEMPLATE_UUID
        for handle in action.detached_handles()
    ]
    # ``existing_workflow_handles`` 仅提供模板投影层已分配的稳定连接点 UUID。
    existing_workflow_handles = next(
        action.detached_handles()
        for action in catalog.actions
        if action.template["uuid"] == CHILD_TEMPLATE_UUID
    )
    projected_handles = [
        {
            **handle,
            "uuid": existing_handle["uuid"],
            "workflow_node_template_uuid": CHILD_TEMPLATE_UUID,
        }
        for handle, existing_handle in zip(
            generation.handle_templates,
            existing_workflow_handles,
            strict=True,
        )
    ]
    # ``published_catalog`` 是来源与模板均来自本次构造的同代创作目录。
    published_catalog = AuthoringCatalogSnapshot.from_entities(
        [*base_templates, published_template],
        [*base_handles, *projected_handles],
    )

    expansion = _compile(
        generation=generation,
        provider=provider,
        catalog=published_catalog,
    )

    # 包目录来源文件摘要与应用快照规范源码摘要属于两类独立证据。
    assert _CATALOG_SOURCE_HASH != APPLIED_SOURCE_HASH
    assert expansion.diagnostics == ()
    assert expansion.invocation_node is not None
    assert len(expansion.nodes) == 1
