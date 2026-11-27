"""预检只复用静态计算，每个参数与当前库存仍独立校验。"""
import json
import sys
from copy import deepcopy
from uuid import UUID

import pytest

import unilabos.registry.material_lock_schema as schemas
import unilabos.workflow.resource_lock_plan as resources
from tests.workflow.test_f05_execution_plan_safety_followup import _real_authoring_graph
from tests.workflow.test_material_source_preflight_inventory import facts, source_runtime
from unilabos.workflow.execution_plan import ExecutionPlanBuilder


def _identity(value: int) -> str:
    return str(UUID(int=value))


def _nodes() -> list[dict]:
    reference = {'type': 'object', 'x-unilabos-material-lock': True,
                 'properties': {'uuid': {'type': 'string', 'format': 'uuid'}}, 'required': ['uuid']}
    schema = {'properties': {'goal': {'type': 'object', 'properties': {
        'plate': reference, 'amount': {'type': 'integer', 'minimum': 0}}}}}
    return [{'uuid': _identity(index), 'kind': 'device_action', 'type': 'device_action', 'device_id': 'robot',
             'material_uuid': _identity(100), 'param_schema': deepcopy(schema),
             'param': {'plate': {'uuid': _identity(200 + index)}, 'amount': index}}
            for index in (1, 2, 3)]


def _resource_plan(nodes: list[dict]):
    return ExecutionPlanBuilder._resource_plan(graph={'workflow': {}, 'nodes': nodes, 'edges': []},
                                               planned_nodes=nodes, planned_edges=[])


@pytest.mark.parametrize('reference', [False, True])
def test_schema_reuse_is_content_based_and_keeps_per_action_locks(monkeypatch, reference) -> None:
    nodes = _nodes()
    if reference:
        for node in nodes:
            goal = node['param_schema']['properties']['goal']
            goal['$defs'] = {'plate': goal['properties']['plate']}
            goal['properties']['plate'] = {'$ref': '#/$defs/plate'}
    # 键排列不同仍是同一 Schema；锁标记不同则必须另行编译。
    goal = nodes[1]['param_schema']['properties']['goal']
    goal['properties'] = dict(reversed(list(goal['properties'].items())))
    third = nodes[2]['param_schema']['properties']['goal']
    third['$defs' if reference else 'properties']['plate']['x-unilabos-material-lock'] = False
    calls = []
    original = schemas.compile_material_lock_schema
    def compile_schema(schema):
        calls.append(json.dumps(schema, sort_keys=True))
        return original(schema)
    monkeypatch.setattr(schemas, 'compile_material_lock_schema', compile_schema)
    plan = _resource_plan(nodes)
    keys = {item.canonical_key for item in plan.resources}
    assert f'material/{_identity(201)}/exclusive' in keys
    assert f'material/{_identity(202)}/exclusive' in keys
    assert f'material/{_identity(203)}/exclusive' not in keys
    assert len(calls) == len(set(calls)) == 2


def test_reused_schema_still_rejects_invalid_later_action() -> None:
    nodes = _nodes()
    nodes[1]['param']['amount'] = -1
    with pytest.raises(schemas.MaterialLockSchemaError):
        _resource_plan(nodes)


def test_schema_changes_between_builds_do_not_reuse_old_lock_rules() -> None:
    nodes = _nodes()
    before = _resource_plan(nodes)
    for node in nodes:
        node['param_schema']['properties']['goal']['properties']['plate']['x-unilabos-material-lock'] = False
    after = _resource_plan(nodes)
    key = f'material/{_identity(201)}/exclusive'
    assert key in {item.canonical_key for item in before.resources}
    assert key not in {item.canonical_key for item in after.resources}


