"""运行时选料的源码、执行计划与真实调度桥集成验证。"""
from copy import deepcopy
from uuid import uuid4

import pytest

from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.authoring_kernel import AuthoringCatalogSnapshot
from unilabos.workflow.execution_plan import ExecutionPlanBuilder

from .test_authoring_engine import WORKFLOW_UUID, _applied_graph, _handle
from .test_f05_material_source_authoring import (
    MATERIAL_SOURCE_NODE_UUID,
    MATERIAL_SOURCE_TEMPLATE_UUID,
    PLATE_SOURCE_SYMBOL,
    PLATE_TEMPLATE_UUID,
)
from .test_f05_material_source_authoring_roundtrip import _goal_material_engine, _goal_material_source
from .test_structured_condition_authoring import CONDITION_NODE_UUID, _condition_template

BACKUP = '20000000-0000-4000-8000-000000000041'
REFILL = '20000000-0000-4000-8000-000000000042'


def compiled_refill():
    base = _goal_material_engine()._catalog
    templates = [action.detached_template() for action in base.actions]
    handles = [handle for action in base.actions for handle in action.detached_handles()]
    templates.append(_condition_template())
    handles.append(_handle(str(uuid4()), node_template_uuid=MATERIAL_SOURCE_TEMPLATE_UUID,
                           key='sufficient', io_type='source', value_type='boolean'))
    for template in templates:
        if template['name'] == 'process_plate':
            template['node_type'] = 'ILab'
            template['schema'] = {'type': 'object', 'properties': {'plate': {'$slot': 'ResourceSlot'}}, 'required': ['plate']}
            template['meta_data']['unilab'] = {'action_contract_schema': {'type': 'object', 'properties': {'goal': template['schema']}}}
    engine = WorkflowAuthoringEngine(catalog=AuthoringCatalogSnapshot.from_entities(
        templates, handles, resource_template_symbols={PLATE_SOURCE_SYMBOL: PLATE_TEMPLATE_UUID}))
    source = _goal_material_source()
    start = source.index('    # unilab:node_uuid=' + MATERIAL_SOURCE_NODE_UUID)
    end = source.index('    # unilab:node_uuid=', start + 10)
    declaration = source[start:end]
    backup = declaration.replace(MATERIAL_SOURCE_NODE_UUID, BACKUP).replace('plate = material_source', 'backup = material_source')
    source = source[:start] + declaration.replace('mode="existing",', 'mode="existing", supports_replenishment=True,') + f'''    # unilab:node_uuid={CONDITION_NODE_UUID}
    if plate.sufficient:
        # unilab:node_uuid=20000000-0000-4000-8000-000000000011
        direct = reactor.process_plate(plate=plate)
    else:
''' + ''.join('    ' + line + '\n' for line in backup.rstrip().splitlines()) + f'''        # unilab:node_uuid={REFILL}
        refilled = reactor.process_plate(plate=backup)
    return workflow_output()
'''
    compiled = engine.compile(workflow_uuid=WORKFLOW_UUID, workflow_revision=7,
        python_source=source, source_uri='package://lab/workflows/refill.py', applied_graph=_applied_graph())
    assert compiled.valid, compiled.diagnostics
    return engine, compiled


def test_runtime_source_roundtrip_and_branch_dependencies():
    engine, compiled = compiled_refill()
    repeated = engine.compile(workflow_uuid=WORKFLOW_UUID, workflow_revision=7,
        python_source=compiled.normalized_python_source, source_uri='package://lab/workflows/refill.py', applied_graph=compiled.graph)
    assert repeated.valid, repeated.diagnostics
    assert repeated.graph == compiled.graph
    plan, jobs = ExecutionPlanBuilder().build(compiled.graph, run_mode='normal', target_node_uuid=None)
    nodes = {node['uuid']: node for node in plan['nodes']}
    assert nodes[MATERIAL_SOURCE_NODE_UUID]['runtime_material_source']
    assert nodes[BACKUP]['runtime_material_source']
    assert 'runtime_material_source_v1' in plan['capabilities']
    assert any(edge['source_node_uuid'] == CONDITION_NODE_UUID and edge['target_node_uuid'] == BACKUP for edge in plan['edges'])


def test_runtime_capability_is_not_an_execution_plan_version():
    """能力名称不能混入版本白名单，非法版本必须在编译入口拒绝。"""
    from unilabos.workflow.workflow_spec_compiler import WorkflowSpecCompilationError, WorkflowSpecCompiler

    _, compiled = compiled_refill()
    plan, jobs = ExecutionPlanBuilder().build(compiled.graph, run_mode='normal', target_node_uuid=None)
    plan['version'] = 'runtime_material_source_v1'
    with pytest.raises(WorkflowSpecCompilationError, match='执行计划版本必须是'):
        WorkflowSpecCompiler().compile({'uuid': str(uuid4()), 'execution_plan': plan}, jobs)

