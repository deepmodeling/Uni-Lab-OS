"""Backend AGV 与目标 Edge 之间的入口库位预留权威。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from unilabos.app.scheduler.inventory.dispatch_admission import (
    InventoryMutationConflict,
    assert_resource_keys_unclaimed,
)
from unilabos.app.scheduler.inventory.store import (
    InventoryStore,
    SiteOccupancyConflict,
    set_site_occupancy,
)
from unilabos.app.scheduler.resource_lock import (
    device_lock_key,
    material_lock_key,
    site_lock_key,
)


class IngressReservationError(ValueError):
    """入口预留请求违反身份、容量或状态转换约束。"""

    def __init__(self, code: str, message: str) -> None:
        """保存稳定错误码和中文原因；构造过程不访问外部状态。"""

        super().__init__(message)
        self.code = code
        self.message = message


class StationIngressAuthority:
    """隐藏入口库位选择、状态机、审计和库存结算的深模块。"""

    def __init__(
        self,
        store: InventoryStore,
        *,
        edge_id: str,
        lab_id: str,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        """绑定库存权威及事件身份。

        参数：``store`` 是目标 Edge 唯一库存库；Edge/Lab 身份写入库存发件箱；
        ``now`` 只用于确定性测试。返回无。异常：构造过程不访问数据库。
        """

        self._store = store
        self._edge_id = str(edge_id)
        self._lab_id = str(lab_id)
        self._now = now or (lambda: datetime.now(timezone.utc))

    def reserve(
        self,
        *,
        idempotency_key: str,
        carrier_material_uuid: str,
        candidate_site_uuids: Sequence[str],
        ttl_seconds: int,
        backend_task_uuid: str | None = None,
        invocation_key: str | None = None,
    ) -> dict[str, Any]:
        """从逻辑入口候选组原子选择并预留首个可用库位。

        参数：幂等键标识 Backend 命令；载体 UUID 必须已同步到目标 Edge；候选
        库位属于领域包声明的同一逻辑入口；未保护的预留在 TTL 后自然释放。返回：持久预留
        投影。异常：重放载荷冲突、物料/库位不存在或所有候选不可用时抛
        ``IngressReservationError``。选择和预留在一个 ``BEGIN IMMEDIATE`` 中
        完成，按 ``sort_order/create_time/uuid`` 取首个可用项。
        """

        with self._store.transaction() as connection:
            return self.reserve_in_transaction(connection, idempotency_key=idempotency_key, carrier_material_uuid=carrier_material_uuid, candidate_site_uuids=candidate_site_uuids, ttl_seconds=ttl_seconds, backend_task_uuid=backend_task_uuid, invocation_key=invocation_key)

    def reserve_in_transaction(self, connection: Any, *, idempotency_key: str, carrier_material_uuid: str, candidate_site_uuids: Sequence[str], ttl_seconds: int, backend_task_uuid: str | None = None, invocation_key: str | None = None) -> dict[str, Any]:
        """在调用方库存事务内执行；不提交、不发送网络请求。"""
        if not connection.in_transaction:
            raise IngressReservationError("transaction_required", "入口操作必须处于库存写事务")

        key = self._required_text(idempotency_key, "idempotency_key")
        carrier_uuid = self._uuid(carrier_material_uuid, "carrier_material_uuid")
        candidates = self._candidate_uuids(candidate_site_uuids)
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
            raise IngressReservationError("invalid_ttl", "ttl_seconds 必须是正整数")
        if ttl_seconds <= 0:
            raise IngressReservationError("invalid_ttl", "ttl_seconds 必须是正整数")
        request = {
            "carrier_material_uuid": carrier_uuid,
            "candidate_site_uuids": sorted(candidates),
            "ttl_seconds": ttl_seconds,
            "backend_task_uuid": self._optional_text(backend_task_uuid),
            "invocation_key": self._optional_text(invocation_key),
        }
        request_hash = hashlib.sha256(
            json.dumps(
                request,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        now = self._utc_now()
        self._expire_due(connection, now)
        replay = connection.execute(
            "SELECT uuid,request_hash FROM station_ingress_reservation "
            "WHERE idempotency_key=?",
            (key,),
        ).fetchone()
        if replay is not None:
            if str(replay["request_hash"]) != request_hash:
                raise IngressReservationError(
                    "idempotency_conflict",
                    "入口预留幂等键已用于不同请求",
                )
            return self._read(connection, str(replay["uuid"]))
        material = connection.execute(
            "SELECT uuid FROM material WHERE uuid=? AND deleted_at IS NULL",
            (carrier_uuid,),
        ).fetchone()
        if material is None:
            raise IngressReservationError(
                "carrier_not_found",
                "可搬运载体尚未同步到目标 Edge",
            )
        placeholders = ",".join("?" for _ in candidates)
        rows = connection.execute(
            "SELECT site.uuid,site.material_uuid,site.sort_order,site.create_time "
            "FROM site LEFT JOIN station_ingress_reservation AS ingress "
            "ON ingress.site_uuid=site.uuid AND ingress.site_active=1 "
            f"WHERE site.uuid IN ({placeholders}) AND site.deleted_at IS NULL "
            "AND site.occupied_material_uuid IS NULL "
            "AND ingress.uuid IS NULL "
            "ORDER BY site.sort_order,site.create_time,site.uuid",
            tuple(candidates),
        ).fetchall()
        available_rows = []
        for row in rows:
            try:
                assert_resource_keys_unclaimed(
                    connection,
                    lock_keys=self._ingress_resource_keys(
                        owner_material_uuid=str(row["material_uuid"]),
                        site_uuid=str(row["uuid"]),
                        carrier_material_uuid=carrier_uuid,
                    ),
                    allow_ingress_device_sharing=True,
                )
            except InventoryMutationConflict:
                continue
            available_rows.append(row)
        found = {
            str(row["uuid"])
            for row in connection.execute(
                f"SELECT uuid FROM site WHERE uuid IN ({placeholders}) "
                "AND deleted_at IS NULL",
                tuple(candidates),
            ).fetchall()
        }
        missing = sorted(set(candidates) - found)
        if missing:
            raise IngressReservationError(
                "ingress_site_not_found",
                "逻辑入口引用不存在的库位：" + ",".join(missing),
            )
        if not available_rows:
            raise IngressReservationError(
                "ingress_capacity_unavailable",
                "逻辑入口当前没有可接收载体的库位",
            )
        reservation_uuid = str(uuid4())
        selected_site_uuid = str(available_rows[0]["uuid"])
        timestamp = self._format(now)
        expires_at = self._format(now + timedelta(seconds=ttl_seconds))
        connection.execute(
            """
            INSERT INTO station_ingress_reservation(
                uuid,create_time,update_time,idempotency_key,request_hash,
                backend_task_uuid,invocation_key,carrier_material_uuid,state,
                expires_at,revision,site_uuid,site_active
            ) VALUES (?,?,?,?,?,?,?,?, 'reserved', ?,1,?,1)
            """,
            (
                reservation_uuid,
                timestamp,
                timestamp,
                key,
                request_hash,
                request["backend_task_uuid"],
                request["invocation_key"],
                carrier_uuid,
                expires_at,
                selected_site_uuid,
            ),
        )
        selected = available_rows[0]
        for lock_key in self._ingress_resource_keys(
            owner_material_uuid=str(selected["material_uuid"]),
            site_uuid=selected_site_uuid,
            carrier_material_uuid=carrier_uuid,
        ):
            connection.execute(
                "INSERT INTO station_ingress_reservation_resource("
                "reservation_uuid,lock_key,active) VALUES (?,?,1)",
                (reservation_uuid, lock_key),
            )
        self._emit(
            connection,
            reservation_uuid=reservation_uuid,
            revision=1,
            state="reserved",
            site_uuid=selected_site_uuid,
            occurred_at=now,
        )
        return self._read(connection, reservation_uuid)

    def protect_prepared(self, reservation_uuid: str) -> dict[str, Any]:
        """在回报 prepared 前持久保护原入口预留，保持 reserved 状态。

        参数：``reservation_uuid`` 是已有且尚未过期的预留身份。返回：含固定
        ``prepared_at`` 的持久投影；重复调用不改期限、修订或审计。异常：原预留
        已过期、已进入其他状态或持续资源损坏时失败关闭。保护、审计和发件箱在
        同一库存事务提交；此后 ``expires_at`` 仅供上层裁决新发运许可，不能用
        本地计时自动释放入口。此方法不授予运输许可，也不表示已经运输。
        """

        with self._store.transaction() as connection:
            return self.protect_prepared_in_transaction(connection, reservation_uuid)

    def protect_prepared_in_transaction(self, connection: Any, reservation_uuid: str) -> dict[str, Any]:
        """在调用方库存事务内执行；不提交、不发送网络请求。"""
        if not connection.in_transaction:
            raise IngressReservationError("transaction_required", "入口操作必须处于库存写事务")

        identity = self._uuid(reservation_uuid, "reservation_uuid")
        # 等待其他库存写者期间也可能到期，必须取得写事务后再读取时钟。
        now = self._utc_now()
        self._expire_due(connection, now)
        current = self._row(connection, identity)
        if current["prepared_at"] is not None:
            return self._read(connection, identity)
        if current["state"] != "reserved":
            raise IngressReservationError(
                "invalid_ingress_transition",
                f"入口预留 {current['state']} 状态不能确认 prepared",
            )
        self._assert_active_resources(connection, current)
        timestamp = self._format(now)
        revision = int(current["revision"]) + 1
        connection.execute(
            "UPDATE station_ingress_reservation SET prepared_at=?,update_time=?,revision=? WHERE uuid=?",
            (timestamp, timestamp, revision, identity),
        )
        self._emit(
            connection, reservation_uuid=identity, revision=revision,
            state="reserved", site_uuid=self._site_uuid(connection, identity),
            occurred_at=now, event_type="prepared",
        )
        return self._read(connection, identity)

    def mark_in_transit(self, reservation_uuid: str) -> dict[str, Any]:
        """确认 AGV 已装载并冻结入口预留为不可自然过期。

        参数：预留 UUID。返回：更新后投影。异常：预留不存在、已过期或处于其他
        终态时失败关闭；对已经 ``in_transit`` 的请求幂等返回。状态与审计事件在
        同一事务提交。此入口记录已经发生的运输，不签发许可；prepared 预留的
        截止时间已过仍可记录此前获准的真实运输，上层必须先核对持久发运许可。
        """

        identity = self._uuid(reservation_uuid, "reservation_uuid")
        now = self._utc_now()
        with self._store.transaction() as connection:
            self._expire_due(connection, now)
            current = self._row(connection, identity)
            if current["state"] == "in_transit":
                return self._read(connection, identity)
            if current["state"] != "reserved":
                raise IngressReservationError(
                    "invalid_ingress_transition",
                    f"入口预留 {current['state']} 状态不能进入运输中",
                )
            self._assert_active_resources(connection, current)
            revision = int(current["revision"]) + 1
            timestamp = self._format(now)
            connection.execute(
                "UPDATE station_ingress_reservation SET state='in_transit',"
                "transport_started_at=?,update_time=?,revision=? WHERE uuid=?",
                (timestamp, timestamp, revision, identity),
            )
            site_uuid = self._site_uuid(connection, identity)
            self._emit(
                connection,
                reservation_uuid=identity,
                revision=revision,
                state="in_transit",
                site_uuid=site_uuid,
                occurred_at=now,
            )
            return self._read(connection, identity)

    def receive(self, reservation_uuid: str) -> dict[str, Any]:
        """以目标 Edge 物理交接确认结算载体进入入口库位。

        参数：预留 UUID。返回：``received`` 投影。异常：仅 ``in_transit`` 可
        接收，且目标库位必须仍为空；完全相同的成功重放幂等。库位占用、载体父级、
        预留终态和 Outbox 在同一库存事务提交。
        """

        with self._store.transaction() as connection:
            return self.receive_in_transaction(connection, reservation_uuid)

    def receive_in_transaction(self, connection: Any, reservation_uuid: str, *, handoff_uuid: str = "", physical_receipt: bool = False) -> dict[str, Any]:
        """在调用方库存事务内执行；不提交、不发送网络请求。"""
        if not connection.in_transaction:
            raise IngressReservationError("transaction_required", "入口操作必须处于库存写事务")

        identity = self._uuid(reservation_uuid, "reservation_uuid")
        now = self._utc_now()
        current = self._row(connection, identity)
        self._assert_handoff_access(connection, identity, handoff_uuid)
        if current["state"] == "received":
            return self._read(connection, identity)
        if current["state"] != "in_transit" and not (physical_receipt and current["state"] == "reserved" and current["prepared_at"] is not None):
            raise IngressReservationError(
                "invalid_ingress_transition",
                f"入口预留 {current['state']} 状态不能完成接收",
            )
        self._assert_active_resources(connection, current)
        site_uuid = self._site_uuid(connection, identity)
        site = connection.execute(
            "SELECT uuid,name,material_uuid,occupied_material_uuid FROM site "
            "WHERE uuid=? AND deleted_at IS NULL",
            (site_uuid,),
        ).fetchone()
        if site is None or site["occupied_material_uuid"] is not None:
            raise IngressReservationError(
                "ingress_site_changed",
                "运输中的目标入口库位不再可接收",
            )
        carrier_uuid = str(current["carrier_material_uuid"])
        occupied_elsewhere = connection.execute(
            "SELECT uuid FROM site WHERE occupied_material_uuid=? "
            "AND deleted_at IS NULL",
            (carrier_uuid,),
        ).fetchone()
        if occupied_elsewhere is not None:
            raise IngressReservationError(
                "carrier_location_conflict",
                "可搬运载体已占用另一个工站内库位",
            )
        try:
            assert_resource_keys_unclaimed(
                connection,
                lock_keys=self._ingress_resource_keys(
                    owner_material_uuid=str(site["material_uuid"]),
                    site_uuid=site_uuid,
                    carrier_material_uuid=carrier_uuid,
                ),
                ignore_ingress_reservation_uuid=identity,
                    ignore_handoff_uuid=handoff_uuid,
                allow_ingress_device_sharing=True,
            )
        except InventoryMutationConflict as error:
            raise IngressReservationError(
                "ingress_resource_claimed",
                f"入口接收命中活动 Claim：{error}",
            ) from error
        timestamp = self._format(now)
        revision = int(current["revision"]) + 1
        previous = connection.execute(
            "SELECT name FROM site WHERE occupied_material_uuid=? "
            "AND deleted_at IS NULL",
            (carrier_uuid,),
        ).fetchone()
        try:
            set_site_occupancy(
                connection,
                site_uuid=site_uuid,
                material_uuid=carrier_uuid,
                update_time=timestamp,
            )
        except SiteOccupancyConflict as error:
            code = (
                "carrier_location_conflict"
                if error.code in {
                    "material_already_occupies_site",
                    "material_multiple_sites",
                }
                else "ingress_site_changed"
            )
            raise IngressReservationError(code, str(error)) from error
        connection.execute(
            "UPDATE material SET parent_uuid=?,update_time=? WHERE uuid=?",
            (str(site["material_uuid"]), timestamp, carrier_uuid),
        )
        from unilabos.app.scheduler.inventory.material_io import (
            _record_material_names,
        )

        _record_material_names(
            connection,
            material_uuids=(carrier_uuid,),
            event_type="material.move",
            changes={
                "site_name_before": str(previous["name"]) if previous else "",
                "site_name_after": str(site["name"] or ""),
            },
        )
        connection.execute(
            "UPDATE station_ingress_reservation SET state='received',"
            "received_at=?,update_time=?,revision=? WHERE uuid=?",
            (timestamp, timestamp, revision, identity),
        )
        connection.execute(
            "UPDATE station_ingress_reservation SET site_active=0 WHERE uuid=?",
            (identity,),
        )
        self._release_resources(connection, identity)
        self._emit(
            connection,
            reservation_uuid=identity,
            revision=revision,
            state="received",
            site_uuid=site_uuid,
            occurred_at=now,
        )
        return self._read(connection, identity)

    @staticmethod
    def _assert_handoff_access(connection: Any, reservation_uuid: str, handoff_uuid: str) -> None:
        """中心管理的预留仅允许交接事务结算，旧入口不能释放保护。"""
        row = connection.execute(
            "SELECT handoff_uuid FROM station_handoff_item WHERE reservation_uuid=?",
            (reservation_uuid,),
        ).fetchone()
        if row is not None and row["handoff_uuid"] != handoff_uuid:
            raise IngressReservationError("handoff_managed_reservation", "该预留必须通过中心交接证据结算")

    @staticmethod
    def _ingress_resource_keys(
        *,
        owner_material_uuid: str,
        site_uuid: str,
        carrier_material_uuid: str,
    ) -> tuple[str, ...]:
        """返回入口预留或结算真正相关的精确物理资源键。

        目标 Site 键自然与其 owner 的整物料锁形成层级冲突；owner 自身未被写入，
        因此不额外申请整物料键，保留同一 owner 下无关 Site 的并行能力。设备键则
        继续保护 owner 与载体被作为可执行设备使用时的活动作业。
        """

        return (
            site_lock_key(owner_material_uuid, site_uuid),
            device_lock_key(owner_material_uuid),
            material_lock_key(carrier_material_uuid),
            device_lock_key(carrier_material_uuid),
        )

    def cancel(self, reservation_uuid: str, *, reason: str) -> dict[str, Any]:
        """由人工明确取消未完成的入口预留。

        参数：预留 UUID 与不可为空的审计理由。返回：``canceled`` 投影。异常：
        已接收或已过期不能取消；取消重放必须使用同一理由。``in_transit`` 只能经
        本接口释放，永不被 TTL 扫描自然释放。
        """

        with self._store.transaction() as connection:
            return self.cancel_in_transaction(connection, reservation_uuid, reason=reason)

    def cancel_in_transaction(self, connection: Any, reservation_uuid: str, *, reason: str, handoff_uuid: str = "") -> dict[str, Any]:
        """在调用方库存事务内执行；不提交、不发送网络请求。"""
        if not connection.in_transaction:
            raise IngressReservationError("transaction_required", "入口操作必须处于库存写事务")

        identity = self._uuid(reservation_uuid, "reservation_uuid")
        normalized_reason = self._required_text(reason, "reason")
        now = self._utc_now()
        self._expire_due(connection, now)
        current = self._row(connection, identity)
        self._assert_handoff_access(connection, identity, handoff_uuid)
        if current["state"] == "canceled":
            if str(current["cancel_reason"] or "") != normalized_reason:
                raise IngressReservationError(
                    "idempotency_conflict",
                    "入口预留已按另一理由取消",
                )
            return self._read(connection, identity)
        if current["state"] not in {"reserved", "in_transit"}:
            raise IngressReservationError(
                "invalid_ingress_transition",
                f"入口预留 {current['state']} 状态不能取消",
            )
        timestamp = self._format(now)
        revision = int(current["revision"]) + 1
        connection.execute(
            "UPDATE station_ingress_reservation SET state='canceled',"
            "cancel_reason=?,canceled_at=?,update_time=?,revision=? WHERE uuid=?",
            (normalized_reason, timestamp, timestamp, revision, identity),
        )
        connection.execute(
            "UPDATE station_ingress_reservation SET site_active=0 WHERE uuid=?",
            (identity,),
        )
        self._release_resources(connection, identity)
        site_uuid = self._site_uuid(connection, identity)
        self._emit(
            connection,
            reservation_uuid=identity,
            revision=revision,
            state="canceled",
            site_uuid=site_uuid,
            occurred_at=now,
            reason=normalized_reason,
        )
        return self._read(connection, identity)

    def get(self, reservation_uuid: str) -> dict[str, Any]:
        """读取并顺便收敛已到期的运输前预留。

        参数：预留 UUID。返回：当前持久投影。异常：不存在时抛稳定错误。读取使用
        写事务是为了让 ``reserved -> expired`` 与活动库位释放原子提交。
        """

        identity = self._uuid(reservation_uuid, "reservation_uuid")
        now = self._utc_now()
        with self._store.transaction() as connection:
            self._expire_due(connection, now)
            return self._read(connection, identity)

    def expire_due(self) -> int:
        """批量释放已到期且尚未开始运输的预留；返回状态转换数量。"""

        with self._store.transaction() as connection:
            return self._expire_due(connection, self._utc_now())

    def _expire_due(self, connection: Any, now: datetime) -> int:
        """只过期尚未持久 prepared 的预留，已保护的入口等待显式结算。"""

        rows = connection.execute(
            "SELECT uuid,revision FROM station_ingress_reservation "
            "WHERE state='reserved' AND prepared_at IS NULL AND expires_at<=? ORDER BY expires_at,uuid",
            (self._format(now),),
        ).fetchall()
        timestamp = self._format(now)
        for row in rows:
            identity = str(row["uuid"])
            revision = int(row["revision"]) + 1
            site_uuid = self._site_uuid(connection, identity)
            connection.execute(
                "UPDATE station_ingress_reservation SET state='expired',"
                "update_time=?,revision=? WHERE uuid=?",
                (timestamp, revision, identity),
            )
            connection.execute(
                "UPDATE station_ingress_reservation SET site_active=0 WHERE uuid=?",
                (identity,),
            )
            self._release_resources(connection, identity)
            self._emit(
                connection,
                reservation_uuid=identity,
                revision=revision,
                state="expired",
                site_uuid=site_uuid,
                occurred_at=now,
            )
        return len(rows)

    @staticmethod
    def _release_resources(connection: Any, reservation_uuid: str) -> None:
        """在入口预留终态转换的同一事务释放全部持续资源占用。"""

        connection.execute(
            "UPDATE station_ingress_reservation_resource SET active=0 "
            "WHERE reservation_uuid=? AND active=1",
            (reservation_uuid,),
        )

    @staticmethod
    def _row(connection: Any, reservation_uuid: str) -> Any:
        """读取预留数据库行；不存在时抛 ``ingress_reservation_not_found``。"""

        row = connection.execute(
            "SELECT * FROM station_ingress_reservation WHERE uuid=?",
            (reservation_uuid,),
        ).fetchone()
        if row is None:
            raise IngressReservationError(
                "ingress_reservation_not_found",
                "入口预留不存在",
            )
        return row

    def _read(self, connection: Any, reservation_uuid: str) -> dict[str, Any]:
        """返回不泄漏内部请求哈希的入口预留公共投影。"""

        row = self._row(connection, reservation_uuid)
        if str(row["state"]) in {"reserved", "in_transit"}:
            self._assert_active_resources(connection, row)
        return {
            "uuid": str(row["uuid"]),
            "idempotency_key": str(row["idempotency_key"]),
            "backend_task_uuid": row["backend_task_uuid"],
            "invocation_key": row["invocation_key"],
            "carrier_material_uuid": str(row["carrier_material_uuid"]),
            "site_uuid": self._site_uuid(connection, reservation_uuid),
            "state": str(row["state"]),
            "expires_at": str(row["expires_at"]),
            "prepared_at": row["prepared_at"],
            "transport_started_at": row["transport_started_at"],
            "received_at": row["received_at"],
            "canceled_at": row["canceled_at"],
            "cancel_reason": row["cancel_reason"],
            "revision": int(row["revision"]),
            "create_time": str(row["create_time"]),
            "update_time": str(row["update_time"]),
        }

    def _assert_active_resources(self, connection: Any, reservation: Any) -> None:
        """证明活动入口预留仍完整持有创建时冻结的四个资源键。"""

        reservation_uuid = str(reservation["uuid"])
        site_uuid = self._site_uuid(connection, reservation_uuid)
        site = connection.execute(
            "SELECT material_uuid FROM site WHERE uuid=? AND deleted_at IS NULL",
            (site_uuid,),
        ).fetchone()
        if site is None:
            raise IngressReservationError(
                "ingress_reservation_corrupt",
                "活动入口预留的目标库位不存在",
            )
        expected = set(
            self._ingress_resource_keys(
                owner_material_uuid=str(site["material_uuid"]),
                site_uuid=site_uuid,
                carrier_material_uuid=str(reservation["carrier_material_uuid"]),
            )
        )
        actual = {
            str(row["lock_key"])
            for row in connection.execute(
                "SELECT lock_key FROM station_ingress_reservation_resource "
                "WHERE reservation_uuid=? AND active=1",
                (reservation_uuid,),
            ).fetchall()
        }
        if actual != expected:
            raise IngressReservationError(
                "ingress_reservation_corrupt",
                "活动入口预留的持续资源集合不完整",
            )

    @staticmethod
    def _site_uuid(connection: Any, reservation_uuid: str) -> str:
        """读取预留固定目标库位；结构损坏时失败关闭。"""

        row = connection.execute(
            "SELECT site_uuid FROM station_ingress_reservation WHERE uuid=?",
            (reservation_uuid,),
        ).fetchone()
        if row is None:
            raise IngressReservationError(
                "ingress_reservation_corrupt",
                "入口预留缺少目标库位事实",
            )
        return str(row["site_uuid"])

    def _emit(
        self,
        connection: Any,
        *,
        reservation_uuid: str,
        revision: int,
        state: str,
        site_uuid: str,
        occurred_at: datetime,
        reason: str = "",
        event_type: str | None = None,
    ) -> None:
        """在同一事务写库存审计与同步发件箱。"""

        occurred_ms = int(occurred_at.timestamp() * 1000)
        event_type = event_type or state
        event_id = str(
            uuid5(
                NAMESPACE_URL,
                f"unilab:station-ingress:{reservation_uuid}:{revision}:{event_type}",
            )
        )
        payload = {
            "reservation_uuid": reservation_uuid,
            "site_uuid": site_uuid,
            "state": state,
            "revision": revision,
        }
        if reason:
            payload["reason"] = reason
        if event_type == "prepared":
            reservation = self._row(connection, reservation_uuid)
            payload["prepared_at"] = reservation["prepared_at"]
            payload["expires_at"] = reservation["expires_at"]
        payload_json = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        connection.execute(
            "INSERT INTO inventory_ledger(occurred_at,op_type,aggregate_type,"
            "aggregate_id,delta_json,reason,causation_id) VALUES (?,?,?,?,?,?,?)",
            (
                occurred_ms,
                "station_ingress." + event_type,
                "station_ingress_reservation",
                reservation_uuid,
                payload_json,
                reason,
                event_id,
            ),
        )
        connection.execute(
            "INSERT INTO sync_outbox(event_id,edge_id,lab_id,aggregate_type,"
            "aggregate_id,aggregate_version,event_type,occurred_at,causation_id,"
            "payload_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                event_id,
                self._edge_id,
                self._lab_id,
                "station_ingress_reservation",
                reservation_uuid,
                revision,
                "station_ingress." + event_type,
                occurred_ms,
                event_id,
                payload_json,
            ),
        )

    def _utc_now(self) -> datetime:
        """返回带时区 UTC 时间；测试时钟若无时区则关闭式拒绝。"""

        value = self._now()
        if value.tzinfo is None:
            raise IngressReservationError(
                "invalid_clock",
                "入口预留时钟必须携带时区",
            )
        return value.astimezone(timezone.utc)

    @staticmethod
    def _format(value: datetime) -> str:
        """把 UTC 时间编码成可词典序比较的微秒 ISO 文本。"""

        return value.astimezone(timezone.utc).isoformat(timespec="microseconds")

    @staticmethod
    def _required_text(value: Any, field: str) -> str:
        """规范非空文本；空值抛稳定输入错误。"""

        normalized = str(value or "").strip()
        if not normalized:
            raise IngressReservationError(
                "invalid_ingress_request", f"{field} 不能为空"
            )
        return normalized

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        """规范可选文本；空串不进入持久业务身份。"""

        normalized = str(value or "").strip()
        return normalized or None

    @staticmethod
    def _uuid(value: Any, field: str) -> str:
        """规范非 nil UUID；非法身份抛稳定输入错误。"""

        try:
            normalized = UUID(str(value))
        except (TypeError, ValueError) as error:
            raise IngressReservationError(
                "invalid_ingress_request",
                f"{field} 不是合法 UUID",
            ) from error
        if normalized.int == 0:
            raise IngressReservationError(
                "invalid_ingress_request",
                f"{field} 不能是 nil UUID",
            )
        return str(normalized)

    @classmethod
    def _candidate_uuids(cls, values: Sequence[str]) -> tuple[str, ...]:
        """规范非空且无重复的逻辑入口候选库位集合。"""

        if isinstance(values, (str, bytes)) or not values:
            raise IngressReservationError(
                "invalid_ingress_request",
                "candidate_site_uuids 至少包含一个库位",
            )
        normalized = tuple(cls._uuid(value, "candidate_site_uuids") for value in values)
        if len(set(normalized)) != len(normalized):
            raise IngressReservationError(
                "invalid_ingress_request",
                "candidate_site_uuids 不能包含重复库位",
            )
        return normalized


__all__ = ["IngressReservationError", "StationIngressAuthority"]
