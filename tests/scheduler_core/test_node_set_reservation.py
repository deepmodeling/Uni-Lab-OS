"""非连续动作集合预留复用原生物理区间执行。"""
import pytest

from unilabos.workflow.authoring_ast import parse_authoring_source
from unilabos.workflow.resource_lock_plan import ResourcePlanError, compile_template_resource_plan


def node(identity, *resources):
    return {"uuid": identity, "resources": list(resources)}


def graph(nodes, edges, members):
    return {"nodes": nodes, "edges": [{"source_node_uuid": a, "target_node_uuid": b} for a, b in edges],
            "resource_scopes": [{"scope_id": "reservation", "kind": "reservation", "node_uuids": members,
                                 "resources": sorted({r for n in nodes if n["uuid"] in members for r in n["resources"]})}]}


def test_nonconsecutive_nodes_hold_explicit_resources_until_all_members_finish():
    plan = compile_template_resource_plan(graph(
        [node('a', 'source', 'robot'), node('x', 'unrelated'), node('b', 'robot', 'target'), node('c', 'target')],
        [('a', 'x'), ('x', 'b'), ('b', 'c')], ['a', 'b', 'c']))
    names = {r.resource_id: r.alias for r in plan.resources}
    acquire = next(a for a in plan.acquire_sets if a.node_uuid == 'a')
    assert {names[r] for r in acquire.resource_ids} == {'source', 'robot', 'target'}
    intervals = {names[i.resource_id]: i for i in plan.intervals}
    assert intervals['source'].node_uuids == ('a', 'b', 'c')
    assert intervals['robot'].node_uuids == ('a', 'b', 'c')
    assert intervals['target'].node_uuids == ('a', 'b', 'c')
    assert intervals['unrelated'].node_uuids == ('x',)


def test_parallel_entries_share_full_atomic_set():
    plan = compile_template_resource_plan(graph([node('a', 'robot'), node('b', 'table'), node('c', 'table')],
        [('a', 'c'), ('b', 'c')], ['a', 'b', 'c']))
    acquired = {a.node_uuid: set(a.resource_ids) for a in plan.acquire_sets}
    assert acquired['a'] == acquired['b']
    assert len(acquired['a']) == 2


def test_external_conflicting_prerequisite_fails_closed_without_expansion():
    with pytest.raises(ResourcePlanError):
        compile_template_resource_plan(graph(
            [node('a', 'r'), node('x', 'r'), node('b', 'r')],
            [('a', 'x'), ('x', 'b')], ['a', 'b']))


def test_outfeed_shares_handoff_without_acquiring_downstream_reservation():
    workflow = graph(
        [node('out', 'stack'), node('pick', 'stack', 'arm'), node('prepare', 'table'), node('place', 'arm', 'table')],
        [('out', 'pick'), ('pick', 'place'), ('prepare', 'place')], ['pick', 'prepare', 'place'])
    workflow['resource_scopes'].append({
        'scope_id': 'handoff', 'kind': 'transfer', 'resources': ['site'], 'node_uuids': ['out', 'pick'],
        'source': 'inventory.material_site_handoff',
    })
    plan = compile_template_resource_plan(workflow)
    assert set(next(s for s in plan.scopes if s.kind == 'reservation').node_uuids) == {'pick', 'prepare', 'place'}
    names = {r.resource_id: r.alias for r in plan.resources}
    acquired = {a.node_uuid: {names[r] for r in a.resource_ids} for a in plan.acquire_sets}
    assert acquired['out'] == {'stack', 'site'}
    assert 'arm' in acquired['prepare'] and 'table' in acquired['prepare']
    stack, = [i for i in plan.intervals if names[i.resource_id] == 'stack']
    assert set(stack.node_uuids) == {'out', 'pick', 'prepare', 'place'}


def test_ast_declaration_does_not_add_execution_edges():
    source = '''from lab.devices import Reactor
from unilabos.workflow.authoring import device, workflow, workflow_output, resources
reactor: Reactor = device()
@workflow(workflow_uuid="00000000-0000-4000-8000-000000000011", displayname="test")
def run():
    # unilab:node_uuid=00000000-0000-4000-8000-000000000012
    a = reactor.prepare()
    # unilab:node_uuid=00000000-0000-4000-8000-000000000013
    unrelated = reactor.prepare()
    # unilab:node_uuid=00000000-0000-4000-8000-000000000014
    b = reactor.prepare()
    with resources() as locks:
        locks.hold("robot", nodes=[a, b], name="Transfer")
    return workflow_output()
'''
    program = parse_authoring_source(python_source=source, expected_workflow_uuid='00000000-0000-4000-8000-000000000011')
    scope, = program.resource_scopes
    assert scope.kind == 'reservation'
    assert scope.name == 'Transfer'
    assert len(scope.node_uuids) == 2
    assert len(program.actions) == 3
    assert len(program.order_dependencies) == 2


