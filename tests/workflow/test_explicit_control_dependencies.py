"""Python 工作流显式控制依赖（Explicit Control Dependency）公共合同。"""

from __future__ import annotations

from typing import Any

import pytest

from .test_authoring_engine import (
    ANALYZE_NODE_UUID,
    ANALYZE_SAMPLE_TARGET,
    PREPARE_NODE_UUID,
    PREPARE_READY_SOURCE,
    PREPARE_READY_TARGET,
    PREPARE_SAMPLE_SOURCE,
    WORKFLOW_UUID,
    _compile,
    _engine,
)
from .test_qg01_group_parallel_authoring import (
    GROUP_A_NODE_UUID,
    GROUP_B_NODE_UUID,
    _group_engine,
)

B_NODE_UUID = "20000000-0000-4000-8000-000000000021"
C_NODE_UUID = "20000000-0000-4000-8000-000000000022"
D_NODE_UUID = "20000000-0000-4000-8000-000000000023"


def _ready_pairs(graph: dict[str, Any]) -> set[tuple[str, str]]:
    """返回候选图中全部 Ready 控制边端点。"""

    return {
        (edge["source_node_uuid"], edge["target_node_uuid"])
        for edge in graph["edges"]
        if edge["source_handle_uuid"] == PREPARE_READY_SOURCE
        and edge["target_handle_uuid"] == PREPARE_READY_TARGET
    }


def _n_source() -> str:
    """返回规范示例中的 A/B/C/D N 型控制图。"""

    return f'''from lab.devices import Reactor
from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.workflow.authoring import device, workflow, workflow_output


reactor: Reactor = device()


@workflow(workflow_uuid="{WORKFLOW_UUID}", displayname="Explicit N")
def explicit_n(
    *,
    sample_a: ResourceSlot,
    sample_b: ResourceSlot,
    sample_c: ResourceSlot,
    sample_d: ResourceSlot,
):
    # unilab:node_uuid={PREPARE_NODE_UUID}
    a = reactor.prepare(sample=sample_a, cycles=1)
    # unilab:node_uuid={B_NODE_UUID}
    b = reactor.prepare(sample=sample_b, cycles=1, depends_on=[])
    # unilab:node_uuid={C_NODE_UUID}
    c = reactor.prepare(sample=sample_c, cycles=1, depends_on=[a, b])
    # unilab:node_uuid={D_NODE_UUID}
    d = reactor.prepare(sample=sample_d, cycles=1, depends_on=[b])
    return workflow_output()
'''


def test_depends_on_replaces_only_the_nodes_implicit_control_predecessor() -> None:
    """显式列表应得到精确 N 型 Ready 边并可规范往返。"""

    engine = _engine()
    result = _compile(engine, _n_source())

    assert result.valid, result.diagnostics
    assert result.graph is not None
    assert _ready_pairs(result.graph) == {
        (PREPARE_NODE_UUID, C_NODE_UUID),
        (B_NODE_UUID, C_NODE_UUID),
        (B_NODE_UUID, D_NODE_UUID),
    }
    assert result.normalized_python_source is not None
    assert "depends_on=[]" in result.normalized_python_source
    assert "depends_on=[a, b]" in result.normalized_python_source
    assert "depends_on=[b]" in result.normalized_python_source

    repeated = _compile(engine, result.normalized_python_source, graph=result.graph)
    assert repeated.valid, repeated.diagnostics
    assert repeated.graph == result.graph


