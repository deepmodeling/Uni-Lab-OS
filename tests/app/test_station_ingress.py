"""目标 Edge 入口库位预留状态机测试。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from unilabos.app.scheduler.inventory.api import create_app
from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
from unilabos.app.scheduler.inventory.dispatch_admission import (
    DispatchAdmissionDecision,
    DispatchAdmissionRequest,
    DispatchResource,
)
from unilabos.app.scheduler.inventory.ingress import (
    IngressReservationError,
    StationIngressAuthority,
)
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.station_resource import (
    SqliteStationResourceInventory,
    TargetSiteRequest,
)
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.app.scheduler.site_target import (
    SiteTargetResolutionError,
    resolve_site_target,
)


@pytest.fixture
def ingress_inventory(tmp_path):
    """构造两个有序入口库位与两个可搬运载体。

    参数：``tmp_path`` 提供隔离数据库目录。返回：库存、可控时钟、入口权威和
    身份集合。异常：建模失败原样传播；fixture 结束后关闭库存连接。
    """

    store = InventoryStore(str(tmp_path / "ingress.db"))
    backend = BackendResourceService(store)
    templates = backend.sync_resource_templates(
        [
            {
                "id": "test.ingress-rack",
                "display_name": "入口架",
                "registry_type": "resource",
                "class": {},
            },
            {
                "id": "test.carrier",
                "display_name": "可搬运载体",
                "registry_type": "material",
                "class": {},
            },
        ]
    )["templates"]
    template_by_name = {row["name"]: row["uuid"] for row in templates}
    rack = backend.create_material(
        {
            "resource_template_uuid": template_by_name["test.ingress-rack"],
            "barcode": "INGRESS-RACK",
            "name": "工站入口",
        }
    )
    carriers = [
        backend.create_material(
            {
                "resource_template_uuid": template_by_name["test.carrier"],
                "barcode": f"CARRIER-{index}",
                "name": f"载体 {index}",
            }
        )
        for index in (1, 2)
    ]
    site_slow = str(uuid4())
    site_first = str(uuid4())
    timestamp = "2026-08-30T00:00:00+00:00"
    with store.transaction() as connection:
        for site_uuid, name, order in (
            (site_slow, "IN-B", 20),
            (site_first, "IN-A", 10),
        ):
            connection.execute(
                """
                INSERT INTO site(
                    uuid,create_time,update_time,meta_data,material_uuid,name,
                    sort_order,allowed_resource_template_uuids,
                    occupied_material_uuid,position_x,position_y,position_z,
                    depth,length,width
                ) VALUES (?,?,?,'{}',?,?,?,?,NULL,0,0,0,0,0,0)
                """,
                (
                    site_uuid,
                    timestamp,
                    timestamp,
                    rack["uuid"],
                    name,
                    order,
                    json.dumps([template_by_name["test.carrier"]]),
                ),
            )
    clock: dict[str, datetime] = {"now": datetime(2026, 8, 30, tzinfo=timezone.utc)}
    authority = StationIngressAuthority(
        store,
        edge_id="edge-test",
        lab_id="lab-test",
        now=lambda: clock["now"],
    )
    try:
        yield (
            store,
            clock,
            authority,
            {
                "rack": rack["uuid"],
                "carriers": [row["uuid"] for row in carriers],
                "sites": (site_slow, site_first),
            },
        )
    finally:
        store.close()


def _reserve(
    authority: StationIngressAuthority,
    identities: dict[str, Any],
    *,
    index: int = 0,
    key: str = "reserve-1",
    ttl_seconds: int = 30,
) -> dict[str, Any]:
    """按逆序候选提交入口预留，返回持久投影。"""

    return authority.reserve(
        idempotency_key=key,
        carrier_material_uuid=identities["carriers"][index],
        candidate_site_uuids=identities["sites"],
        ttl_seconds=ttl_seconds,
        backend_task_uuid="backend-task-1",
        invocation_key=f"invocation-{index}",
    )


def _claim_resources(
    store: InventoryStore,
    resources: tuple[DispatchResource, ...],
) -> str:
    """创建一个活动库存 Claim，返回其稳定身份。"""

    decision = _admit_resources(store, resources)
    assert decision.acquired
    return decision.claim_uuid


def _admit_resources(
    store: InventoryStore,
    resources: tuple[DispatchResource, ...],
) -> DispatchAdmissionDecision:
    """尝试创建库存 Claim，供测试同时断言取得与等待结果。"""

    return InventoryService(store).station_resources.acquire_dispatch_permit(
        DispatchAdmissionRequest(
            effect_uuid=str(uuid4()),
            task_uuid=str(uuid4()),
            job_uuid=str(uuid4()),
            attempt=1,
            parameter_hash="sha256:station-ingress-claim",
            expected_change_set={"kind": "no_inventory_change"},
            resources=resources,
        )
    )


def _site_resource(identities: dict[str, Any], site_uuid: str) -> DispatchResource:
    """构造入口架下一个库位的规范派发资源。"""

    return DispatchResource(
        lock_key=f"material/{identities['rack']}/site/{site_uuid}/exclusive",
        scope="material_site",
        material_uuid=identities["rack"],
        site_uuid=site_uuid,
    )


def test_ingress_selects_first_available_site_and_replays_idempotently(
    ingress_inventory,
) -> None:
    """入口组按库位顺序选择，活动预留互斥且命令重放不重复写事件。"""

    store, _clock, authority, identities = ingress_inventory
    first = _reserve(authority, identities)
    event_count = len(store.pending_outbox(0, 100))
    replay = _reserve(authority, identities)
    second = _reserve(authority, identities, index=1, key="reserve-2")

    assert first["site_uuid"] == identities["sites"][1]
    assert replay == first
    assert second["site_uuid"] == identities["sites"][0]
    assert len(store.pending_outbox(0, 100)) == event_count + 1
    with pytest.raises(IngressReservationError, match="没有可接收"):
        authority.reserve(
            idempotency_key="reserve-3",
            carrier_material_uuid=identities["carriers"][0],
            candidate_site_uuids=identities["sites"],
            ttl_seconds=30,
        )


def test_ingress_reservation_keeps_carrier_exclusive_across_other_sites(
    ingress_inventory,
) -> None:
    """同一载体已分配给入口运输后，不能再预留到另一个空库位。"""

    _store, _clock, authority, identities = ingress_inventory
    first = _reserve(authority, identities)
    other_site = next(
        site_uuid
        for site_uuid in identities["sites"]
        if site_uuid != first["site_uuid"]
    )

    with pytest.raises(IngressReservationError, match="没有可接收") as raised:
        authority.reserve(
            idempotency_key="same-carrier-other-site",
            carrier_material_uuid=first["carrier_material_uuid"],
            candidate_site_uuids=[other_site],
            ttl_seconds=30,
        )

    assert raised.value.code == "ingress_capacity_unavailable"


def test_inventory_reopen_backfills_active_ingress_resource_locks(
    ingress_inventory,
) -> None:
    """旧库升级时必须为仍活动的入口预留补齐持续资源锁。"""

    store, _clock, authority, identities = ingress_inventory
    reserved = _reserve(authority, identities)
    with store.transaction() as connection:
        connection.execute(
            "DELETE FROM station_ingress_reservation_resource "
            "WHERE reservation_uuid=?",
            (reserved["uuid"],),
        )
    path = store.path
    store.close()

    reopened = InventoryStore(path)
    try:
        rows = reopened.query_all(
            "SELECT lock_key FROM station_ingress_reservation_resource "
            "WHERE reservation_uuid=? AND active=1 ORDER BY lock_key",
            (reserved["uuid"],),
        )
        assert len(rows) == 4
    finally:
        reopened.close()


def test_ingress_refuses_transport_when_persistent_resource_set_is_corrupt(
    ingress_inventory,
) -> None:
    """活动资源键缺失时不能进入物理运输边界。"""

    store, _clock, authority, identities = ingress_inventory
    reserved = _reserve(authority, identities)
    with store.transaction() as connection:
        connection.execute(
            "DELETE FROM station_ingress_reservation_resource "
            "WHERE reservation_uuid=? AND lock_key=?",
            (
                reserved["uuid"],
                f"material/{reserved['carrier_material_uuid']}/exclusive",
            ),
        )

    with pytest.raises(IngressReservationError, match="持续资源集合不完整") as raised:
        authority.mark_in_transit(reserved["uuid"])

    assert raised.value.code == "ingress_reservation_corrupt"
    assert store.query_one(
        "SELECT state FROM station_ingress_reservation WHERE uuid=?",
        (reserved["uuid"],),
    ) == {"state": "reserved"}


def test_legacy_relation_write_cannot_bypass_ingress_reservation(
    ingress_inventory,
) -> None:
    """旧 relation 写入口不能把入口已预留的载体提前落入目标库位。"""

    store, _clock, authority, identities = ingress_inventory
    reserved = _reserve(authority, identities)
    site = store.query_one(
        "SELECT name FROM site WHERE uuid=?",
        (reserved["site_uuid"],),
    )

    with pytest.raises(sqlite3.IntegrityError, match="active claim"):
        with store.transaction() as connection:
            connection.execute(
                "INSERT INTO resource_relation(parent_uuid,slot_id,child_uuid,version) "
                "VALUES (?,?,?,1)",
                (
                    identities["rack"],
                    site["name"],
                    reserved["carrier_material_uuid"],
                ),
            )

    assert authority.get(reserved["uuid"])["state"] == "reserved"
    assert store.query_one(
        "SELECT occupied_material_uuid FROM site WHERE uuid=?",
        (reserved["site_uuid"],),
    ) == {"occupied_material_uuid": None}


def test_legacy_relation_write_cannot_bypass_dispatch_claim(
    ingress_inventory,
) -> None:
    """旧 relation 写入口不能移动正由动作 Claim 持有的物料。"""

    store, _clock, _authority, identities = ingress_inventory
    carrier_uuid = identities["carriers"][0]
    _claim_resources(
        store,
        (
            DispatchResource(
                lock_key=f"material/{carrier_uuid}/exclusive",
                scope="material",
                material_uuid=carrier_uuid,
            ),
        ),
    )
    target_site_uuid = identities["sites"][0]
    site = store.query_one("SELECT name FROM site WHERE uuid=?", (target_site_uuid,))

    with pytest.raises(sqlite3.IntegrityError, match="active claim"):
        with store.transaction() as connection:
            connection.execute(
                "INSERT INTO resource_relation(parent_uuid,slot_id,child_uuid,version) "
                "VALUES (?,?,?,1)",
                (identities["rack"], site["name"], carrier_uuid),
            )

    assert store.query_one(
        "SELECT occupied_material_uuid FROM site WHERE uuid=?",
        (target_site_uuid,),
    ) == {"occupied_material_uuid": None}


def test_ingress_reserve_skips_site_held_by_active_dispatch_claim(
    ingress_inventory,
) -> None:
    """入口预留跳过被作业 Claim 占用的首选 Site，并保持稳定候选排序。"""

    store, _clock, authority, identities = ingress_inventory
    preferred_site = identities["sites"][1]
    fallback_site = identities["sites"][0]
    _claim_resources(store, (_site_resource(identities, preferred_site),))

    reserved = _reserve(authority, identities)

    assert reserved["site_uuid"] == fallback_site


def test_ingress_reserve_reports_capacity_when_all_sites_have_active_claims(
    ingress_inventory,
) -> None:
    """全部候选 Site 被活动 Claim 占用时返回稳定容量错误且不创建预留。"""

    store, _clock, authority, identities = ingress_inventory
    _claim_resources(
        store,
        tuple(_site_resource(identities, site_uuid) for site_uuid in identities["sites"]),
    )

    with pytest.raises(IngressReservationError, match="没有可接收") as raised:
        _reserve(authority, identities)

    assert raised.value.code == "ingress_capacity_unavailable"
    assert store.query_all("SELECT * FROM station_ingress_reservation") == []


def test_ingress_reserve_reports_capacity_while_owner_device_is_claimed(
    ingress_inventory,
) -> None:
    """入口架的设备 Claim 会保护其全部入口 Site，不能被预留流程绕过。"""

    store, _clock, authority, identities = ingress_inventory
    with store.transaction() as connection:
        connection.execute(
            "UPDATE material SET type='device' WHERE uuid=?",
            (identities["rack"],),
        )
    _claim_resources(
        store,
        (
            DispatchResource(
                lock_key=f"/devices/{identities['rack']}",
                scope="device",
                material_uuid=identities["rack"],
            ),
        ),
    )

    with pytest.raises(IngressReservationError, match="没有可接收") as raised:
        _reserve(authority, identities)

    assert raised.value.code == "ingress_capacity_unavailable"
    assert store.query_all("SELECT * FROM station_ingress_reservation") == []


def test_reserved_ingress_expires_and_releases_site(ingress_inventory) -> None:
    """尚未运输的预留到期后释放库位，另一命令可立即重新选择该位置。"""

    _store, clock, authority, identities = ingress_inventory
    first = _reserve(authority, identities, ttl_seconds=10)
    clock["now"] += timedelta(seconds=11)

    assert authority.get(first["uuid"])["state"] == "expired"
    replacement = _reserve(authority, identities, index=1, key="replacement")
    assert replacement["site_uuid"] == first["site_uuid"]


def test_prepared_survives_restart_and_expiry_without_releasing_resources(ingress_inventory) -> None:
    """prepared 已对外承诺的入口跨重启和截止时间仍禁止其他预留或派发取得。"""

    store, clock, authority, identities = ingress_inventory
    reserved = _reserve(authority, identities, ttl_seconds=5)
    prepared = authority.protect_prepared(reserved["uuid"])
    assert prepared["state"] == "reserved"
    assert prepared["transport_started_at"] is None
    assert prepared["expires_at"] == reserved["expires_at"]
    assert prepared["revision"] == reserved["revision"] + 1
    before = store.pending_outbox(0, 100)
    assert before[-1]["event_type"] == "station_ingress.prepared"
    payload = json.loads(before[-1]["payload_json"])
    assert payload["state"] == "reserved"
    assert payload["prepared_at"] == prepared["prepared_at"]
    assert payload["expires_at"] == reserved["expires_at"]
    path = store.path
    store.close()
    clock["now"] += timedelta(days=3)
    restarted = InventoryStore(path)
    try:
        recovered = StationIngressAuthority(restarted, edge_id="edge-test", lab_id="lab-test", now=lambda: clock["now"])
        assert recovered.expire_due() == 0
        assert recovered.get(reserved["uuid"]) == prepared
        assert recovered.protect_prepared(reserved["uuid"]) == prepared
        assert restarted.pending_outbox(0, 100) == before
        with pytest.raises(IngressReservationError, match="没有可接收"):
            recovered.reserve(idempotency_key="competing-site", carrier_material_uuid=identities["carriers"][1],
                              candidate_site_uuids=[prepared["site_uuid"]], ttl_seconds=5)
        with pytest.raises(IngressReservationError, match="没有可接收"):
            recovered.reserve(idempotency_key="competing-carrier", carrier_material_uuid=identities["carriers"][0],
                              candidate_site_uuids=identities["sites"], ttl_seconds=5)
        decision = _admit_resources(restarted, (_site_resource(identities, prepared["site_uuid"]),))
        assert not decision.acquired and decision.wait_code == "station_ingress_reserved"
    finally:
        restarted.close()


@pytest.mark.parametrize("seconds", [5, 6])
def test_expired_reservation_cannot_become_prepared(ingress_inventory, seconds) -> None:
    """截止时间已到的旧预留不能通过准备操作复活。"""

    store, clock, authority, identities = ingress_inventory
    reserved = _reserve(authority, identities, ttl_seconds=5)
    clock["now"] += timedelta(seconds=seconds)
    with pytest.raises(IngressReservationError, match="expired"):
        authority.protect_prepared(reserved["uuid"])
    expired = authority.get(reserved["uuid"])
    assert expired["state"] == "expired" and expired["prepared_at"] is None
    assert not any(event["event_type"] == "station_ingress.prepared" for event in store.pending_outbox(0, 100))
    replacement = _reserve(authority, identities, index=1, key="after-expiry")
    assert replacement["site_uuid"] == reserved["site_uuid"]


@pytest.mark.parametrize("state", ["in_transit", "received", "canceled"])
def test_first_prepared_refuses_later_or_closed_reservation(ingress_inventory, state) -> None:
    """不能在已运输或已结束的旧记录上倒填第一次 prepared 事实。"""

    store, _clock, authority, identities = ingress_inventory
    reserved = _reserve(authority, identities)
    if state == "canceled":
        current = authority.cancel(reserved["uuid"], reason="取消准备")
    else:
        current = authority.mark_in_transit(reserved["uuid"])
        if state == "received":
            current = authority.receive(reserved["uuid"])
    before = store.pending_outbox(0, 100)
    with pytest.raises(IngressReservationError, match=state):
        authority.protect_prepared(reserved["uuid"])
    assert authority.get(reserved["uuid"]) == current
    assert store.pending_outbox(0, 100) == before


def test_prepared_cancel_releases_resources_and_preserves_audit(ingress_inventory) -> None:
    """明确取消才能解除过期 prepared 的资源保护，重放仍保留原 prepared 时间。"""

    store, clock, authority, identities = ingress_inventory
    reserved = _reserve(authority, identities, ttl_seconds=5)
    prepared = authority.protect_prepared(reserved["uuid"])
    clock["now"] += timedelta(days=1)
    canceled = authority.cancel(reserved["uuid"], reason="已核实未发运，撤销入口")
    assert canceled["prepared_at"] == prepared["prepared_at"]
    assert canceled["state"] == "canceled"
    assert store.query_one("SELECT COUNT(*) AS n FROM station_ingress_reservation_resource WHERE reservation_uuid=? AND active=1",
                           (reserved["uuid"],)) == {"n": 0}
    before = store.pending_outbox(0, 100)
    assert authority.protect_prepared(reserved["uuid"]) == canceled
    assert authority.cancel(reserved["uuid"], reason="已核实未发运，撤销入口") == canceled
    assert store.pending_outbox(0, 100) == before
    replacement = _reserve(authority, identities, index=1, key="after-cancel")
    assert replacement["site_uuid"] == prepared["site_uuid"]


def test_prepared_records_actual_transport_after_deadline_then_receives(ingress_inventory) -> None:
    """过期 prepared 不自动运输，但显式记录此前获准的物理运输后可以正常收货。"""

    store, clock, authority, identities = ingress_inventory
    reserved = _reserve(authority, identities, ttl_seconds=5)
    prepared = authority.protect_prepared(reserved["uuid"])
    clock["now"] += timedelta(days=1)
    assert authority.get(reserved["uuid"])["transport_started_at"] is None
    with pytest.raises(IngressReservationError, match="reserved"):
        authority.receive(reserved["uuid"])
    transit = authority.mark_in_transit(reserved["uuid"])
    assert transit["prepared_at"] == prepared["prepared_at"]
    assert transit["expires_at"] == reserved["expires_at"]
    received = authority.receive(reserved["uuid"])
    assert received["prepared_at"] == prepared["prepared_at"]
    assert received["state"] == "received"
    assert store.query_one("SELECT occupied_material_uuid FROM site WHERE uuid=?", (received["site_uuid"],)) == {
        "occupied_material_uuid": identities["carriers"][0],
    }
    before = store.pending_outbox(0, 100)
    assert authority.receive(reserved["uuid"]) == received
    assert authority.protect_prepared(reserved["uuid"]) == received
    assert store.pending_outbox(0, 100) == before
    assert _admit_resources(store, (_site_resource(identities, received["site_uuid"]),)).acquired


def test_prepared_and_event_commit_atomically(ingress_inventory, monkeypatch) -> None:
    """发件箱写入之后失败也必须回滚保护标记、修订和审计，允许原预留重试。"""

    store, _clock, authority, identities = ingress_inventory
    reserved = _reserve(authority, identities)
    before = store.pending_outbox(0, 100)
    original = authority._emit

    def fail_after_event(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("prepared 事务故障")

    with monkeypatch.context() as scoped:
        scoped.setattr(authority, "_emit", fail_after_event)
        with pytest.raises(RuntimeError, match="事务故障"):
            authority.protect_prepared(reserved["uuid"])
    assert authority.get(reserved["uuid"]) == reserved
    assert store.pending_outbox(0, 100) == before
    assert store.query_one("SELECT COUNT(*) AS n FROM inventory_ledger WHERE op_type='station_ingress.prepared'") == {"n": 0}
    assert authority.protect_prepared(reserved["uuid"])["prepared_at"] is not None


def test_prepared_rejects_incomplete_resource_protection(ingress_inventory) -> None:
    """持久占有集合缺失时不能对外确认入口准备完成。"""

    store, _clock, authority, identities = ingress_inventory
    reserved = _reserve(authority, identities)
    with store.transaction() as connection:
        connection.execute("DELETE FROM station_ingress_reservation_resource WHERE reservation_uuid=?",
                           (reserved["uuid"],))
    before = store.pending_outbox(0, 100)
    with pytest.raises(IngressReservationError, match="持续资源集合不完整"):
        authority.protect_prepared(reserved["uuid"])
    assert store.query_one("SELECT prepared_at FROM station_ingress_reservation WHERE uuid=?", (reserved["uuid"],)) == {"prepared_at": None}
    assert store.pending_outbox(0, 100) == before


def test_prepared_column_migration_preserves_unprotected_legacy_expiry(ingress_inventory) -> None:
    """旧库补列不把历史预留误当成已承诺 prepared，既有期限和资源仍保留。"""

    store, clock, authority, identities = ingress_inventory
    reserved = _reserve(authority, identities, ttl_seconds=5)
    with store.transaction() as connection:
        connection.execute("ALTER TABLE station_ingress_reservation DROP COLUMN prepared_at")
    path = store.path
    store.close()
    reopened = InventoryStore(path)
    try:
        recovered = StationIngressAuthority(reopened, edge_id="edge-test", lab_id="lab-test", now=lambda: clock["now"])
        assert recovered.get(reserved["uuid"]) == reserved
        clock["now"] += timedelta(seconds=6)
        assert recovered.get(reserved["uuid"])["state"] == "expired"
    finally:
        reopened.close()


def test_normal_job_target_selection_never_steals_ingress_reservation(
    ingress_inventory,
) -> None:
    """普通 DAG 节点跳过 AGV 已预留入口；全部预留时失败关闭。

    参数：``ingress_inventory`` 提供共享同一 SQLite 权威的入口与节点选择模块。
    返回：无。异常：若两个深模块看到不同占用事实，断言明确暴露回归。
    """

    store, _clock, authority, identities = ingress_inventory
    inventory = SqliteStationResourceInventory(
        store,
        settle_material_transfer=lambda _command: {},
    )
    first = _reserve(authority, identities)

    selected = inventory.resolve_target_site(
        TargetSiteRequest(
            owner_material_uuid=identities["rack"],
            equivalent_site_uuids=identities["sites"],
            occupant_material_uuid=identities["carriers"][1],
        )
    )
    assert selected.uuid != first["site_uuid"]

    _reserve(authority, identities, index=1, key="reserve-second")
    with pytest.raises(SiteTargetResolutionError, match="没有可接收") as raised:
        resolve_site_target(
            inventory,
            owner_material_uuid=identities["rack"],
            site_uuids=identities["sites"],
            occupant_material_uuid=identities["carriers"][0],
        )
    assert {
        (resource["scope"], resource["material_uuid"], resource["site_uuid"])
        for resource in raised.value.resources
    } == {
        ("material_site", identities["rack"], site_uuid)
        for site_uuid in identities["sites"]
    }


def test_in_transit_never_expires_and_receive_commits_site_occupancy(
    ingress_inventory,
) -> None:
    """运输中预留跨过 TTL 仍保持，接收时原子形成入口库位物理事实。"""

    store, clock, authority, identities = ingress_inventory
    reserved = _reserve(authority, identities, ttl_seconds=5)
    in_transit = authority.mark_in_transit(reserved["uuid"])
    clock["now"] += timedelta(days=3)

    assert authority.expire_due() == 0
    assert authority.get(reserved["uuid"])["state"] == "in_transit"
    received = authority.receive(reserved["uuid"])
    site = store.query_one(
        "SELECT occupied_material_uuid FROM site WHERE uuid=?",
        (received["site_uuid"],),
    )
    carrier = store.query_one(
        "SELECT parent_uuid FROM material WHERE uuid=?",
        (received["carrier_material_uuid"],),
    )

    assert in_transit["state"] == "in_transit"
    assert received["state"] == "received"
    assert site == {"occupied_material_uuid": identities["carriers"][0]}
    assert carrier == {"parent_uuid": identities["rack"]}
    move = store.query_one(
        "SELECT delta_json FROM inventory_ledger "
        "WHERE subject_type='material' AND material_uuid=? AND op_type='material.move'",
        (received["carrier_material_uuid"],),
    )
    changes = json.loads(move["delta_json"])["changes"]
    site_name = store.query_one(
        "SELECT name FROM site WHERE uuid=?", (received["site_uuid"],)
    )["name"]
    assert changes["site_name_before"] == ""
    assert changes["site_name_after"] == site_name


@pytest.mark.parametrize("claimed_resource", ["target_site", "carrier_material"])
def test_ingress_blocks_claim_from_reservation_until_receive(
    ingress_inventory,
    claimed_resource: str,
) -> None:
    """预留和运输中都持续保护目标 Site/载体，接收完成后才释放。"""

    store, _clock, authority, identities = ingress_inventory
    reserved = _reserve(authority, identities)
    if claimed_resource == "target_site":
        resource = _site_resource(identities, reserved["site_uuid"])
    else:
        carrier_uuid = reserved["carrier_material_uuid"]
        resource = DispatchResource(
            lock_key=f"material/{carrier_uuid}/exclusive",
            scope="material",
            material_uuid=carrier_uuid,
        )
    before_transport = _admit_resources(store, (resource,))
    authority.mark_in_transit(reserved["uuid"])
    during_transport = _admit_resources(store, (resource,))
    authority.receive(reserved["uuid"])
    after_receive = _admit_resources(store, (resource,))

    assert not before_transport.acquired
    assert before_transport.wait_code == "station_ingress_reserved"
    assert not during_transport.acquired
    assert during_transport.wait_code == "station_ingress_reserved"
    assert after_receive.acquired


def test_in_transit_requires_manual_cancel_reason(ingress_inventory) -> None:
    """运输中只能由带理由的人工取消释放，且不同理由重放必须冲突。"""

    _store, clock, authority, identities = ingress_inventory
    reserved = _reserve(authority, identities, ttl_seconds=5)
    authority.mark_in_transit(reserved["uuid"])
    clock["now"] += timedelta(days=3)

    with pytest.raises(IngressReservationError, match="reason 不能为空"):
        authority.cancel(reserved["uuid"], reason="")
    canceled = authority.cancel(reserved["uuid"], reason="AGV 人工撤回")
    assert canceled["state"] == "canceled"
    assert canceled["cancel_reason"] == "AGV 人工撤回"
    assert authority.cancel(reserved["uuid"], reason="AGV 人工撤回") == canceled
    with pytest.raises(IngressReservationError, match="另一理由"):
        authority.cancel(reserved["uuid"], reason="另一个原因")


def test_ingress_http_contract_uses_persistent_authority(ingress_inventory) -> None:
    """公开 HTTP 接口只传命令，状态转换和库位事实仍由入口深模块持久化。"""

    store, _clock, _authority, identities = ingress_inventory
    client = TestClient(create_app(InventoryService(store)))
    created_response = client.post(
        "/api/v1/inventory/ingress-reservations",
        json={
            "idempotency_key": "http-reserve",
            "carrier_material_uuid": identities["carriers"][0],
            "candidate_site_uuids": list(identities["sites"]),
            "ttl_seconds": 60,
            "backend_task_uuid": "backend-http",
            "invocation_key": "invocation-http",
        },
    )
    assert created_response.status_code == 201
    created = created_response.json()
    assert created["state"] == "reserved"
    assert created["prepared_at"] is None

    prepared = client.post(f"/api/v1/inventory/ingress-reservations/{created['uuid']}/prepared")
    assert prepared.status_code == 200
    assert prepared.json()["state"] == "reserved"
    assert prepared.json()["prepared_at"] is not None
    assert prepared.json()["transport_started_at"] is None

    in_transit = client.post(
        f"/api/v1/inventory/ingress-reservations/{created['uuid']}/in-transit"
    )
    received = client.post(
        f"/api/v1/inventory/ingress-reservations/{created['uuid']}/receive"
    )

    assert in_transit.status_code == 200
    assert in_transit.json()["state"] == "in_transit"
    assert received.status_code == 200
    assert received.json()["state"] == "received"
    assert store.query_one(
        "SELECT occupied_material_uuid FROM site WHERE uuid=?",
        (created["site_uuid"],),
    ) == {"occupied_material_uuid": identities["carriers"][0]}
