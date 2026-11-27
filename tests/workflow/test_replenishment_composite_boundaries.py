"""补料实验操作必须能放入条件分支，并能返回在内部选出的新物料。"""
from unilabos.workflow._execution_plan_graph import ExecutionPlanGraphNormalizer
from unilabos.workflow.material_graph_validation import _resolved_producer_schema


def test_condition_expands_operation_members_including_runtime_source():
    nodes = {
        'check': {'type': 'material_source'},
        'condition': {'type': 'condition', 'param': {'branches': [
            {'node_uuids': ['direct'], 'entry_node_uuids': ['direct'], 'exit_node_uuids': ['direct']},
            {'node_uuids': ['refill', 'consume'], 'entry_node_uuids': ['refill'], 'exit_node_uuids': ['consume']},
        ]}},
        'direct': {'type': 'ILab', 'parent_uuid': 'condition'},
        'refill': {'type': 'workflow', 'parent_uuid': 'condition', 'meta_data': {'unilab': {'composite': {'structural_mappings': {
            'entry_targets': [{'workflow_node_uuid': 'backup'}],
            'completion_sources': [{'workflow_node_uuid': 'move'}],
        }}}}},
        'backup': {'type': 'material_source', 'parent_uuid': 'refill'},
        'move': {'type': 'ILab', 'parent_uuid': 'refill'},
        'consume': {'type': 'ILab', 'parent_uuid': 'condition'},
    }
    result = ExecutionPlanGraphNormalizer().flatten_control_members(nodes)
    branch = result['condition']['param']['branches'][1]
    assert branch['node_uuids'] == ['backup', 'move', 'consume']
    assert branch['entry_node_uuids'] == ['backup']
    assert nodes['condition']['param']['branches'][1]['entry_node_uuids'] == ['refill']
    assert result['backup']['parent_uuid'] == 'refill'


def test_explicit_material_output_does_not_require_same_named_input():
    source = {'uuid': 'out', 'workflow_node_template_uuid': 'operation', 'handle_key': 'material', 'io_type': 'source', 'type': 'ResourceSlot', 'data_key': 'material', 'meta_data': {'unilab': {'value_schema': {'$slot': 'ResourceSlot'}, 'implicit_passthrough': False}}}
    result = _resolved_producer_schema(node_uuid='refill', source_handle_uuid='out', nodes={'refill': {'uuid': 'refill', 'type': 'workflow', 'param': {}}}, templates={}, handles={'out': source}, effective_params={'refill': {}}, incoming_by_target={}, cache={}, resolving=set())
    assert result.to_dict() == {'$slot': 'ResourceSlot'}


def test_all_operation_members_wait_for_outer_branch_selection():
    """子实验中的独立条件不能在外层选路前求值，否则之后无法跳过。"""
    from uuid import uuid4

    from unilabos.workflow.execution_plan import ExecutionPlanBuilder
    outer, call, entry, nested, fail = [str(uuid4()) for _ in range(5)]
    nodes = {
        outer: {'type': 'condition', 'param': {'branches': [{
            'node_uuids': [call], 'entry_node_uuids': [call], 'exit_node_uuids': [call],
        }]}},
        call: {'type': 'workflow', 'parent_uuid': outer, 'meta_data': {'unilab': {'composite': {'structural_mappings': {
            'entry_targets': [{'workflow_node_uuid': entry}],
            'completion_sources': [{'workflow_node_uuid': entry}],
        }}}}},
        entry: {'type': 'ILab', 'parent_uuid': call},
        nested: {'type': 'condition', 'parent_uuid': call, 'param': {'branches': [{
            'node_uuids': [fail], 'entry_node_uuids': [fail], 'exit_node_uuids': [fail],
        }]}},
        fail: {'type': 'ILab', 'parent_uuid': nested},
    }
    normalized = ExecutionPlanGraphNormalizer().flatten_control_members(nodes)
    edges = ExecutionPlanBuilder._condition_control_edges(
        nodes=normalized, kinds={outer: 'condition', nested: 'condition', entry: 'device_action', fail: 'device_action'},
        active_node_uuids={outer, nested, entry, fail},
    )
    pairs = {(edge['source_node_uuid'], edge['target_node_uuid']) for edge in edges}
    assert (outer, nested) in pairs
    assert (outer, entry) in pairs
    assert (nested, fail) in pairs
    from unilabos.app.scheduler.dag_state import WorkflowRun
    from unilabos.app.scheduler.models import WorkflowEdge, WorkflowNode, WorkflowSpec
    run = WorkflowRun(WorkflowSpec(workflow_id=str(uuid4()),
        nodes=[WorkflowNode(id=key, executor_kind='condition' if key in {outer, nested} else 'device_action')
               for key in [outer, nested, entry, fail]],
        edges=[WorkflowEdge(uuid=edge['uuid'], source_node_id=edge['source_node_uuid'],
                            target_node_id=edge['target_node_uuid']) for edge in edges]))
    assert [node.id for node in run.ready_nodes()] == [outer]
