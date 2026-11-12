"""持久区间所有权与单次动作使用权；没有定时过期或推测性释放。"""
from __future__ import annotations

import logging
from typing import Any, Iterable
from uuid import NAMESPACE_URL, uuid5

from unilabos.protocol.utils.json_codec import encode_json, decode_json_bytes
from unilabos.server.backend.scheduler.models import SchedulerResourceRequest
from unilabos.server.backend.scheduler.resource_keys import device_lock_key, material_lock_key
from unilabos.server.backend.scheduler.resource_plan import deserialize_resource_plan, validate_station_resource_plans
from .errors import StoreConflict
from .device_tenancy import DeviceTenancies
from .resource_loans import ResourceLoans
from .store import WorkflowStore, utc_now

logger = logging.getLogger(__name__)

_TERMINAL = {"succeeded", "skipped", "failed", "canceled", "timeout"}
_UNRESOLVED = {"running", "dispatched", "cancel_requested", "execution_unknown", "intervention_required"}
# 已终态、但仍按超时持有锁的 attempt（ADR 0008）：使用权与准入不随终态收回。
_NOT_HELD_AFTER_TIMEOUT = ("NOT EXISTS (SELECT 1 FROM workflow_execution_claim c "
                           "WHERE c.job_uuid=workflow_node_job.uuid AND c.state='timed_out')")


def _json(value: Any) -> str:
    return encode_json(value, sort_keys=True).decode("utf-8")


def _load(value: str) -> Any:
    return decode_json_bytes(value.encode("utf-8"))


def physical_keys(request: SchedulerResourceRequest) -> set[str]:
    keys = {device_lock_key(item.device_id) for item in request.action_lock_claims}
    if request.current_action and not request.always_free:
        keys.add(device_lock_key(request.current_action.device_id))
    keys.update(device_lock_key(value) for value in request.device_claims)
    if request.device_tenancy:
        transition = request.device_tenancy
        keys.update(device_lock_key(value) for value in (transition.acquire_device_id, transition.release_device_id) if value)
        keys.add(material_lock_key(transition.material_uuid))
    if request.operate_in_place:
        keys.add(device_lock_key(request.operate_in_place.device_id))
        keys.add(material_lock_key(request.operate_in_place.material_uuid))
    keys.update(material_lock_key(item.material_uuid) for item in request.material_lock_claims)
    return keys


