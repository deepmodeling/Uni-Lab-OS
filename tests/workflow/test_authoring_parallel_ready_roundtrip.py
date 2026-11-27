"""原生并行动作与跨展示分组 ready 边的生成回归。"""

import pytest

from .test_authoring_engine import WORKFLOW_UUID, _compile
from .test_qg01_group_parallel_authoring import _group_engine


def _source(body: str) -> str:
    return f'''from lab.devices import Reactor
from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.workflow.authoring import device, group, parallel, resources, workflow, workflow_output
reactor: Reactor = device()
@workflow(workflow_uuid="{WORKFLOW_UUID}", displayname="Parallel control")
def run(*, sample1: ResourceSlot, sample2: ResourceSlot, sample3: ResourceSlot, sample4: ResourceSlot):
{body}
    return workflow_output()
'''


@pytest.mark.parametrize("with_resources", [False, True])
@pytest.mark.parametrize("body", [
    '''    with parallel():
        # unilab:node_uuid=20000000-0000-4000-8000-000000000021
        first = reactor.prepare(sample=sample1, cycles=1)
        # unilab:node_uuid=20000000-0000-4000-8000-000000000022
        second = reactor.prepare(sample=sample2, cycles=2)
    # unilab:node_uuid=20000000-0000-4000-8000-000000000023
    joined = reactor.prepare(sample=sample3, cycles=3)''',
    '''    # unilab:node_uuid=20000000-0000-4000-8000-000000000011
    with group(name="Preparation"):
        with parallel():
            # unilab:node_uuid=20000000-0000-4000-8000-000000000021
            first = reactor.prepare(sample=sample1, cycles=1)
            # unilab:node_uuid=20000000-0000-4000-8000-000000000022
            second = reactor.prepare(sample=sample2, cycles=2)
        # unilab:node_uuid=20000000-0000-4000-8000-000000000023
        joined = reactor.prepare(sample=sample3, cycles=3)''',
    '''    with parallel():
        # unilab:node_uuid=20000000-0000-4000-8000-000000000011
        with group(name="Left"):
            # unilab:node_uuid=20000000-0000-4000-8000-000000000021
            first = reactor.prepare(sample=sample1, cycles=1)
            # unilab:node_uuid=20000000-0000-4000-8000-000000000022
            second = reactor.prepare(sample=sample2, cycles=2)
        # unilab:node_uuid=20000000-0000-4000-8000-000000000012
        with group(name="Right"):
            # unilab:node_uuid=20000000-0000-4000-8000-000000000023
            third = reactor.prepare(sample=sample3, cycles=3)
            # unilab:node_uuid=20000000-0000-4000-8000-000000000024
            fourth = reactor.prepare(sample=sample4, cycles=4)
    # unilab:ready_edge source=20000000-0000-4000-8000-000000000021 target=20000000-0000-4000-8000-000000000024''',
    '''    with parallel():
        # unilab:node_uuid=20000000-0000-4000-8000-000000000021
        first = reactor.prepare(sample=sample1, cycles=1)
        # unilab:node_uuid=20000000-0000-4000-8000-000000000022
        second = reactor.prepare(sample=sample2, cycles=2)
        # unilab:node_uuid=20000000-0000-4000-8000-000000000023
        third = reactor.prepare(sample=sample3, cycles=3)
        # unilab:node_uuid=20000000-0000-4000-8000-000000000024
        fourth = reactor.prepare(sample=sample4, cycles=4)
    # unilab:ready_edge source=20000000-0000-4000-8000-000000000021 target=20000000-0000-4000-8000-000000000023
    # unilab:ready_edge source=20000000-0000-4000-8000-000000000022 target=20000000-0000-4000-8000-000000000023
    # unilab:ready_edge source=20000000-0000-4000-8000-000000000022 target=20000000-0000-4000-8000-000000000024''',
], ids=["root_fork_join", "group_fork_join", "cross_group_ready", "irregular_ready"])
def test_native_parallel_and_ready_graph_survives_generated_source(
    body: str, with_resources: bool,
) -> None:
    """生成源码后边集合、父子关系和再次生成结果必须达到同一固定点。"""
    if with_resources:
        body = '    with resources("shared.exchange"):\n' + "\n".join(
            "    " + line for line in body.splitlines()
        )
    engine = _group_engine()
    compiled = _compile(engine, _source(body))
    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    generated = engine.generate_python(
        workflow_uuid=WORKFLOW_UUID, workflow_revision=7,
        graph=compiled.graph, source_uri="package://test/control.py",
    )
    assert generated.valid, generated.diagnostics
    repeated = _compile(engine, generated.normalized_python_source, graph=compiled.graph)
    assert repeated.valid, repeated.diagnostics
    assert repeated.graph == compiled.graph
    assert repeated.normalized_python_source == generated.normalized_python_source


