"""手工交接跨库存保护、真实收货及可恢复边界 intent。"""

from __future__ import annotations

import copy
import json
from uuid import uuid4

import pytest

from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
from unilabos.app.scheduler.inventory.dispatch_admission import (
    DispatchAdmissionRequest,
    DispatchResource,
    InventoryMutationConflict,
    acquire_dispatch_permit,
    assert_inventory_mutation_unclaimed,
)
from unilabos.app.scheduler.inventory.handoff import HandoffInventoryError, StationHandoffAuthority
from unilabos.app.scheduler.inventory.ingress import IngressReservationError
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.app.scheduler.resource_lock import material_lock_key
from unilabos.center_protocol.digest import digest


def uid():
    return str(uuid4())


def package(payload):
    return {"payload": copy.deepcopy(payload), "payload_digest": digest(payload)}


@pytest.fixture
def setup(tmp_path):
    yield from handoff_inventory_fixture(tmp_path)


def handoff_inventory_fixture(tmp_path, store_factory=InventoryStore):
    ids = {
        key: uid()
        for key in (
            "lab",
            "source",
            "target",
            "source_instance",
            "target_instance",
            "source_epoch",
            "target_epoch",
            "handoff",
            "item",
            "container",
            "lineage",
        )
    }
    stores = {side: store_factory(str(tmp_path / (side + ".db"))) for side in ("source", "target")}
    clock = [1_800_000_000_000]
    authorities = {
        side: StationHandoffAuthority(
            stores[side],
            station_uuid=ids[side],
            instance_uuid=ids[side + "_instance"],
            event_epoch_uuid=ids[side + "_epoch"],
            lab_uuid=ids["lab"],
            now_ms=lambda: clock[0],
        )
        for side in stores
    }
    templates = {}
    sites = {}
    backends = {}
    for side, store in stores.items():
        backends[side] = backend = BackendResourceService(store)
        result = backend.sync_resource_templates(
            [
                {"id": "test.carrier", "display_name": "载体", "registry_type": "material", "class": {}, "category": ["container"]},
                {"id": "test.rack", "display_name": "入口", "registry_type": "resource", "class": {}},
            ]
        )["templates"]
        templates[side] = {row["name"]: row["uuid"] for row in result}
        rack = backend.create_material({"resource_template_uuid": templates[side]["test.rack"], "name": "rack"})
        sites[side] = uid()
        with store.transaction() as c:
            c.execute(
                "INSERT INTO site(uuid,create_time,update_time,material_uuid,name,allowed_resource_template_uuids) VALUES (?,'2026-01-01','2026-01-01',?,'entry',?)",
                (sites[side], rack["uuid"], json.dumps([templates[side]["test.carrier"]])),
            )
    carrier = backends["source"].create_material(
        {
            "resource_template_uuid": templates["source"]["test.carrier"],
            "name": "beaker",
            "barcode": "BKR-1",
            "site_placement": {"action": "place", "site_uuid": sites["source"]},
        }
    )
    authorities["source"].bind_container(
        container_uuid=ids["container"],
        local_carrier_material_uuid=carrier["uuid"],
        barcode_namespace="lab",
        barcode="BKR-1",
    )
    payload = {
        "handoff_uuid": ids["handoff"],
        "lab_uuid": ids["lab"],
        "source_station_uuid": ids["source"],
        "target_station_uuid": ids["target"],
        "source_instance_uuid": ids["source_instance"],
        "target_instance_uuid": ids["target_instance"],
        "status": "requested",
        "revision": 1,
        "attention": False,
        "attention_reasons": [],
        "items": [
            {
                "handoff_item_uuid": ids["item"],
                "container_uuid": ids["container"],
                "expected_state_version": 1,
                "reserved_state_version": 2,
                "current_state_version": 2,
                "source_material_refs": [
                    {
                        "station_uuid": ids["source"],
                        "local_material_uuid": carrier["uuid"],
                        "local_revision": carrier["revision"],
                        "material_lineage_uuid": ids["lineage"],
                    }
                ],
                "source_snapshot": {"label": "beaker"},
                "receipt": None,
            }
        ],
        "prepared": None,
        "released": None,
        "permit": None,
        "cancel_acks": [],
        "created_at_ms": clock[0],
        "in_transit_deadline_ms": clock[0] + 600000,
    }
    details = {
        ids["container"]: {
            "container_uuid": ids["container"],
            "lab_uuid": ids["lab"],
            "barcode_namespace": "lab",
            "barcode": "BKR-1",
            "current_station_uuid": ids["source"],
            "state_version": 2,
            "active_handoff_item_uuid": ids["item"],
            "custody_status": "reserved",
            "attention": False,
        }
    }
    plans = {
        ids["item"]: {
            "carrier_template_uuid": templates["target"]["test.carrier"],
            "carrier_lineage_uuid": ids["lineage"],
            "lineage_template_uuids": {ids["lineage"]: templates["target"]["test.carrier"]},
            "candidate_site_uuids": [sites["target"]],
            "ttl_seconds": 60,
        }
    }
    result = dict(
        ids=ids,
        stores=stores,
        clock=clock,
        auth=authorities,
        payload=payload,
        details=details,
        plans=plans,
        carrier=carrier,
        sites=sites,
        templates=templates,
        path=tmp_path,
    )
    yield result
    for store in stores.values():
        store.close()


def prepared(s):
    event = s["auth"]["target"].prepare_incoming(
        package(s["payload"]), container_details=s["details"], plans=s["plans"]
    )
    s["payload"].update(
        status="prepared",
        revision=2,
        prepared={
            "event_epoch_uuid": s["ids"]["target_epoch"],
            **{
                key: value
                for key, value in event["payload"].items()
                if key in {"protection_uuid", "reservation_until_ms", "evidence_refs"}
            },
        },
    )
    return event


def protect_workflow_origin(s):
    """通过真实来源准入、谱系回执及结果边界建立上游工作流的交接保护。"""
    from unilabos.app.scheduler.inventory.domain import MaterialRequirement, MaterialSourceAdmissionRequest
    from unilabos.app.scheduler.inventory.handoff_adapter import ADAPTER
    from unilabos.app.scheduler.inventory.material_exports import InventoryMaterialAuthority
    from unilabos.app.scheduler.inventory.outbound import protect_outputs
    from unilabos.app.scheduler.inventory.service import InventoryService
    store=s['stores']['source']
    task,invocation=uid(),uid()
    inventory=InventoryService(store)
    local=s['carrier']['uuid']
    template=s['templates']['source']['test.carrier']
    outputs=[{'material_port':'sample','item_key':local,'local_material_uuid':local,'resource_template_uuid':template}]
    inventory.admit_material_sources(task,[MaterialSourceAdmissionRequest(node_id=uid(),resource_template_uuid=template,
        custody_policy='task_exclusive',requirement=MaterialRequirement(template_id=template,instance_uuid=local))])
    protect_outputs(store,invocation_uuid=invocation,task_uuid=task,outputs=outputs)
    inventory.release_workflow(task,reason='workflow_succeeded')
    revision=store.query_one('SELECT aggregate_version FROM material WHERE uuid=?',(local,))['aggregate_version']
    refs=s['payload']['items'][0]['source_material_refs']
    refs[0]['local_revision']=revision
    authority=InventoryMaterialAuthority(store,station_uuid=s['ids']['source'],instance_uuid=s['ids']['source_instance'],event_epoch_uuid=s['ids']['source_epoch'])
    source={'invocation_uuid':invocation,'local_task_uuid':task,'station_uuid':s['ids']['source'],
        'owner_instance_uuid':s['ids']['source_instance'],'event_epoch_uuid':s['ids']['source_epoch'],
        'task_payload_digest':digest({'fixture_task':task}),'input_material_refs':[],'outputs':outputs}
    intent=authority.prepare(source)
    request=intent['request']
    receipt={'station_invocation_uuid':invocation,'owner_instance_uuid':s['ids']['source_instance'],
        'task_payload_digest':source['task_payload_digest'],'request_digest':digest(request),
        'items':[{'material_port':'sample','item_key':local,'origin':request['items'][0]['origin'],
            'material_ref':{**refs[0],'handoff_item_uuid':None}}]}
    authority.remember_response(invocation,receipt)
    containers=s['auth']['source'].export_output_containers([receipt['items'][0]['material_ref']])
    containers[0]['center_snapshot']={**s['details'][s['ids']['container']],'state_version':1,
        'active_handoff_item_uuid':None,'custody_status':'available'}
    authority.record_boundary(invocation,containers,handoff_authority=s['auth']['source'])
    authority.acknowledge(invocation)
    boundary=authority.pending_boundaries()[0]
    authority.acknowledge_boundary(invocation,{'station_invocation_uuid':invocation,
        'owner_instance_uuid':s['ids']['source_instance'],'request_digest':digest(boundary['request'])})
    s['payload']['items'][0]['source_snapshot']=containers[0]['source_snapshot']
    s['plans'][s['ids']['item']]['adapter']={'kind':ADAPTER,'site_names':{local:{}},'reagent_info_uuids':{}}


