"""已发布图的抽象设备必须在工站调用中恢复为库存身份。"""
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest

from tests.workflow.test_f05_authoring_fixed_executor_projection import (
    DEVICE_MATERIAL_UUID,
    DEVICE_RESOURCE_TEMPLATE_UUID,
    _compile,
)
from unilabos.workflow.composite_invocation import compile_composite_invocation
from unilabos.workflow.execution_plan import ExecutionPlanBuilder
from unilabos.workflow.published_contract import PublishedWorkflowContractStore
from unilabos.workflow.station_contract import bind_station_contract
from unilabos.workflow.store import WorkflowStore


@pytest.fixture
def published(tmp_path: Path):
    store = WorkflowStore(tmp_path / 'workflow.db')
    graph = deepcopy(_compile(DEVICE_MATERIAL_UUID).graph)
    graph['workflow']['revision'] = 1
    store.create_workflow(workflow_uuid=graph['workflow']['uuid'], name='冻结设备验收', tags=[], description=None, meta_data={})
    contracts = PublishedWorkflowContractStore(store)
    contract = contracts.publish(graph=graph, expected_revision=1)
    try:
        yield contract
    finally:
        store.close()


def test_published_device_reaches_execution_plan_without_changing_contract(published) -> None:
    original = deepcopy(published)
    assert published['graph_snapshot']['nodes'][0]['material_uuid'] is None
    def resolve(identity: str):
        assert identity == DEVICE_MATERIAL_UUID
        return {'uuid': identity, 'resource_template_uuid': DEVICE_RESOURCE_TEMPLATE_UUID}
    graph = bind_station_contract(published, resolve)
    plan, _ = ExecutionPlanBuilder().build(graph, run_mode='normal', target_node_uuid=None)
    action = next(node for node in plan['nodes'] if node.get('device_id'))
    assert action['material_uuid'] == DEVICE_MATERIAL_UUID
    assert published == original


@pytest.mark.parametrize('resolved', [None, {'uuid': DEVICE_MATERIAL_UUID, 'resource_template_uuid': str(uuid4())}])
def test_missing_or_retyped_inventory_rejects_frozen_device(published, resolved) -> None:
    with pytest.raises(ValueError):
        bind_station_contract(published, lambda _identity: resolved)


def test_no_inventory_authority_cannot_guess_fixed_device(published) -> None:
    with pytest.raises(ValueError, match='库存解析端口'):
        bind_station_contract(published, None)


@pytest.mark.parametrize('depth', [1, 2])
@pytest.mark.parametrize('legacy_alias', [False, True])
def test_parent_binding_overrides_child_alias_in_frozen_execution_plan(
    tmp_path: Path, depth: int, legacy_alias: bool,
) -> None:
    """真实发布和两层展开后，计划必须执行父调用选定的设备。"""
    store = WorkflowStore(tmp_path / 'nested.db')
    contracts = PublishedWorkflowContractStore(store)

    def publish(graph):
        graph['workflow']['revision'] = 1
        graph['workflow']['workflow_type'] = 'experiment_operation'
        store.create_workflow(workflow_uuid=graph['workflow']['uuid'], name='嵌套绑定', tags=[], description=None, meta_data={})
        return contracts.publish(graph=graph, expected_revision=1)

    try:
        child = publish(deepcopy(_compile(DEVICE_MATERIAL_UUID).graph))
        selected = DEVICE_MATERIAL_UUID
        for _ in range(depth):
            selected = str(uuid4())
            parent = deepcopy(_compile(DEVICE_MATERIAL_UUID).graph)
            parent['workflow']['uuid'] = str(uuid4())
            parent['nodes'] = []
            parent['edges'] = []
            expansion = compile_composite_invocation(
                parent_graph=parent, contract=child, invocation_uuid=str(uuid4()),
                pose={}, param={}, device_bindings={r['key']: selected for r in child['executor_requirements']},
            )
            parent['nodes'] = list(expansion.nodes)
            parent['edges'] = list(expansion.edges)
            parent['workflow']['meta_data'] = expansion.workflow_meta_data
            parent['inventory_requirements'] = list(expansion.inventory_requirements)
            parent['node_templates'] = list(child['graph_snapshot']['node_templates'])
            if legacy_alias:
                for node in parent['nodes']:
                    binding = node.get('meta_data', {}).get('unilab', {}).get('executor_binding')
                    if binding:
                        binding['device_id'] = DEVICE_MATERIAL_UUID
            child = publish(parent)
        before = deepcopy(child)

        def resolve(identity: str):
            assert identity == selected
            return {'uuid': identity, 'resource_template_uuid': DEVICE_RESOURCE_TEMPLATE_UUID}

        with store.transaction() as connection:
            connection.execute("UPDATE published_workflow_contract SET deleted_at = '2026-10-19' WHERE uuid != ?", (child['uuid'],))
        graph = bind_station_contract(child, resolve, contracts.frozen_dependency)
        actions = [n for n in graph['nodes'] if n.get('material_uuid')]
        assert actions
        assert all(n['material_uuid'] == selected and n['meta_data']['unilab']['executor_binding']['device_id'] == selected for n in actions)
        assert child == before
        with pytest.raises(ValueError, match='子合同解析端口'):
            bind_station_contract(child, resolve)
    finally:
        store.close()
