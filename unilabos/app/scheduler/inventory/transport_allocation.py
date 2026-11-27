"""搬运车辆预占：与普通库存派发共用事务、冲突检查及持久围栏。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from unilabos.center_protocol.digest import digest
from unilabos.workflow.resource_lock_key import device_lock_key

SCHEMA = """
CREATE TABLE IF NOT EXISTS transport_preparation (
 request_uuid TEXT NOT NULL, generation INTEGER NOT NULL CHECK(generation>0),
 invocation_uuid TEXT NOT NULL, request_digest TEXT, request_json TEXT,
 state TEXT NOT NULL CHECK(state IN ('waiting','reserved','closed')),
 allocation_uuid TEXT, PRIMARY KEY(request_uuid,generation)
);
CREATE UNIQUE INDEX IF NOT EXISTS transport_preparation_live_invocation
 ON transport_preparation(invocation_uuid) WHERE state<>'closed';
CREATE TABLE IF NOT EXISTS transport_allocation (
 allocation_uuid TEXT PRIMARY KEY, request_uuid TEXT NOT NULL, generation INTEGER NOT NULL,
 vehicle_resource_uuid TEXT NOT NULL, vehicle_id TEXT NOT NULL, fence TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN
 ('reserved','committed','adopted','settlement_hold','needs_review','released')),
 task_uuid TEXT, binding_json TEXT NOT NULL, release_digest TEXT, release_json TEXT,
 UNIQUE(request_uuid,generation)
);
CREATE TABLE IF NOT EXISTS transport_allocation_resource (
 allocation_uuid TEXT NOT NULL REFERENCES transport_allocation(allocation_uuid),
 lock_key TEXT NOT NULL, PRIMARY KEY(allocation_uuid,lock_key)
);
CREATE TABLE IF NOT EXISTS transport_allocation_reconciliation (
 allocation_uuid TEXT PRIMARY KEY REFERENCES transport_allocation(allocation_uuid),
 evidence_digest TEXT NOT NULL, evidence_json TEXT NOT NULL
);
"""


class AllocationConflict(ValueError):
    """请求身份、执行归属或关闭证据不一致，不能按空闲重试。"""


@dataclass(frozen=True)
class VehicleCandidate:
    """由已校验合同和实时注册事实产生的完整车辆资源候选。"""

    material_uuid: str
    vehicle_id: str
    resource_keys: tuple[str, ...] = ()


def migrate(connection: sqlite3.Connection) -> None:
    """库存初始化时创建预占账本，不在业务事务内隐式提交。"""
    connection.executescript(SCHEMA)


def blocker(connection: sqlite3.Connection, keys: Sequence[str], *, task_uuid: str = "") -> str | None:
    """所有普通 Job 和人工写操作均检查预占；仅已接管原 Task 可使用自己的车。"""
    from unilabos.app.scheduler.resource_lock import conflicting_resource_lock_keys

    rows = connection.execute(
        "SELECT a.allocation_uuid,a.state,a.task_uuid,r.lock_key FROM transport_allocation a "
        "JOIN transport_allocation_resource r USING(allocation_uuid) WHERE a.state<>'released'"
    ).fetchall()
    for row in rows:
        if task_uuid and row["state"] == "adopted" and row["task_uuid"] == task_uuid:
            continue
        if conflicting_resource_lock_keys(set(keys), {row["lock_key"]}):
            return str(row["allocation_uuid"])
    return None


class TransportAllocations:
    """调用方持有 InventoryStore，全部状态变更通过其 BEGIN IMMEDIATE 事务。"""

    def __init__(self, store: Any) -> None:
        self.store = store

    def reserve(
        self,
        request: Mapping[str, Any],
        candidates: Sequence[VehicleCandidate],
        *,
        route_binding: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """稳定排序选车并原子取得整包资源；同请求重放不重新选车。"""
        from .dispatch_admission import InventoryMutationConflict, assert_resource_keys_unclaimed

        request_id = _uuid(request["request_uuid"])
        invocation = _uuid(request["station_invocation_uuid"])
        generation = request["generation"]
        if type(generation) is not int or not 1 <= generation <= 9_007_199_254_740_991:
            raise AllocationConflict("准备代次必须为正整数")
        request_hash = digest(dict(request))
        with self.store.transaction() as c:
            prior = c.execute(
                "SELECT * FROM transport_preparation WHERE request_uuid=? AND generation=?", (request_id, generation)
            ).fetchone()
            if prior is not None:
                if prior["invocation_uuid"] != invocation:
                    raise AllocationConflict("准备请求不能更换原调用")
                if prior["state"] == "closed":
                    return {"state": "closed", "request_uuid": request_id, "generation": generation}
                if prior["request_digest"] != request_hash:
                    raise AllocationConflict("同一准备代次的约束发生变化")
                if prior["allocation_uuid"]:
                    return _binding(c, prior["allocation_uuid"])
            else:
                live = c.execute(
                    "SELECT 1 FROM transport_preparation WHERE invocation_uuid=? AND state<>'closed'", (invocation,)
                ).fetchone()
                maximum = c.execute(
                    "SELECT MAX(generation) FROM transport_preparation WHERE request_uuid=?", (request_id,)
                ).fetchone()[0]
                if live or (maximum is not None and generation <= maximum):
                    raise AllocationConflict("旧准备代次尚未关闭或代次回退")
                c.execute(
                    "INSERT INTO transport_preparation VALUES(?,?,?,?,?,'waiting',NULL)",
                    (request_id, generation, invocation, request_hash, _json(request)),
                )
            for candidate in sorted(candidates, key=lambda v: (v.material_uuid, v.vehicle_id)):
                identity = _uuid(candidate.material_uuid)
                keys = tuple(sorted({device_lock_key(identity), *candidate.resource_keys}))
                # 读取当前库存，不接受孤立配置中的任意设备 UUID。
                material = c.execute(
                    "SELECT uuid FROM material WHERE uuid=? AND type='device' AND deleted_at IS NULL", (identity,)
                ).fetchone()
                if material is None:
                    raise AllocationConflict("候选车辆不属于当前库存")
                try:
                    assert_resource_keys_unclaimed(c, lock_keys=keys)
                except InventoryMutationConflict:
                    continue
                allocation, fence = str(uuid4()), str(uuid4())
                binding = {
                    "schema_version": 1,
                    "request_uuid": request_id,
                    "generation": generation,
                    "allocation_uuid": allocation,
                    "vehicle_resource_uuid": identity,
                    "vehicle_id": candidate.vehicle_id,
                    "allocation_fence": fence,
                    "executor_bindings": {"transport_vehicle": identity},
                    "resource_keys": list(keys),
                }
                if "source_endpoint" in request:
                    if route_binding is None:
                        raise AllocationConflict("车辆预占缺少已安装路线绑定")
                    binding.update(
                        endpoint_snapshot_digest=digest([request["source_endpoint"], request["target_endpoint"]]),
                        route_binding=dict(route_binding),
                        route_binding_digest=digest(dict(route_binding)),
                    )
                c.execute(
                    "INSERT INTO transport_allocation VALUES(?,?,?,?,?,?,'reserved',NULL,?,NULL,NULL)",
                    (allocation, request_id, generation, identity, candidate.vehicle_id, fence, _json(binding)),
                )
                c.executemany(
                    "INSERT INTO transport_allocation_resource VALUES(?,?)", [(allocation, key) for key in keys]
                )
                c.execute(
                    "UPDATE transport_preparation SET state='reserved',allocation_uuid=? WHERE request_uuid=? AND generation=?",
                    (allocation, request_id, generation),
                )
                return binding
            return {"state": "waiting", "request_uuid": request_id, "generation": generation}

    def commit(self, binding: Mapping[str, Any]) -> None:
        """持久任务包到达后封住许可前取消路径；并不授予执行权限。"""
        with self.store.transaction() as c:
            row = _matching(c, binding, exact=True)
            if row["state"] == "reserved":
                c.execute(
                    "UPDATE transport_allocation SET state='committed' WHERE allocation_uuid=?",
                    (row["allocation_uuid"],),
                )
            elif row["state"] not in {"committed", "adopted", "settlement_hold"}:
                raise AllocationConflict("车辆预占已关闭或需要核实")

    def quarantine(self, binding):
        """已固定包失配或明确终结未派发时保留原 owner，等待物理核实。"""
        with self.store.transaction() as c:
            row = _matching(c, binding, exact=True)
            if row["state"] != "released":
                c.execute(
                    "UPDATE transport_allocation SET state='needs_review' WHERE allocation_uuid=?",
                    (row["allocation_uuid"],),
                )

    def recover_execution_restart(self) -> None:
        """纯预占没有跨过执行边界，仍可重报或关闭；已提交的执行责任保持隔离。"""
        with self.store.transaction() as c:
            c.execute("UPDATE transport_allocation SET state='needs_review' WHERE state NOT IN ('released','reserved')")

    def restore_task_reference(self, binding: Mapping[str, Any], task_uuid: str) -> None:
        """仅修复已核对的原 Task 引用，保持隔离而不恢复物理执行。"""
        task = _uuid(task_uuid)
        with self.store.transaction() as c:
            row = _matching(c, binding, exact=True)
            if row["state"] != "needs_review" or row["task_uuid"] not in {None, task}:
                raise AllocationConflict("原任务引用修复只能用于同一待核实占用")
            c.execute("UPDATE transport_allocation SET task_uuid=? WHERE allocation_uuid=?", (task, row["allocation_uuid"]))

    def adopt(self, binding: Mapping[str, Any], *, task_uuid: str) -> None:
        """只供已正常 claim 的真实 Task 接管，资源在事务前后持续被占用。"""
        task = _uuid(task_uuid)
        with self.store.transaction() as c:
            row = _matching(c, binding, exact=True)
            if row["state"] == "adopted" and row["task_uuid"] == task:
                return
            if row["state"] != "committed" or row["task_uuid"] not in {None, task}:
                raise AllocationConflict("原车辆预占不能交给此 Task")
            c.execute(
                "UPDATE transport_allocation SET state='adopted',task_uuid=? WHERE allocation_uuid=?",
                (task, row["allocation_uuid"]),
            )

    def hold(self, task_uuid: str, *, uncertain: bool = False) -> None:
        """最后执行锁释放前转为结算占用；重启或未知物理结果形成持久屏障。"""
        with self.store.transaction() as c:
            phases = "('adopted','settlement_hold','needs_review')" if uncertain else "('adopted','settlement_hold')"
            c.execute(
                f"UPDATE transport_allocation SET state=? WHERE task_uuid=? AND state IN {phases}",
                ("needs_review" if uncertain else "settlement_hold", _uuid(task_uuid)),
            )

    def record_reconciliation(self, allocation_uuid: str, *, physical_safe: bool, evidence_refs: Sequence[str]) -> str:
        """操作权威取得真实物理核实后登记；此方法本身不释放资源。"""
        if (
            physical_safe is not True
            or not evidence_refs
            or not all(isinstance(v, str) and v.strip() for v in evidence_refs)
        ):
            raise AllocationConflict("车辆核实需要明确物理安全和真实证据")
        evidence = {
            "allocation_uuid": _uuid(allocation_uuid),
            "physical_safe": True,
            "evidence_refs": list(evidence_refs),
        }
        evidence_hash = digest(evidence)
        with self.store.transaction() as c:
            row = c.execute(
                "SELECT state FROM transport_allocation WHERE allocation_uuid=?", (allocation_uuid,)
            ).fetchone()
            previous = c.execute(
                "SELECT evidence_digest FROM transport_allocation_reconciliation WHERE allocation_uuid=?",
                (allocation_uuid,),
            ).fetchone()
            if previous is not None and previous[0] != evidence_hash:
                raise AllocationConflict("原车辆核实证据不能被覆盖")
            if previous is not None:
                return evidence_hash
            if row is None or row[0] != "needs_review":
                raise AllocationConflict("只有原待核实车辆可以登记该物理证明")
            c.execute(
                "INSERT INTO transport_allocation_reconciliation VALUES(?,?,?) ON CONFLICT DO NOTHING",
                (allocation_uuid, evidence_hash, _json(evidence)),
            )
        return evidence_hash

    def cancel(self, *, request_uuid: str, generation: int, invocation_uuid: str) -> dict[str, Any]:
        """可信许可前取消保存墓碑，即使 prepare 通知尚未到达也阻止复活。"""
        key, invocation = _uuid(request_uuid), _uuid(invocation_uuid)
        if type(generation) is not int or not 1 <= generation <= 9_007_199_254_740_991:
            raise AllocationConflict("取消代次无效")
        with self.store.transaction() as c:
            prior = c.execute(
                "SELECT * FROM transport_preparation WHERE request_uuid=? AND generation=?", (key, generation)
            ).fetchone()
            if prior is None:
                c.execute(
                    "INSERT INTO transport_preparation VALUES(?,?,?,NULL,NULL,'closed',NULL)",
                    (key, generation, invocation),
                )
            else:
                if prior["invocation_uuid"] != invocation:
                    raise AllocationConflict("取消原调用不匹配")
                if prior["allocation_uuid"]:
                    row = c.execute(
                        "SELECT * FROM transport_allocation WHERE allocation_uuid=?", (prior["allocation_uuid"],)
                    ).fetchone()
                    if row["state"] not in {"reserved", "released"}:
                        raise AllocationConflict("许可已经固定，必须撤销并核实后释放")
                    c.execute(
                        "UPDATE transport_allocation SET state='released' WHERE allocation_uuid=?",
                        (prior["allocation_uuid"],),
                    )
                c.execute(
                    "UPDATE transport_preparation SET state='closed' WHERE request_uuid=? AND generation=?",
                    (key, generation),
                )
            return {"state": "closed", "request_uuid": key, "generation": generation}

    def release(self, decision: Mapping[str, Any]) -> dict[str, Any]:
        """只处理经过 Center 命令认证的决定，匹配原 owner/fence 后幂等释放。"""
        _uuid(decision["decision_uuid"])
        request_hash = digest(dict(decision))
        with self.store.transaction() as c:
            row = _matching(c, decision)
            if row["release_digest"] is not None:
                if row["release_digest"] != request_hash:
                    raise AllocationConflict("同一车辆关闭决定不能被替换")
                return json.loads(row["release_json"])
            reason = decision.get("close_reason")
            if not decision.get("evidence_digest"):
                raise AllocationConflict("关闭决定缺少权威证据摘要")
            allowed = (
                (reason == "normal_settled" and row["state"] == "settlement_hold")
                or (reason == "aborted_before_permit" and row["state"] == "reserved")
                or (reason == "reconciled_safe" and row["state"] in {"needs_review", "settlement_hold"})
            )
            if not allowed:
                raise AllocationConflict("车辆状态尚不允许按此决定释放")
            if reason == "reconciled_safe":
                proof = c.execute(
                    "SELECT evidence_digest FROM transport_allocation_reconciliation WHERE allocation_uuid=?",
                    (row["allocation_uuid"],),
                ).fetchone()
                if proof is None or proof[0] != decision["evidence_digest"]:
                    raise AllocationConflict("车辆释放缺少本站已核实的物理安全证明")
            response = {
                "state": "released",
                "decision_uuid": decision["decision_uuid"],
                "allocation_uuid": row["allocation_uuid"],
                "generation": row["generation"],
            }
            c.execute(
                "UPDATE transport_allocation SET state='released',release_digest=?,release_json=? WHERE allocation_uuid=?",
                (request_hash, _json(response), row["allocation_uuid"]),
            )
            c.execute(
                "UPDATE transport_preparation SET state='closed' WHERE request_uuid=? AND generation=?",
                (row["request_uuid"], row["generation"]),
            )
            return response


def _matching(c: sqlite3.Connection, binding: Mapping[str, Any], *, exact: bool = False) -> sqlite3.Row:
    row = c.execute(
        "SELECT * FROM transport_allocation WHERE allocation_uuid=?", (_uuid(binding["allocation_uuid"]),)
    ).fetchone()
    if (
        row is None
        or type(binding.get("generation")) is not int
        or row["generation"] != binding.get("generation")
        or row["fence"] != binding.get("allocation_fence")
    ):
        raise AllocationConflict("车辆占用身份、代次或围栏不一致")
    if exact and digest(dict(binding)) != digest(json.loads(row["binding_json"])):
        raise AllocationConflict("任务包车辆绑定与原预占不一致")
    return row


def _binding(c: sqlite3.Connection, allocation: str) -> dict[str, Any]:
    row = c.execute(
        "SELECT binding_json,state FROM transport_allocation WHERE allocation_uuid=?", (allocation,)
    ).fetchone()
    if row is None or row["state"] in {"released", "needs_review"}:
        raise AllocationConflict("车辆占用记录已关闭")
    return json.loads(row["binding_json"])


def _uuid(value: str) -> str:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value or UUID(value).int == 0:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise AllocationConflict("必须使用非空规范 UUID") from None
    return value


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