def frozen(s):
    if s['payload'].get('transfer_source_kind') == 'node_output':
        protect_workflow_origin(s)
    prepared(s)
    event = s["auth"]["source"].freeze_source(
        package(s["payload"]), physical_ready=True, evidence_refs=["operator:source-ready"]
    )
    s["payload"].update(
        status="released",
        revision=3,
        released={
            "event_epoch_uuid": s["ids"]["source_epoch"],
            **{key: value for key, value in event["payload"].items() if key in {"release_uuid", "evidence_refs"}},
        },
    )
    for authority in s["auth"].values():
        authority.apply_center_snapshot(package(s["payload"]))
    return event


def receive(s):
    return s["auth"]["target"].receive(
        s["ids"]["handoff"],
        s["ids"]["item"],
        barcode_namespace="lab",
        barcode="BKR-1",
        evidence_refs=["scan:1", "operator:placed"],
    )


@pytest.mark.parametrize("transport_status", ["dispatch_authorized", "in_transit"])
def test_target_receipt_is_durable_before_transport_workflow_finishes(setup, transport_status):
    """接货依据实际扫码和落位，不等待运输工作流的送达或返航终态。"""
    s = setup
    frozen(s)
    s["payload"].update(status=transport_status, revision=4,
                        permit={"authorization_uuid": uid(), "logistics_subject_uuid": uid()})
    s["auth"]["target"].apply_center_snapshot(package(s["payload"]))
    receipt = receive(s)
    restarted = StationHandoffAuthority(
        s["stores"]["target"], station_uuid=s["ids"]["target"],
        instance_uuid=s["ids"]["target_instance"], event_epoch_uuid=s["ids"]["target_epoch"],
        lab_uuid=s["ids"]["lab"], now_ms=lambda: s["clock"][0],
    )
    assert restarted.receive(s["ids"]["handoff"], s["ids"]["item"],
                             barcode_namespace="lab", barcode="BKR-1",
                             evidence_refs=["scan:1", "operator:placed"]) == receipt
    received_events = [intent for intent in restarted.pending_intents()
                       if intent["payload"]["kind"] == "received"]
    assert len(received_events) == 1
    assert received_events[0]["payload"]["items"] == [receipt]


def test_manual_loop_preserves_local_identity_and_only_releases_after_center_receipt(setup):
    s = setup
    frozen(s)
    target = s["stores"]["target"].query_one("SELECT * FROM material WHERE barcode='BKR-1'")
    assert target["uuid"] != s["carrier"]["uuid"]
    assert target["inventory_status"] == "incoming"
    assert s["stores"]["source"].get_instance(s["carrier"]["uuid"])["status"] == "handoff_outgoing"
    receipt = receive(s)
    assert receive(s) == receipt
    assert receipt["target_material_refs"][0]["local_material_uuid"] == target["uuid"]
    assert s["stores"]["target"].get_instance(target["uuid"])["status"] == "handoff_received"
    s["payload"]["items"][0]["receipt"] = receipt
    s["payload"]["items"][0]["current_state_version"] = 3
    s["payload"].update(status="received", revision=4)
    for authority in s["auth"].values():
        authority.apply_center_snapshot(package(s["payload"]))
        authority.apply_center_snapshot(package(s["payload"]))
    assert s["stores"]["target"].get_instance(target["uuid"])["status"] == "warehouse"
    assert s["stores"]["source"].get_instance(s["carrier"]["uuid"])["status"] == "handoff_departed"
    assert (
        s["stores"]["source"].query_one(
            "SELECT occupied_material_uuid FROM site WHERE uuid=?", (s["sites"]["source"],)
        )["occupied_material_uuid"]
        is None
    )
    intents = s["auth"]["target"].pending_intents()
    assert {i["payload"]["kind"] for i in intents} == {"prepared", "received", "snapshot_applied"}
    assert len(intents) == 3


def test_incoming_and_source_freeze_block_public_writes_and_direct_dispatch(setup):
    s = setup
    frozen(s)
    for side in ("source", "target"):
        store = s["stores"][side]
        material = store.query_one("SELECT uuid FROM material WHERE barcode='BKR-1'")["uuid"]
        with pytest.raises(InventoryMutationConflict), store.transaction() as c:
            assert_inventory_mutation_unclaimed(c, material_uuids=[material])
        with store.transaction() as c:
            decision = acquire_dispatch_permit(
                c,
                DispatchAdmissionRequest(
                    effect_uuid=uid(),
                    task_uuid=uid(),
                    job_uuid=uid(),
                    attempt=1,
                    parameter_hash="hash",
                    expected_change_set={},
                    resources=(DispatchResource(material_lock_key(material), "material", material),),
                ),
            )
            assert not decision.acquired
            assert decision.wait_code == "station_handoff_protected"
        with pytest.raises(Exception, match="active claim"), store.transaction() as c:
            c.execute(
                "INSERT INTO resource_relation(child_uuid,parent_uuid,slot_id) VALUES (?,?,'')", (material, uid())
            )


def test_prepare_replay_restart_and_expiry_preserve_one_identity(setup):
    s = setup
    original = prepared(s)
    store = s["stores"]["target"]
    local = store.query_one("SELECT * FROM station_handoff_item")
    again = StationHandoffAuthority(
        store,
        station_uuid=s["ids"]["target"],
        instance_uuid=s["ids"]["target_instance"],
        event_epoch_uuid=s["ids"]["target_epoch"],
        lab_uuid=s["ids"]["lab"],
        now_ms=lambda: s["clock"][0],
    )
    s["clock"][0] += 120000
    assert again.prepare_incoming(package(s["payload"]), container_details=s["details"], plans=s["plans"]) == original
    assert again.ingress.get(local["reservation_uuid"])["state"] == "reserved"
    assert len(again.pending_intents()) == 1
    with pytest.raises(HandoffInventoryError, match="纪元"):
        StationHandoffAuthority(
            store,
            station_uuid=s["ids"]["target"],
            instance_uuid=s["ids"]["target_instance"],
            event_epoch_uuid=uid(),
            lab_uuid=s["ids"]["lab"],
        )


def test_bad_scan_and_conflicting_receipt_preserve_original(setup):
    s = setup
    frozen(s)
    with pytest.raises(HandoffInventoryError, match="扫描"):
        s["auth"]["target"].receive(
            s["ids"]["handoff"], s["ids"]["item"], barcode_namespace="lab", barcode="WRONG", evidence_refs=["scan"]
        )
    receipt = receive(s)
    with pytest.raises(HandoffInventoryError, match="证据不同"):
        s["auth"]["target"].receive(
            s["ids"]["handoff"], s["ids"]["item"], barcode_namespace="lab", barcode="BKR-1", evidence_refs=["different"]
        )
    assert receive(s) == receipt


