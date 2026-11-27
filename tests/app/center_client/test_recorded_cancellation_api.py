"""显式历史核实入口及异常投影共同遵守分阶段结算边界。"""
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.app.center_client.test_material_exports import inventory  # noqa: F401
from tests.app.center_client.test_recorded_cancellation import EVIDENCE, REASON, historical_case, history
from unilabos.app.center_client.material_settlement_api import create_router


def api(case):
    case.runtime._poll_lock = threading.RLock()
    case.runtime.wake = lambda: None
    app = FastAPI()
    app.include_router(create_router(lambda: case.runtime))
    return TestClient(app), f'/api/v1/center-material-settlements/{case.invocation}/recorded-cancellation'


def reviewed(client, path):
    response = client.get(path)
    assert response.status_code == 200, response.text
    body = response.json()['data']
    return {'reason': REASON, 'evidence_refs': EVIDENCE,
            'expected_local_facts_digest': body['local_facts_digest'],
            'expected_center_evidence_digest': body['center_evidence_digest']}


def test_history_api_requires_review_and_closes_attention_only_after_full_proof(tmp_path, inventory):
    with historical_case(tmp_path, inventory) as case:
        client, path = api(case)
        before, attention = history(case), case.runtime.materials.attention_state()
        backlog = case.runtime.store.capacity_backlog()['unfinished_claims']
        body = reviewed(client, path)
        assert case.client.review_requests == []
        assert client.post(path, json={'reason': REASON, 'evidence_refs': EVIDENCE}).status_code == 422
        assert client.post(path, json={**body, 'resources_released': True}).status_code == 422
        response = client.post(path, json=body)
        assert response.status_code == 200, response.text
        state = response.json()['data']
        assert state['completed'] and state['center_terminal_corrected'] is False
        assert client.get(path).json()['data']['state'] == state
        assert client.post(path, json=body).json()['data'] == state
        assert case.runtime.materials.attention_state() != attention
        assert case.runtime.materials.export_attention_items() == []
        assert all(p.station_invocation_uuid != case.invocation for p in case.runtime.store.packages())
        assert case.runtime.store.capacity_backlog()['unfinished_claims'] == backlog
        case.runtime.materials.pump()
        assert history(case) == before
        assert case.client.snapshot['state']['execution_status'] == 'canceled'
        assert client.post(path.removesuffix('/recorded-cancellation'),
                           json={'reason': REASON, 'evidence_refs': EVIDENCE}).status_code == 409


def test_pending_history_cannot_be_hidden_or_restarted_as_normal_export(tmp_path, inventory):
    with historical_case(tmp_path, inventory) as case:
        client, path = api(case)
        body = reviewed(client, path)
        case.client.lose_on_commit = 2
        assert client.post(path, json=body).status_code == 409
        detail = client.get(path).json()['data']['state']
        assert detail['resources_released'] and not detail['completed']
        item = case.runtime.materials.export_attention_items()[0]
        with pytest.raises(ValueError, match='历史核实'):
            case.runtime.materials.resume_failure(case.invocation, item['phase'], expected_revision=item['revision'])
        assert client.post(path, json=body).status_code == 200
        assert case.runtime.materials.export_attention_items() == []


def test_history_checks_bound_carrier_when_output_is_a_contained_sample(tmp_path, inventory):
    from unilabos.app.center_client.material_settlement import _assert_safe_release
    from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService

    with historical_case(tmp_path, inventory) as case:
        child = BackendResourceService(case.inventory).create_material({
            'resource_template_uuid': case.template, 'name': '容器内样品',
        })['uuid']
        with case.inventory.transaction() as c:
            c.execute('UPDATE material SET parent_uuid=? WHERE uuid=?', (case.local, child))
            c.execute("UPDATE material SET inventory_status='in_use' WHERE uuid=?", (case.local,))
            with pytest.raises(ValueError, match='设备'):
                _assert_safe_release(c, case.runtime.handoffs.authority, case.invocation,
                                     case.task['uuid'], extra_materials=[child])


def test_history_checks_unregistered_parent_without_expanding_unrelated_siblings(tmp_path, inventory):
    from uuid import uuid4

    from unilabos.app.center_client.material_settlement import _assert_safe_release
    from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
    from unilabos.app.scheduler.inventory.domain import MaterialRequirement, MaterialSourceAdmissionRequest
    from unilabos.app.scheduler.inventory.service import InventoryService

    with historical_case(tmp_path, inventory) as case:
        backend = BackendResourceService(case.inventory)
        parent = backend.create_material({'resource_template_uuid': case.template, 'name': '未登记的父载体'})['uuid']
        child = backend.create_material({'resource_template_uuid': case.template, 'name': '无旧占用的实际输出'})['uuid']
        sibling = backend.create_material({'resource_template_uuid': case.template, 'name': '无关兄弟物料'})['uuid']
        with case.inventory.transaction() as c:
            c.execute('UPDATE material SET parent_uuid=? WHERE uuid IN (?,?)', (parent, child, sibling))
            c.execute("UPDATE material SET inventory_status='in_use' WHERE uuid=?", (sibling,))
            checked = _assert_safe_release(c, case.runtime.handoffs.authority, case.invocation,
                                          case.task['uuid'], extra_materials=[child])
            assert parent in {row['uuid'] for row in checked}
            assert sibling not in {row['uuid'] for row in checked}
            c.execute("UPDATE material SET inventory_status='warehouse' WHERE uuid=?", (sibling,))
        InventoryService(case.inventory).admit_material_sources(str(uuid4()), [MaterialSourceAdmissionRequest(
            node_id=str(uuid4()), resource_template_uuid=case.template, custody_policy='task_exclusive',
            requirement=MaterialRequirement(template_id=case.template, instance_uuid=parent),
        )])
        with case.inventory.read_connection() as c:
            with pytest.raises(ValueError, match='其他'):
                _assert_safe_release(c, case.runtime.handoffs.authority, case.invocation,
                                     case.task['uuid'], extra_materials=[child])