def test_build_and_refresh_validate_node_projections_once(monkeypatch) -> None:
    callers = []
    original = resources.validate_resource_plan
    def validate(plan, **kwargs):
        caller = sys._getframe(1).f_code.co_name
        if caller in {'resource_plan_for_node', 'resource_plan_for_nodes'}:
            callers.append(caller)
        return original(plan, **kwargs)
    monkeypatch.setattr(resources, 'validate_resource_plan', validate)
    graph = _real_authoring_graph()
    plan, jobs = ExecutionPlanBuilder().build(graph, run_mode='normal', target_node_uuid=None)
    assert callers == ['resource_plan_for_nodes']
    callers.clear()
    ExecutionPlanBuilder.refresh_resource_plan(graph=graph, plan=plan, jobs=jobs)
    assert callers == ['resource_plan_for_nodes']
    parsed = resources.deserialize_resource_plan(plan['resource_plan'])
    projections = resources.resource_plan_for_nodes(parsed, [node['uuid'] for node in plan['nodes']])
    for node in plan['nodes']:
        assert node.get('resource_interval_ids', []) == [item['interval_id'] for item in projections[node['uuid']]['intervals']]


@pytest.mark.parametrize('batch', [False, True])
def test_preflight_builds_once_but_reads_inventory_again(tmp_path, monkeypatch, batch) -> None:
    with source_runtime(tmp_path) as runtime:
        count = 0
        original = ExecutionPlanBuilder.build
        def build(self, *args, **kwargs):
            nonlocal count
            count += 1
            return original(self, *args, **kwargs)
        monkeypatch.setattr(ExecutionPlanBuilder, 'build', build)
        workflow = runtime.candidate['workflow_uuid']
        def check():
            if batch:
                response = runtime.client.post('/api/v1/workflows/run-preflight', json={'items': [runtime.candidate]})
                return response.json()['data']['items'][0]
            response = runtime.client.post(f'/api/v1/workflows/{workflow}/run-preflight', json={'input': runtime.candidate['input']})
            return response.json()['data']
        before = facts(runtime)
        report = check()
        assert count == 1
        assert facts(runtime) == before
        assert not [c for c in report['checks'] if c.get('type') == 'material_source' and c.get('blocking')]
        runtime.backend.update_material(runtime.carrier, {'site_placement': {'action': 'remove'}})
        before = facts(runtime)
        count = 0
        report = check()
        assert count == 1
        assert any(c.get('code') == 'material_source_empty' and c.get('blocking') for c in report['checks'])
        assert facts(runtime) == before
        assert runtime.dispatcher.dispatched == []


def test_distinct_schema_constraints_are_not_shared() -> None:
    nodes = _nodes()
    nodes[1]['param_schema']['properties']['goal']['properties']['amount']['minimum'] = 10
    with pytest.raises(schemas.MaterialLockSchemaError):
        _resource_plan(nodes)


def test_invalid_schema_is_not_hidden_by_a_cached_valid_schema() -> None:
    nodes = _nodes()
    nodes[1]['param_schema']['properties']['goal']['properties']['amount']['type'] = 'not-a-json-schema-type'
    with pytest.raises(schemas.MaterialLockSchemaError):
        _resource_plan(nodes)


def test_preflight_reuses_bound_plan_and_keeps_missing_input_fallback(tmp_path, monkeypatch) -> None:
    with source_runtime(tmp_path) as runtime:
        prepared_plans = []
        prepare = runtime.service._prepare_task_input
        def capture(*args, **kwargs):
            prepared = prepare(*args, **kwargs)
            prepared_plans.append(prepared.execution_plan)
            return prepared
        monkeypatch.setattr(runtime.service, '_prepare_task_input', capture)
        workflow = runtime.candidate['workflow_uuid']
        report = runtime.client.post(f'/api/v1/workflows/{workflow}/run-preflight',
                                     json={'input': runtime.candidate['input']}).json()['data']
        assert report['summary']['execution_node_count'] == len(prepared_plans[0]['nodes'])
        before = facts(runtime)
        prepared_plans.clear()
        missing = runtime.client.post(f'/api/v1/workflows/{workflow}/run-preflight', json={'input': {}}).json()['data']
        assert not prepared_plans
        assert missing['can_run'] is False
        assert any(check['code'] == 'execution_plan_ready' for check in missing['checks'])
        assert any(check['blocking'] for check in missing['checks'])
        assert facts(runtime) == before
        assert runtime.dispatcher.dispatched == []