def test_receipt_and_outbox_rollback_together(setup, monkeypatch):
    s = setup
    frozen(s)
    original = InventoryStore.tx_append_inventory_event

    def fail(*args, **kwargs):
        if kwargs["event_type"] == "station_handoff.received":
            raise RuntimeError("fault injection")
        return original(*args, **kwargs)

    monkeypatch.setattr(InventoryStore, "tx_append_inventory_event", fail)
    with pytest.raises(RuntimeError, match="fault"):
        receive(s)
    assert s["stores"]["target"].query_one("SELECT * FROM station_handoff_item")["receipt_json"] is None
    assert (
        s["stores"]["target"].query_one(
            "SELECT occupied_material_uuid FROM site WHERE uuid=?", (s["sites"]["target"],)
        )["occupied_material_uuid"]
        is None
    )
    assert len(s["auth"]["target"].pending_intents()) == 1


def test_cancel_before_permit_releases_source_and_entry_but_never_incoming_material(setup):
    s = setup
    frozen(s)
    s["payload"].update(status="canceling", revision=4)
    for auth in s["auth"].values():
        auth.apply_center_snapshot(package(s["payload"]))
    assert s["stores"]["source"].get_instance(s["carrier"]["uuid"])["status"] == "warehouse"
    item = s["stores"]["target"].query_one("SELECT * FROM station_handoff_item")
    assert s["auth"]["target"].ingress.get(item["reservation_uuid"])["state"] == "canceled"
    assert s["stores"]["target"].get_instance(item["carrier_material_uuid"])["status"] == "incoming_canceled"
    assert s["auth"]["target"].pending_intents()[-1]["payload"]["protection_released"] is True


@pytest.mark.parametrize("source_frozen", [False, True])
def test_expired_prepared_cancel_releases_durable_protection_and_reuses_entry(setup, source_frozen):
    s = setup
    if source_frozen:
        frozen(s)
    else:
        prepared(s)
    target = s["auth"]["target"]
    item = s["stores"]["target"].query_one("SELECT * FROM station_handoff_item")
    s["clock"][0] += 61_000
    assert target.ingress.expire_due() == 0
    assert target.ingress.get(item["reservation_uuid"])["state"] == "reserved"
    s["payload"].update(revision=4, attention=True, attention_reasons=["HANDOFF_RESERVATION_EXPIRED"])
    for authority in s["auth"].values():
        authority.apply_center_snapshot(package(s["payload"]))
    assert target.ingress.get(item["reservation_uuid"])["state"] == "reserved"
    # 中心明确取消后仅清除预留过期；恢复工站仍需事务撤销保护并留下证明。
    s["payload"].update(status="canceling", revision=5, attention=False, attention_reasons=[])
    for side in ("source", "target"):
        prior = s["stores"][side]
        prior.close()
        s["stores"][side] = s.get("open_inventory", InventoryStore)(prior.path)
        s["auth"][side] = authority = StationHandoffAuthority(
            s["stores"][side], station_uuid=s["ids"][side], instance_uuid=s["ids"][side + "_instance"],
            event_epoch_uuid=s["ids"][side + "_epoch"], lab_uuid=s["ids"]["lab"],
            now_ms=lambda: s["clock"][0],
        )
        authority.apply_center_snapshot(package(s["payload"]))
        authority.apply_center_snapshot(package(s["payload"]))
        assert sum(i["payload"]["kind"] == "cancel_ack" for i in authority.pending_intents()) == 1
    target = s["auth"]["target"]
    assert s["stores"]["source"].get_instance(s["carrier"]["uuid"])["status"] == "warehouse"
    assert target.ingress.get(item["reservation_uuid"])["state"] == "canceled"
    assert s["stores"]["target"].get_instance(item["carrier_material_uuid"])["status"] == "incoming_canceled"
    carrier = BackendResourceService(s["stores"]["target"]).create_material({
        "resource_template_uuid": s["templates"]["target"]["test.carrier"], "name": "next carrier",
    })
    reused = target.ingress.reserve(
        idempotency_key="after-expired-handoff-cancel", carrier_material_uuid=carrier["uuid"],
        candidate_site_uuids=[s["sites"]["target"]], ttl_seconds=60,
    )
    assert reused["site_uuid"] == s["sites"]["target"]
    assert reused["state"] == "reserved"


def test_cancel_with_movement_review_keeps_protection_even_without_attention(setup):
    s = setup
    frozen(s)
    s["payload"].update(status="canceling", revision=4)
    s["payload"]["items"][0]["movement_review"] = {
        "review_revision": 4, "previous_review_revision": None,
        "previous_settlement": {
            "decision": "source_present", "actual_holder_station_uuid": s["ids"]["source"],
            "proof_digest": digest({"previous": "source-proof"}), "evidence_refs": ["scan:previous"],
            "reason": "历史在场核实", "reconciled_at_ms": s["clock"][0], "handoff_revision": 3,
        },
        "trigger_event_digest": None, "cause": "HANDOFF_TRANSPORT_DURING_CANCEL",
        "source_verification": None, "target_verification": None,
    }
    for authority in s["auth"].values():
        authority.apply_center_snapshot(package(s["payload"]))
        assert all(i["payload"]["kind"] != "cancel_ack" for i in authority.pending_intents())
    assert s["stores"]["source"].get_instance(s["carrier"]["uuid"])["status"] == "handoff_outgoing"
    item = s["stores"]["target"].query_one("SELECT * FROM station_handoff_item")
    assert s["auth"]["target"].ingress.get(item["reservation_uuid"])["state"] == "reserved"


def test_expired_cancel_snapshot_cannot_erase_local_receipt(setup):
    s = setup
    frozen(s)
    receive(s)
    s["payload"].update(status="canceling", revision=4)
    target = s["auth"]["target"]
    target.apply_center_snapshot(package(s["payload"]))
    item = s["stores"]["target"].query_one("SELECT * FROM station_handoff_item")
    assert item["receipt_json"] is not None
    assert s["stores"]["target"].get_instance(item["carrier_material_uuid"])["status"] == "handoff_received"
    assert all(i["payload"]["kind"] != "cancel_ack" for i in target.pending_intents())


def test_cancel_after_permit_and_attention_never_release(setup):
    s = setup
    frozen(s)
    s["payload"].update(
        status="canceling",
        revision=4,
        permit={"authorization_uuid": uid(), "logistics_subject_uuid": uid()},
        attention=True,
        attention_reasons=["cancel_after_permit"],
    )
    for auth in s["auth"].values():
        auth.apply_center_snapshot(package(s["payload"]))
    assert s["stores"]["source"].get_instance(s["carrier"]["uuid"])["status"] == "handoff_outgoing"
    receipt = receive(s)
    s["payload"]["items"][0]["receipt"] = receipt
    s["payload"].update(revision=5)
    for auth in s["auth"].values():
        auth.apply_center_snapshot(package(s["payload"]))
    assert (
        s["stores"]["target"].query_one("SELECT inventory_status FROM material WHERE barcode='BKR-1'")[
            "inventory_status"
        ]
        == "handoff_received"
    )


def test_cancel_before_permit_with_attention_preserves_all_protection(setup):
    s = setup
    frozen(s)
    s["payload"].update(
        status="canceling", revision=4, attention=True, attention_reasons=["conflicting_release_evidence"]
    )
    assert s["payload"]["permit"] is None
    for authority in s["auth"].values():
        authority.apply_center_snapshot(package(s["payload"]))
        assert all(intent["payload"]["kind"] != "cancel_ack" for intent in authority.pending_intents())
    assert s["stores"]["source"].get_instance(s["carrier"]["uuid"])["status"] == "handoff_outgoing"
    target_item = s["stores"]["target"].query_one("SELECT * FROM station_handoff_item")
    assert s["auth"]["target"].ingress.get(target_item["reservation_uuid"])["state"] == "reserved"
    for store in s["stores"].values():
        assert store.query_one("SELECT COUNT(*) AS inactive FROM station_handoff_resource WHERE active=0")["inactive"] == 0


def test_old_ingress_api_cannot_release_or_receive_handoff_protection(setup):
    s = setup
    prepared(s)
    item = s["stores"]["target"].query_one("SELECT * FROM station_handoff_item")
    with pytest.raises(IngressReservationError, match="交接"):
        s["auth"]["target"].ingress.cancel(item["reservation_uuid"], reason="bypass")
    with pytest.raises(IngressReservationError, match="交接"):
        s["auth"]["target"].ingress.receive(item["reservation_uuid"])