def test_depends_on_cannot_remove_a_node_output_dependency() -> None:
    """节点输出参数产生的数据/物料边必须比显式控制覆盖更强。"""

    source = f'''from lab.devices import Reactor
from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.workflow.authoring import device, workflow, workflow_output


reactor: Reactor = device()


@workflow(workflow_uuid="{WORKFLOW_UUID}", displayname="Hard material edge")
def hard_material_edge(*, sample: ResourceSlot):
    # unilab:node_uuid={PREPARE_NODE_UUID}
    prepared = reactor.prepare(sample=sample, cycles=1)
    # unilab:node_uuid={ANALYZE_NODE_UUID}
    analyzed = reactor.analyze(
        prepared=prepared.prepared,
        label="complete",
        depends_on=[],
    )
    return workflow_output()
'''
    result = _compile(_engine(), source)

    assert result.valid, result.diagnostics
    assert result.graph is not None
    assert any(
        edge["source_node_uuid"] == PREPARE_NODE_UUID
        and edge["source_handle_uuid"] == PREPARE_SAMPLE_SOURCE
        and edge["target_node_uuid"] == ANALYZE_NODE_UUID
        and edge["target_handle_uuid"] == ANALYZE_SAMPLE_TARGET
        for edge in result.graph["edges"]
    )
    assert result.normalized_python_source is not None
    assert "depends_on" not in result.normalized_python_source


def test_redundant_control_reference_normalizes_to_the_richer_data_edge() -> None:
    """同一上游的数据边与显式控制边重合时只保留并生成数据绑定。"""

    source = f'''from lab.devices import Reactor
from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.workflow.authoring import device, workflow, workflow_output


reactor: Reactor = device()


@workflow(workflow_uuid="{WORKFLOW_UUID}", displayname="Redundant dependency")
def redundant_dependency(*, sample: ResourceSlot):
    # unilab:node_uuid={PREPARE_NODE_UUID}
    prepared = reactor.prepare(sample=sample, cycles=1)
    # unilab:node_uuid={ANALYZE_NODE_UUID}
    analyzed = reactor.analyze(
        prepared=prepared.prepared,
        label="complete",
        depends_on=[prepared],
    )
    return workflow_output()
'''
    result = _compile(_engine(), source)

    assert result.valid, result.diagnostics
    assert result.graph is not None
    assert len(result.graph["edges"]) == 1
    assert result.normalized_python_source is not None
    assert "depends_on" not in result.normalized_python_source


def test_parallel_branch_can_use_an_earlier_sibling_as_control_only() -> None:
    """并行分组允许显式控制引用更早分支，但普通数据引用仍由既有测试拒绝。"""

    source = f'''from lab.devices import Reactor
from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.workflow.authoring import device, group, parallel, workflow, workflow_output


reactor: Reactor = device()


@workflow(workflow_uuid="{WORKFLOW_UUID}", displayname="Rolling parallel")
def rolling_parallel(*, sample_a: ResourceSlot, sample_b: ResourceSlot):
    with parallel():
        # unilab:node_uuid={GROUP_A_NODE_UUID}
        with group(name="Plate A"):
            # unilab:node_uuid={PREPARE_NODE_UUID}
            a = reactor.prepare(sample=sample_a, cycles=1)
        # unilab:node_uuid={GROUP_B_NODE_UUID}
        with group(name="Plate B"):
            # unilab:node_uuid={B_NODE_UUID}
            b = reactor.prepare(sample=sample_b, cycles=1, depends_on=[a])
    return workflow_output()
'''
    result = _compile(_group_engine(), source)

    assert result.valid, result.diagnostics
    assert result.graph is not None
    assert _ready_pairs(result.graph) == {(PREPARE_NODE_UUID, B_NODE_UUID)}
    assert result.normalized_python_source is not None
    assert "depends_on=[a]" in result.normalized_python_source


@pytest.mark.parametrize(
    ("replacement", "code"),
    [
        ("depends_on=a", "invalid_explicit_dependency"),
        ("depends_on=[a, a]", "invalid_explicit_dependency"),
        ("depends_on=[future]", "invalid_explicit_dependency"),
    ],
)
def test_depends_on_rejects_non_list_duplicate_and_future_references(
    replacement: str,
    code: str,
) -> None:
    """显式控制依赖首版语法必须保持静态、向后且无重复。"""

    source = _n_source().replace("depends_on=[]", replacement, 1)
    result = _compile(_engine(), source)

    assert not result.valid
    assert [diagnostic["code"] for diagnostic in result.diagnostics] == [code]


