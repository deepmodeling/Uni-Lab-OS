"""首次出站登记的稳定身份与跨进程恢复。"""

from copy import deepcopy

import pytest

from tests.app.test_station_handoff import setup, uid
from unilabos.app.scheduler.inventory.handoff import HandoffInventoryError


class Center:
    def __init__(self, authority):
        self.authority = authority
        self.requests = []
        self.rows = {}
        self.timeout = False

    def register_container(self, request, epoch):
        self.requests.append(deepcopy(request))
        identity = request["container_uuid"]
        self.rows.setdefault(
            identity,
            dict(
                container_uuid=identity,
                lab_uuid=self.authority.lab_uuid,
                barcode_namespace=request["barcode_namespace"],
                barcode=request["barcode"],
                current_station_uuid=self.authority.station_uuid,
                state_version=1,
                active_handoff_item_uuid=None,
                custody_status="available",
                attention=False,
            ),
        )
        if self.timeout:
            self.timeout = False
            raise TimeoutError("中心已登记，响应丢失")
        return deepcopy(self.rows[identity])

    def container(self, identity, epoch):
        return deepcopy(self.rows[identity])


def test_registration_persists_original_request_before_network_and_reuses_after_timeout(setup):
    from unilabos.app.center_client.container_registration import ContainerRegistrationBridge

    s = setup
    authority = s["auth"]["source"]
    # 当前载体模拟首次登记，不改写其本地物料身份。
    with authority.store.transaction() as c:
        c.execute("DELETE FROM container_station_binding")
    center = Center(authority)
    bridge = ContainerRegistrationBridge(center, authority)
    center.timeout = True
    with pytest.raises(TimeoutError):
        bridge.ensure(
            local_carrier_material_uuid=s["carrier"]["uuid"],
            barcode_namespace="lab",
            barcode="BKR-1",
            evidence_refs=["scan:1"],
        )
    pending = bridge.registration(s["carrier"]["uuid"])
    assert pending["status"] == "pending"
    assert pending["last_error"]
    restored = ContainerRegistrationBridge(center, authority)
    done = restored.ensure(
        local_carrier_material_uuid=s["carrier"]["uuid"],
        barcode_namespace="lab",
        barcode="BKR-1",
        evidence_refs=["scan:2"],
    )
    assert done["status"] == "registered"
    assert done["container_uuid"] == pending["container_uuid"]
    assert center.requests[0] == center.requests[1]
    assert len(center.rows) == 1


def test_registered_container_must_still_belong_to_station(setup):
    from unilabos.app.center_client.container_registration import ContainerRegistrationBridge

    s = setup
    authority = s["auth"]["source"]
    center = Center(authority)
    center.rows[s["ids"]["container"]] = deepcopy(s["details"][s["ids"]["container"]])
    bridge = ContainerRegistrationBridge(center, authority)
    with pytest.raises(HandoffInventoryError, match="归属|交接"):
        bridge.ensure(
            local_carrier_material_uuid=s["carrier"]["uuid"],
            barcode_namespace="lab",
            barcode="BKR-1",
            evidence_refs=["scan:1"],
        )


def test_bind_rejects_unavailable_carrier_before_creating_identity(setup):
    authority = setup["auth"]["source"]
    with authority.store.transaction() as c:
        c.execute("UPDATE material SET inventory_status='handoff_departed' WHERE uuid=?", (setup["carrier"]["uuid"],))
    with pytest.raises(HandoffInventoryError):
        authority.bind_container(
            container_uuid=uid(),
            local_carrier_material_uuid=setup["carrier"]["uuid"],
            barcode_namespace="lab",
            barcode="BKR-1",
        )


def test_registration_api_reports_durable_failure_and_recovery(setup):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from unilabos.app.center_client.registration_api import create_router

    s = setup
    authority = s["auth"]["source"]
    with authority.store.transaction() as c:
        c.execute("DELETE FROM container_station_binding")
    center = Center(authority)
    runtime = SimpleNamespace(
        handoffs=SimpleNamespace(client=center, authority=authority),
        store=SimpleNamespace(require_execution=lambda: None),
    )
    app = FastAPI()
    app.include_router(create_router(lambda: runtime))
    client = TestClient(app)
    url = "/api/v1/center-containers/" + s["carrier"]["uuid"] + "/registration"
    assert client.get(url).json()["data"] is None
    response = client.post(url, json={"barcode_namespace": "lab", "barcode": "BKR-1", "evidence_refs": ["scan:1"]})
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "registered"
    assert client.get(url).json()["data"]["container_uuid"] == response.json()["data"]["container_uuid"]