def test_source_version_conflict_rolls_back_entire_freeze(setup):
    s = setup
    prepared(s)
    s["payload"]["items"][0]["source_material_refs"][0]["local_revision"] += 1
    with pytest.raises(HandoffInventoryError, match="版本"):
        s["auth"]["source"].freeze_source(package(s["payload"]), physical_ready=True, evidence_refs=["operator"])
    assert s["stores"]["source"].get_instance(s["carrier"]["uuid"])["status"] == "warehouse"
    assert not s["auth"]["source"].pending_intents()
    assert s["stores"]["source"].query_one("SELECT * FROM station_handoff") is None


def test_failed_target_plan_rolls_back_material_mapping_and_reservation(setup):
    s = setup
    s["plans"][s["ids"]["item"]]["candidate_site_uuids"] = [uid()]
    with pytest.raises(IngressReservationError):
        prepared(s)
    assert s["stores"]["target"].query_one("SELECT * FROM material WHERE barcode='BKR-1'") is None
    assert s["stores"]["target"].query_one("SELECT * FROM container_station_binding") is None
    assert s["stores"]["target"].query_one("SELECT * FROM station_ingress_reservation") is None


def test_boundary_envelope_and_exact_ack_survive_cross_database_crash(setup, monkeypatch):
    from unilabos.app.center_client.durable import DurableCenterStore
    from unilabos.app.center_client.handoff import HandoffBridge

    s = setup
    durable = DurableCenterStore(
        s["path"] / "center.db", station_uuid=s["ids"]["target"], instance_uuid=s["ids"]["target_instance"]
    )
    # 初始化尚无事件的新账本时固定测试实例的纪元；生产中顺序相反，由durable构造authority。
    with durable._db.transaction():
        durable._db.execute("UPDATE center_runtime_state SET event_epoch_uuid=?", (s["ids"]["target_epoch"],))
    durable.event_epoch_uuid = s["ids"]["target_epoch"]
    prepared(s)
    calls = []

    class Client:
        def report_handoff(self, event):
            calls.append(copy.deepcopy(event))
            return {
                "handoff": s["payload"],
                "ack": {
                    **{
                        field: event[field]
                        for field in ("event_uuid", "event_epoch_uuid", "event_sequence", "event_digest")
                    },
                    "application": "applied",
                    "last_contiguous_event_sequence": event["event_sequence"],
                },
            }

    bridge = HandoffBridge(Client(), durable, s["auth"]["target"])
    original = bridge.authority.apply_center_snapshot
    try:

        def fail(package):
            raise RuntimeError("crash after durable ACK")

        monkeypatch.setattr(bridge.authority, "apply_center_snapshot", fail)
        with pytest.raises(RuntimeError, match="crash"):
            bridge.pump()
        assert len(calls) == 1
        assert len(bridge.authority.pending_intents()) == 1
        monkeypatch.setattr(bridge.authority, "apply_center_snapshot", original)
        bridge.pump()
        assert len(calls) == 1
        assert not bridge.authority.pending_intents()
        event = calls[0]
        changed = {**event, "event_sequence": event["event_sequence"] + 1}
        changed["event_digest"] = digest({key: value for key, value in changed.items() if key != "event_digest"})
        with pytest.raises(HandoffInventoryError, match="信封"):
            bridge.authority.remember_envelope(event["event_uuid"], changed)
    finally:
        durable.close()


def test_target_notification_without_local_plan_stays_pending_and_never_creates_stock(setup):
    from types import SimpleNamespace

    from unilabos.app.center_client.handoff import HandoffBridge

    s = setup
    store = SimpleNamespace(
        station_uuid=s["ids"]["target"],
        instance_uuid=s["ids"]["target_instance"],
        event_epoch_uuid=s["ids"]["target_epoch"],
    )

    class Client:
        def handoff_package(self, *args):
            return package(s["payload"])

    bridge = HandoffBridge(Client(), store, s["auth"]["target"])
    identity = s["ids"]["handoff"]
    command = {
        "command": {
            "type": "handoff.available",
            "payload": {
                "handoff_uuid": identity,
                "handoff_revision": 1,
                "handoff_package_path": f"/api/v1/handoffs/{identity}/packages/1",
                "payload_digest": digest(s["payload"]),
            },
        }
    }
    assert bridge.handle_command(command) is False
    assert s["stores"]["target"].query_one("SELECT * FROM station_handoff") is None


def test_http_never_accepts_forged_center_package_or_fake_physical_ready(setup):
    from types import SimpleNamespace

    from fastapi import FastAPI, Header, HTTPException
    from fastapi.testclient import TestClient

    from unilabos.app.center_client.handoff import HandoffBridge
    from unilabos.app.scheduler.inventory.handoff_api import create_handoff_router

    s = setup
    prepared(s)
    store = SimpleNamespace(
        station_uuid=s["ids"]["source"],
        instance_uuid=s["ids"]["source_instance"],
        event_epoch_uuid=s["ids"]["source_epoch"],
    )

    class Client:
        def handoff_package(self, *args):
            return package(s["payload"])

    bridge = HandoffBridge(Client(), store, s["auth"]["source"])

    def require_operator(authorization: str | None = Header(default=None)):
        if authorization != "Bearer test-operator":
            raise HTTPException(401, "需要本站操作者身份")

    app = FastAPI()
    with pytest.raises(ValueError, match="鉴权"):
        create_handoff_router(bridge, operator_dependency=None)
    app.include_router(
        create_handoff_router(bridge, operator_dependency=require_operator), prefix="/api/v1/inventory"
    )
    client = TestClient(app)
    path = f"/api/v1/inventory/center-handoffs/{s['ids']['handoff']}/release"
    assert client.post(path, json={"physical_ready": True, "evidence_refs": ["operator"]}).status_code == 401
    assert not s["auth"]["source"].pending_intents()
    for action, body in [
        ("verify-source-presence", {"barcode_namespace": "lab", "barcode": "BKR-1", "physical_present": True, "evidence_refs": ["operator"]}),
        ("verify-source-departure", {"physical_absent": True, "evidence_refs": ["operator"]}),
    ]:
        verify_path = f"/api/v1/inventory/center-handoffs/{s['ids']['handoff']}/items/{s['ids']['item']}/{action}"
        assert client.post(verify_path, json=body).status_code == 401
        assert client.post(verify_path, json={**body, "package": package(s["payload"])},
                           headers={"authorization": "Bearer test-operator"}).status_code == 422
    client.headers["authorization"] = "Bearer test-operator"
    assert (
        client.post(
            path, json={"physical_ready": True, "evidence_refs": ["operator"], "package": package(s["payload"])}
        ).status_code
        == 422
    )
    assert client.post(path, json={"physical_ready": "true", "evidence_refs": ["operator"]}).status_code == 422
    assert client.post(path, json={"physical_ready": False, "evidence_refs": ["operator"]}).status_code == 409
    response = client.post(path, json={"physical_ready": True, "evidence_refs": ["operator"]})
    assert response.status_code == 200
    assert response.json()["payload"]["kind"] == "released"


def test_stale_center_package_cannot_undo_later_attention(setup):
    s = setup
    frozen(s)
    receipt = receive(s)
    s["payload"]["items"][0]["receipt"] = receipt
    s["payload"].update(revision=4, status="received")
    settled = package(s["payload"])
    s["auth"]["target"].apply_center_snapshot(settled)
    s["payload"].update(revision=5, attention=True, attention_reasons=["conflicting_late_evidence"])
    s["auth"]["target"].apply_center_snapshot(package(s["payload"]))
    s["auth"]["target"].apply_center_snapshot(settled)
    assert (
        s["stores"]["target"].query_one("SELECT inventory_status FROM material WHERE barcode='BKR-1'")[
            "inventory_status"
        ]
        == "handoff_quarantined"
    )


