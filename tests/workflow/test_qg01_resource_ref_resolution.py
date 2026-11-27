"""QG01 SZLab ``resource_ref`` 真实物料身份解析合同。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.authoring_kernel import AuthoringCatalogSnapshot

from .test_authoring_engine import (
    PREPARE_SAMPLE_TARGET,
    PREPARE_TEMPLATE_UUID,
    WORKFLOW_UUID,
    _applied_graph,
    _handle,
    _template,
)
from .test_f05_material_source_authoring import (
    MATERIAL_SOURCE_NODE_UUID,
    MATERIAL_SOURCE_TEMPLATE_UUID,
    PLATE_SOURCE_SYMBOL,
    PLATE_TEMPLATE_UUID,
    _material_source_template,
)

# ``S3_WAREHOUSE_UUID`` 是库存权威（Inventory Authority）分配给 SZLab
# ``s3_unused_beaker`` 启动资源的稳定物料（Material）UUID。
S3_WAREHOUSE_UUID = "61000000-0000-4000-8000-000000000001"
# ``S3_WAREHOUSE_TEMPLATE_UUID`` 是该挂载资源对应的资源模板（ResourceTemplate）身份。
S3_WAREHOUSE_TEMPLATE_UUID = "62000000-0000-4000-8000-000000000001"
# ``POWDER_WAREHOUSE_UUID`` 是粉桶仓启动资源的实际物料（Material）UUID。
POWDER_WAREHOUSE_UUID = "61000000-0000-4000-8000-000000000002"
# ``ACTION_NODE_UUID`` 是直接消费 SZLab 启动资源的动作节点稳定身份。
ACTION_NODE_UUID = "63000000-0000-4000-8000-000000000001"
# ``ACTION_SAMPLE_SOURCE`` 是动作返回实际物料引用的源连接点（Handle）身份。
ACTION_SAMPLE_SOURCE = "63000000-0000-4000-8000-000000000002"


class _ResourceReferenceResolver:
    """模拟只读库存权威（Inventory Authority）的启动资源解析端口。"""

    def __init__(self, resources: Mapping[str, Mapping[str, str]]) -> None:
        """保存业务资源 ID 到实际物料身份的隔离映射。

        参数：``resources`` 的键是部署业务资源 ID，值包含物料 UUID 与资源模板
        UUID。返回：无。异常：无；非法回执由被测编译边界关闭式拒绝。
        """

        # ``_resources`` 是测试代际冻结的只读资源身份，不代表第二库存权威。
        self._resources = {
            resource_id: dict(resource) for resource_id, resource in resources.items()
        }

    def __call__(self, resource_id: str) -> dict[str, str] | None:
        """按部署业务资源 ID 返回实际物料身份。

        参数：``resource_id`` 是 ``resource_ref`` 的静态字符串。返回：命中时返回
        分离字典，未知身份返回 ``None``。异常：无。
        """

        # ``resolved`` 是本次编译读取的库存身份摘要，修改它不能污染夹具。
        resolved = self._resources.get(resource_id)
        return dict(resolved) if resolved is not None else None


class _AmbiguousResourceReferenceResolver:
    """模拟库存中同一部署业务 ID 命中多个物料的非法状态。"""

    def __call__(self, resource_id: str) -> dict[str, str]:
        """拒绝歧义业务资源 ID。

        参数：``resource_id`` 是待解析部署业务 ID。返回：永不正常返回。
        异常：固定抛出 ``ValueError``，模拟库存唯一性证明失败。
        """

        raise ValueError(f"资源 ID 不唯一: {resource_id}")


class _ForgedResourceReferenceResolver:
    """模拟把部署业务 ID 错当成物料 UUID 的非法适配器。"""

    def __call__(self, resource_id: str) -> dict[str, str]:
        """返回伪造的非 UUID 物料身份。

        参数：``resource_id`` 是待解析部署业务 ID。返回：故意把同一业务 ID
        放入 ``uuid``，同时给出合法模板 UUID。异常：无；被测边界必须拒绝。
        """

        return {
            "uuid": resource_id,
            "resource_template_uuid": S3_WAREHOUSE_TEMPLATE_UUID,
        }


def _catalog() -> AuthoringCatalogSnapshot:
    """构造物料来源（MaterialSource）及其消费动作的目录快照。

    参数：无。返回：带真实物料占位符（ResourceSlot）目标连接点和资源模板
    双向身份的不可变目录快照。异常：夹具违反目录合同时直接传播。
    """

    # ``source_template`` 与 ``source_handle`` 是物料来源（MaterialSource）框架合同。
    source_template, source_handle = _material_source_template()
    # ``prepare_template`` 与 ``prepare_handles`` 是普通动作（Action）合同，目标
    # 物料占位符（ResourceSlot）只接受 S3 仓库资源模板。
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
            )
        ],
    )
    prepare_handles[0]["meta_data"]["unilab"][
        "value_schema"
    ] = {
        "$slot": "ResourceSlot",
        "allowed_resource_template_uuids": [S3_WAREHOUSE_TEMPLATE_UUID],
    }
    return AuthoringCatalogSnapshot.from_entities(
        [source_template, prepare_template],
        [source_handle, *prepare_handles],
        resource_template_symbols={PLATE_SOURCE_SYMBOL: PLATE_TEMPLATE_UUID},
    )


def _resolver() -> _ResourceReferenceResolver:
    """返回能解析真实 SZLab 挂载资源的库存端口替身。

    参数：无。返回：S3 空烧杯仓与粉桶仓业务 ID 唯一映射到实际物料 UUID 与
    模板 UUID 的解析器。
    异常：无。
    """

    return _ResourceReferenceResolver(
        {
            "s3_unused_beaker": {
                "uuid": S3_WAREHOUSE_UUID,
                "resource_template_uuid": S3_WAREHOUSE_TEMPLATE_UUID,
            },
            "powder_container_warehouse": {
                "uuid": POWDER_WAREHOUSE_UUID,
                "resource_template_uuid": S3_WAREHOUSE_TEMPLATE_UUID,
            },
        }
    )


def _material_source_code() -> str:
    """生成使用真实 SZLab 业务资源 ID 的物料来源作者源码。

    参数：无。返回：``mount`` 使用 ``resource_ref('s3_unused_beaker')`` 的可信
    Python 源码。异常：无；源码只用于静态编译，不创建工作流任务
    （WorkflowTask）或执行动作。
    """

    return f'''from lab.resources import plate_96
from unilabos.workflow.authoring import (
    MaterialCustodyPolicy,
    MaterialFlowRole,
    material_source,
    resource_ref,
    workflow,
    workflow_output,
)


@workflow(workflow_uuid="{WORKFLOW_UUID}", displayname="S07 material source")
def s07_material_source():
    # unilab:node_uuid={MATERIAL_SOURCE_NODE_UUID}
    beaker = material_source(
        resource_template=plate_96,
        mode="existing",
        mount=resource_ref("s3_unused_beaker"),
        material_uuid=None,
        site=None,
        slot_range=None,
        flow_role=MaterialFlowRole.PRIMARY_SAMPLE,
        custody_policy=MaterialCustodyPolicy.TASK_EXCLUSIVE,
    )
    return workflow_output()
'''


def test_szlab_material_source_resource_id_resolves_to_actual_material_uuid() -> None:
    """SZLab 物料来源（MaterialSource）挂载业务 ID 必须解析为实际物料 UUID。

    参数：无。返回：无。断言：公共编译结果有效，选择器仅冻结库存权威返回的
    实际物料 UUID，不能把 ``s3_unused_beaker`` 业务 ID 冒充 UUID；本测试不
    创建工作流任务（WorkflowTask）或执行动作。
    """

    # ``engine`` 是注入只读库存身份解析端口的可信工作流创作编译器。
    engine = WorkflowAuthoringEngine(
        catalog=_catalog(),
        resource_reference_resolver=_resolver(),
    )
    # ``compiled`` 是仅含候选工作流图的静态编译回执。
    compiled = engine.compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        python_source=_material_source_code(),
        source_uri="package://szlab/workflows/s07_material_source.py",
        applied_graph=_applied_graph(),
    )

    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    # ``source_node`` 是物料来源（MaterialSource）候选节点，不是可执行动作节点。
    source_node = next(
        node
        for node in compiled.graph["nodes"]
        if node["workflow_node_template_uuid"] == MATERIAL_SOURCE_TEMPLATE_UUID
    )
    assert source_node["param"]["mount"] == {"uuid": S3_WAREHOUSE_UUID}
    assert source_node["param"]["mount"]["uuid"] != "s3_unused_beaker"
    assert compiled.normalized_python_source is not None
    assert 'resource_ref("s3_unused_beaker")' in compiled.normalized_python_source
    # ``repeated`` 证明规范源码往返仍通过库存解析，不把实际 UUID 改写成作者 ID。
    repeated = engine.compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        python_source=compiled.normalized_python_source,
        source_uri="package://szlab/workflows/s07_material_source.py",
        applied_graph=compiled.graph,
    )
    assert repeated.valid and repeated.graph == compiled.graph, repeated.diagnostics


def _action_code() -> str:
    """生成普通动作参数使用真实 SZLab 粉桶仓业务 ID 的作者源码。

    参数：无。返回：``sample`` 通过 ``resource_ref`` 静态提供的工作流源码。
    异常：无；源码不创建工作流任务（WorkflowTask）或执行动作。
    """

    return f'''from lab.devices import Reactor
from unilabos.workflow.authoring import device, resource_ref, workflow, workflow_output


reactor: Reactor = device()


@workflow(workflow_uuid="{WORKFLOW_UUID}", displayname="S07 action resource")
def s07_action_resource():
    # unilab:node_uuid={ACTION_NODE_UUID}
    prepared = reactor.prepare(
        sample=resource_ref("powder_container_warehouse"),
    )
    return workflow_output()
'''


def test_szlab_action_resource_ref_freezes_uuid_and_preserves_authoring_id() -> None:
    """普通动作的 ``resource_ref`` 必须冻结 UUID 且往返保留部署业务 ID。

    参数：无。返回：无。断言：动作参数只含实际物料 UUID，连接点元数据保留
    ``powder_container_warehouse`` 用于规范源码往返，重复编译语义不漂移；本
    测试不创建工作流任务（WorkflowTask）或执行动作。
    """

    # ``engine`` 复用同一库存代际解析器，保证首次编译与重复编译身份一致。
    engine = WorkflowAuthoringEngine(
        catalog=_catalog(),
        resource_reference_resolver=_resolver(),
    )
    # ``compiled`` 是普通动作（Action）静态参数已解析的候选编译结果。
    compiled = engine.compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        python_source=_action_code(),
        source_uri="package://szlab/workflows/s07_action_resource.py",
        applied_graph=_applied_graph(),
    )

    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    # ``action_node`` 是唯一普通动作节点，其 ``param`` 不能保留业务名称。
    action_node = compiled.graph["nodes"][0]
    assert action_node["param"]["sample"] == {"uuid": POWDER_WAREHOUSE_UUID}
    assert action_node["meta_data"]["unilab"]["resource_refs"] == {
        PREPARE_SAMPLE_TARGET: {"resource_id": "powder_container_warehouse"}
    }
    assert compiled.normalized_python_source is not None
    assert 'resource_ref("powder_container_warehouse")' in (
        compiled.normalized_python_source
    )
    # ``repeated`` 证明规范源码只重放身份解析，不把业务 ID 改写成 UUID 字面量。
    repeated = engine.compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        python_source=compiled.normalized_python_source,
        source_uri="package://szlab/workflows/s07_action_resource.py",
        applied_graph=compiled.graph,
    )
    assert repeated.valid and repeated.graph == compiled.graph, repeated.diagnostics


def test_action_resource_ref_accepts_projected_nested_material_schema() -> None:
    """真实动作模板投影的嵌套物料 Schema 必须可用于资源引用解析。

    参数：无。返回：无。断言：连接点（Handle）仍以 ``ResourceSlot`` 标识
    物料占位符、值 Schema 使用嵌套 ``properties.uuid`` 和物料锁标记时，冻结
    目录不会因只读容器深拷贝失败，也不要求遗留 ``$slot`` 字段；本测试不创建
    工作流任务（WorkflowTask）或执行动作。
    """

    # ``source_template`` 与 ``source_handle`` 保持目录中的框架物料来源合同。
    source_template, source_handle = _material_source_template()
    # ``action_template`` 与 ``action_handles`` 模拟注册表投影产生的真实嵌套合同。
    action_template, action_handles = _template(
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
            )
        ],
    )
    action_handles[0]["meta_data"]["unilab"]["value_schema"] = {
        "additionalProperties": False,
        "properties": {"uuid": {"format": "uuid", "type": "string"}},
        "required": ["uuid"],
        "type": "object",
        "x-unilabos-material-lock": True,
    }
    # ``catalog`` 递归冻结嵌套对象，覆盖 SZLab 启动时暴露的只读映射路径。
    catalog = AuthoringCatalogSnapshot.from_entities(
        [source_template, action_template],
        [source_handle, *action_handles],
        resource_template_symbols={PLATE_SOURCE_SYMBOL: PLATE_TEMPLATE_UUID},
    )
    # ``compiled`` 必须识别连接点的物料占位符语义并保留稳定实际 UUID。
    compiled = WorkflowAuthoringEngine(
        catalog=catalog,
        resource_reference_resolver=_resolver(),
    ).compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        python_source=_action_code(),
        source_uri="package://szlab/workflows/projected_resource_ref.py",
        applied_graph=_applied_graph(),
    )

    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    assert compiled.graph["nodes"][0]["param"]["sample"] == {
        "uuid": POWDER_WAREHOUSE_UUID
    }


def test_projected_material_output_satisfies_resource_slot_result_record() -> None:
    """动作结果物料 JSON Schema 必须满足工作流的物料占位符结果声明。

    参数：无。返回：无。断言：注册表动作结果使用 ``properties.uuid`` JSON
    Schema、连接点（Handle）类型为 ``ResourceSlot`` 时，可赋给作者源码声明的
    ``ResourceSlot`` 结果字段，并保留动作输出的资源模板允许集合；本测试不创建
    工作流任务（WorkflowTask）或执行动作。
    """

    # ``action_template`` 与 ``action_handles`` 同时提供物料输入和显式物料输出。
    action_template, action_handles = _template(
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
                ACTION_SAMPLE_SOURCE,
                node_template_uuid=PREPARE_TEMPLATE_UUID,
                key="sample",
                io_type="source",
                value_type="ResourceSlot",
                required=False,
            ),
        ],
    )
    # ``projected_schema`` 是动作合同投影实际写入两个连接点的嵌套 JSON Schema。
    projected_schema = {
        "additionalProperties": False,
        "properties": {"uuid": {"format": "uuid", "type": "string"}},
        "required": ["uuid"],
        "type": "object",
    }
    for handle in action_handles:
        handle["meta_data"]["unilab"]["value_schema"] = dict(projected_schema)
        handle["meta_data"]["unilab"][
            "allowed_resource_template_uuids"
        ] = [S3_WAREHOUSE_TEMPLATE_UUID]
    # ``catalog`` 冻结真实动作投影，确保编译不依赖测试专用 ``$slot`` 旁路。
    catalog = AuthoringCatalogSnapshot.from_entities(
        [action_template],
        action_handles,
    )
    # ``python_source`` 用显式结果记录承诺标准物料占位符（ResourceSlot）。
    python_source = f'''from typing import TypedDict
from lab.devices import Reactor
from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.workflow.authoring import device, resource_ref, workflow


class ProjectedMaterialResult(TypedDict):
    sample: ResourceSlot


reactor: Reactor = device()


@workflow(workflow_uuid="{WORKFLOW_UUID}", displayname="Projected material output")
def projected_material_output() -> ProjectedMaterialResult:
    # unilab:node_uuid={ACTION_NODE_UUID}
    prepared = reactor.prepare(sample=resource_ref("powder_container_warehouse"))
    return {{"sample": prepared.sample}}
'''
    # ``compiled`` 必须采用生产者可赋给消费者的集合关系，而非两种表示的文本相等。
    compiled = WorkflowAuthoringEngine(
        catalog=catalog,
        resource_reference_resolver=_resolver(),
    ).compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        python_source=python_source,
        source_uri="package://szlab/workflows/projected_material_output.py",
        applied_graph=_applied_graph(),
    )

    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    assert compiled.graph["workflow"]["meta_data"]["unilab"]["output_contract"][
        "outputs"
    ][0]["schema"] == {
        "$slot": "ResourceSlot",
        "allowed_resource_template_uuids": [S3_WAREHOUSE_TEMPLATE_UUID],
    }


@pytest.mark.parametrize(
    "resolver",
    [
        _ResourceReferenceResolver({}),
        _AmbiguousResourceReferenceResolver(),
        _ForgedResourceReferenceResolver(),
    ],
    ids=["unknown", "ambiguous", "forged-business-id-as-uuid"],
)
def test_resource_ref_resolution_failures_never_create_candidate_graph(
    resolver: Any,
) -> None:
    """未知、歧义和伪造 UUID 的资源解析必须统一关闭式失败。

    参数：``resolver`` 分别模拟不存在、命中多个物料和把业务 ID 冒充 UUID 的
    库存适配器。返回：无。断言：公共编译结果只有稳定
    ``resource_reference_resolution_error``，且不产生候选图；不创建工作流任务
    （WorkflowTask）或执行动作。
    """

    # ``compiled`` 是不可信解析回执穿越公共编译边界后的稳定失败结果。
    compiled = WorkflowAuthoringEngine(
        catalog=_catalog(),
        resource_reference_resolver=resolver,
    ).compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        python_source=_action_code(),
        source_uri="package://szlab/workflows/invalid_resource_ref.py",
        applied_graph=_applied_graph(),
    )

    assert not compiled.valid
    assert compiled.graph is None
    assert [item["code"] for item in compiled.diagnostics] == [
        "resource_reference_resolution_error"
    ]