def test_explicit_dependency_cannot_escape_repeat_scope() -> None:
    """动态循环体结果不能成为循环外静态显式前序。"""
    from .test_repeat_until_authoring import _compile, _repeat_engine, _repeat_source

    source = _repeat_source().replace("reactor.finish(label=\"done\")", "reactor.finish(label=\"done\", depends_on=[measurement])")
    # 使用源码断言保证修改确实命中测试夹具。
    assert "depends_on=[measurement]" in source
    compiled = _compile(_repeat_engine(), source)
    assert not compiled.valid
    assert any(item["code"] == "invalid_explicit_dependency" for item in compiled.diagnostics)


@pytest.mark.parametrize('override,predecessor', [('', 'p'), ('depends_on=[a]', 'a'), ('depends_on=[]', None)])
def test_parallel_block_overrides_only_its_inherited_frontier(override, predecessor):
    """准备与四板装载分叉，压盖等待四板，CEM等待准备和压盖。"""
    from uuid import NAMESPACE_URL, uuid5

    from unilabos.workflow.authoring_ast import parse_authoring_source
    names = ['a', 'p', 'l1', 'l2', 'l3', 'l4', 'cover', 'c']
    ids = {name: str(uuid5(NAMESPACE_URL, 'parallel-frontier:' + name)) for name in names}
    lines = [
        'from lab.devices import Reactor',
        'from unilabos.registry.placeholder_type import ResourceSlot',
        'from unilabos.workflow.authoring import device, parallel, workflow, workflow_output',
        'reactor: Reactor = device()',
        f'@workflow(workflow_uuid="{WORKFLOW_UUID}", displayname="Frontier")',
        'def run(*, ' + ', '.join(name + '_sample: ResourceSlot' for name in names[:-1]) + '):',
    ]
    def prepare(name, indent=4, extra=''):
        lines.extend([' ' * indent + '# unilab:node_uuid=' + ids[name],
                      ' ' * indent + f'{name} = reactor.prepare(sample={name}_sample, cycles=1{extra})'])
    prepare('a')
    prepare('p')
    lines.append(f'    with parallel({override}):')
    for name in ['l1', 'l2', 'l3', 'l4']:
        prepare(name, 8)
    prepare('cover')
    lines.extend(['    # unilab:node_uuid=' + ids['c'],
                  '    c = reactor.analyze(prepared=p.prepared, label="done", depends_on=[cover])',
                  '    return workflow_output()'])
    source = '\n'.join(lines)
    program = parse_authoring_source(python_source=source, expected_workflow_uuid=WORKFLOW_UUID)
    expected = {(ids['a'], ids['p']), (ids['cover'], ids['c'])}
    expected.update((ids[name], ids['cover']) for name in ['l1', 'l2', 'l3', 'l4'])
    if predecessor:
        expected.update((ids[predecessor], ids[name]) for name in ['l1', 'l2', 'l3', 'l4'])
    assert set(program.order_dependencies) == expected
    engine = _engine()
    compiled = _compile(engine, source)
    assert compiled.valid, compiled.diagnostics
    assert any(e['source_node_uuid'] == ids['p'] and e['target_node_uuid'] == ids['c'] for e in compiled.graph['edges'])
    repeated = _compile(engine, compiled.normalized_python_source, graph=compiled.graph)
    assert repeated.valid, repeated.diagnostics
    assert repeated.graph == compiled.graph
    # 块内节点的显式覆盖优先于块继承；数据依赖仍由绑定保留。
    changed = source.replace('l1 = reactor.prepare(sample=l1_sample, cycles=1)', 'l1 = reactor.prepare(sample=l1_sample, cycles=1, depends_on=[])')
    parsed = parse_authoring_source(python_source=changed, expected_workflow_uuid=WORKFLOW_UUID)
    assert not any(target == ids['l1'] for _, target in parsed.order_dependencies)


@pytest.mark.parametrize('override', ['depends_on=[future]', 'depends_on=None', 'depends_on=[a, a]', 'unknown=[]'])
def test_parallel_block_rejects_invalid_dependency_declarations(override):
    from unilabos.workflow.authoring_ast import AuthoringSyntaxError, parse_authoring_source
    source = _n_source().replace('    # unilab:node_uuid=' + B_NODE_UUID,
        f'    with parallel({override}):\n        # unilab:node_uuid=' + B_NODE_UUID)
    # 使用独立的最小非法块；在解析分支前就拒绝无效依赖。
    source = source[:source.index('    with parallel(')] + f'    with parallel({override}):\n        pass\n        pass\n    return workflow_output()\n'
    with pytest.raises(AuthoringSyntaxError):
        parse_authoring_source(python_source=source, expected_workflow_uuid=WORKFLOW_UUID)