def test_source_cannot_silently_drop_unmapped_child_material(setup):
    s = setup
    child = BackendResourceService(s["stores"]["source"]).create_material(
        {
            "resource_template_uuid": s["templates"]["source"]["test.carrier"],
            "name": "unmapped-child",
            "parent_uuid": s["carrier"]["uuid"],
        }
    )
    prepared(s)
    with pytest.raises(HandoffInventoryError, match="未登记谱系"):
        s["auth"]["source"].freeze_source(package(s["payload"]), physical_ready=True, evidence_refs=["operator"])
    assert s["stores"]["source"].get_instance(child["uuid"])["status"] == "warehouse"
    assert s["stores"]["source"].query_one("SELECT * FROM station_handoff") is None


def test_explicit_runtime_assembly_binds_shared_epoch_and_rejects_reconfiguration(tmp_path):
    from dataclasses import replace
    from types import SimpleNamespace

    from tests.app.center_client.test_center_client import config
    from unilabos.app.center_client.client import CenterClient
    from unilabos.app.center_client.durable import DurableCenterStore
    from unilabos.app.center_client.handoff import configure_handoffs
    from unilabos.app.center_client.runtime import CenterRuntime

    durable = DurableCenterStore(tmp_path / "workflow.db", station_uuid=uid(), instance_uuid=uid())
    inventory = InventoryStore(str(tmp_path / "inventory.db"))
    from unilabos.workflow.service import WorkflowService
    from unilabos.workflow.store import WorkflowStore

    service = WorkflowService(WorkflowStore(tmp_path / "workflow.db"))
    cfg = replace(config(tmp_path), station_uuid=durable.station_uuid, instance_uuid=durable.instance_uuid)
    runtime = CenterRuntime(cfg, CenterClient(cfg), durable, service)
    try:
        bridge = configure_handoffs(runtime, inventory, lab_uuid=uid())
        assert runtime.handoffs is bridge
        assert bridge.authority.event_epoch_uuid == durable.event_epoch_uuid
        assert bridge.authority.station_uuid == durable.station_uuid
        assert service._center_material_input_authority.store is inventory
        with pytest.raises(RuntimeError, match="配置一次"):
            configure_handoffs(runtime, inventory, lab_uuid=uid())
        runtime._thread = SimpleNamespace()
        with pytest.raises(RuntimeError, match="启动前"):
            configure_handoffs(runtime, inventory, lab_uuid=uid())
    finally:
        durable.close()
        inventory.close()
        service.close()


def test_source_with_sample_contents_requires_explicit_adapter(setup):
    from unilabos.app.scheduler.inventory.content_contract import BackendContainerContentService

    s = setup
    BackendContainerContentService(s["stores"]["source"]).create_sample(
        {"material_uuid": s["carrier"]["uuid"], "code": "sample", "name": "样品", "quantity": 5, "quantity_unit": "mL"}
    )
    current = s["stores"]["source"].query_one("SELECT aggregate_version FROM material WHERE uuid=?", (s["carrier"]["uuid"],))
    s["payload"]["items"][0]["source_material_refs"][0]["local_revision"] = current["aggregate_version"]
    prepared(s)
    with pytest.raises(HandoffInventoryError, match="内容适配"):
        s["auth"]["source"].freeze_source(package(s["payload"]), physical_ready=True, evidence_refs=["operator"])
    assert s["stores"]["source"].query_one("SELECT * FROM station_handoff") is None


@pytest.mark.parametrize("side", ["source", "target"])
def test_cancel_unseen_handoff_persists_ack_and_rejects_late_prepare(setup, side):
    from types import SimpleNamespace

    from unilabos.app.center_client.handoff import HandoffBridge

    s = setup
    old = copy.deepcopy(s["payload"])
    s["payload"].update(status="canceling", revision=2)
    store = SimpleNamespace(
        station_uuid=s["ids"][side], instance_uuid=s["ids"][side + "_instance"],
        event_epoch_uuid=s["ids"][side + "_epoch"],
    )

    class Client:
        def handoff_package(self, *args):
            return package(s["payload"])

    authority = s["auth"][side]
    bridge = HandoffBridge(Client(), store, authority)
    identity = s["ids"]["handoff"]
    notification = {"command": {"type": "handoff.changed", "payload": {
        "handoff_uuid": identity, "handoff_revision": 2,
        "handoff_package_path": f"/api/v1/handoffs/{identity}/packages/2",
        "payload_digest": digest(s["payload"]),
    }}}
    assert bridge.handle_command(notification) is True
    intents = authority.pending_intents()
    assert len(intents) == 1
    assert intents[0]["payload"]["kind"] == "cancel_ack"
    assert intents[0]["payload"]["protection_released"] is True
    assert bridge.handle_command(notification) is True
    authority.apply_center_snapshot(package(old))
    assert authority.pending_intents() == intents
    assert s["stores"][side].query_one("SELECT * FROM station_handoff_item") is None
    with pytest.raises(HandoffInventoryError, match="取消"):
        if side == "target":
            authority.prepare_incoming(package(old), container_details=s["details"], plans=s["plans"])
        else:
            old.update(status="prepared", revision=2, prepared={
                "event_epoch_uuid": s["ids"]["target_epoch"], "protection_uuid": uid(),
                "reservation_until_ms": s["clock"][0] + 60000, "evidence_refs": ["entry"],
            })
            authority.freeze_source(package(old), physical_ready=True, evidence_refs=["operator"])
    assert authority.pending_intents() == intents


def test_unknown_unsafe_cancel_stays_pending_without_false_release(setup):
    from types import SimpleNamespace

    from unilabos.app.center_client.handoff import HandoffBridge

    s = setup
    s["payload"].update(status="canceling", revision=2)
    for attention in (True, False):
        s["payload"].update(
            attention=attention,
            attention_reasons=["conflict"] if attention else [],
            permit=None if attention else {"authorization_uuid": uid(), "logistics_subject_uuid": uid()},
        )
        for side in ("source", "target"):
            authority = s["auth"][side]
            store = SimpleNamespace(
                station_uuid=s["ids"][side], instance_uuid=s["ids"][side + "_instance"],
                event_epoch_uuid=s["ids"][side + "_epoch"],
            )
            client = SimpleNamespace(handoff_package=lambda *args: package(s["payload"]))
            bridge = HandoffBridge(client, store, authority)
            identity = s["ids"]["handoff"]
            assert bridge.handle_command({"command": {"type": "handoff.changed", "payload": {
                "handoff_uuid": identity, "handoff_revision": 2,
                "handoff_package_path": f"/api/v1/handoffs/{identity}/packages/2",
                "payload_digest": digest(s["payload"]),
            }}}) is False
            assert not authority.has_handoff(identity)
            assert authority.pending_intents() == []


def test_conflicting_cancel_ack_requarantines_released_source_even_after_crash(setup, monkeypatch):
    from unilabos.app.center_client.durable import DurableCenterStore
    from unilabos.app.center_client.handoff import HandoffBridge

    s = setup
    frozen(s)
    released_package = copy.deepcopy(s["payload"])
    s["payload"].update(status="canceling", revision=4)
    source = s["auth"]["source"]
    source.apply_center_snapshot(package(s["payload"]))
    assert s["stores"]["source"].get_instance(s["carrier"]["uuid"])["status"] == "warehouse"
    # 目标还未看到取消，实物已经到货。中心因此保留实际 receipt 并要求核实。
    receipt = receive(s)
    s["payload"]["items"][0]["receipt"] = receipt
    s["payload"]["items"][0]["current_state_version"] = 3
    s["payload"].update(revision=5, attention=True, attention_reasons=["received_during_cancel"])
    durable = DurableCenterStore(
        s["path"] / "cancel-center.db", station_uuid=s["ids"]["source"], instance_uuid=s["ids"]["source_instance"]
    )
    with durable._db.transaction():
        durable._db.execute("UPDATE center_runtime_state SET event_epoch_uuid=?", (s["ids"]["source_epoch"],))
    durable.event_epoch_uuid = s["ids"]["source_epoch"]
    calls = []

    class Client:
        def report_handoff(self, event):
            calls.append(copy.deepcopy(event))
            conflict = event["payload"]["kind"] == "cancel_ack"
            return {"handoff": copy.deepcopy(s["payload"] if conflict else released_package), "ack": {
                **{key: event[key] for key in ("event_uuid", "event_epoch_uuid", "event_sequence", "event_digest")},
                "application": "conflict" if conflict else "applied",
                "last_contiguous_event_sequence": event["event_sequence"],
            }}

    bridge = HandoffBridge(Client(), durable, source)
    original = source.quarantine_center_snapshot

    def crash(_package):
        raise RuntimeError("crash after conflict ACK was persisted")

    try:
        monkeypatch.setattr(source, "quarantine_center_snapshot", crash)
        with pytest.raises(RuntimeError, match="crash"):
            bridge.pump()
        assert len(calls) == 2
        assert len(source.pending_intents()) == 1
        monkeypatch.setattr(source, "quarantine_center_snapshot", original)
        bridge.pump()
        assert len(calls) == 2  # 恢复只重放持久 ACK，不重新生成取消或收货事实。
        assert source.pending_intents() == []
        assert s["stores"]["source"].get_instance(s["carrier"]["uuid"])["status"] == "handoff_quarantined"
        assert s["stores"]["source"].query_one(
            "SELECT COUNT(*) AS inactive FROM station_handoff_resource WHERE active=0"
        )["inactive"] == 0
        with pytest.raises(InventoryMutationConflict), s["stores"]["source"].transaction() as c:
            assert_inventory_mutation_unclaimed(c, material_uuids=[s["carrier"]["uuid"]])
    finally:
        durable.close()