@pytest.mark.parametrize('available,backup_available', [(True, False), (False, True), (False, False)])
def test_runtime_source_selects_only_actual_branch(tmp_path, available, backup_available):
    import json

    from tests.app.test_f05_material_source_scheduler import MATERIAL_UUID, TASK_UUID, _seed_task, _ToggleInventory
    from unilabos.app.scheduler.dispatch import RecordingDispatcher
    from unilabos.app.scheduler.inventory.domain import InsufficientStock
    from unilabos.app.scheduler.service import EdgeScheduler
    from unilabos.workflow.store import WorkflowStore
    from unilabos.workflow.task_scheduler_bridge import TaskSchedulerBridge

    _, compiled = compiled_refill()
    plan, jobs = ExecutionPlanBuilder().build(compiled.graph, run_mode='normal', target_node_uuid=None)
    # 本测试聚焦真实 DAG 与持久投影；设备和库存边界使用可控替身。
    plan.pop('resource_plan', None)
    plan.pop('inventory_resource_binding', None)
    for node in plan['nodes']:
        if node['kind'] == 'device_action':
            node['device_id'] = 'reactor-a'
    class Inventory(_ToggleInventory):
        def admit_task_materials(self, task_uuid, requests, quantities):
            if ((requests[0].node_id == MATERIAL_SOURCE_NODE_UUID and not available)
                    or (requests[0].node_id == BACKUP and not backup_available)):
                self.admission_calls.append((task_uuid, requests))
                raise InsufficientStock('工作位无料')
            return super().admit_task_materials(task_uuid, requests, quantities)
    store = WorkflowStore(tmp_path / 'workflow.db')
    _seed_task(store, with_action=False)
    with store.transaction() as connection:
        connection.execute('UPDATE workflow_task SET execution_plan=?,workflow_snapshot=? WHERE uuid=?', (json.dumps(plan), json.dumps(compiled.graph), TASK_UUID))
        connection.execute('DELETE FROM workflow_node_job WHERE workflow_task_uuid=?', (TASK_UUID,))
        for job in jobs:
            connection.execute('''INSERT INTO workflow_node_job(
                uuid,create_time,update_time,meta_data,workflow_task_uuid,workflow_node_uuid,
                feedback_sequence,topological_index,executor_kind,execution_policy,
                execution_timeout_seconds,status,attempt,param,feedback_data,return_info,control_data,error_info)
                VALUES (?, '2026-09-16T00:00:00Z','2026-09-16T00:00:00Z','{}',?,?,0,?,?,'{}',0,'pending',1,?,'{}','{}','{}','[]')''',
                (job['uuid'], TASK_UUID, job['workflow_node_uuid'], job['topological_index'], job['executor_kind'], json.dumps(job['param'])))
    inventory = Inventory(available=True)
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher, inventory=inventory)
    bridge = TaskSchedulerBridge(store, scheduler=scheduler)
    try:
        bridge.submit(store.get_task(TASK_UUID))
        if not available and not backup_available:
            assert dispatcher.dispatched == []
            failed_backup = next(job for job in store.list_jobs(TASK_UUID) if job['workflow_node_uuid'] == BACKUP)
            assert failed_backup['status'] == 'failed'
            return
        assert len(dispatcher.dispatched) == 1, store.list_jobs(TASK_UUID)
        command = dispatcher.dispatched[0]
        by_uuid = {job['uuid']: job for job in store.list_jobs(TASK_UUID)}
        assert by_uuid[command['job_id']]['workflow_node_uuid'] == ('20000000-0000-4000-8000-000000000011' if available else REFILL)
        args = command['action_args']
        if isinstance(args, str):
            args = json.loads(args)
        args = args.get('goal', args)
        if isinstance(args, str):
            args = json.loads(args)
        assert args['plate']['uuid'] == MATERIAL_UUID
        assert [req.node_id for _, requests in inventory.admission_calls for req in requests] == ([MATERIAL_SOURCE_NODE_UUID] if available else [MATERIAL_SOURCE_NODE_UUID, BACKUP])
        scheduler.on_job_finished(command['job_id'], True, {})
        assert store.get_task(TASK_UUID)['status'] == 'succeeded'
    finally:
        bridge.close()
        store.close()

