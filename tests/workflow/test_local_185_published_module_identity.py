"""LOCAL-185 已发布工作流（Published Workflow）模块身份回归合同。"""

from __future__ import annotations

from typing import Any

import pytest

from unilabos.workflow.authoring_kernel import AuthoringCatalogSnapshot
from unilabos.workflow.composite import CompositeAuthoring
from unilabos.workflow.published_workflow_runtime import (
    PublishedWorkflowGeneration,
    PublishedWorkflowGenerationError,
    build_published_workflow_generation,
)

from .test_c1_r2_static_expansion_contract import (
    CHILD_WORKFLOW_UUID,
    HOST_RESOURCE_TEMPLATE_UUID,
    INVOCATION_UUID,
    PARENT_WORKFLOW_UUID,
    MemorySnapshotProvider,
    _world_components,
)


def _published_generation(
    *,
    package_id: str,
    relative_path: str,
    provider: MemorySnapshotProvider | None = None,
) -> PublishedWorkflowGeneration:
    """用一项活动来源构造已发布工作流目录代际。

    参数：``package_id`` 是发布包的稳定导入身份；``relative_path`` 是来源注册
    提供的包内路径；``provider`` 可复用组合展开夹具的只读已应用快照端口。
    返回：包含规范来源模块身份和工作流节点模板的完整发布代际。
    异常：来源身份、快照或宿主模板不合法时由生产构造接口原样抛出。
    """

    # ``snapshot_provider`` 是本测试唯一的已应用工作流快照权威读取端口。
    snapshot_provider = provider
    if snapshot_provider is None:
        # ``world_components`` 提供已完成合同校验的快照端口和模板目录测试世界。
        world_components = _world_components()
        snapshot_provider = world_components[1]
    # ``workflow_snapshot`` 是与来源注册中工作流 UUID 对应的冻结应用事实。
    workflow_snapshot = snapshot_provider.snapshots[CHILD_WORKFLOW_UUID]
    # ``workflow_metadata`` 保存发布运行时取得作者函数符号的同修订元数据。
    workflow_metadata = workflow_snapshot["workflow"]["meta_data"]["unilab"]
    workflow_metadata["authoring_function_name"] = "prepare_sample"
    # ``registration`` 模拟工作区包目录向发布运行时交付的来源身份。
    registration = {
        "workflow_uuid": CHILD_WORKFLOW_UUID,
        "package_id": package_id,
        "relative_path": relative_path,
        "source_uri": f"package://{package_id}/{relative_path}",
    }
    # ``host_template`` 只提供发布工作流模板所属宿主资源模板的稳定身份。
    host_template: dict[str, Any] = {
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
    return build_published_workflow_generation(
        registrations=(registration,),
        snapshot_provider=snapshot_provider,
        base_node_templates=(host_template,),
    )


def test_package_rooted_source_path_does_not_duplicate_package_module() -> None:
    """包根已出现在来源首段时，已发布模块身份只保留一次包根。

    参数：无。返回：无；断言 SZLab 形式的包根路径生成唯一绝对模块。
    异常：发布代际构造或断言失败时由 pytest 报告。
    """

    # ``generation`` 是 PackageCatalog 已交付包根路径时的发布目录事实。
    generation = _published_generation(
        package_id="szlab_poly_studio",
        relative_path="szlab_poly_studio/workflows/material_transfer.py",
    )

    assert generation.source_catalog.sources[0].module == (
        "szlab_poly_studio.workflows.material_transfer"
    )


def test_package_relative_source_path_receives_package_module_prefix() -> None:
    """来源不含包根时，已发布模块身份仍准确前置一次包身份。

    参数：无。返回：无；断言既有 ``workflows/*.py`` 注册语义保持不变。
    异常：发布代际构造或断言失败时由 pytest 报告。
    """

    # ``generation`` 是既有包根相对来源路径对应的发布目录事实。
    generation = _published_generation(
        package_id="szlab_poly_studio",
        relative_path="workflows/material_transfer.py",
    )

    assert generation.source_catalog.sources[0].module == (
        "szlab_poly_studio.workflows.material_transfer"
    )


def test_similar_source_prefix_is_not_mistaken_for_package_root() -> None:
    """仅首段精确等于包身份才去重，前缀相似的子包名必须保留。

    参数：无。返回：无；断言 ``szlab_poly_studio`` 不会被误判为
    ``szlab_poly`` 的重复包根。异常：发布代际构造或断言失败时由 pytest 报告。
    """

    # ``generation`` 验证近似字符串不会改变来源的真实模块层级。
    generation = _published_generation(
        package_id="szlab_poly",
        relative_path="szlab_poly_studio/workflows/material_transfer.py",
    )

    assert generation.source_catalog.sources[0].module == (
        "szlab_poly.szlab_poly_studio.workflows.material_transfer"
    )


def test_python_module_named_like_package_is_not_a_repeated_root_segment() -> None:
    """与包同名的 Python 文件仍是模块，不得被误删为重复包根。

    参数：无。返回：无；断言 ``package_id=pkg`` 与 ``relative_path=pkg.py``
    形成 ``pkg.pkg``。异常：发布代际构造或断言失败时由 pytest 报告。
    """

    # ``generation`` 表达包根下恰有一个同名 Python 模块的合法来源身份。
    generation = _published_generation(
        package_id="pkg",
        relative_path="pkg.py",
    )

    assert generation.source_catalog.sources[0].module == "pkg.pkg"


@pytest.mark.parametrize(
    "relative_path",
    (
        "workflows/demo.txt",
        "workflows/demo",
        "./workflows/demo.py",
        "workflows/./demo.py",
        "workflows//demo.py",
        "/workflows/demo.py",
        "workflows/../demo.py",
    ),
    ids=(
        "non-python-suffix",
        "missing-python-suffix",
        "leading-dot-segment",
        "inner-dot-segment",
        "empty-segment",
        "absolute-path",
        "parent-segment",
    ),
)
def test_noncanonical_or_non_python_source_path_is_rejected(
    relative_path: str,
) -> None:
    """已发布来源只接受规范相对路径和精确 ``.py`` 后缀。

    参数：``relative_path`` 是缺少 Python 后缀、含路径归一化歧义、绝对根或
    父目录跳转的非规范来源身份。返回：无；断言构造关闭式拒绝。
    异常：未抛出稳定 ``PublishedWorkflowGenerationError`` 时由 pytest 报告。
    """

    with pytest.raises(
        PublishedWorkflowGenerationError,
        match="工作流来源不能转换为绝对模块",
    ):
        _published_generation(
            package_id="pkg",
            relative_path=relative_path,
        )


def test_package_rooted_published_source_resolves_composite_invocation() -> None:
    """包根路径发布后，组合工作流调用可命中子工作流且完成静态展开。

    参数：无。返回：无；断言真实组合创作接缝不再返回
    ``composite_child_not_found``，并产生调用节点和内部动作节点。
    异常：发布目录、组合展开或断言失败时由 pytest 报告。
    """

    # ``world_components`` 提供一致的应用快照、节点模板与连接点（Handle）全集。
    world_components = _world_components()
    provider = world_components[1]
    catalog = world_components[2]
    # ``generation`` 是用包根已存在路径构成的实际已发布来源目录。
    generation = _published_generation(
        package_id="c1_published_lab",
        relative_path="c1_published_lab/workflows/child.py",
        provider=provider,
    )
    # ``published_template`` 使用本次发布代际的来源证据，同时沿用测试投影已经
    # 分配的稳定节点模板 UUID，模拟同一事务完成身份物化后的真实目录。
    published_template = {
        **generation.node_templates[0],
        "uuid": next(
            action.template["uuid"]
            for action in catalog.actions
            if action.template["name"] == f"workflow:{CHILD_WORKFLOW_UUID}"
        ),
    }
    # ``templates`` 用本代已发布工作流模板替换旧夹具中的等价投影，保证目录
    # 证据摘要与来源解析器来自同一代际。
    templates = [
        (
            published_template
            if action.template["name"] == f"workflow:{CHILD_WORKFLOW_UUID}"
            else action.detached_template()
        )
        for action in catalog.actions
    ]
    # ``handles`` 的边界合同未变化，继续使用已经物化稳定 UUID 的连接点集合。
    handles = [
        handle for action in catalog.actions for handle in action.detached_handles()
    ]
    # ``published_catalog`` 模拟模板投影事务提交后的同代只读创作目录。
    published_catalog = AuthoringCatalogSnapshot.from_entities(templates, handles)
    # ``composite_authoring`` 复用生产组合展开深模块，只替换本次发布来源目录。
    composite_authoring = CompositeAuthoring(
        snapshot_provider=provider,
        catalog=published_catalog,
        resolver=generation.source_catalog,
    )

    # ``expansion`` 是父工作流对已发布子工作流的一次静态展开结果。
    expansion = composite_authoring.compile_invocation(
        parent_workflow_uuid=PARENT_WORKFLOW_UUID,
        invocation_uuid=INVOCATION_UUID,
        module="c1_published_lab.workflows.child",
        symbol="prepare_sample",
        keyword_arguments={"value": 7.5},
    )

    assert not any(
        diagnostic["code"] == "composite_child_not_found"
        for diagnostic in expansion.diagnostics
    )
    assert expansion.diagnostics == ()
    assert expansion.invocation_node is not None
    assert len(expansion.nodes) == 1