def test_multilineage_carrier_can_roundtrip_without_historical_children(setup):
    _multilineage_roundtrip(setup)


def test_canceled_incoming_children_do_not_block_retry_and_return(setup):
    _multilineage_roundtrip(setup, cancel_first=True)


def _multilineage_roundtrip(s, *, cancel_first=False):
    child = BackendResourceService(s["stores"]["source"]).create_material({
        "resource_template_uuid": s["templates"]["source"]["test.carrier"],
        "name": "inner-carrier", "parent_uuid": s["carrier"]["uuid"],
    })
    child_lineage = uid()
    refs = copy.deepcopy(s["payload"]["items"][0]["source_material_refs"])
    refs.append({
        "station_uuid": s["ids"]["source"], "local_material_uuid": child["uuid"],
        "local_revision": child["revision"], "material_lineage_uuid": child_lineage,
    })
    state_version = 1
    legs = (
        (("source", "target"), ("source", "target"), ("target", "source")) if cancel_first
        else (("source", "target"), ("target", "source"), ("source", "target"))
    )
    canceled_carrier = None
    for leg, (source_side, target_side) in enumerate(legs):
        payload = copy.deepcopy(s["payload"])
        item_uuid = uid()
        payload.update(
            handoff_uuid=uid(), source_station_uuid=s["ids"][source_side], target_station_uuid=s["ids"][target_side],
            source_instance_uuid=s["ids"][source_side + "_instance"],
            target_instance_uuid=s["ids"][target_side + "_instance"],
            items=[{
                "handoff_item_uuid": item_uuid, "container_uuid": s["ids"]["container"],
                "expected_state_version": state_version, "reserved_state_version": state_version + 1,
                "current_state_version": state_version + 1, "source_material_refs": refs,
                "source_snapshot": {"name": "two-lineage-carrier"}, "receipt": None,
            }],
        )
        details = copy.deepcopy(s["details"])
        details[s["ids"]["container"]].update(
            current_station_uuid=s["ids"][source_side], active_handoff_item_uuid=item_uuid,
            state_version=state_version + 1,
        )
        template = s["templates"][target_side]["test.carrier"]
        plans = {item_uuid: {
            "carrier_template_uuid": template, "carrier_lineage_uuid": s["ids"]["lineage"],
            "lineage_template_uuids": {s["ids"]["lineage"]: template, child_lineage: template},
            "candidate_site_uuids": [s["sites"][target_side]], "ttl_seconds": 60,
        }}
        source, target = s["auth"][source_side], s["auth"][target_side]
        ready = target.prepare_incoming(package(payload), container_details=details, plans=plans)
        payload.update(status="prepared", revision=2, prepared={
            "event_epoch_uuid": s["ids"][target_side + "_epoch"],
            **{key: ready["payload"][key] for key in ("protection_uuid", "reservation_until_ms", "evidence_refs")},
        })
        released = source.freeze_source(package(payload), physical_ready=True, evidence_refs=["operator"])
        payload.update(status="released", revision=3, released={
            "event_epoch_uuid": s["ids"][source_side + "_epoch"],
            **{key: released["payload"][key] for key in ("release_uuid", "evidence_refs")},
        })
        for authority in (source, target):
            authority.apply_center_snapshot(package(payload))
        if cancel_first and leg == 0:
            payload.update(status="canceling", revision=4)
            for authority in (source, target):
                authority.apply_center_snapshot(package(payload))
            target_item = s["stores"][target_side].query_one(
                "SELECT * FROM station_handoff_item WHERE handoff_item_uuid=?", (item_uuid,)
            )
            canceled_carrier = target_item["carrier_material_uuid"]
            canceled = s["stores"][target_side].query_all("SELECT * FROM material WHERE inventory_status='incoming_canceled'")
            assert len(canceled) == 2
            assert all(material["parent_uuid"] is None for material in canceled)
            # 源物料尚未离站，取消不改变其当前父子结构或可用状态。
            source_child = s["stores"][source_side].query_one("SELECT * FROM material WHERE uuid=?", (child["uuid"],))
            assert source_child["parent_uuid"] == s["carrier"]["uuid"]
            assert source_child["inventory_status"] == "warehouse"
            assert s["stores"][target_side].query_one(
                "SELECT COUNT(*) AS count FROM material_station_binding WHERE handoff_item_uuid=?", (item_uuid,)
            )["count"] == 2
            assert any(event["payload"]["kind"] == "cancel_ack" for event in target.pending_intents())
            state_version += 2
            continue
        receipt = target.receive(
            payload["handoff_uuid"], item_uuid, barcode_namespace="lab", barcode="BKR-1", evidence_refs=["scan"]
        )
        payload["items"][0].update(receipt=receipt, current_state_version=state_version + 2)
        payload.update(status="received", revision=4)
        for authority in (source, target):
            authority.apply_center_snapshot(package(payload))
        for ref in refs:
            departed = s["stores"][source_side].query_one("SELECT * FROM material WHERE uuid=?", (ref["local_material_uuid"],))
            assert departed["parent_uuid"] is None
            assert departed["inventory_status"] == "handoff_departed"
        refs = receipt["target_material_refs"]
        carrier = next(ref["local_material_uuid"] for ref in refs if ref["material_lineage_uuid"] == s["ids"]["lineage"])
        if cancel_first and leg == 1:
            assert carrier == canceled_carrier
        children = s["stores"][target_side].query_all("SELECT uuid,inventory_status FROM material WHERE parent_uuid=?", (carrier,))
        assert len(children) == 1
        assert children[0]["inventory_status"] == "warehouse"
        assert children[0]["uuid"] in {ref["local_material_uuid"] for ref in refs}
        state_version += 2


def mark_exception(s):
    s["payload"].update(status="canceling", revision=4, attention=True,
                        attention_reasons=["HANDOFF_CANCEL_REQUIRES_RECONCILIATION"],
                        permit={"authorization_uuid": uid(), "logistics_subject_uuid": uid()})
    for authority in s["auth"].values():
        authority.apply_center_snapshot(package(s["payload"]))


def source_proof(s):
    return s["auth"]["source"].verify_source_presence(
        package(s["payload"]), handoff_item_uuid=s["ids"]["item"],
        barcode_namespace="lab", barcode="BKR-1", physical_present=True,
        evidence_refs=["scan:source-return", "operator:source-present"],
    )


def settlement_payload(s, *, decision, proof, attention=False):
    payload = copy.deepcopy(s["payload"])
    payload["revision"] += 1
    payload["attention"] = attention
    payload["status"] = "canceled" if decision == "source_present" else "received"
    item = payload["items"][0]
    item["current_state_version"] += 1
    item["settlement"] = {
        "decision": decision,
        "actual_holder_station_uuid": s["ids"]["source" if decision == "source_present" else "target"],
        "proof_digest": proof,
        "evidence_refs": ["operator:reviewed"], "reason": "逐件核实实际持有者",
        "reconciled_at_ms": s["clock"][0], "handoff_revision": payload["revision"],
    }
    return payload