class ResourceIntervals:
    def __init__(self, store: WorkflowStore):
        self.store = store

    @staticmethod
    def _rows(conn, task_uuid: str) -> dict[str, Any]:
        rows = conn.execute("SELECT r.*, j.uncertainty_reason FROM workflow_node_run r "
                            "JOIN workflow_node_job j ON j.uuid=r.current_job_uuid "
                            "WHERE r.workflow_task_uuid=? AND r.deleted_at IS NULL", (task_uuid,)).fetchall()
        return {row["workflow_node_uuid"]: row for row in rows}

    @staticmethod
    def _generation(plan: dict[str, Any], parent: str | None, runs: dict[str, Any]) -> list[Any]:
        parents = {node["uuid"]: node.get("parent_uuid") for node in plan["nodes"]}
        generation = []
        seen = set()
        while parent is not None:
            if parent in seen or parent not in runs:
                raise StoreConflict("资源区间的循环父级不存在或成环")
            seen.add(parent)
            row = runs[parent]
            progress = _load(row["control_data"]).get("loop", {})
            generation.append([parent, row["current_job_uuid"], progress.get("iteration", 0)])
            parent = parents.get(parent)
        return generation

    @classmethod
    def _requirements(cls, conn, request: SchedulerResourceRequest, job) -> dict[str, tuple[str, dict[str, Any]]]:
        task = conn.execute("SELECT execution_plan FROM workflow_task WHERE uuid=?", (request.task_uuid,)).fetchone()
        plan = _load(task[0])
        runs = cls._rows(conn, request.task_uuid)
        wanted = {}
        depths = {}
        actual = physical_keys(request)
        for frame in (plan.get("resource_intervals") or {}).get("frames", []):
            frozen = deserialize_resource_plan(frame["plan"])
            resources = {resource.resource_id: resource.canonical_key for resource in frozen.resources}
            for interval in frozen.intervals:
                admission_members = frame.get("admission_members", {}).get(interval.interval_id, interval.node_uuids)
                if job["workflow_node_uuid"] not in admission_members:
                    continue
                key = resources[interval.resource_id]
                if not interval.explicit_boundary and key not in actual:
                    continue  # always_free 只免除当前动作隐式占用，显式作用域仍保留。
                generation = cls._generation(plan, frame["parent_node_uuid"], runs)
                definition = {"interval_id": interval.interval_id, "plan_id": frozen.plan_id,
                              "members": list(interval.node_uuids), "explicit": interval.explicit_boundary,
                              "parent": frame["parent_node_uuid"], "generation": generation}
                owner = str(uuid5(NAMESPACE_URL, _json([request.task_uuid, interval.interval_id, generation])))
                if key in wanted and wanted[key][0] != owner:
                    previous = wanted[key][1]
                    # 内层每轮的使用权复用外层显式所有权，不能逐轮释放或重复取得同一设备。
                    if previous["explicit"] and depths[key] < len(generation):
                        continue
                    if not (interval.explicit_boundary and len(generation) < depths[key]):
                        raise StoreConflict("同一动作映射到互相冲突的资源区间")
                wanted[key] = (owner, definition)
                depths[key] = len(generation)
        for key in actual - wanted.keys():
            owner = str(uuid5(NAMESPACE_URL, _json([request.owner_uuid, key])))
            wanted[key] = (owner, {"members": [job["workflow_node_uuid"]], "job_uuid": request.owner_uuid,
                                  "explicit": False, "generation": []})
        return wanted

    def admit(self, request: SchedulerResourceRequest, epoch: str) -> bool:
        """同时取得完整区间集合和实际使用权；冲突时不持有任何新增成员。"""
        from .execution_claim import ExecutionClaims
        with self.store.transaction() as conn:
            ExecutionClaims._require_epoch(conn, epoch)
            job = ExecutionClaims._job(conn, request.owner_uuid)
            if (job["workflow_task_uuid"] != request.task_uuid or job["current_job_uuid"] != request.owner_uuid
                    or job["status"] != "pending" or job["task_status"] not in {"pending", "running"}):
                raise StoreConflict("只有当前未派发尝试可以申请资源区间")
            prior = conn.execute("SELECT * FROM workflow_resource_admission WHERE job_uuid=?", (request.owner_uuid,)).fetchone()
            if prior:
                self.require_admission(conn, request)
                return True
            self.reconcile(conn, request.task_uuid)
            wanted = self._requirements(conn, request, job)
            actual = physical_keys(request)
            # 跨任务按同一套静态关系检查；仅拒绝实际可能并发的资源等待环路。
            active_plans = []
            for row in conn.execute("SELECT DISTINCT t.uuid,t.execution_plan FROM workflow_task t "
                                    "JOIN workflow_resource_interval i ON i.task_uuid=t.uuid WHERE i.state!='released' AND t.uuid!=?", (request.task_uuid,)):
                for frame in (_load(row["execution_plan"]).get("resource_intervals") or {}).get("frames", []):
                    active_plans.append(deserialize_resource_plan(frame["plan"]))
            task_plan = _load(conn.execute("SELECT execution_plan FROM workflow_task WHERE uuid=?", (request.task_uuid,)).fetchone()[0])
            active_plans.extend(deserialize_resource_plan(frame["plan"]) for frame in (task_plan.get("resource_intervals") or {}).get("frames", []))
            validate_station_resource_plans(active_plans)
            mapped, loans = {}, {}
            blockers = DeviceTenancies.blockers(conn, request, set(wanted))
            for key, (owner, definition) in wanted.items():
                occupied = conn.execute("SELECT * FROM workflow_resource_interval WHERE lock_key=? AND state!='released'", (key,)).fetchone()
                loan = ResourceLoans.active(conn, occupied["owner_uuid"]) if occupied else None
                if occupied and occupied["owner_uuid"] != owner:
                    if occupied["state"] == "held" and ResourceLoans.can_use(loan, request, owner, definition):
                        if request.device_tenancy:
                            raise StoreConflict("借用区间不能建立跨动作样品托管，请归还后由独占任务装载")
                        mapped[key] = occupied["owner_uuid"]
                        loans[key] = loan
                    else:
                        blockers.add(loan["borrower_task_uuid"] if loan else occupied["task_uuid"])
                elif occupied and (occupied["state"] != "held" or loan):
                    if loan:
                        blockers.add(loan["borrower_task_uuid"])
                    else:
                        return ResourceLoans.wait(conn, request, blockers)
                else:
                    mapped[key] = owner
                historical = conn.execute("SELECT state,definition FROM workflow_resource_interval WHERE owner_uuid=?", (owner,)).fetchone()
                if historical and (historical[0] != "held" or historical[1] != _json(definition)):
                    raise StoreConflict("资源区间已经释放或冻结边界发生变化")
            for key in actual:
                user = conn.execute("SELECT u.job_uuid,j.workflow_task_uuid FROM workflow_resource_use u "
                                    "JOIN workflow_node_job j ON j.uuid=u.job_uuid WHERE u.lock_key=?", (key,)).fetchone()
                if user and user["job_uuid"] != request.owner_uuid:
                    blockers.add(user["workflow_task_uuid"])
            if blockers or len(mapped) != len(wanted):
                return ResourceLoans.wait(conn, request, blockers)
            now = utc_now()
            # 托管准备、所有区间、借用身份与实际使用权在同一事务中提交。
            DeviceTenancies.prepare(conn, request)
            for key, (owner, definition) in wanted.items():
                if key in loans:
                    ResourceLoans.bind(conn, loans[key], owner, definition)
                    continue
                conn.execute("INSERT OR IGNORE INTO workflow_resource_interval "
                             "(owner_uuid,task_uuid,lock_key,definition,state,version,acquired_job_uuid,acquired_at) "
                             "VALUES (?,?,?,?, 'held',1,?,?)", (owner,request.task_uuid,key,_json(definition),request.owner_uuid,now))
            conn.execute("DELETE FROM workflow_resource_waiter WHERE job_uuid=?", (request.owner_uuid,))
            ownerships = mapped
            uses = {key: ownerships[key] for key in actual}
            conn.execute("INSERT INTO workflow_resource_admission(job_uuid,request,ownerships,uses,state,admitted_at) "
                         "VALUES (?,?,?,?,'active',?)", (request.owner_uuid,_json(request.model_dump(mode="json")),_json(ownerships),_json(uses),now))
            for key, owner in uses.items():
                conn.execute("INSERT INTO workflow_resource_use(lock_key,job_uuid,owner_uuid) VALUES (?,?,?)", (key,request.owner_uuid,owner))
            return True

    @staticmethod
    def require_admission(conn, request: SchedulerResourceRequest, *, for_start: bool = True) -> None:
        row = conn.execute("SELECT * FROM workflow_resource_admission WHERE job_uuid=?", (request.owner_uuid,)).fetchone()
        if not row or SchedulerResourceRequest.model_validate(_load(row["request"])) != request:
            raise StoreConflict("动作缺少有效的持久资源区间准入")
        terminal_release = False
        if row["state"] == "released" and not for_start:
            job = conn.execute("SELECT status FROM workflow_node_job WHERE uuid=?", (request.owner_uuid,)).fetchone()
            terminal_release = bool(job and job["status"] in _TERMINAL)
        if row["state"] != "active" and not terminal_release:
            raise StoreConflict("动作缺少有效的持久资源区间准入")
        ownerships, uses = _load(row["ownerships"]), _load(row["uses"])
        if set(uses) != physical_keys(request) or any(ownerships.get(key) != owner for key, owner in uses.items()):
            raise StoreConflict("动作实际资源使用清单损坏")
        if for_start:
            from .execution_claim import ExecutionClaims
            job = ExecutionClaims._job(conn, request.owner_uuid)
            expected = ResourceIntervals._requirements(conn, request, job)
            mapped = {}
            for key, (logical_owner, definition) in expected.items():
                physical_owner = ownerships.get(key)
                loan = ResourceLoans.active(conn, physical_owner)
                if physical_owner == logical_owner:
                    if loan:
                        raise StoreConflict("资源仍在借出，原任务必须等待归还")
                elif not ResourceLoans.can_use(loan, request, logical_owner, definition):
                    raise StoreConflict("动作资源区间准入清单与冻结计划不一致或缺少借用许可")
                mapped[key] = physical_owner
            if set(ownerships) != set(expected) or any(value is None for value in mapped.values()):
                raise StoreConflict("动作资源区间准入清单与冻结计划不一致")
        DeviceTenancies.require(conn, request, for_start=for_start)
        allowed_states = {"held"} if for_start else {"held", "retained"}
        if terminal_release:
            allowed_states.add("released")
        for key, owner in ownerships.items():
            held = conn.execute("SELECT task_uuid,lock_key,state FROM workflow_resource_interval WHERE owner_uuid=?", (owner,)).fetchone()
            borrowed = conn.execute("SELECT 1 FROM workflow_resource_loan WHERE lender_owner_uuid=? AND borrower_task_uuid=? "
                                    + ("AND state='lent' " if for_start else ("" if terminal_release else "AND state!='returned' ")) + "LIMIT 1", (owner,request.task_uuid)).fetchone()
            if (not held or (held["task_uuid"] != request.task_uuid and not borrowed) or held["lock_key"] != key
                    or held["state"] not in allowed_states):
                raise StoreConflict("资源区间已冻结或所有权失效")
        actual = {item["lock_key"]: item["owner_uuid"] for item in conn.execute("SELECT * FROM workflow_resource_use WHERE job_uuid=?", (request.owner_uuid,))}
        if actual != ({} if terminal_release else uses):
            raise StoreConflict("动作资源使用权已经失效")

    @classmethod
    def reconcile(cls, conn, task_uuid: str) -> None:
        """仅按持久动作事实收敛；显式异常区间保留给人工确认。"""
        DeviceTenancies.reconcile(conn, task_uuid)
        task = conn.execute("SELECT status,execution_plan FROM workflow_task WHERE uuid=?", (task_uuid,)).fetchone()
        runs = cls._rows(conn, task_uuid)
        # 已落终态而尚未清理的崩溃窗口可以收敛；未知执行与超时后仍持有锁的 attempt 不会命中。
        conn.execute("DELETE FROM workflow_resource_use WHERE job_uuid IN (SELECT uuid FROM workflow_node_job "
                     "WHERE workflow_task_uuid=? AND status IN ('succeeded','skipped','failed','canceled','timeout') "
                     "AND " + _NOT_HELD_AFTER_TIMEOUT + ")", (task_uuid,))
        conn.execute("UPDATE workflow_resource_admission SET state='released' WHERE job_uuid IN (SELECT uuid FROM workflow_node_job "
                     "WHERE workflow_task_uuid=? AND status IN ('succeeded','skipped','failed','canceled','timeout') "
                     "AND " + _NOT_HELD_AFTER_TIMEOUT + ")", (task_uuid,))
        ResourceLoans.reconcile(conn, task_uuid)
        conn.execute("DELETE FROM workflow_resource_waiter WHERE job_uuid IN (SELECT uuid FROM workflow_node_job WHERE status!='pending')")
        aborted = task["status"] in _TERMINAL | {"canceling"} or any(row["status"] in {"failed","canceled","timeout"} for row in runs.values())
        for owner in conn.execute("SELECT * FROM workflow_resource_interval WHERE task_uuid=? AND state!='released'", (task_uuid,)).fetchall():
            definition = _load(owner["definition"])
            members = [runs[key] for key in definition["members"] if key in runs]
            if len(members) != len(definition["members"]):
                raise StoreConflict("持久资源区间引用的节点运行缺失")
            if definition.get("job_uuid"):
                member = conn.execute("SELECT status FROM workflow_node_job WHERE uuid=?", (definition["job_uuid"],)).fetchone()
                done = member and member[0] in _TERMINAL
            else:
                generation = cls._generation(_load(task["execution_plan"]), definition.get("parent"), runs)
                if generation != definition["generation"]:
                    # 前轮必须在换代前收口；错误残留不能以新轮的终态自动释放。
                    done = False
                else:
                    done = all(row["status"] in _TERMINAL for row in members) or (aborted and not any(row["status"] in _UNRESOLVED for row in members))
            abnormal = any(row["status"] in {"failed","canceled","timeout","execution_unknown"} or row["uncertainty_reason"] for row in members)
            if definition["explicit"]:
                abnormal = abnormal or bool(conn.execute(
                    "SELECT 1 FROM workflow_resource_admission a JOIN workflow_node_job j ON j.uuid=a.job_uuid, "
                    "json_each(a.ownerships) o WHERE o.value=? AND j.workflow_task_uuid=? AND "
                    "(j.status IN ('failed','canceled','timeout','execution_unknown','intervention_required') "
                    "OR COALESCE(j.uncertainty_reason,'')!='') LIMIT 1", (owner["owner_uuid"],task_uuid)).fetchone())
            if definition["explicit"] and abnormal and owner["state"] == "held":
                conn.execute("UPDATE workflow_resource_interval SET state='retained',version=version+1 WHERE owner_uuid=?", (owner["owner_uuid"],))
            elif owner["state"] == "held" and done and not ResourceLoans.active(conn, owner["owner_uuid"]) and not conn.execute("SELECT 1 FROM workflow_resource_use WHERE owner_uuid=?", (owner["owner_uuid"],)).fetchone():
                conn.execute("UPDATE workflow_resource_interval SET state='released',version=version+1,released_at=?,release_reason='interval_completed' WHERE owner_uuid=?", (utc_now(),owner["owner_uuid"]))

    @classmethod
    def settle_jobs(cls, conn, job_uuids: Iterable[str]) -> None:
        """在写入 attempt 状态的同一事务里收敛由该状态派生的占用。

        终态 attempt 放开执行 Claim（超时后仍持有的除外），再按任务收敛使用权、准入、
        区间与设备托管；``execution_unknown`` 只把托管转为待核实。观察者因此看不到
        "节点已成功、托管仍是 reserved" 这类中间态，崩溃也不会把两步拆开。收敛本身出错
        （如区间引用损坏）只回滚到保存点并记日志：状态事实照常提交，之后的
        ``release_job`` / ``recover()`` 仍会重试并按原口径报错。
        """
        from .execution_claim import ExecutionClaims

        tasks: dict[str, None] = {}
        jobs = list(dict.fromkeys(str(job) for job in job_uuids))
        conn.execute("SAVEPOINT settle_job_resources")
        try:
            for job_uuid in jobs:
                row = conn.execute("SELECT workflow_task_uuid,status FROM workflow_node_job WHERE uuid=?",
                                   (job_uuid,)).fetchone()
                if row is None:
                    continue
                if row["status"] in _TERMINAL:
                    ExecutionClaims.release_terminal(conn, job_uuid)
                elif row["status"] == "execution_unknown":
                    conn.execute("UPDATE workflow_execution_claim SET state='uncertain' WHERE job_uuid=? "
                                 "AND state IN ('reserved','running')", (job_uuid,))
                tasks[row["workflow_task_uuid"]] = None
            for task_uuid in tasks:
                cls.reconcile(conn, task_uuid)
        except Exception:  # noqa: BLE001 - 状态事实优先提交，占用由 release_job / recover() 兜底
            conn.execute("ROLLBACK TO SAVEPOINT settle_job_resources")
            logger.exception("attempt %s 的占用未能与状态同事务收敛，留给后续释放与恢复", ",".join(jobs))
        conn.execute("RELEASE SAVEPOINT settle_job_resources")

    def release_job(self, job_uuid: str) -> None:
        with self.store.transaction() as conn:
            job = conn.execute("SELECT workflow_task_uuid FROM workflow_node_job WHERE uuid=?", (job_uuid,)).fetchone()
            if job:
                self.reconcile(conn, job[0])

    def withdraw(self, job_uuid: str) -> None:
        """未派发的排队动作撤回使用权；已开始的区间仍跨暂停保留。"""
        with self.store.transaction() as conn:
            job = conn.execute("SELECT status FROM workflow_node_job WHERE uuid=?", (job_uuid,)).fetchone()
            if not job or job[0] != "pending" or conn.execute("SELECT 1 FROM workflow_execution_claim WHERE job_uuid=?", (job_uuid,)).fetchone():
                return
            DeviceTenancies.withdraw(conn, job_uuid)
            conn.execute("DELETE FROM workflow_resource_waiter WHERE job_uuid=?", (job_uuid,))
            conn.execute("DELETE FROM workflow_resource_use WHERE job_uuid=?", (job_uuid,))
            conn.execute("DELETE FROM workflow_resource_admission WHERE job_uuid=?", (job_uuid,))
            # 其他动作已经准入或使用过的区间必须保留，动作间隙和暂停期间也不能让出。
            conn.execute("DELETE FROM workflow_resource_interval WHERE acquired_job_uuid=? AND state='held' "
                         "AND NOT EXISTS (SELECT 1 FROM workflow_resource_loan l WHERE l.lender_owner_uuid=workflow_resource_interval.owner_uuid) "
                         "AND NOT EXISTS (SELECT 1 FROM workflow_resource_admission a, json_each(a.ownerships) o "
                         "WHERE o.value=workflow_resource_interval.owner_uuid)", (job_uuid,))

    def recover(self) -> None:
        with self.store.transaction() as conn:
            tasks = [row[0] for row in conn.execute("SELECT task_uuid FROM workflow_resource_interval WHERE state!='released' UNION SELECT borrower_task_uuid FROM workflow_resource_loan WHERE state!='returned' UNION SELECT task_uuid FROM workflow_device_tenancy WHERE state!='released'")]
            for task in tasks:
                self.reconcile(conn, task)
            for row in conn.execute("SELECT * FROM workflow_resource_admission WHERE state='active'"):
                self.require_admission(conn, SchedulerResourceRequest.model_validate(_load(row["request"])), for_start=False)

    def unadmitted_active_claims(self) -> list[str]:
        with self.store.write_lock:
            return [row[0] for row in self.store.connection.execute("SELECT c.job_uuid FROM workflow_execution_claim c "
                "JOIN workflow_node_job j ON j.uuid=c.job_uuid WHERE c.state!='released' "
                "AND j.status IN ('running','dispatched','cancel_requested','execution_unknown','intervention_required') "
                "AND NOT EXISTS (SELECT 1 FROM workflow_resource_admission a WHERE a.job_uuid=c.job_uuid)")]

    def blocking_jobs(self, request: SchedulerResourceRequest) -> list[str]:
        """区间准入未通过的申请此刻被哪些 attempt 挡住：实际使用者与其他任务的区间获得者。"""

        blockers: set[str] = set()
        with self.store.write_lock:
            conn = self.store.connection
            for key in sorted(physical_keys(request)):
                for row in conn.execute("SELECT job_uuid FROM workflow_resource_use WHERE lock_key=?", (key,)):
                    if row["job_uuid"] != request.owner_uuid:
                        blockers.add(row["job_uuid"])
                held = conn.execute("SELECT task_uuid,acquired_job_uuid FROM workflow_resource_interval "
                                    "WHERE lock_key=? AND state!='released'", (key,)).fetchone()
                if held and held["task_uuid"] != request.task_uuid and held["acquired_job_uuid"] != request.owner_uuid:
                    blockers.add(held["acquired_job_uuid"])
        return sorted(blockers)

    def snapshot(self, task_uuid: str | None = None) -> list[dict[str, Any]]:
        with self.store.write_lock:
            return [{**dict(row), "definition": _load(row["definition"])} for row in self.store.connection.execute(
                "SELECT * FROM workflow_resource_interval WHERE state!='released' " + ("AND task_uuid=? " if task_uuid else "") + "ORDER BY acquired_at,owner_uuid",
                (task_uuid,) if task_uuid else ())]

    def confirm_release(self, owner_uuid: str, *, expected_version: int, reason: str) -> dict[str, Any]:
        if type(expected_version) is not int or expected_version < 1:
            raise StoreConflict("资源区间版本必须为正整数")
        if not isinstance(reason, str) or not reason.strip():
            raise StoreConflict("人工释放资源必须填写物理状态核实说明")
        with self.store.transaction() as conn:
            row = conn.execute("SELECT * FROM workflow_resource_interval WHERE owner_uuid=?", (owner_uuid,)).fetchone()
            if not row:
                raise StoreConflict("资源区间不存在")
            if row["state"] == "released":
                return {**dict(row), "definition": _load(row["definition"])}
            if row["state"] != "retained":
                raise StoreConflict("正常执行中的资源区间不能提前手动释放")
            if row["version"] != expected_version:
                raise StoreConflict("资源区间已变化，请刷新后重新核实")
            if ResourceLoans.active(conn, owner_uuid):
                raise StoreConflict("资源仍在借出，请先归还借用")
            definition = _load(row["definition"])
            runs = self._rows(conn, row["task_uuid"])
            task = conn.execute("SELECT execution_plan FROM workflow_task WHERE uuid=?", (row["task_uuid"],)).fetchone()
            same_generation = self._generation(_load(task["execution_plan"]), definition.get("parent"), runs) == definition["generation"]
            if same_generation and any(key not in runs or runs[key]["status"] not in _TERMINAL for key in definition["members"]):
                raise StoreConflict("区间仍有未收敛动作或待执行步骤，请先取消或完成该作用域")
            for admission in conn.execute("SELECT a.*,j.status FROM workflow_resource_admission a JOIN workflow_node_job j ON j.uuid=a.job_uuid"):
                if owner_uuid in _load(admission["ownerships"]).values() and admission["status"] not in _TERMINAL:
                    raise StoreConflict("区间仍有未收敛动作，请先核实执行结果")
            if conn.execute("SELECT 1 FROM workflow_resource_use u JOIN workflow_execution_claim c ON c.job_uuid=u.job_uuid "
                            "WHERE u.owner_uuid=? AND c.state='timed_out'", (owner_uuid,)).fetchone():
                raise StoreConflict("区间仍被超时后驱动尚未返回的动作占用，请等待驱动返回或先强制释放执行锁")
            conn.execute("UPDATE workflow_resource_interval SET state='released',version=version+1,released_at=?,release_reason=? WHERE owner_uuid=?", (utc_now(),reason,owner_uuid))
            released = conn.execute("SELECT * FROM workflow_resource_interval WHERE owner_uuid=?", (owner_uuid,)).fetchone()
            return {**dict(released), "definition": _load(released["definition"])}