@pytest.mark.parametrize('required, enough', [(2.0, True), (20.0, False)])
def test_quantity_is_checked_with_runtime_source_atomically(tmp_path, required, enough):
    from tests.workflow.quantity_inventory_test_support import NODE_UUID, TASK_UUID, _inventory, _workflow
    from unilabos.app.scheduler.inventory.domain import MaterialRequirement, MaterialSourceAdmissionRequest
    from unilabos.app.scheduler.inventory.service import InventoryService
    from unilabos.app.scheduler.inventory.workflow_quantity import WorkflowQuantityReservationError
    from unilabos.workflow.quantity_inventory import WorkflowQuantityInventory
    from unilabos.workflow.task_input import PreparedTaskInput

    inventory_store, material_uuid, info_uuid, _ = _inventory(tmp_path)
    store, graph, prepared = _workflow(tmp_path / 'workflow.db', reagent_info_uuid=info_uuid, material_uuid=material_uuid)
    inventory = InventoryService(inventory_store)
    coordinator = WorkflowQuantityInventory(store, inventory)
    template_uuid = inventory_store.query_one('SELECT resource_template_uuid FROM material WHERE uuid=?', (material_uuid,))['resource_template_uuid']
    source = {'uuid': MATERIAL_SOURCE_NODE_UUID, 'kind': 'material_source', 'runtime_material_source': True,
              'material_requirements': [{'instance_uuid': material_uuid, 'template_id': template_uuid}]}
    snapshot = deepcopy(prepared.workflow_snapshot)
    requirement = snapshot['inventory_requirements'][0]
    requirement['required_quantity'] = required
    requirement['meta_data'] = {'unilab': {'material_source_node_uuid': MATERIAL_SOURCE_NODE_UUID}}
    plan = deepcopy(prepared.execution_plan)
    plan['nodes'].append(source)
    frozen = PreparedTaskInput(snapshot, {}, plan, prepared.jobs)
    try:
        with store.transaction() as connection:
            assert coordinator.prepare_task_allocations(connection, graph=snapshot, prepared=frozen, task_uuid=TASK_UUID, bindings=[], defer_inventory_reservation=True) == []
        task = {'uuid': TASK_UUID, 'workflow_snapshot': snapshot, 'execution_plan': plan}
        request = MaterialSourceAdmissionRequest(node_id=MATERIAL_SOURCE_NODE_UUID, resource_template_uuid=template_uuid,
                    custody_policy='task_exclusive', requirement=MaterialRequirement.from_dict(source['material_requirements'][0]))
        def build(result):
            return coordinator.prepare_runtime_source_allocations(task, prepared.jobs, MATERIAL_SOURCE_NODE_UUID, result)
        if enough:
            result = inventory.admit_task_materials(TASK_UUID, [request], (), quantity_builder=build)
            assert result['allocations'][MATERIAL_SOURCE_NODE_UUID] == [material_uuid]
        else:
            with pytest.raises(WorkflowQuantityReservationError):
                inventory.admit_task_materials(TASK_UUID, [request], (), quantity_builder=build)
            assert not inventory_store.query_all('SELECT * FROM inventory_material_source_binding')
            assert not inventory_store.query_all('SELECT * FROM inventory_reservation')
    finally:
        store.close()
        inventory_store.close()


def test_missing_condition_is_rejected_before_execution():
    from unilabos.workflow._execution_plan_graph import ExecutionPlanBuildError
    from unilabos.workflow.runtime_material_source import validate_replenishment_plan
    _, compiled = compiled_refill()
    plan, _ = ExecutionPlanBuilder().build(compiled.graph, run_mode='normal', target_node_uuid=None)
    with pytest.raises(ExecutionPlanBuildError, match='sufficient'):
        validate_replenishment_plan([n for n in plan['nodes'] if n['kind'] != 'condition'])


def test_shortage_cannot_consume_original_source_after_join():
    from unilabos.workflow._execution_plan_graph import ExecutionPlanBuildError
    from unilabos.workflow.runtime_material_source import validate_replenishment_plan
    _, compiled = compiled_refill()
    plan, _ = ExecutionPlanBuilder().build(compiled.graph, run_mode='normal', target_node_uuid=None)
    source = next(n for n in plan['nodes'] if n['uuid'] == MATERIAL_SOURCE_NODE_UUID)
    source['material_binding_targets'].append({'workflow_node_uuid': REFILL, 'param_key': 'plate'})
    with pytest.raises(ExecutionPlanBuildError, match='sufficient=true'):
        validate_replenishment_plan(plan['nodes'])