def test_verified_source_return_releases_only_original_inventory_and_cancels_incoming(setup):
    s = setup
    frozen(s)
    mark_exception(s)
    intent = source_proof(s)
    assert source_proof(s) == intent
    assert s["stores"]["source"].get_instance(s["carrier"]["uuid"])["status"] == "handoff_outgoing"
    proof = intent["payload"]["items"][0]
    s["payload"]["items"][0]["source_verification"] = proof
    final = settlement_payload(s, decision="source_present", proof=proof["verification_digest"])
    original_ids = {side: {r["uuid"] for r in store.query_all("SELECT uuid FROM material")} for side, store in s["stores"].items()}
    for authority in s["auth"].values():
        authority.apply_center_snapshot(package(final))
        authority.apply_center_snapshot(package(final))
    assert s["stores"]["source"].get_instance(s["carrier"]["uuid"])["status"] == "warehouse"
    assert s["stores"]["target"].query_one("SELECT inventory_status FROM material WHERE barcode='BKR-1'")["inventory_status"] == "incoming_canceled"
    for side, store in s["stores"].items():
        assert {r["uuid"] for r in store.query_all("SELECT uuid FROM material")} == original_ids[side]
    # 比核实更旧的取消/超时包不会重新改变已释放库存。
    s["auth"]["source"].apply_center_snapshot(package(s["payload"]))
    assert s["stores"]["source"].get_instance(s["carrier"]["uuid"])["status"] == "warehouse"


def test_source_settlement_requires_exact_local_proof_and_never_erases_target_receipt(setup):
    s = setup
    frozen(s)
    mark_exception(s)
    proof = source_proof(s)["payload"]["items"][0]
    s["payload"]["items"][0]["source_verification"] = proof
    final = settlement_payload(s, decision="source_present", proof=proof["verification_digest"])
    changed = copy.deepcopy(final)
    other = changed["items"][0]["source_verification"]
    other["verification_uuid"] = uid()
    other["verification_digest"] = digest({k: v for k, v in other.items() if k != "verification_digest"})
    changed["items"][0]["settlement"]["proof_digest"] = other["verification_digest"]
    with pytest.raises(HandoffInventoryError, match="原复核证明"):
        s["auth"]["source"].apply_center_snapshot(package(changed))
    receive(s)
    with pytest.raises(HandoffInventoryError, match="本站已收货"):
        s["auth"]["target"].apply_center_snapshot(package(final))
    assert s["stores"]["target"].query_one("SELECT inventory_status FROM material WHERE barcode='BKR-1'")["inventory_status"] == "handoff_received"


def test_verified_late_receipt_settles_exact_target_identity_even_other_items_need_attention(setup):
    s = setup
    frozen(s)
    mark_exception(s)
    receipt = receive(s)
    s["payload"]["items"][0]["receipt"] = receipt
    # global attention 保持；本件明确结算仍可释放，生产批量由中心保留其他件的隔离。
    final = settlement_payload(s, decision="target_received", proof=receipt["receipt_digest"], attention=True)
    for authority in s["auth"].values():
        authority.apply_center_snapshot(package(final))
        authority.apply_center_snapshot(package(final))
    target_id = receipt["target_material_refs"][0]["local_material_uuid"]
    assert s["stores"]["target"].get_instance(target_id)["status"] == "warehouse"
    assert s["stores"]["source"].get_instance(s["carrier"]["uuid"])["status"] == "handoff_departed"
    assert receive(s) == receipt


def test_source_verification_rejects_no_scan_and_conflicting_replay(setup):
    s = setup
    frozen(s)
    with pytest.raises(HandoffInventoryError, match="异常原明细"):
        source_proof(s)
    mark_exception(s)
    with pytest.raises(HandoffInventoryError, match="条码"):
        s["auth"]["source"].verify_source_presence(
            package(s["payload"]), handoff_item_uuid=s["ids"]["item"],
            barcode_namespace="lab", barcode="OTHER", physical_present=True, evidence_refs=["scan:other"],
        )
    source_proof(s)
    with pytest.raises(HandoffInventoryError, match="不可覆盖"):
        s["auth"]["source"].verify_source_presence(
            package(s["payload"]), handoff_item_uuid=s["ids"]["item"],
            barcode_namespace="lab", barcode="BKR-1", physical_present=True, evidence_refs=["scan:changed"],
        )


def test_early_receipt_can_recover_through_source_departure_verification_without_fabricating_release(setup):
    s = setup
    prepared(s)
    receipt = receive(s)
    s["payload"].update(status="received", revision=3, attention=True,
                        attention_reasons=["HANDOFF_RECEIPT_REQUIRES_RECONCILIATION"])
    s["payload"]["items"][0]["receipt"] = receipt
    assert not s["auth"]["source"].has_handoff(s["ids"]["handoff"])
    assert s["payload"]["released"] is None
    args = dict(handoff_item_uuid=s["ids"]["item"], physical_absent=True, evidence_refs=["operator:source-location-empty"])
    event = s["auth"]["source"].verify_source_departure(package(s["payload"]), **args)
    assert event == s["auth"]["source"].verify_source_departure(package(s["payload"]), **args)
    assert event["payload"]["kind"] == "source_departure_verified"
    assert s["stores"]["source"].get_instance(s["carrier"]["uuid"])["status"] == "handoff_outgoing"
    s["payload"]["items"][0]["source_departure_verification"] = event["payload"]["items"][0]
    final = settlement_payload(s, decision="target_received", proof=receipt["receipt_digest"])
    for authority in s["auth"].values():
        authority.apply_center_snapshot(package(final))
    assert s["stores"]["source"].get_instance(s["carrier"]["uuid"])["status"] == "handoff_departed"
    target = receipt["target_material_refs"][0]["local_material_uuid"]
    assert s["stores"]["target"].get_instance(target)["status"] == "warehouse"
    assert all(i["payload"]["kind"] != "released" for i in s["auth"]["source"].pending_intents())


def add_second_item(s):
    second_container, second_item, second_lineage, second_site = uid(), uid(), uid(), uid()
    second_carrier = BackendResourceService(s["stores"]["source"]).create_material({
        "resource_template_uuid": s["templates"]["source"]["test.carrier"], "name": "second", "barcode": "BKR-2",
    })
    s["auth"]["source"].bind_container(container_uuid=second_container, local_carrier_material_uuid=second_carrier["uuid"],
                                       barcode_namespace="lab", barcode="BKR-2")
    rack = s["stores"]["target"].query_one("SELECT material_uuid FROM site WHERE uuid=?", (s["sites"]["target"],))["material_uuid"]
    with s["stores"]["target"].transaction() as c:
        c.execute("INSERT INTO site(uuid,create_time,update_time,material_uuid,name,allowed_resource_template_uuids) VALUES (?,'2026-01-01','2026-01-01',?,'entry-2',?)",
                  (second_site, rack, json.dumps([s["templates"]["target"]["test.carrier"]])))
    item = copy.deepcopy(s["payload"]["items"][0])
    item.update(handoff_item_uuid=second_item, container_uuid=second_container)
    item["source_material_refs"][0].update(local_material_uuid=second_carrier["uuid"], local_revision=second_carrier["revision"], material_lineage_uuid=second_lineage)
    s["payload"]["items"].append(item)
    details = copy.deepcopy(s["details"][s["ids"]["container"]])
    details.update(container_uuid=second_container, barcode="BKR-2", active_handoff_item_uuid=second_item)
    s["details"][second_container] = details
    plan = copy.deepcopy(s["plans"][s["ids"]["item"]])
    plan.update(carrier_lineage_uuid=second_lineage, lineage_template_uuids={second_lineage: s["templates"]["target"]["test.carrier"]}, candidate_site_uuids=[second_site])
    s["plans"][second_item] = plan
    return second_container, second_item, second_carrier


