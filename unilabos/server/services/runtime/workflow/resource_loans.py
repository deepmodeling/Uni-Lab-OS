"""显式借出暂停任务的空闲区间；物理所有权始终只有一份。"""
from __future__ import annotations
from unilabos.protocol.utils.json_codec import encode_json, decode_json_bytes
from .errors import StoreConflict
from .store import utc_now


def _json(value):
    return encode_json(value, sort_keys=True).decode("utf-8")


def _load(value):
    return decode_json_bytes(value.encode("utf-8"))


def _record(row) -> dict:
    return {**dict(row), "definition": _load(row["definition"])}


class ResourceLoans:
    @staticmethod
    def lend(conn, owner_uuid: str, *, loan_uuid: str, borrower_task_uuid: str, expected_version: int, reason: str) -> dict:
        prior = conn.execute("SELECT * FROM workflow_resource_loan WHERE loan_uuid=?", (loan_uuid,)).fetchone()
        if prior:
            if prior["lender_owner_uuid"] != owner_uuid or prior["borrower_task_uuid"] != borrower_task_uuid or prior["reason"] != reason:
                raise StoreConflict("借用幂等键已经用于不同请求")
            return _record(prior)
        row = conn.execute("SELECT i.*,t.control_status FROM workflow_resource_interval i JOIN workflow_task t ON t.uuid=i.task_uuid WHERE owner_uuid=?",
                           (owner_uuid,)).fetchone()
        if not row or row["state"] != "held" or row["version"] != expected_version:
            raise StoreConflict("可借出的资源区间不存在或版本已经变化")
        if row["control_status"] != "paused":
            raise StoreConflict("借出资源前必须暂停原任务")
        if row["task_uuid"] == borrower_task_uuid:
            raise StoreConflict("不能借给原任务本身")
        borrower = conn.execute("SELECT status FROM workflow_task WHERE uuid=? AND deleted_at IS NULL", (borrower_task_uuid,)).fetchone()
        if not borrower or borrower[0] not in {"pending","running"}:
            raise StoreConflict("借用目标必须是未结束的任务")
        if conn.execute("SELECT 1 FROM workflow_resource_use WHERE owner_uuid=?", (owner_uuid,)).fetchone():
            raise StoreConflict("资源仍有动作占用，暂不能借出")
        if conn.execute("SELECT 1 FROM workflow_device_tenancy WHERE state!='released' AND (device_key=? OR 'material/' || material_uuid || '/exclusive'=?)",
                        (row["lock_key"],row["lock_key"])).fetchone():
            raise StoreConflict("已装载样品的设备和样品不能借出")
        if conn.execute("SELECT 1 FROM workflow_resource_loan WHERE lender_owner_uuid=? AND state!='returned'", (owner_uuid,)).fetchone():
            raise StoreConflict("资源已经借出")
        conn.execute("INSERT INTO workflow_resource_loan(loan_uuid,lender_owner_uuid,borrower_task_uuid,definition,state,version,reason,created_at) "
                     "VALUES (?,?,?,'{}','lent',1,?,?)", (loan_uuid,owner_uuid,borrower_task_uuid,reason,utc_now()))
        conn.execute("UPDATE workflow_resource_interval SET version=version+1 WHERE owner_uuid=?", (owner_uuid,))
        return _record(conn.execute("SELECT * FROM workflow_resource_loan WHERE loan_uuid=?", (loan_uuid,)).fetchone())

    @staticmethod
    def active(conn, owner_uuid):
        return conn.execute("SELECT * FROM workflow_resource_loan WHERE lender_owner_uuid=? AND state!='returned'", (owner_uuid,)).fetchone()

    @staticmethod
    def can_use(loan, request, logical_owner, definition) -> bool:
        return bool(loan and loan["state"] == "lent" and loan["borrower_task_uuid"] == request.task_uuid
                    and (loan["borrower_owner_uuid"] is None or
                         (loan["borrower_owner_uuid"] == logical_owner and loan["definition"] == _json(definition))))

    @staticmethod
    def bind(conn, loan, logical_owner, definition) -> None:
        if loan["borrower_owner_uuid"] is None:
            conn.execute("UPDATE workflow_resource_loan SET borrower_owner_uuid=?,definition=?,version=version+1 WHERE loan_uuid=?",
                         (logical_owner,_json(definition),loan["loan_uuid"]))

    @staticmethod
    def live_blockers(conn, task_uuid: str, keys: set[str]) -> set[str]:
        """等待边必须仍有占用事实支撑，不能用已释放资源的旧队列记录误判环路。"""
        from unilabos.server.backend.scheduler.resource_keys import material_lock_key
        result = set()
        for row in conn.execute("SELECT * FROM workflow_resource_interval WHERE state!='released'"):
            if row["lock_key"] in keys:
                loan = ResourceLoans.active(conn,row["owner_uuid"])
                result.add(loan["borrower_task_uuid"] if loan else row["task_uuid"])
        for row in conn.execute("SELECT u.lock_key,j.workflow_task_uuid FROM workflow_resource_use u JOIN workflow_node_job j ON j.uuid=u.job_uuid"):
            if row["lock_key"] in keys:
                result.add(row["workflow_task_uuid"])
        for row in conn.execute("SELECT * FROM workflow_device_tenancy WHERE state!='released'"):
            if row["device_key"] in keys or material_lock_key(row["material_uuid"]) in keys:
                result.add(row["task_uuid"])
        result.discard(task_uuid)
        return result

    @staticmethod
    def wait(conn, request, blockers: set[str]) -> bool:
        blockers.discard(request.task_uuid)
        # 持久等待关系按当前 attempt 过滤，取消或重试的旧等待不能制造假环。
        graph = {}
        for row in conn.execute("SELECT w.* FROM workflow_resource_waiter w JOIN workflow_node_job j ON j.uuid=w.job_uuid "
                                "JOIN workflow_node_run r ON r.current_job_uuid=j.uuid JOIN workflow_task t ON t.uuid=w.task_uuid "
                                "WHERE j.status='pending' AND t.control_status='active'"):
            if row["job_uuid"] != request.owner_uuid:
                live = ResourceLoans.live_blockers(conn,row["task_uuid"],set(_load(row["lock_keys"])))
                graph.setdefault(row["task_uuid"], set()).update(live & set(_load(row["blockers"])))
        graph.setdefault(request.task_uuid,set()).update(blockers)
        todo, seen = list(blockers), set()
        while todo:
            task = todo.pop()
            if task == request.task_uuid:
                raise StoreConflict("资源借还或设备托管形成等待环路，请调整资源顺序")
            if task not in seen:
                seen.add(task)
                todo.extend(graph.get(task, ()))
        from .resource_intervals import ResourceIntervals, physical_keys
        from .execution_claim import ExecutionClaims
        # 区间键 + 实际使用键（设备、物料、托管；always_free 动作不含当前设备）：排队与环路判定都按它。
        keys = set(ResourceIntervals._requirements(conn,request,ExecutionClaims._job(conn,request.owner_uuid)))
        keys.update(physical_keys(request))
        # 入队时间只在第一次等待时写入：重试、重启都保留原排队位置。
        conn.execute("INSERT INTO workflow_resource_waiter(job_uuid,task_uuid,lock_keys,blockers,enqueued_at) VALUES (?,?,?,?,?) "
                     "ON CONFLICT(job_uuid) DO UPDATE SET lock_keys=excluded.lock_keys,blockers=excluded.blockers",
                     (request.owner_uuid,request.task_uuid,_json(sorted(keys)),_json(sorted(blockers)),utc_now()))
        return False

    @staticmethod
    def reconcile(conn, task_uuid: str) -> None:
        from .resource_intervals import ResourceIntervals, _TERMINAL, _UNRESOLVED
        for loan in conn.execute("SELECT * FROM workflow_resource_loan WHERE borrower_task_uuid=? AND state!='returned'", (task_uuid,)).fetchall():
            admissions = conn.execute("SELECT j.status,j.uncertainty_reason FROM workflow_resource_admission a "
                                      "JOIN workflow_node_job j ON j.uuid=a.job_uuid,json_each(a.ownerships) o "
                                      "WHERE j.workflow_task_uuid=? AND o.value=? AND a.admitted_at>=?",
                                      (task_uuid,loan["lender_owner_uuid"],loan["created_at"])).fetchall()
            abnormal = any(row["status"] in {"failed","canceled","timeout","execution_unknown","intervention_required"} or row["uncertainty_reason"] for row in admissions)
            if abnormal and loan["state"] == "lent":
                conn.execute("UPDATE workflow_resource_loan SET state='retained',version=version+1 WHERE loan_uuid=?", (loan["loan_uuid"],))
                continue
            task = conn.execute("SELECT status,execution_plan FROM workflow_task WHERE uuid=?", (task_uuid,)).fetchone()
            definition = _load(loan["definition"])
            done = not definition and task["status"] in _TERMINAL
            if definition:
                runs = ResourceIntervals._rows(conn, task_uuid)
                members = [runs.get(key) for key in definition["members"]]
                same = ResourceIntervals._generation(_load(task["execution_plan"]),definition.get("parent"),runs) == definition["generation"]
                done = same and all(row is not None and row["status"] in _TERMINAL for row in members)
            if done and loan["state"] == "lent" and not conn.execute("SELECT 1 FROM workflow_resource_use WHERE owner_uuid=?", (loan["lender_owner_uuid"],)).fetchone():
                ResourceLoans._return(conn, loan, "borrowed_interval_completed")
                lender = conn.execute("SELECT task_uuid FROM workflow_resource_interval WHERE owner_uuid=?", (loan["lender_owner_uuid"],)).fetchone()
                ResourceIntervals.reconcile(conn, lender[0])

    @staticmethod
    def _return(conn, loan, reason):
        conn.execute("UPDATE workflow_resource_loan SET state='returned',version=version+1,returned_at=?,return_reason=? WHERE loan_uuid=?",
                     (utc_now(),reason,loan["loan_uuid"]))
        conn.execute("UPDATE workflow_resource_interval SET version=version+1 WHERE owner_uuid=?", (loan["lender_owner_uuid"],))

    @staticmethod
    def confirm_return(conn, loan_uuid, *, expected_version, reason):
        from .resource_intervals import ResourceIntervals, _TERMINAL
        row = conn.execute("SELECT * FROM workflow_resource_loan WHERE loan_uuid=?", (loan_uuid,)).fetchone()
        if not row:
            raise StoreConflict("借用记录不存在")
        if row["state"] == "returned":
            return _record(row)
        if row["version"] != expected_version:
            raise StoreConflict("借用状态已变化，请刷新后重新核实")
        if conn.execute("SELECT 1 FROM workflow_resource_use WHERE owner_uuid=?", (row["lender_owner_uuid"],)).fetchone():
            raise StoreConflict("借用仍有未收敛动作，不能归还")
        definition = _load(row["definition"])
        runs = ResourceIntervals._rows(conn,row["borrower_task_uuid"])
        if definition and any(key not in runs or runs[key]["status"] not in _TERMINAL for key in definition["members"]):
            raise StoreConflict("借用区间仍有待执行步骤，请先完成或取消借用任务")
        ResourceLoans._return(conn,row,reason)
        lender = conn.execute("SELECT task_uuid FROM workflow_resource_interval WHERE owner_uuid=?", (row["lender_owner_uuid"],)).fetchone()
        ResourceIntervals.reconcile(conn,lender[0])
        return _record(conn.execute("SELECT * FROM workflow_resource_loan WHERE loan_uuid=?", (loan_uuid,)).fetchone())

    @staticmethod
    def snapshot(conn, task_uuid=None):
        return [_record(row) for row in conn.execute("SELECT l.*,i.lock_key,i.task_uuid AS lender_task_uuid FROM workflow_resource_loan l "
                    "JOIN workflow_resource_interval i ON i.owner_uuid=l.lender_owner_uuid WHERE l.state!='returned' "
                    + ("AND (i.task_uuid=? OR l.borrower_task_uuid=?) " if task_uuid else "") + "ORDER BY l.created_at,l.loan_uuid",
                    (task_uuid,task_uuid) if task_uuid else ())]