@pytest.mark.parametrize('declaration', [
    'locks.hold("robot", nodes=[prepared, prepared])',
    'locks.hold("robot", nodes=[missing])',
    'locks.hold("robot", nodes=prepared)',
    'locks.hold("robot", nodes=[prepared], name=mode)',
])
def test_invalid_node_set_declarations_fail_closed(declaration):
    from tests.workflow.test_authoring_engine import WORKFLOW_UUID, _source
    from unilabos.workflow.authoring_ast import AuthoringSyntaxError
    source = _source().replace('device, workflow, workflow_output', 'device, workflow, workflow_output, resources')
    source = source.replace('    return workflow_output(', f'    with resources() as locks:\n        {declaration}\n    return workflow_output(')
    with pytest.raises(AuthoringSyntaxError):
        parse_authoring_source(python_source=source, expected_workflow_uuid=WORKFLOW_UUID)


@pytest.mark.parametrize("statement", [
    'with resources("robot") as locks:\n        locks.hold("robot", nodes=[prepared])',
    'with resources() as prepared:\n        prepared.hold("robot", nodes=[prepared])',
    'with resources() as locks:\n        other.hold("robot", nodes=[prepared])',
    'with resources() as locks:\n        copied = prepared',
    'with resources() as locks:\n        pass',
    'locks.hold("robot", nodes=[prepared])',
    'reserve_resources("robot", nodes=[prepared])',
])
def test_only_bound_hold_declarations_are_accepted(statement):
    from tests.workflow.test_authoring_engine import WORKFLOW_UUID, _source
    from unilabos.workflow import authoring
    from unilabos.workflow.authoring_ast import AuthoringSyntaxError

    assert not hasattr(authoring, "reserve_resources")
    source = _source().replace("workflow_output\n", "workflow_output, resources\n", 1)
    source = source.replace("    return workflow_output(", f"    {statement}\n    return workflow_output(")
    with pytest.raises(AuthoringSyntaxError):
        parse_authoring_source(python_source=source, expected_workflow_uuid=WORKFLOW_UUID)


@pytest.mark.parametrize('inner_members', [('b', 'c'), ('d', 'e')])
def test_nested_and_intersecting_requirements_merge_per_resource(inner_members):
    workflow = graph(
        [node('a', 'robot', 'source'), node('b', 'robot'), node('c', 'robot'),
         node('d', 'robot'), node('e', 'robot')],
        list(zip('abcd', 'bcde')), list('abcd'))
    workflow['resource_scopes'].append({
        'scope_id': 'automatic-transfer', 'kind': 'transfer',
        'resources': ['robot'], 'node_uuids': list(inner_members),
    })
    plan = compile_template_resource_plan(workflow)
    aliases = {r.resource_id: r.alias for r in plan.resources}
    robot = [i for i in plan.intervals if aliases[i.resource_id] == 'robot']
    merged = next(i for i in robot if 'a' in i.node_uuids)
    assert merged.node_uuids == tuple('abcde' if inner_members == ('d', 'e') else 'abcd')
    source, = [i for i in plan.intervals if aliases[i.resource_id] == 'source']
    assert source.node_uuids == ('a', 'b', 'c', 'd')


def test_same_resource_in_unrelated_reservations_does_not_share_ownership():
    workflow = graph([node(n, 'robot') for n in 'abcd'], [('a', 'b'), ('c', 'd')], ['a', 'b'])
    workflow['resource_scopes'].append({
        'scope_id': 'other', 'kind': 'reservation', 'resources': ['robot'], 'node_uuids': ['c', 'd'],
    })
    plan = compile_template_resource_plan(workflow)
    assert {i.node_uuids for i in plan.intervals} == {('a', 'b'), ('c', 'd')}


