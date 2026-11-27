"""外部交接通过共同动作连接手工预留时，复用同一资源的显式所有权。"""
from types import SimpleNamespace

import pytest

from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.workflow.resource_lock_plan import bind_station_resource_plan, compile_template_resource_plan


@pytest.mark.parametrize("boundary", ["connected", "disconnected", "foreign_task", "released_key"])
def test_external_handoff_shares_selected_preparation_via_common_pick(monkeypatch, boundary):
    """01 与03不在单一范围，但通过02共享栈资源；不能借此共享机械臂。"""
    graph = {
        'nodes': [
            {'uuid': '01-out', 'resources': ['stack']},
            {'uuid': '02-pick', 'resources': ['stack', 'arm']},
            {'uuid': '03-prepare', 'resources': ['table']},
            {'uuid': '04-place', 'resources': ['arm', 'table']},
        ],
        'edges': [{'source_node_uuid': a, 'target_node_uuid': b} for a, b in
                  [('01-out', '02-pick'), ('02-pick', '04-place'), ('03-prepare', '04-place')]],
        'resource_scopes': [
            {'scope_id': 'selected', 'kind': 'reservation', 'resources': ['stack', 'arm', 'table', 'stack-site'],
             'node_uuids': ['02-pick', '03-prepare', '04-place']},
            {'scope_id': 'handoff', 'kind': 'transfer', 'resources': ['stack-site'],
             'node_uuids': ['01-out', '02-pick'], 'source': 'inventory.material_site_handoff'},
        ],
    }
    if boundary == "disconnected":
        graph["resource_scopes"][1]["node_uuids"] = ["01-out"]
    template = compile_template_resource_plan(graph)
    plan = bind_station_resource_plan(template, {
        resource.alias: {'canonical_key': f'/devices/{resource.alias}', 'kind': 'device'}
        for resource in template.resources
    })
    scheduler = EdgeScheduler()
    run = SimpleNamespace(spec=object(), resource_template_node_uuid=lambda value: value)
    scheduler._workflows['task'] = run
    monkeypatch.setattr(scheduler, '_resource_plan_for_spec', lambda _: plan)
    def intervals(node):
        return [item.interval_id for item in plan.intervals if node in item.node_uuids]
    scheduler._inflight['prepare-job'] = SimpleNamespace(
        job_id='prepare-job', workflow_id='other-task' if boundary == 'foreign_task' else 'task', node_id='03-prepare',
        resource_interval_ids=intervals('03-prepare'),
        operator_released_keys={'/devices/stack'} if boundary == 'released_key' else set(),
    )
    candidate = SimpleNamespace(id='01-out', resource_interval_ids=intervals('01-out'))
    expected = {'/devices/stack-site'} if boundary == 'released_key' else {'/devices/stack', '/devices/stack-site'}
    assert scheduler._shared_scope_holders('task', candidate) == (
        {} if boundary in {'disconnected', 'foreign_task'} else {'prepare-job': expected}
    )
