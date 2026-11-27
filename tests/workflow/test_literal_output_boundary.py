"""Frozen literal outputs wait for real child completion before publication."""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tests.app.test_f05_material_source_scheduler import TASK_UUID, _seed_task
from unilabos.workflow.store import WorkflowStore
from unilabos.workflow.task_input import TaskInputError, _add_boundary_jobs
from unilabos.workflow.workflow_boundary import project_ready_workflow_output


@pytest.mark.parametrize('completion_status', ['pending', 'failed', 'skipped', 'succeeded'])
def test_literal_site_is_published_only_after_current_child_succeeds(tmp_path, completion_status):
    output_node, completed_node = str(uuid4()), str(uuid4())
    plan = {'nodes': [{'uuid': output_node, 'kind': 'workflow_output', 'output_bindings': {
        'site': {'kind': 'literal', 'value': 'cold-site-02', 'completion_node_uuids': [completed_node]},
    }}], 'edges': [], 'handles': []}
    store = WorkflowStore(tmp_path / 'literal-output.db')
    try:
        _seed_task(store, with_action=False)
        with store.transaction() as conn:
            conn.execute('DELETE FROM workflow_node_job WHERE workflow_task_uuid=?', (TASK_UUID,))
            conn.execute('UPDATE workflow_task SET execution_plan=? WHERE uuid=?', (json.dumps(plan), TASK_UUID))
            for node, kind, status, attempt, current in (
                (completed_node, 'device_action', 'succeeded', 1, 0),
                (completed_node, 'device_action', completion_status, 2, 1),
                (output_node, 'workflow_output', 'pending', 1, 1),
            ):
                conn.execute('''INSERT INTO workflow_node_job(
                    uuid,create_time,update_time,meta_data,workflow_task_uuid,workflow_node_uuid,
                    feedback_sequence,topological_index,executor_kind,execution_policy,
                    execution_timeout_seconds,status,attempt,current_attempt,param,
                    feedback_data,return_info,control_data,error_info)
                    VALUES (?, 'now','now','{}',?,?,0,0,?,'{}',0,?,?,?,'{}','{}','{}','{}','[]')''',
                    (str(uuid4()), TASK_UUID, node, kind, status, attempt, current))
            result = project_ready_workflow_output(conn, task_uuid=TASK_UUID, now='now', complete_task=False)
            if completion_status == 'succeeded':
                assert result.result == {'site': 'cold-site-02'}
            else:
                assert result.result is None
                assert conn.execute('SELECT output FROM workflow_task WHERE uuid=?', (TASK_UUID,)).fetchone()[0] == '{}'
    finally:
        store.close()


def test_literal_output_adds_only_the_real_completion_dependency():
    class Contract:
        def __init__(self, value):
            self.value = value

        def to_dict(self):
            return self.value

    completed_node = str(uuid4())
    workflow_io = SimpleNamespace(input_contract=Contract({'parameters': []}),
                                  output_contract=Contract({'outputs': [{'name': 'site'}]}))
    plan = {'nodes': [{'uuid': completed_node}], 'edges': []}
    jobs = [{'workflow_node_uuid': completed_node}]
    binding = {'site': {'kind': 'literal', 'value': 'cold-site-02', 'completion_node_uuids': [completed_node]}}
    _add_boundary_jobs(snapshot={'workflow': {'uuid': str(uuid4())}}, plan=plan, jobs=jobs,
                       resolved_input={}, workflow_io=workflow_io, output_bindings=binding)
    output_node, = [node for node in plan['nodes'] if node.get('kind') == 'workflow_output']
    assert output_node['output_bindings'] == binding
    assert [(edge['source_node_uuid'], edge['target_node_uuid']) for edge in plan['edges']] == [
        (completed_node, output_node['uuid'])]

    with pytest.raises(TaskInputError, match='完成边界'):
        _add_boundary_jobs(snapshot={'workflow': {'uuid': str(uuid4())}}, plan={'nodes': [], 'edges': []}, jobs=[],
                           resolved_input={}, workflow_io=workflow_io,
                           output_bindings={'site': {'kind': 'literal', 'value': 'cold-site-02'}})