def test_explicit_reservation_does_not_absorb_transfer_scope_aliases():
    workflow = graph(
        [node('out', 'stack'), node('pick', 'stack', 'arm'), node('prepare', 'table'), node('place', 'arm', 'table')],
        [('out', 'pick'), ('pick', 'place'), ('prepare', 'place')], ['pick', 'prepare', 'place'])
    workflow['resource_scopes'].extend([
        {'scope_id': 'stack-handoff', 'kind': 'transfer', 'resources': ['stack-site'],
         'node_uuids': ['out', 'pick'], 'source': 'inventory.material_site_handoff'},
        {'scope_id': 'arm-handoff', 'kind': 'transfer', 'resources': ['arm-site'],
         'node_uuids': ['pick', 'place'], 'source': 'inventory.material_site_handoff'},
    ])
    plan = compile_template_resource_plan(workflow)
    names = {r.resource_id: r.alias for r in plan.resources}
    acquired = {a.node_uuid: {names[r] for r in a.resource_ids} for a in plan.acquire_sets}
    assert acquired['out'] == {'stack', 'stack-site'}
    assert acquired['prepare'] == {'stack', 'arm', 'table'}
    intervals = {names[item.resource_id]: item for item in plan.intervals}
    assert set(intervals['stack-site'].node_uuids) == {'out', 'pick'}
    assert 'place' not in intervals['stack-site'].node_uuids
    assert set(intervals['arm-site'].node_uuids) == {'pick', 'place'}


def test_node_only_reservation_rejected_at_public_and_graph_boundaries():
    from unilabos.workflow.authoring import resources
    with resources() as locks, pytest.raises(ValueError):
        locks.hold(nodes=[object()])
    workflow = graph([node('a', 'robot')], [], ['a'])
    workflow['resource_scopes'][0]['resources'] = []
    with pytest.raises(ResourcePlanError, match='不能为空'):
        compile_template_resource_plan(workflow)
    from tests.workflow.test_authoring_engine import WORKFLOW_UUID, _source
    text = _source().replace('device, workflow, workflow_output', 'device, workflow, workflow_output, resources')
    text = text.replace('    return workflow_output(', '    with resources() as locks:\n        locks.hold(nodes=[prepared])\n    return workflow_output(')
    with pytest.raises(Exception, match='资源别名'):
        parse_authoring_source(python_source=text, expected_workflow_uuid=WORKFLOW_UUID)


def test_turntable_reservation_survives_last_rotation_until_robot_pick():
    from types import SimpleNamespace

    from unilabos.app.scheduler.dag_state import WorkflowRun
    from unilabos.app.scheduler.dispatch import RecordingDispatcher
    from unilabos.app.scheduler.models import WorkflowNode, WorkflowSpec
    from unilabos.app.scheduler.service import EdgeScheduler
    from unilabos.workflow.resource_lock_plan import bind_station_resource_plan, serialize_resource_plan

    workflow = graph([node('rotate', 'table'), node('place', 'robot'),
                      node('turn', 'table'), node('pick', 'robot')],
                     [('rotate', 'place'), ('place', 'turn'), ('turn', 'pick')],
                     ['rotate', 'place', 'turn', 'pick'])
    workflow['resource_scopes'][0]['resources'] = ['table']
    plan = bind_station_resource_plan(compile_template_resource_plan(workflow), {
        name: {'canonical_key': '/devices/' + name, 'kind': 'device'} for name in ['table', 'robot']})
    reservation = next(i for i in plan.intervals if len(i.node_uuids) == 4)
    nodes = [WorkflowNode(id=name, resource_plan_id=plan.plan_id,
                         resource_interval_ids=[reservation.interval_id]) for name in ['rotate', 'place', 'turn', 'pick']]
    run = WorkflowRun(WorkflowSpec(workflow_id='wf', nodes=nodes, edges=[], resource_plan=serialize_resource_plan(plan)))
    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    scheduler._workflows['wf'] = run
    try:
        for name in ['rotate', 'place', 'turn', 'pick']:
            run.mark_finished(name, {})
            job = SimpleNamespace(workflow_id='wf', node_id=name, job_id=name,
                                  resource_interval_ids=[reservation.interval_id],
                                  resource_lock_keys={'/devices/table'})
            scheduler._record_interval_handoff(job, success=True)
            held = scheduler._interval_resource_holders.get(('wf', reservation.interval_id), set())
            assert ('/devices/table' in held) == (name != 'pick')
            assert '/devices/robot' not in scheduler._busy_keys()
    finally:
        scheduler.close()
