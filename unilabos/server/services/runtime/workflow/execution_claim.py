"""完整资源申请在派发前落库；未知执行的占用不按时间自动释放。"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from unilabos.protocol.utils.json_codec import encode_json, decode_json_bytes
from unilabos.server.backend.scheduler.models import SchedulerResourceRequest
from unilabos.server.services.runtime.workflow.errors import StoreConflict
from unilabos.server.services.runtime.workflow.store import WorkflowStore, utc_now

_TERMINAL = frozenset({"succeeded", "failed", "skipped", "canceled", "timeout"})


def _json(value: Any) -> str:
    return encode_json(value, sort_keys=True).decode("utf-8")


def _load(value: str) -> Any:
    return decode_json_bytes(value.encode("utf-8"))


class ExecutionClaims:
    """WorkflowStore 的事务内声明与租约；内存资源队列只负责选择候选。"""

    def __init__(self, store: WorkflowStore):
        self.store = store

    @staticmethod
    def _job(conn, job_uuid: str):
        row = conn.execute(
            "SELECT j.*, r.current_job_uuid, t.status AS task_status, t.control_status "
            "FROM workflow_node_job j JOIN workflow_node_run r ON r.uuid=j.workflow_node_run_uuid "
            "JOIN workflow_task t ON t.uuid=j.workflow_task_uuid "
            "WHERE j.uuid=? AND j.deleted_at IS NULL AND r.deleted_at IS NULL AND t.deleted_at IS NULL",
            (job_uuid,),
        ).fetchone()
        if row is None:
            raise StoreConflict(f"资源声明所属的任务尝试不存在：{job_uuid}")
        return row

    @staticmethod
    def _read(conn, row) -> dict[str, Any]:
        request = SchedulerResourceRequest.model_validate(_load(row["request"]))
        fences = _load(row["fences"])
        keys = {_json(list(item.canonical_key)) for item in request.identifiers}
        if (not isinstance(fences, dict) or set(fences) != keys
                or any(type(token) is not int or token < 1 for token in fences.values())
                or request.owner_uuid != row["job_uuid"] or request.task_uuid != row["task_uuid"]):
            raise StoreConflict("持久资源声明的身份或栅栏快照损坏")
        if row["state"] != "released":
            if request.interval_admission or conn.execute("SELECT 1 FROM workflow_resource_admission WHERE job_uuid=?", (request.owner_uuid,)).fetchone():
                from .resource_intervals import ResourceIntervals
                ResourceIntervals.require_admission(conn, request, for_start=False)
            leases = conn.execute(
                "SELECT l.lock_key, l.token, f.token AS current_token FROM workflow_execution_lease l "
                "JOIN workflow_execution_fence f ON f.lock_key=l.lock_key WHERE l.claim_uuid=?",
                (row["claim_uuid"],),
            ).fetchall()
            actual = {lease["lock_key"]: lease["token"] for lease in leases}
            if actual != fences or any(lease["token"] != lease["current_token"] for lease in leases):
                raise StoreConflict("活动资源声明缺少匹配的租约或栅栏已过期")
        return {**dict(row), "request": request.model_dump(mode="json"), "fences": fences}

    def get(self, job_uuid: str) -> dict[str, Any] | None:
        with self.store.write_lock:
            row = self.store.connection.execute(
                "SELECT * FROM workflow_execution_claim WHERE job_uuid=?", (job_uuid,),
            ).fetchone()
            return None if row is None else self._read(self.store.connection, row)

    def reserve(self, request: SchedulerResourceRequest, *, authority_epoch: str | None = None) -> dict[str, Any]:
        """原子取得全部租约；重放保持身份和令牌，资源集合变化则拒绝。"""
        request_json = _json(request.model_dump(mode="json"))
        with self.store.transaction() as conn:
            if authority_epoch is not None:
                self._require_epoch(conn, authority_epoch)
            job = self._job(conn, request.owner_uuid)
            if (job["workflow_task_uuid"] != request.task_uuid or job["current_job_uuid"] != request.owner_uuid
                    or job["status"] not in {"pending", "dispatched", "running"}
                    or job["task_status"] not in {"pending", "running"}):
                raise StoreConflict("过期、取消或已结束的尝试不能取得执行资源")
            prior = conn.execute("SELECT * FROM workflow_execution_claim WHERE job_uuid=?", (request.owner_uuid,)).fetchone()
            if prior is not None:
                if _json(SchedulerResourceRequest.model_validate(_load(prior["request"])).model_dump(mode="json")) != request_json or prior["state"] in {"released", "uncertain"}:
                    raise StoreConflict("资源声明已失效或同一尝试的资源集合发生变化")
                return self._read(conn, prior)
            if request.interval_admission:
                from .resource_intervals import ResourceIntervals
                ResourceIntervals.require_admission(conn, request)
            keys = sorted({_json(list(item.canonical_key)) for item in request.identifiers})
            for key in keys:
                if conn.execute("SELECT 1 FROM workflow_execution_lease WHERE lock_key=?", (key,)).fetchone():
                    raise StoreConflict(f"资源已有持久占用：{key}")
            fences = {}
            for key in keys:
                conn.execute("INSERT INTO workflow_execution_fence(lock_key, token) VALUES (?, 1) "
                             "ON CONFLICT(lock_key) DO UPDATE SET token=token+1", (key,))
                fences[key] = conn.execute("SELECT token FROM workflow_execution_fence WHERE lock_key=?", (key,)).fetchone()[0]
            claim_uuid = str(uuid4())
            conn.execute("INSERT INTO workflow_execution_claim "
                         "(claim_uuid, job_uuid, task_uuid, attempt_no, request, fences, state, acquired_at) "
                         "VALUES (?, ?, ?, ?, ?, ?, 'reserved', ?)",
                         (claim_uuid, request.owner_uuid, request.task_uuid, job["attempt_no"], request_json, _json(fences), utc_now()))
            for key, token in fences.items():
                conn.execute("INSERT INTO workflow_execution_lease(lock_key, claim_uuid, token) VALUES (?, ?, ?)",
                             (key, claim_uuid, token))
            row = conn.execute("SELECT * FROM workflow_execution_claim WHERE claim_uuid=?", (claim_uuid,)).fetchone()
            return self._read(conn, row)

    def require_current(self, job_uuid: str, claim_uuid: str) -> dict[str, Any]:
        """读取执行资格；历史 Claim 或已冻结的尝试不能再获得开始许可。"""
        with self.store.transaction() as conn:
            job = self._job(conn, job_uuid)
            row = conn.execute("SELECT * FROM workflow_execution_claim WHERE job_uuid=?", (job_uuid,)).fetchone()
            if (row is None or row["claim_uuid"] != claim_uuid or row["state"] not in {"reserved", "running"}
                    or job["current_job_uuid"] != job_uuid or job["status"] not in {"pending", "dispatched", "running"}
                    or job["task_status"] not in {"pending", "running"}):
                raise StoreConflict("执行资源凭据已失效，禁止再次派发")
            result = self._read(conn, row)
            request = SchedulerResourceRequest.model_validate(result["request"])
            if request.interval_admission or conn.execute("SELECT 1 FROM workflow_resource_admission WHERE job_uuid=?", (job_uuid,)).fetchone():
                from .resource_intervals import ResourceIntervals
                ResourceIntervals.require_admission(conn, request)
            return result

    def mark_dispatched(self, job_uuid: str) -> None:
        with self.store.transaction() as conn:
            row = conn.execute("SELECT * FROM workflow_execution_claim WHERE job_uuid=?", (job_uuid,)).fetchone()
            if row is None or row["state"] != "reserved":
                raise StoreConflict("派发前必须持有尚未使用的资源声明")
            self._read(conn, row)
            conn.execute("UPDATE workflow_execution_claim SET state='running' WHERE job_uuid=?", (job_uuid,))

    def mark_uncertain(self, job_uuid: str) -> None:
        with self.store.transaction() as conn:
            conn.execute("UPDATE workflow_execution_claim SET state='uncertain' WHERE job_uuid=? AND state!='released'", (job_uuid,))

    @staticmethod
    def _release(conn, row) -> None:
        conn.execute("DELETE FROM workflow_execution_lease WHERE claim_uuid=?", (row["claim_uuid"],))
        conn.execute("UPDATE workflow_execution_claim SET state='released', released_at=? WHERE claim_uuid=?",
                     (utc_now(), row["claim_uuid"]))

    @classmethod
    def release_terminal(cls, conn, job_uuid: str) -> bool:
        """事务内按 attempt 终态放开 Claim；返回 Claim 是否已不再占用。"""
        row = conn.execute("SELECT * FROM workflow_execution_claim WHERE job_uuid=?", (job_uuid,)).fetchone()
        if row is None or row["state"] == "released":
            return True
        job = cls._job(conn, job_uuid)
        if job["status"] not in _TERMINAL:
            return False
        cls._read(conn, row)
        cls._release(conn, row)
        return True

    def release_if_terminal(self, job_uuid: str) -> bool:
        """只有已核实的 attempt 终态才能放锁；停进程或回执丢失均不算终态。"""
        with self.store.transaction() as conn:
            return self.release_terminal(conn, job_uuid)

    def unclaimed_active_jobs(self) -> list[str]:
        """旧库在飞尝试没有冻结的资源集合，升级后必须先核实再开放新派发。"""
        with self.store.write_lock:
            rows = self.store.connection.execute(
                "SELECT j.uuid FROM workflow_node_job j JOIN workflow_node_run r ON r.current_job_uuid=j.uuid "
                "WHERE j.deleted_at IS NULL AND r.deleted_at IS NULL "
                "AND j.status IN ('dispatched','running','cancel_requested','execution_unknown','intervention_required') "
                "AND r.executor_kind NOT IN ('loop','condition') "
                "AND NOT EXISTS (SELECT 1 FROM workflow_execution_claim c WHERE c.job_uuid=j.uuid) "
                "AND NOT EXISTS (SELECT 1 FROM workflow_manual_confirmation m WHERE m.workflow_node_job_uuid=j.uuid)"
            ).fetchall()
        return [str(row[0]) for row in rows]

    def restore_requests(self) -> list[SchedulerResourceRequest]:
        """先恢复全部冻结的占用，再允许任务竞争；已落终态但未放锁的崩溃窗口可收敛。"""
        requests = []
        with self.store.transaction() as conn:
            rows = conn.execute("SELECT * FROM workflow_execution_claim WHERE state!='released' ORDER BY acquired_at, claim_uuid").fetchall()
            for row in rows:
                claim = self._read(conn, row)
                job = self._job(conn, row["job_uuid"])
                if job["status"] in _TERMINAL:
                    self._release(conn, row)
                else:
                    requests.append(SchedulerResourceRequest.model_validate(claim["request"]))
        return requests


    @staticmethod
    def _require_epoch(conn, epoch: str) -> None:
        row = conn.execute("SELECT epoch FROM workflow_execution_authority WHERE singleton=1").fetchone()
        if row is None or row[0] != epoch:
            raise StoreConflict("调度进程代次已失效，禁止旧进程继续派发")

    def activate_authority(self) -> str:
        """新调度器接管时更换代次；遗留派发只能核实，不能继续领用旧许可。"""
        epoch = str(uuid4())
        with self.store.transaction() as conn:
            conn.execute("INSERT INTO workflow_execution_authority(singleton, epoch) VALUES (1, ?) "
                         "ON CONFLICT(singleton) DO UPDATE SET epoch=excluded.epoch", (epoch,))
            conn.execute("UPDATE workflow_execution_claim SET state='uncertain' "
                         "WHERE state IN ('reserved','running') AND job_uuid IN (SELECT job_uuid FROM workflow_execution_dispatch)")
        return epoch

    def deactivate_authority(self, epoch: str) -> None:
        """关闭自己仍持有的代次；旧进程停止不能撤销已经接管的新权威。"""
        with self.store.transaction() as conn:
            changed = conn.execute("UPDATE workflow_execution_authority SET epoch=? WHERE singleton=1 AND epoch=?",
                                   (str(uuid4()), epoch)).rowcount
            if changed:
                conn.execute("UPDATE workflow_execution_claim SET state='uncertain' WHERE state IN ('reserved','running') "
                             "AND job_uuid IN (SELECT job_uuid FROM workflow_execution_dispatch)")

    def seal_dispatch(self, job_uuid: str, payload: dict[str, Any], epoch: str) -> dict[str, Any]:
        from unilabos.protocol.runtime.execution_start import execution_identity, identity_hash
        identity = execution_identity(payload)
        digest = identity_hash(identity)
        with self.store.transaction() as conn:
            self._require_epoch(conn, epoch)
            job = self._job(conn, job_uuid)
            row = conn.execute("SELECT * FROM workflow_execution_claim WHERE job_uuid=?", (job_uuid,)).fetchone()
            if row is None or row["state"] not in {"reserved", "running"}:
                raise StoreConflict("派发前没有有效资源声明")
            claim = self._read(conn, row)
            action = claim["request"].get("current_action") or {}
            if (identity["job_id"] != job_uuid or identity["task_id"] != job["workflow_task_uuid"]
                    or identity["node_id"] != job["workflow_node_uuid"] or identity["node_run_uuid"] != job["workflow_node_run_uuid"]
                    or identity["attempt_no"] != job["attempt_no"] or job["current_job_uuid"] != job_uuid
                    or identity["device_id"] != action.get("device_id") or identity["action"] != action.get("action_name")
                    or job["status"] != "running" or job["task_status"] not in {"pending", "running"}):
                raise StoreConflict("派发身份与当前尝试或资源声明不一致")
            prior = conn.execute("SELECT * FROM workflow_execution_dispatch WHERE job_uuid=?", (job_uuid,)).fetchone()
            if prior is not None:
                if prior["authority_epoch"] != epoch or prior["payload_hash"] != digest:
                    raise StoreConflict("同一次尝试的派发内容不可改变")
            else:
                conn.execute("INSERT INTO workflow_execution_dispatch(job_uuid,claim_uuid,authority_epoch,payload_hash,identity) "
                             "VALUES (?, ?, ?, ?, ?)", (job_uuid, claim["claim_uuid"], epoch, digest, _json(identity)))
            conn.execute("UPDATE workflow_execution_claim SET state='running' WHERE job_uuid=?", (job_uuid,))
            return {"claim_uuid": claim["claim_uuid"], "authority_epoch": epoch, "attempt_no": job["attempt_no"],
                    "payload_hash": digest, "fences": claim["fences"]}

    @staticmethod
    def _match_permit(conn, job_uuid: str, permit):
        dispatch = conn.execute("SELECT * FROM workflow_execution_dispatch WHERE job_uuid=?", (job_uuid,)).fetchone()
        claim = conn.execute("SELECT * FROM workflow_execution_claim WHERE job_uuid=?", (job_uuid,)).fetchone()
        if (dispatch is None or claim is None or permit.claim_uuid != dispatch["claim_uuid"]
                or permit.authority_epoch != dispatch["authority_epoch"] or permit.payload_hash != dispatch["payload_hash"]
                or permit.attempt_no != claim["attempt_no"] or permit.fences != _load(claim["fences"])):
            raise StoreConflict("开始许可与持久派发凭据不一致")
        return dispatch, claim

    def begin_execution(self, job_uuid: str, body: dict[str, Any]) -> dict[str, str]:
        from unilabos.protocol.runtime.execution_start import ExecutionBeginRequest, identity_hash
        request = ExecutionBeginRequest.model_validate(body)
        with self.store.transaction() as conn:
            self._require_epoch(conn, request.permit.authority_epoch)
            dispatch, claim = self._match_permit(conn, job_uuid, request.permit)
            if identity_hash(request.identity) != dispatch["payload_hash"]:
                raise StoreConflict("设备收到的参数与冻结派发内容不一致")
            self._read(conn, claim)
            request_claim = SchedulerResourceRequest.model_validate(_load(claim["request"]))
            if request_claim.interval_admission or conn.execute("SELECT 1 FROM workflow_resource_admission WHERE job_uuid=?", (job_uuid,)).fetchone():
                from .resource_intervals import ResourceIntervals
                ResourceIntervals.require_admission(conn, request_claim)
            job = self._job(conn, job_uuid)
            if (job["current_job_uuid"] != job_uuid or job["status"] not in {"dispatched", "running"}
                    or job["task_status"] not in {"pending", "running"} or claim["state"] != "running"):
                return {"decision": "UNKNOWN"}
            # 相同请求重放也不能再次返回 START；首个应答丢失时只能核实物理状态。
            if dispatch["invocation_uuid"] is not None:
                return {"decision": "UNKNOWN"}
            if job["control_status"] != "active":
                return {"decision": "WAIT"}
            conn.execute("UPDATE workflow_execution_dispatch SET invocation_uuid=?,executor_epoch=?,authorized_at=? WHERE job_uuid=?",
                         (request.invocation_uuid, request.executor_epoch, utc_now(), job_uuid))
            return {"decision": "START"}

    def record_start_receipt(self, job_uuid: str, body: dict[str, Any]) -> dict[str, Any]:
        from unilabos.protocol.runtime.execution_start import ExecutionStartReceipt
        receipt = ExecutionStartReceipt.model_validate(body)
        encoded = _json(receipt.model_dump(mode="json"))
        with self.store.transaction() as conn:
            dispatch, _claim = self._match_permit(conn, job_uuid, receipt.permit)
            if (dispatch["invocation_uuid"] != receipt.invocation_uuid or dispatch["executor_epoch"] != receipt.executor_epoch
                    or dispatch["authorized_at"] is None):
                raise StoreConflict("开始回执未对应已提交的开始许可")
            if dispatch["receipt"] is not None:
                if dispatch["receipt"] != encoded:
                    raise StoreConflict("同一次尝试的开始回执不可改变")
                created = False
            else:
                conn.execute("UPDATE workflow_execution_dispatch SET receipt=? WHERE job_uuid=?", (encoded, job_uuid))
                created = True
            epoch = conn.execute("SELECT epoch FROM workflow_execution_authority WHERE singleton=1").fetchone()
            job = self._job(conn, job_uuid)
            may_start = bool(epoch and epoch[0] == receipt.permit.authority_epoch and _claim["state"] == "running"
                             and job["status"] in {"running", "dispatched"} and job["current_job_uuid"] == job_uuid
                             and job["task_status"] in {"pending", "running"})
            if may_start:
                self._read(conn, _claim)
                request_claim = SchedulerResourceRequest.model_validate(_load(_claim["request"]))
                if request_claim.interval_admission or conn.execute("SELECT 1 FROM workflow_resource_admission WHERE job_uuid=?", (job_uuid,)).fetchone():
                    from .resource_intervals import ResourceIntervals
                    try:
                        ResourceIntervals.require_admission(conn, request_claim)
                    except StoreConflict:
                        may_start = False
            return {"receipt_uuid": receipt.receipt_uuid, "created": created, "may_start": may_start}