def test_dynamic_material_parameters_follow_only_resource_edges():
    from unilabos.workflow.runtime_material_source import runtime_material_parameters
    nodes = [{'uuid': 'source', 'runtime_material_source': True}, {'uuid': 'move'}, {'uuid': 'use'}, {'uuid': 'other'}]
    edges = [
        {'source_node_uuid': 'source', 'target_node_uuid': 'move', 'source_type': 'ResourceSlot', 'target_data_key': 'goal@@@material'},
        {'source_node_uuid': 'move', 'target_node_uuid': 'use', 'source_type': 'ResourceSlot', 'target_data_key': 'goal@@@material'},
        {'source_node_uuid': 'source', 'target_node_uuid': 'other', 'source_type': 'boolean', 'target_data_key': 'sufficient'},
    ]
    assert runtime_material_parameters(nodes, edges) == {'move': ['material'], 'use': ['material']}


def test_preadmitted_material_source_marks_downstream_runtime_identity():
    """普通来源也在计划生成后由准入绑定，转运 Site 应交给库存 Permit 复验。"""

    from unilabos.workflow.runtime_material_source import runtime_material_parameters

    nodes = [
        {"uuid": "source", "kind": "material_source"},
        {"uuid": "move", "kind": "material_transfer"},
    ]
    edges = [
        {
            "source_node_uuid": "source",
            "target_node_uuid": "move",
            "source_type": "ResourceSlot",
            "target_data_key": "goal@@@resource",
        }
    ]

    assert runtime_material_parameters(nodes, edges) == {"move": ["resource"]}


def test_material_io_load_marks_downstream_runtime_identity() -> None:
    """装载产出的新物料必须进入下游动作的运行时锁集合。"""

    from unilabos.workflow.runtime_material_source import runtime_material_parameters

    nodes = [
        {"uuid": "load", "kind": "material_io"},
        {"uuid": "carry", "kind": "device_action"},
        {"uuid": "source", "kind": "material_source"},
    ]
    edges = [
        {
            "source_node_uuid": "load",
            "target_node_uuid": "carry",
            "source_type": "ResourceSlot",
            "target_data_key": "goal@@@material",
        },
        {
            "source_node_uuid": "load",
            "target_node_uuid": "source",
            "source_type": "ResourceSlot",
            "target_data_key": "goal@@@mount",
        },
    ]

    assert runtime_material_parameters(nodes, edges) == {
        "carry": ["material"],
        "source": ["mount"],
    }


def test_command_output_known_only_at_runtime_marks_downstream_owner() -> None:
    """命令读出的所属物料（非透传）在执行后才确定，下游搬运的所属参数按运行时物料处理。"""

    from unilabos.workflow.runtime_material_source import runtime_material_parameters

    nodes = [
        {"uuid": "locate", "kind": "workflow_command",
         "meta_data": {"unilab": {"material_passthrough_handles": {"out-material": "in-material"}}}},
        {"uuid": "carry", "kind": "device_action"},
        {"uuid": "use", "kind": "device_action"},
    ]
    handles = [
        {"uuid": "h-owner", "template_handle_uuid": "out-owner"},
        {"uuid": "h-material", "template_handle_uuid": "out-material"},
    ]
    edges = [
        {"uuid": "e1", "source_node_uuid": "locate", "source_handle_uuid": "h-owner",
         "target_node_uuid": "carry", "source_type": "ResourceSlot", "target_data_key": "goal@@@material"},
        {"uuid": "e2", "source_node_uuid": "locate", "source_handle_uuid": "h-material",
         "target_node_uuid": "use", "source_type": "ResourceSlot", "target_data_key": "goal@@@material"},
    ]
    # 透传的原物料身份在计划期已知，不因经过命令而变成运行时物料。
    assert runtime_material_parameters(nodes, edges, handles) == {"carry": ["material"]}
    assert runtime_material_parameters(nodes, edges) == {"carry": ["material"], "use": ["material"]}


def test_transfer_with_runtime_owner_counts_its_inventory_keys_as_runtime() -> None:
    from types import SimpleNamespace

    from unilabos.app.scheduler.service import EdgeScheduler

    contract = {"material_param": "material", "source_owner_param": "", "target_owner_param": "target_owner"}
    assert EdgeScheduler._transfer_uses_runtime_material(
        SimpleNamespace(runtime_material_parameters=["target_owner"]), contract)
    assert not EdgeScheduler._transfer_uses_runtime_material(
        SimpleNamespace(runtime_material_parameters=[]), contract)


