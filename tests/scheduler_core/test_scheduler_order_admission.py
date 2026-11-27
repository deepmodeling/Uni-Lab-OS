"""资源顺序能力、设备别名绑定和串行排程准入。"""

from dataclasses import replace

import pytest

from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.models import WorkflowNode, WorkflowSpec
from unilabos.app.scheduler.service import EdgeScheduler


def test_scheduler_capability_skips_only_cycle_proof():
    from unilabos.workflow.resource_lock_plan import (
        SCHEDULER_RESOURCE_ORDER_CAPABILITY,
        ResourcePlanError,
        compile_template_resource_plan,
        scheduler_resource_order,
        validate_resource_plan,
    )
    graph={'workflow_uuid':'abba','nodes':[
        {'uuid':'a1','resource_defaults':['A']}, {'uuid':'a2','resource_defaults':['B']},
        {'uuid':'b1','resource_defaults':['B']}, {'uuid':'b2','resource_defaults':['A']}],
        'edges':[{'source_node_uuid':'a1','target_node_uuid':'a2'}, {'source_node_uuid':'b1','target_node_uuid':'b2'}],
        'resource_scopes':[{'scope_id':'ha','kind':'reservation','resources':['A'],'node_uuids':['a1','a2']},
                           {'scope_id':'hb','kind':'reservation','resources':['B'],'node_uuids':['b1','b2']}]}
    with pytest.raises(ResourcePlanError, match='成环'):compile_template_resource_plan(graph)
    with scheduler_resource_order(True):plan=compile_template_resource_plan(graph)
    assert SCHEDULER_RESOURCE_ORDER_CAPABILITY in plan.capabilities
    validate_resource_plan(plan)
    with pytest.raises(ResourcePlanError):validate_resource_plan(replace(plan,resources=()))


def test_reservation_alias_freezes_from_authoritative_executor_identity():
    from unilabos.workflow.task_input import freeze_scope_device_bindings
    graph={'nodes':[{'uuid':'rotate','action_name':'rotate','material_uuid':'device-uuid'}],
           'workflow':{'meta_data':{'unilab':{'resource_scopes':[{'resources':['table','unknown']} ]}}}}
    bound=freeze_scope_device_bindings(graph, lambda alias: {'uuid':'device-uuid'} if alias=='table' else None)
    assert bound['resource_bindings']['table']['canonical_key']=='/devices/device-uuid'
    assert 'unknown' not in bound['resource_bindings']
    assert 'resource_bindings' not in graph


def test_serial_scheduler_rejects_plan_requiring_resource_order():
    from unilabos.app.scheduler.models import WorkflowEdge
    from unilabos.app.scheduler.service import ExecutionPolicyError
    from unilabos.workflow.resource_lock_plan import (
        bind_station_resource_plan,
        compile_template_resource_plan,
        resource_plan_for_node,
        scheduler_resource_order,
        serialize_resource_plan,
    )
    graph = {'workflow_uuid': 'abba-runtime', 'nodes': [
        {'uuid': name, 'resource_defaults': [device]}
        for name, device in [('a1', 'A'), ('a2', 'B'), ('b1', 'B'), ('b2', 'A')]],
        'edges': [{'source_node_uuid': 'a1', 'target_node_uuid': 'a2'},
                  {'source_node_uuid': 'b1', 'target_node_uuid': 'b2'}],
        'resource_scopes': [
            {'scope_id': 'a', 'kind': 'reservation', 'resources': ['A'], 'node_uuids': ['a1', 'a2']},
            {'scope_id': 'b', 'kind': 'reservation', 'resources': ['B'], 'node_uuids': ['b1', 'b2']}]}
    with scheduler_resource_order(True):
        plan = bind_station_resource_plan(compile_template_resource_plan(graph), {
            device: {'canonical_key': '/devices/' + device, 'kind': 'device'} for device in ['A', 'B']})
    projections = {n['uuid']: resource_plan_for_node(plan, n['uuid']) for n in graph['nodes']}
    nodes = []
    for node in graph['nodes']:
        name = node['uuid']
        projection = projections[name]
        nodes.append(WorkflowNode(id=name, job_id=name, device_id=node['resource_defaults'][0],
            action_name='run', action_type='goal',
            resource_plan_id=plan.plan_id,
            resource_interval_ids=[i['interval_id'] for i in projection['intervals']],
            resource_acquire_set_id=next((a['acquire_set_id'] for a in projection['acquire_sets']), '')))
    spec = WorkflowSpec('abba-runtime', nodes, [
        WorkflowEdge(uuid='a', source_node_id='a1', target_node_id='a2'),
        WorkflowEdge(uuid='b', source_node_id='b1', target_node_id='b2')],
        resource_plan=serialize_resource_plan(plan))
    legacy = EdgeScheduler(dispatcher=RecordingDispatcher(), serial_queue=True)
    try:
        with pytest.raises(ExecutionPolicyError, match='顺序门禁'):
            legacy.submit_workflow(spec)
    finally:
        legacy.close()


def test_alias_rebinding_preserves_scheduler_requirement_outside_compile_context():
    from unilabos.workflow.resource_lock_plan import (
        SCHEDULER_RESOURCE_ORDER_CAPABILITY,
        STATIC_RESOURCE_DAG_CAPABILITY,
        bind_station_resource_plan,
        compile_template_resource_plan,
        scheduler_resource_order,
    )
    with scheduler_resource_order(True):
        template = compile_template_resource_plan({'workflow_uuid': 'aliases', 'nodes': [
            {'uuid': 'a', 'resource_defaults': ['robot']},
            {'uuid': 'b', 'resource_defaults': ['alias']}], 'edges': []})
    bound = bind_station_resource_plan(template, {
        alias: {'canonical_key': '/devices/robot', 'kind': 'device'} for alias in ['robot', 'alias']})
    assert SCHEDULER_RESOURCE_ORDER_CAPABILITY in bound.capabilities
    assert STATIC_RESOURCE_DAG_CAPABILITY not in bound.capabilities