def test_local_history_attestation_exports_current_known_lineage_without_new_identity(setup):
    from unilabos.app.center_client.container_registration import ContainerRegistrationBridge
    from unilabos.app.scheduler.inventory.material_exports import InventoryMaterialAuthority
    from unilabos.center_protocol.digest import digest

    s = setup
    authority = s["auth"]["source"]
    # 已完成的原谱系回执是身份来源，新的版本证明不申请新身份。
    InventoryMaterialAuthority(
        authority.store,
        station_uuid=authority.station_uuid,
        instance_uuid=authority.instance_uuid,
        event_epoch_uuid=authority.event_epoch_uuid,
    )
    invocation, lineage = uid(), uid()
    with authority.store.transaction() as c:
        c.execute(
            "INSERT INTO station_material_export(invocation_uuid,local_task_uuid,source_digest,source_json,request_json,snapshot_json) VALUES (?,?,?,?,?,?)",
            (invocation, uid(), "x", "{}", "{}", "[]"),
        )
        c.execute(
            "INSERT INTO station_material_export_item(invocation_uuid,local_material_uuid,material_lineage_uuid) VALUES (?,?,?)",
            (invocation, s["carrier"]["uuid"], lineage),
        )
        c.execute(
            "UPDATE station_material_export SET completed=1,response_json='{}' WHERE invocation_uuid=?", (invocation,)
        )
    center = Center(authority)
    center.rows[s["ids"]["container"]] = {
        **s["details"][s["ids"]["container"]],
        "active_handoff_item_uuid": None,
        "custody_status": "available",
    }
    requests = []

    def attest(identity, request, epoch):
        requests.append(request)
        return {
            "container_uuid": identity,
            "container_state_version": request["container_state_version"],
            "request_digest": digest(request),
        }

    center.attest_material_inputs = attest
    result = ContainerRegistrationBridge(center, authority).attest_material_inputs(s["carrier"]["uuid"])
    assert result["container_uuid"] == s["ids"]["container"]
    assert requests[0]["material_refs"][0]["material_lineage_uuid"] == lineage
    assert requests[0]["inventory_snapshot"]["adapter"] == "unilab.inventory.v1"
    assert center.requests == []


def test_local_history_attestation_rejects_unknown_carrier_lineage(setup):
    from unilabos.app.center_client.container_registration import ContainerRegistrationBridge

    s = setup
    authority = s["auth"]["source"]
    center = Center(authority)
    center.rows[s["ids"]["container"]] = {
        **s["details"][s["ids"]["container"]],
        "active_handoff_item_uuid": None,
        "custody_status": "available",
    }
    with pytest.raises(HandoffInventoryError, match="谱系"):
        ContainerRegistrationBridge(center, authority).attest_material_inputs(s["carrier"]["uuid"])


@pytest.mark.parametrize('guard_kind',['evidence_only','store_conflict'])
def test_registration_queries_survive_recovery_but_writes_return_conflict(setup,tmp_path,monkeypatch,guard_kind):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from unilabos.app.center_client.durable import DurableCenterStore
    from unilabos.app.center_client.registration_api import create_router
    from unilabos.persistence.recovery_mode import enter
    from unilabos.workflow.store import StoreConflict
    s=setup
    durable=DurableCenterStore(tmp_path/'registration-recovery.db',station_uuid=s['ids']['source'],instance_uuid=s['ids']['source_instance'])
    try:
        if guard_kind=='evidence_only':
            with durable._db.transaction():
                enter(durable._db,'恢复屏障下仍须读取登记失败原因')
        else:
            def blocked():
                raise StoreConflict('登记操作与原执行责任冲突')
            monkeypatch.setattr(durable,'require_execution',blocked)
        authority=s['auth']['source']
        with authority.store.transaction() as c:
            c.execute("UPDATE container_station_binding SET registration_last_error='原登记响应丢失' WHERE carrier_material_uuid=?",(s['carrier']['uuid'],))
        center=Center(authority)
        runtime=SimpleNamespace(store=durable,handoffs=SimpleNamespace(client=center,authority=authority))
        app=FastAPI()
        app.include_router(create_router(lambda:runtime))
        client=TestClient(app,raise_server_exceptions=False)
        base='/api/v1/center-containers/'+s['carrier']['uuid']
        read=client.get(base+'/registration')
        assert read.status_code==200
        assert read.json()['data']['last_error']=='原登记响应丢失'
        body={'barcode_namespace':'lab','barcode':'BKR-1','evidence_refs':['scan:1']}
        assert client.post(base+'/registration',json=body).status_code==409
        assert client.post(base+'/material-inputs').status_code==409
        assert center.requests==[]
    finally:
        durable.close()
