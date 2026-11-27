"""QG01 工作流物料输入隐式输出（Implicit Material Output）合同。"""

from __future__ import annotations

import ast

from unilabos.workflow.models import CandidateCompilation

from .test_authoring_engine import (
    ANALYZE_NODE_UUID,
    ANALYZE_REPORT_SOURCE,
    PREPARE_NODE_UUID,
    WORKFLOW_UUID,
    _applied_graph,
    _engine,
)


def _source_with_implicit_material_output() -> str:
    """构造只显式返回报告、由服务端透传物料输入的作者源码。

    参数：无。返回：包含一个物料占位符（ResourceSlot）输入和一个显式标量输出
    的确定性 Python 文本。异常：无；函数不执行作者源码或读取库存（Inventory）。
    """

    return f'''from typing import TypedDict

from lab.devices import Reactor
from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.workflow.authoring import device, workflow


class TransferResult(TypedDict):
    report: str


reactor: Reactor = device()


@workflow(
    workflow_uuid="{WORKFLOW_UUID}",
    displayname="Implicit material pass-through",
)
def transfer(*, sample: ResourceSlot) -> TransferResult:
    # unilab:node_uuid={PREPARE_NODE_UUID}
    prepared = reactor.prepare(sample=sample, cycles=1)
    # unilab:node_uuid={ANALYZE_NODE_UUID}
    analyzed = reactor.analyze(prepared=prepared.prepared, label="done")
    return {{"report": analyzed.report}}
'''


def _compile(source: str) -> CandidateCompilation:
    """通过公共编译接缝转换隐式物料输出源码。

    参数说明：``source`` 是待编译 Python 文本。返回：公共候选编译结果；编译
    使用冻结动作目录，不访问实际物料（Material）、库位（Site）或设备。
    """

    return _engine().compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        python_source=source,
        source_uri="package://lab/workflows/implicit-material-output.py",
        applied_graph=_applied_graph(),
    )


def test_material_workflow_input_gets_server_managed_output_fixed_point() -> None:
    """物料工作流输入必须获得服务端同名隐式输出并保持源码固定点。

    参数：无。返回：无；断言 ``sample`` 输出完全继承输入 Schema、绑定原工作流
    输入且标记为隐式，规范源码仍只声明作者显式的 ``report`` 结果。异常：公共
    编译和源码生成接缝不得泄漏内部异常或要求作者手写冗余物料返回值。
    """

    engine = _engine()
    source = _source_with_implicit_material_output()
    compiled = engine.compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        python_source=source,
        source_uri="package://lab/workflows/implicit-material-output.py",
        applied_graph=_applied_graph(),
    )

    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    unilab = compiled.graph["workflow"]["meta_data"]["unilab"]
    assert unilab["output_contract"] == {
        "version": 1,
        "outputs": [
            {"name": "report", "schema": {"type": "string"}, "implicit": False},
            {
                "name": "sample",
                "schema": {"$slot": "ResourceSlot"},
                "implicit": True,
            },
        ],
    }
    assert unilab["output_bindings"] == {
        "report": {
            "kind": "node_output",
            "workflow_node_uuid": ANALYZE_NODE_UUID,
            "source_handle_uuid": ANALYZE_REPORT_SOURCE,
        },
        "sample": {"kind": "workflow_input", "parameter": "sample"},
    }
    assert compiled.normalized_python_source is not None
    assert "class TransferResult(TypedDict):\n    report: str" in (
        compiled.normalized_python_source
    )
    module = ast.parse(compiled.normalized_python_source)
    result_record = next(
        statement
        for statement in module.body
        if isinstance(statement, ast.ClassDef)
        and statement.name == "TransferResult"
    )
    assert [
        statement.target.id
        for statement in result_record.body
        if isinstance(statement, ast.AnnAssign)
        and isinstance(statement.target, ast.Name)
    ] == ["report"]
    repeated = _compile(compiled.normalized_python_source)
    assert repeated.valid and repeated.graph is not None, repeated.diagnostics
    assert repeated.graph == compiled.graph