def test_mixed_data_ready_dag_roundtrips_without_added_sequence() -> None:
    """无现成展示分组的不规则数据 DAG 不能靠新增 ready 边伪造顺序源码。"""
    from uuid import NAMESPACE_URL, uuid5

    from .test_authoring_engine import ANALYZE_READY_TARGET, PREPARE_READY_SOURCE

    engine = _group_engine()
    body = '''    # unilab:node_uuid=20000000-0000-4000-8000-000000000021
    first = reactor.prepare(sample=sample1, cycles=1)
    # unilab:node_uuid=20000000-0000-4000-8000-000000000022
    second = reactor.prepare(sample=sample2, cycles=2)
    # unilab:node_uuid=20000000-0000-4000-8000-000000000023
    third = reactor.analyze(prepared=first.prepared, label="left")
    # unilab:node_uuid=20000000-0000-4000-8000-000000000024
    fourth = reactor.analyze(prepared=second.prepared, label="right")'''
    compiled = _compile(engine, _source(body))
    assert compiled.valid, compiled.diagnostics
    graph = compiled.graph
    ready_handles = {h["uuid"] for a in engine._catalog.actions for h in a.handles
                     if h["handle_key"] == "ready"}
    graph["edges"] = [e for e in graph["edges"] if e["source_handle_uuid"] not in ready_handles]
    graph["edges"].append({
        "uuid": str(uuid5(NAMESPACE_URL, "mixed-ready-edge")),
        "source_node_uuid": "20000000-0000-4000-8000-000000000021",
        "target_node_uuid": "20000000-0000-4000-8000-000000000024",
        "source_handle_uuid": PREPARE_READY_SOURCE,
        "target_handle_uuid": ANALYZE_READY_TARGET,
    })
    generated = engine.generate_python(workflow_uuid=WORKFLOW_UUID, workflow_revision=7,
                                       graph=graph, source_uri="package://test/mixed.py")
    assert generated.valid, generated.diagnostics
    assert "depends_on=" in generated.normalized_python_source
    repeated = _compile(engine, generated.normalized_python_source, graph=graph)
    assert repeated.valid, repeated.diagnostics
    def endpoints(candidate):
        return {(e["source_node_uuid"], e["source_handle_uuid"], e["target_node_uuid"], e["target_handle_uuid"]) for e in candidate["edges"]}
    assert endpoints(repeated.graph) == endpoints(graph)
    assert repeated.graph["nodes"] == generated.graph["nodes"]
    stable = _compile(engine, repeated.normalized_python_source, graph=repeated.graph)
    assert stable.valid, stable.diagnostics
    assert stable.graph == repeated.graph


def test_grouped_mixed_data_ready_dag_roundtrips() -> None:
    """原生分组能保持两条真实结果链及交叉 ready 边，无需牺牲并发。"""
    body = '''    with parallel():
        # unilab:node_uuid=20000000-0000-4000-8000-000000000011
        with group(name="Left"):
            # unilab:node_uuid=20000000-0000-4000-8000-000000000021
            first = reactor.prepare(sample=sample1, cycles=1)
            # unilab:node_uuid=20000000-0000-4000-8000-000000000023
            third = reactor.analyze(prepared=first.prepared, label="left")
        # unilab:node_uuid=20000000-0000-4000-8000-000000000012
        with group(name="Right"):
            # unilab:node_uuid=20000000-0000-4000-8000-000000000022
            second = reactor.prepare(sample=sample2, cycles=2)
            # unilab:node_uuid=20000000-0000-4000-8000-000000000024
            fourth = reactor.analyze(prepared=second.prepared, label="right")
    # unilab:ready_edge source=20000000-0000-4000-8000-000000000021 target=20000000-0000-4000-8000-000000000024'''
    test_native_parallel_and_ready_graph_survives_generated_source(body, False)