def _saved_refill_service(tmp_path):
    """保存一份合法补料图，返回服务、工作流身份、当前修订与已保存图。"""
    import json

    from unilabos.workflow.service import WorkflowService
    from unilabos.workflow.store import WorkflowStore
    from unilabos.workflow.template_projection_store import RegistryTemplateProjectionStore

    _, compiled = compiled_refill()
    graph = compiled.graph
    store = WorkflowStore(tmp_path / 'workflow.db')
    templates = {template['uuid']: template for template in graph['node_templates']}
    handles = []
    for original in graph['handle_templates']:
        handle = dict(original)
        template = templates[handle.pop('workflow_node_template_uuid')]
        handle['node_business_key'] = (template['resource_template_uuid'], template['name'])
        handles.append(handle)
    RegistryTemplateProjectionStore(store).replace(
        authority_id='refill-save', node_templates=graph['node_templates'], handle_templates=handles)
    service = WorkflowService(store)
    workflow_uuid = service.create_workflow(name='refill', tags=[], description='', meta_data={})['uuid']
    graph = json.loads(json.dumps(graph).replace(WORKFLOW_UUID, workflow_uuid))
    saved = service.save_graph(workflow_uuid, revision=service.get_graph(workflow_uuid)['workflow']['revision'],
                               nodes=graph['nodes'], edges=graph['edges'])
    return service, workflow_uuid, saved


def _without_shortage_branch(graph, *, keep_empty_branch, replenish=True):
    """删掉缺料分支成员；按需保留空分支壳或整条去掉，并可关闭支持补料。"""
    removed = {BACKUP, REFILL}
    nodes = [deepcopy(node) for node in graph['nodes'] if node['uuid'] not in removed]
    edges = [edge for edge in graph['edges']
             if edge['source_node_uuid'] not in removed and edge['target_node_uuid'] not in removed]
    condition = next(node for node in nodes if node['uuid'] == CONDITION_NODE_UUID)
    if keep_empty_branch:
        condition['param']['branches'][1].update(node_uuids=[], entry_node_uuids=[], exit_node_uuids=[])
    else:
        condition['param']['branches'] = condition['param']['branches'][:1]
    if not replenish:
        source = next(node for node in nodes if node['uuid'] == MATERIAL_SOURCE_NODE_UUID)
        source['param'].pop('supports_replenishment', None)
    return nodes, edges


@pytest.mark.parametrize('keep_empty_branch, code, reason', [
    # 空分支壳先被通用条件分支校验拒绝
    (True, 'invalid_control_region', '条件分支成员或边界无效'),
    # 单分支条件本身合法，只有补料规则能拦住
    (False, 'material_shortage_unhandled', f'物料来源 {MATERIAL_SOURCE_NODE_UUID} 启用支持补料后'),
])
def test_saving_refill_graph_without_shortage_branch_keeps_current_graph(tmp_path, keep_empty_branch, code, reason):
    """缺料分支为空或缺失的补料图必须在保存时拒绝，并给出具体原因，不能覆盖当前有效图。"""
    from fastapi.testclient import TestClient

    from unilabos.app.workflow_api import create_workflow_app
    from unilabos.workflow.service import WorkflowError

    service, workflow_uuid, saved = _saved_refill_service(tmp_path)
    try:
        nodes, edges = _without_shortage_branch(saved, keep_empty_branch=keep_empty_branch)
        with pytest.raises(WorkflowError) as raised:
            service.save_graph(workflow_uuid, revision=saved['workflow']['revision'], nodes=nodes, edges=edges)
        assert raised.value.code == code
        assert reason in raised.value.message
        with TestClient(create_workflow_app(service)) as client:
            response = client.put(f'/api/v1/workflows/{workflow_uuid}/graph', json={
                'revision': saved['workflow']['revision'], 'nodes': nodes, 'edges': edges})
        body = response.json()
        assert body['code'] == 1000
        assert reason in body['error']['msg']
        current = service.get_graph(workflow_uuid)
        assert current['workflow']['revision'] == saved['workflow']['revision']
        assert {node['uuid'] for node in current['nodes']} == {node['uuid'] for node in saved['nodes']}
        ExecutionPlanBuilder().build(current, run_mode='normal', target_node_uuid=None)
    finally:
        service.close()


def test_single_branch_condition_without_refill_still_saves(tmp_path):
    """未启用补料时单分支条件仍按原规则保存，保存校验只收紧补料图。"""
    service, workflow_uuid, saved = _saved_refill_service(tmp_path)
    try:
        nodes, edges = _without_shortage_branch(saved, keep_empty_branch=False, replenish=False)
        after = service.save_graph(workflow_uuid, revision=saved['workflow']['revision'], nodes=nodes, edges=edges)
        assert after['workflow']['revision'] == saved['workflow']['revision'] + 1
    finally:
        service.close()