def test_material_only_display_group_does_not_enter_execution_frontier():
    """展示分组内的普通物料来源不能凭空阻塞后续无物料动作。"""
    from unilabos.workflow.authoring_ast import parse_authoring_source

    from .test_f05_material_source_authoring import MATERIAL_SOURCE_NODE_UUID, PREPARE_NODE_UUID, _source
    source = _source().replace('    workflow,', '    workflow,\n    group,')
    start = source.index(f'    # unilab:node_uuid={MATERIAL_SOURCE_NODE_UUID}')
    end = source.index(f'    # unilab:node_uuid={PREPARE_NODE_UUID}')
    source = (source[:start] + '    # unilab:node_uuid=20000000-0000-4000-8000-000000009999\n'
              + "    with group(name='物料装配'):\n"
              + ''.join('    ' + line + '\n' for line in source[start:end].splitlines())
              + source[end:])
    program = parse_authoring_source(python_source=source, expected_workflow_uuid=WORKFLOW_UUID)
    assert (MATERIAL_SOURCE_NODE_UUID, PREPARE_NODE_UUID) not in program.order_dependencies
    from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
    from unilabos.workflow.authoring_kernel import AuthoringCatalogSnapshot

    from .test_f05_material_source_authoring import PLATE_SOURCE_SYMBOL, PLATE_TEMPLATE_UUID
    from .test_f05_material_source_authoring import _engine as material_engine
    from .test_qg01_group_parallel_authoring import _group_template
    catalog = material_engine()._catalog
    engine = WorkflowAuthoringEngine(catalog=AuthoringCatalogSnapshot.from_entities(
        [*[a.detached_template() for a in catalog.actions], _group_template()],
        [h for a in catalog.actions for h in a.detached_handles()],
        resource_template_symbols={PLATE_SOURCE_SYMBOL: PLATE_TEMPLATE_UUID}))
    from .test_authoring_engine import _applied_graph
    def compile_group(text):
        return engine.compile(workflow_uuid=WORKFLOW_UUID, workflow_revision=7,
            python_source=text, source_uri='package://lab/workflows/group.py', applied_graph=_applied_graph())
    compiled = compile_group(source)
    assert compiled.valid, compiled.diagnostics
    repeated = compile_group(compiled.normalized_python_source)
    assert repeated.valid, repeated.diagnostics
    assert 'ready_edge' not in repeated.normalized_python_source


@pytest.mark.parametrize("consumer", ["else", "after"])
def test_condition_branch_results_cannot_be_used_as_external_control_dependencies(consumer):
    """条件分支的结果只在该分支内可作为控制前序。"""
    from unilabos.workflow.authoring_ast import AuthoringSyntaxError, parse_authoring_source

    other_dependency = "depends_on=[first]" if consumer == "else" else ""
    after_dependency = "depends_on=[first]" if consumer == "after" else ""
    source = f'''from lab.devices import Reactor
from unilabos.workflow.authoring import device, workflow, workflow_output

reactor: Reactor = device()
@workflow(workflow_uuid="{WORKFLOW_UUID}", displayname="Conditional control")
def run(*, enabled: bool):
    # unilab:node_uuid={GROUP_A_NODE_UUID}
    if enabled:
        # unilab:node_uuid={PREPARE_NODE_UUID}
        first = reactor.tick()
    else:
        # unilab:node_uuid={B_NODE_UUID}
        second = reactor.tick({other_dependency})
    # unilab:node_uuid={C_NODE_UUID}
    final = reactor.tick({after_dependency})
    return workflow_output()
'''
    with pytest.raises(AuthoringSyntaxError) as error:
        parse_authoring_source(python_source=source, expected_workflow_uuid=WORKFLOW_UUID)
    assert error.value.code == "invalid_explicit_dependency"