def test_partial_settlement_replay_survives_inventory_change_restart_and_next_transport(setup):
    s = setup
    _, second_item, _ = add_second_item(s)
    frozen(s)
    mark_exception(s)
    first_receipt = receive(s)
    s["payload"]["items"][0]["receipt"] = first_receipt
    partial = settlement_payload(s, decision="target_received", proof=first_receipt["receipt_digest"], attention=True)
    for authority in s["auth"].values():
        authority.apply_center_snapshot(package(partial))
    first_id = first_receipt["target_material_refs"][0]["local_material_uuid"]
    updated = BackendResourceService(s["stores"]["target"]).update_material(first_id, {"name": "processed-locally"})
    assert updated["revision"] > first_receipt["target_material_refs"][0]["local_revision"]
    # 模拟接收后续新的真实运输：该站再次作为source冻结同一容器。
    onward = copy.deepcopy(partial)
    onward.update(handoff_uuid=uid(), source_station_uuid=s["ids"]["target"], target_station_uuid=s["ids"]["source"],
                  source_instance_uuid=s["ids"]["target_instance"], target_instance_uuid=s["ids"]["source_instance"],
                  revision=1, status="prepared", attention=False, attention_reasons=[], permit=None, released=None,
                  prepared={"event_epoch_uuid": s["ids"]["source_epoch"], "protection_uuid": uid(),
                            "reservation_until_ms": s["clock"][0] + 100000, "evidence_refs": ["next:protected"]})
    onward["items"] = [copy.deepcopy(partial["items"][0])]
    onward_item = onward["items"][0]
    onward_item.pop("settlement")
    onward_item.update(handoff_item_uuid=uid(), receipt=None, expected_state_version=3, reserved_state_version=4, current_state_version=4,
                       source_material_refs=[{**first_receipt["target_material_refs"][0], "local_revision": updated["revision"]}])
    s["auth"]["target"].freeze_source(package(onward), physical_ready=True, evidence_refs=["next:physical-ready"])
    second_receipt = s["auth"]["target"].receive(s["ids"]["handoff"], second_item, barcode_namespace="lab", barcode="BKR-2", evidence_refs=["scan:2"])
    final = copy.deepcopy(partial)
    final.update(revision=partial["revision"] + 1, attention=False, status="received")
    final["items"][1].update(receipt=second_receipt, current_state_version=3,
        settlement={**partial["items"][0]["settlement"], "proof_digest": second_receipt["receipt_digest"], "handoff_revision": final["revision"]})
    # 真正重开库存库，证明短路依据是事务内保存的结算记录，而不是进程缓存。
    s["stores"]["target"].close()
    s["stores"]["target"] = s.get("open_inventory", InventoryStore)(str(s["path"] / "target.db"))
    s["auth"]["target"] = StationHandoffAuthority(s["stores"]["target"], station_uuid=s["ids"]["target"],
        instance_uuid=s["ids"]["target_instance"], event_epoch_uuid=s["ids"]["target_epoch"], lab_uuid=s["ids"]["lab"], now_ms=lambda: s["clock"][0])
    for authority in s["auth"].values():
        authority.apply_center_snapshot(package(final))
        authority.apply_center_snapshot(package(final))
    assert s["stores"]["target"].get_instance(first_id)["status"] == "handoff_outgoing"
    assert s["stores"]["target"].query_one("SELECT aggregate_version FROM material WHERE uuid=?", (first_id,))["aggregate_version"] == updated["revision"]
    assert s["stores"]["target"].get_instance(second_receipt["target_material_refs"][0]["local_material_uuid"])["status"] == "warehouse"


def test_before_permit_reconciled_source_return_does_not_emit_cancel_ack(setup):
    s = setup
    frozen(s)
    s["payload"].update(revision=4, attention=True, attention_reasons=["HANDOFF_RESERVATION_EXPIRED"])
    proof = source_proof(s)["payload"]["items"][0]
    s["payload"]["items"][0]["source_verification"] = proof
    final = settlement_payload(s, decision="source_present", proof=proof["verification_digest"])
    assert final["permit"] is None
    for authority in s["auth"].values():
        authority.apply_center_snapshot(package(final))
        assert all(i["payload"]["kind"] != "cancel_ack" for i in authority.pending_intents())


def test_partial_early_receipt_and_unfrozen_source_remainder_can_both_settle(setup):
    s = setup
    _, second_item, second_carrier = add_second_item(s)
    prepared(s)
    first_receipt = receive(s)
    s["payload"].update(revision=3, status="partial_received", attention=True,
                        attention_reasons=["HANDOFF_RECEIPT_REQUIRES_RECONCILIATION"])
    s["payload"]["items"][0]["receipt"] = first_receipt
    source = s["auth"]["source"]
    departure = source.verify_source_departure(package(s["payload"]), handoff_item_uuid=s["ids"]["item"],
                                               physical_absent=True, evidence_refs=["operator:first-absent"])
    assert s["stores"]["source"].query_one("SELECT handoff_item_uuid FROM station_handoff_item WHERE handoff_item_uuid=?", (second_item,)) is None
    presence = source.verify_source_presence(package(s["payload"]), handoff_item_uuid=second_item,
        barcode_namespace="lab", barcode="BKR-2", physical_present=True, evidence_refs=["scan:second-still-here"])
    s["payload"]["items"][0]["source_departure_verification"] = departure["payload"]["items"][0]
    s["payload"]["items"][1]["source_verification"] = presence["payload"]["items"][0]
    final = settlement_payload(s, decision="target_received", proof=first_receipt["receipt_digest"])
    final["status"] = "reconciled"
    final["items"][1].update(current_state_version=3, settlement={
        **final["items"][0]["settlement"], "decision": "source_present", "actual_holder_station_uuid": s["ids"]["source"],
        "proof_digest": presence["payload"]["items"][0]["verification_digest"],
    })
    for authority in s["auth"].values():
        authority.apply_center_snapshot(package(final))
        authority.apply_center_snapshot(package(final))
    assert s["stores"]["source"].get_instance(s["carrier"]["uuid"])["status"] == "handoff_departed"
    assert s["stores"]["source"].get_instance(second_carrier["uuid"])["status"] == "warehouse"
    assert s["stores"]["target"].query_one("SELECT inventory_status FROM material WHERE barcode='BKR-2'")["inventory_status"] == "incoming_canceled"
    assert final["released"] is None
    assert all(i["payload"]["kind"] != "released" for i in source.pending_intents())


def test_unprepared_target_persists_source_settlement_tombstone(setup):
    s = setup
    original = package(copy.deepcopy(s['payload']))
    s['payload'].update(revision=2, attention=True, attention_reasons=['HANDOFF_RESERVATION_EXPIRED'])
    proof = source_proof(s)['payload']['items'][0]
    s['payload']['items'][0]['source_verification'] = proof
    final = settlement_payload(s, decision='source_present', proof=proof['verification_digest'])
    target = s['auth']['target']
    target.quarantine_center_snapshot(package(final))
    assert not target.has_handoff(s['ids']['handoff'])
    target.apply_center_snapshot(package(final))
    assert target.has_handoff(s['ids']['handoff'])
    assert len(target.pending_intents()) == 1
    assert target.pending_intents()[0]["payload"]["kind"] == "snapshot_applied"
    assert target.pending_intents()[0]["payload"]["applied"] is True
    s['stores']['target'].close()
    store = s.get('open_inventory', InventoryStore)(str(s['path'] / 'target.db'))
    s['stores']['target'] = store
    target = StationHandoffAuthority(store, station_uuid=s['ids']['target'],
        instance_uuid=s['ids']['target_instance'], event_epoch_uuid=s['ids']['target_epoch'],
        lab_uuid=s['ids']['lab'], now_ms=lambda: s['clock'][0])
    s['auth']['target'] = target
    target.apply_center_snapshot(package(final))
    assert len(target.pending_intents()) == 1
    assert target.pending_intents()[0]["payload"]["kind"] == "snapshot_applied"
    assert target.pending_intents()[0]["payload"]["applied"] is True
    with pytest.raises(HandoffInventoryError, match='不能使用旧包重建'):
        target.prepare_incoming(original, container_details=s['details'], plans=s['plans'])
    assert store.query_one('SELECT COUNT(*) AS total FROM station_handoff_item')['total'] == 0