@pytest.mark.parametrize("with_resources", [False, True])
def test_reverse_source_order_ready_edge_roundtrips_by_topology(with_resources: bool) -> None:
    """平面图新增反向 ready 边后按拓扑生成源码，保留精确依赖。"""
    engine = _group_engine()
    body = '''    with parallel():
        # unilab:node_uuid=20000000-0000-4000-8000-000000000021
        first = reactor.prepare(sample=sample1, cycles=1)
        # unilab:node_uuid=20000000-0000-4000-8000-000000000022
        second = reactor.prepare(sample=sample2, cycles=2)
    # unilab:ready_edge source=20000000-0000-4000-8000-000000000022 target=20000000-0000-4000-8000-000000000021'''
    if with_resources:
        body = '    with resources("shared.exchange"):\n' + "\n".join(
            "    " + line for line in body.splitlines()
        )
    compiled = _compile(engine, _source(body))
    if with_resources:
        assert not compiled.valid
        assert any("源码节点顺序" in d["message"] for d in compiled.diagnostics)
        return
    assert compiled.valid, compiled.diagnostics
    generated = engine.generate_python(
        workflow_uuid=WORKFLOW_UUID, workflow_revision=7,
        graph=compiled.graph, source_uri="package://test/reordered.py",
    )
    assert generated.valid, generated.diagnostics
    repeated = _compile(engine, generated.normalized_python_source, graph=compiled.graph)
    assert repeated.valid, repeated.diagnostics
    assert repeated.graph["edges"] == compiled.graph["edges"]
    assert repeated.graph["workflow"]["meta_data"] == compiled.graph["workflow"]["meta_data"]
    assert repeated.normalized_python_source == generated.normalized_python_source


@pytest.mark.parametrize("boundary", [None, "group", "resources", "reservation"])
def test_reordering_preserves_cycle_and_structured_boundary_guards(boundary: str | None) -> None:
    """拓扑重排不能接受依赖环，也不能移动结构化区域内的源码顺序。"""
    from uuid import uuid4

    from .test_authoring_engine import PREPARE_READY_SOURCE, PREPARE_READY_TARGET

    body = '''    with parallel():
        # unilab:node_uuid=20000000-0000-4000-8000-000000000021
        first = reactor.prepare(sample=sample1, cycles=1)
        # unilab:node_uuid=20000000-0000-4000-8000-000000000022
        second = reactor.prepare(sample=sample2, cycles=2)'''
    if boundary == "group":
        body = '''    # unilab:node_uuid=20000000-0000-4000-8000-000000000011
    with group(name="Keep boundary"):
''' + "\n".join("    " + line for line in body.splitlines())
    elif boundary == "resources":
        body = '    with resources("shared.exchange"):\n' + "\n".join(
            "    " + line for line in body.splitlines()
        )
    elif boundary == "reservation":
        body += '\n    with resources() as locks:\n        locks.hold("robot", nodes=[first, second], name="Keep reservation")'
    engine = _group_engine()
    compiled = _compile(engine, _source(body))
    assert compiled.valid, compiled.diagnostics
    graph = compiled.graph
    first = "20000000-0000-4000-8000-000000000021"
    second = "20000000-0000-4000-8000-000000000022"
    pairs = [(second, first)] if boundary else [(first, second), (second, first)]
    for source, target in pairs:
        graph["edges"].append({
            "uuid": str(uuid4()), "source_node_uuid": source, "target_node_uuid": target,
            "source_handle_uuid": PREPARE_READY_SOURCE,
            "target_handle_uuid": PREPARE_READY_TARGET,
        })
    generated = engine.generate_python(
        workflow_uuid=WORKFLOW_UUID, workflow_revision=7,
        graph=graph, source_uri="package://test/guard.py",
    )
    assert not generated.valid
    assert generated.normalized_python_source is None
    expected = "源码节点顺序" if boundary else "依赖环"
    assert any(expected in d["message"] for d in generated.diagnostics)


def test_parallel_ties_keep_source_order_over_uuid_order() -> None:
    """没有新增依赖时，平面同级节点保留原源码顺序而非按 UUID 重排。"""
    body = '''    with parallel():
        # unilab:node_uuid=20000000-0000-4000-8000-000000000022
        first = reactor.prepare(sample=sample1, cycles=1)
        # unilab:node_uuid=20000000-0000-4000-8000-000000000021
        second = reactor.prepare(sample=sample2, cycles=2)'''
    engine = _group_engine()
    compiled = _compile(engine, _source(body))
    assert compiled.valid, compiled.diagnostics
    generated = engine.generate_python(
        workflow_uuid=WORKFLOW_UUID, workflow_revision=7,
        graph=compiled.graph, source_uri="package://test/stable.py",
    )
    assert generated.valid, generated.diagnostics
    source = generated.normalized_python_source
    assert source.index("000000000022") < source.index("000000000021")
    repeated = _compile(engine, source, graph=compiled.graph)
    assert repeated.valid, repeated.diagnostics
    assert repeated.graph == compiled.graph
    assert repeated.normalized_python_source == source
