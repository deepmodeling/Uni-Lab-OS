"""F05.2 物料图校验（Material Graph Validation）的纯图测试夹具。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.authoring_identity import authoring_edge_uuid
from unilabos.workflow.authoring_kernel import AuthoringCatalogSnapshot
from unilabos.workflow.models import CandidateCompilation
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore
from unilabos.workflow.template_projection_store import (
    RegistryTemplateProjectionStore,
)

from .test_authoring_engine import (
    PREPARE_READY_SOURCE,
    PREPARE_READY_TARGET,
    PREPARE_SAMPLE_TARGET,
    PREPARE_TEMPLATE_UUID,
    WORKFLOW_UUID,
    _applied_graph,
    _handle,
    _template,
)
from .test_f05_material_source_authoring import (
    MATERIAL_SOURCE_HANDLE_UUID,
    MATERIAL_SOURCE_NODE_UUID,
    PLATE_SOURCE_SYMBOL,
    PLATE_TEMPLATE_UUID,
    PREPARE_NODE_UUID,
    _material_source_template,
    _source,
)

SECOND_CONSUMER_NODE_UUID = "20000000-0000-4000-8000-000000000012"
PASSTHROUGH_NODE_UUID = "20000000-0000-4000-8000-000000000013"
PASSTHROUGH_TEMPLATE_UUID = "30000000-0000-4000-8000-000000000013"
PASSTHROUGH_TARGET_UUID = "40000000-0000-4000-8000-000000000013"
PASSTHROUGH_SOURCE_UUID = "40000000-0000-4000-8000-000000000014"
INCOMPATIBLE_TEMPLATE_UUID = "32000000-0000-4000-8000-000000000099"


@dataclass(frozen=True, slots=True)
class MaterialGraphStoreContext:
    """直接保存物料图（Material Graph）测试的持久化上下文。

    ``store`` 是真实 SQLite 存储适配器（Store Adapter），``service`` 提供公共
    读取投影，``applied_graph`` 是测试动作前的工作流权威快照。
    """

    store: WorkflowStore
    service: WorkflowService
    applied_graph: dict[str, Any]


def material_graph_engine(
    *,
    include_passthrough: bool = False,
    prepare_allowlist: tuple[str, ...] | None = None,
    passthrough_input_allowlist: tuple[str, ...] | None = None,
    passthrough_output_allowlist: tuple[str, ...] | None = None,
    passthrough_implicit: bool = True,
) -> WorkflowAuthoringEngine:
    """构造 F05.2 所需的纯工作流创作编译器（Authoring Compiler）。

    参数说明：``include_passthrough`` 决定是否加入带同名输入/输出的普通动作
    （Action）；三个 ``allowlist`` 参数分别限制最终消费者、透传输入与透传输出
    的资源模板（ResourceTemplate）；``passthrough_implicit`` 标记服务端生成的
    同名输出。函数中的模板与连接点（Handle）局部变量组成不可变目录快照。
    返回：不访问数据库或库存（Inventory）的编译器。
    """

    templates, handles = material_graph_catalog_entities(
        include_passthrough=include_passthrough,
        prepare_allowlist=prepare_allowlist,
        passthrough_input_allowlist=passthrough_input_allowlist,
        passthrough_output_allowlist=passthrough_output_allowlist,
        passthrough_implicit=passthrough_implicit,
    )
    return WorkflowAuthoringEngine(
        catalog=AuthoringCatalogSnapshot.from_entities(
            templates,
            handles,
            resource_template_symbols={PLATE_SOURCE_SYMBOL: PLATE_TEMPLATE_UUID},
        )
    )


def material_graph_catalog_entities(
    *,
    include_passthrough: bool = False,
    prepare_allowlist: tuple[str, ...] | None = None,
    passthrough_input_allowlist: tuple[str, ...] | None = None,
    passthrough_output_allowlist: tuple[str, ...] | None = None,
    passthrough_implicit: bool = True,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """构造物料图测试使用的目录实体。

    参数说明：布尔参数决定是否加入透传动作；三个允许集合分别投影到最终消费
    输入、透传输入和透传输出；``passthrough_implicit`` 决定输出保证是否应从
    同名输入继承。返回：节点模板与连接点（Handle）模板列表。
    """

    source_template, source_handle = _material_source_template()
    prepare_template, prepare_handles = _template(
        PREPARE_TEMPLATE_UUID,
        name="prepare",
        handles=[
            _handle(
                PREPARE_SAMPLE_TARGET,
                node_template_uuid=PREPARE_TEMPLATE_UUID,
                key="sample",
                io_type="target",
                value_type="ResourceSlot",
                required=True,
            ),
            _handle(
                PREPARE_READY_TARGET,
                node_template_uuid=PREPARE_TEMPLATE_UUID,
                key="ready",
                io_type="target",
                value_type="any",
                data_source="dependency",
            ),
            _handle(
                PREPARE_READY_SOURCE,
                node_template_uuid=PREPARE_TEMPLATE_UUID,
                key="ready",
                io_type="source",
                value_type="any",
                data_source="dependency",
            ),
        ],
    )
    _set_resource_template_allowlist(prepare_handles[0], prepare_allowlist)
    templates = [source_template, prepare_template]
    handles = [source_handle, *prepare_handles]
    if include_passthrough:
        passthrough_template, passthrough_handles = _template(
            PASSTHROUGH_TEMPLATE_UUID,
            name="pass_material",
            handles=[
                _handle(
                    PASSTHROUGH_TARGET_UUID,
                    node_template_uuid=PASSTHROUGH_TEMPLATE_UUID,
                    key="sample",
                    io_type="target",
                    value_type="ResourceSlot",
                    required=True,
                ),
                _handle(
                    PASSTHROUGH_SOURCE_UUID,
                    node_template_uuid=PASSTHROUGH_TEMPLATE_UUID,
                    key="sample",
                    io_type="source",
                    value_type="ResourceSlot",
                ),
            ],
        )
        _set_resource_template_allowlist(
            passthrough_handles[0],
            passthrough_input_allowlist,
        )
        _set_resource_template_allowlist(
            passthrough_handles[1],
            passthrough_output_allowlist,
        )
        passthrough_handles[1]["meta_data"]["unilab"]["implicit_passthrough"] = (
            passthrough_implicit
        )
        templates.append(passthrough_template)
        handles.extend(passthrough_handles)
    return templates, handles


@contextmanager
def opened_material_graph_store(
    database_path: Path,
    *,
    prepare_allowlist: tuple[str, ...] | None,
    workflow_meta_data: dict[str, Any] | None = None,
) -> Iterator[MaterialGraphStoreContext]:
    """打开带指定消费模板约束的真实工作流写模型。

    参数说明：``database_path`` 是隔离 SQLite 文件；``prepare_allowlist`` 是
    消费动作接受的资源模板 UUID 集合；``workflow_meta_data`` 可冻结工作流
    输入/输出（Workflow I/O）合同。局部 ``projection_store`` 把同一目录投影进
    数据库。返回：服务和保存前权威图；退出时关闭服务。
    """

    templates, handles = material_graph_catalog_entities(
        prepare_allowlist=prepare_allowlist,
    )
    store = WorkflowStore(database_path)
    projection_store = RegistryTemplateProjectionStore(store)
    projected_templates, projected_handles = projection_store.replace(
        authority_id="f05-material-graph",
        node_templates=templates,
        handle_templates=[
            _projection_handle(handle, templates=templates) for handle in handles
        ],
    )
    engine = WorkflowAuthoringEngine(
        catalog=AuthoringCatalogSnapshot.from_entities(
            projected_templates,
            projected_handles,
            resource_template_symbols={PLATE_SOURCE_SYMBOL: PLATE_TEMPLATE_UUID},
        )
    )
    service = WorkflowService(store, compiler=engine)
    workflow_values = {
        "workflow_uuid": WORKFLOW_UUID,
        "name": "Material template compatibility",
        "tags": [],
        "description": None,
        "meta_data": (
            deepcopy(workflow_meta_data) if workflow_meta_data is not None else {}
        ),
    }
    if workflow_meta_data is None:
        service.create_workflow(**workflow_values)
    else:
        # ``workflow_values`` 在存储适配器测试中冻结服务端管理的 Workflow I/O
        # 元数据；公共创建接口按设计会移除该保留命名空间。
        store.create_workflow(**workflow_values)
    try:
        yield MaterialGraphStoreContext(
            store=store,
            service=service,
            applied_graph=service.get_graph(WORKFLOW_UUID),
        )
    finally:
        service.close()


def compile_material_source_graph(
    engine: WorkflowAuthoringEngine,
    source: str,
) -> CandidateCompilation:
    """通过公共接缝编译一份物料来源（MaterialSource）作者源码。

    参数说明：``engine`` 是纯编译器，``source`` 是待验证 Python 文本。
    返回：候选编译结果（CandidateCompilation）；函数不读取实时库存。
    """

    return engine.compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        python_source=source,
        source_uri="package://lab/workflows/f05_material_graph.py",
        applied_graph=_applied_graph(),
    )


def single_chain_source() -> str:
    """返回物料来源到单一消费动作的合法线性源码。

    参数：无。返回：不访问物料权威的确定性 Python 文本。异常：无。
    """

    return _source()


def ordered_reuse_source() -> str:
    """返回同一物料占位符（ResourceSlot）被两个顺序动作复用的源码。

    参数：无。返回：带两个物理消费者且由作者源码顺序形成严格全序的确定性
    Python 文本。异常：无；真正无序的兄弟分叉由 ``fan_out_candidate`` 构造。
    """

    return _source().replace(
        "    prepared = reactor.prepare(sample=assay_plate)",
        (
            "    prepared = reactor.prepare(sample=assay_plate)\n"
            f"    # unilab:node_uuid={SECOND_CONSUMER_NODE_UUID}\n"
            "    duplicate = reactor.prepare(sample=assay_plate)"
        ),
    )


def passthrough_chain_source() -> str:
    """返回同名物料输入/输出顺序透传到最终动作的合法源码。

    参数：无。返回：含隐式物料透传的确定性 Python 文本。异常：无。
    """

    return _source().replace(
        (
            f"    # unilab:node_uuid={PREPARE_NODE_UUID}\n"
            "    prepared = reactor.prepare(sample=assay_plate)"
        ),
        (
            f"    # unilab:node_uuid={PASSTHROUGH_NODE_UUID}\n"
            "    passed = reactor.pass_material(sample=assay_plate)\n"
            f"    # unilab:node_uuid={PREPARE_NODE_UUID}\n"
            "    prepared = reactor.prepare(sample=passed.sample)"
        ),
    )


def fan_out_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    """从合法单链候选构造同一物料输出的双消费反例。

    参数说明：``candidate`` 是合法后端（Backend）形状图；``duplicate_node``
    与 ``duplicate_edge`` 是保持模板身份、只改变节点与边身份的第二消费者。
    返回：不修改输入的非法完整图。
    """

    graph = deepcopy(candidate)
    original_node = next(
        node for node in graph["nodes"] if node["uuid"] == PREPARE_NODE_UUID
    )
    duplicate_node = deepcopy(original_node)
    duplicate_node["uuid"] = SECOND_CONSUMER_NODE_UUID
    duplicate_node["name"] = "Duplicate prepare"
    duplicate_node["meta_data"]["unilab"]["authoring_result_name"] = "duplicate"
    graph["nodes"].append(duplicate_node)
    original_edge = graph["edges"][0]
    duplicate_edge = deepcopy(original_edge)
    duplicate_edge.update(
        {
            "uuid": authoring_edge_uuid(
                workflow_uuid=WORKFLOW_UUID,
                source_node_uuid=MATERIAL_SOURCE_NODE_UUID,
                source_handle_uuid=MATERIAL_SOURCE_HANDLE_UUID,
                target_node_uuid=SECOND_CONSUMER_NODE_UUID,
                target_handle_uuid=PREPARE_SAMPLE_TARGET,
            ),
            "target_node_uuid": SECOND_CONSUMER_NODE_UUID,
        }
    )
    graph["edges"].append(duplicate_edge)
    graph["edges"].sort(key=lambda edge: edge["uuid"])
    return graph


def candidate_with_prepare_allowlist(
    candidate: dict[str, Any],
    allowlist: tuple[str, ...],
) -> dict[str, Any]:
    """替换候选图最终消费者的资源模板允许集合。

    参数说明：``candidate`` 是合法完整图，``allowlist`` 是新的非空允许集合；
    局部 ``target`` 是最终消费连接点（Handle）。返回：不修改输入的候选图。
    """

    graph = deepcopy(candidate)
    target = next(
        handle
        for handle in graph["handle_templates"]
        if handle["uuid"] == PREPARE_SAMPLE_TARGET
    )
    _set_resource_template_allowlist(target, allowlist)
    return graph


def _set_resource_template_allowlist(
    handle: dict[str, Any],
    allowlist: tuple[str, ...] | None,
) -> None:
    """在连接点（Handle）元数据中设置可选资源模板允许集合。

    参数说明：``handle`` 是可变测试目录实体；``allowlist`` 为 ``None`` 时删除
    旁路字段，非空时写入独立列表。返回：无；只修改传入测试对象。
    """

    unilab = handle["meta_data"]["unilab"]
    if allowlist is None:
        unilab.pop("allowed_resource_template_uuids", None)
    else:
        unilab["allowed_resource_template_uuids"] = list(allowlist)


def _projection_handle(
    handle: dict[str, Any],
    *,
    templates: list[dict[str, Any]],
) -> dict[str, Any]:
    """把测试连接点转换为模板投影存储接受的业务键形状。

    参数说明：``handle`` 是完整连接点，``templates`` 用于解析父节点模板业务
    键。局部 ``parent`` 是唯一父模板。返回：保留显式连接点 UUID 的新字典；
    找不到匹配父模板时，``next()`` 抛出 ``StopIteration``，使测试目录装配失败
    关闭而不伪造父模板身份。
    """

    candidate = deepcopy(handle)
    parent_uuid = candidate.pop("workflow_node_template_uuid")
    parent = next(template for template in templates if template["uuid"] == parent_uuid)
    candidate["node_business_key"] = (
        parent["resource_template_uuid"],
        parent["name"],
    )
    return candidate
