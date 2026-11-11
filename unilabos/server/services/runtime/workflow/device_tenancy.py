"""装载前预占，成功卸载后释放；异常执行保留原始托管身份。"""
from __future__ import annotations
from uuid import uuid4

from unilabos.protocol.utils.json_codec import encode_json, decode_json_bytes
from unilabos.server.backend.scheduler.resource_keys import device_lock_key, material_lock_key
from .errors import StoreConflict
from .store import utc_now


def _json(value):
    return encode_json(value, sort_keys=True).decode("utf-8")


def _load(value):
    return decode_json_bytes(value.encode("utf-8"))


class DeviceTenancies:
    @staticmethod
    def blockers(conn, request, keys) -> set[str]:
        keys = set(keys)
        if request.current_action:
            keys.add(device_lock_key(request.current_action.device_id))
        transition = request.device_tenancy
        material = transition.material_uuid if transition else None
        blockers = set()
        for row in conn.execute("SELECT * FROM workflow_device_tenancy WHERE state!='released'"):
            if row["device_key"] in keys or material_lock_key(row["material_uuid"]) in keys or row["material_uuid"] == material:
                if row["task_uuid"] != request.task_uuid:
                    blockers.add(row["task_uuid"])
                elif row["state"] == "retained":
                    raise StoreConflict("设备托管处于待核实状态，不能继续执行")
        return blockers

    @staticmethod
    def _source(conn, request):
        transition = request.device_tenancy
        if not transition or not transition.release_device_id:
            return None
        row = conn.execute("SELECT * FROM workflow_device_tenancy WHERE device_key=? AND state!='released'",
                           (device_lock_key(transition.release_device_id),)).fetchone()
        if not row or row["task_uuid"] != request.task_uuid or row["material_uuid"] != transition.material_uuid or row["state"] != "loaded":
            raise StoreConflict("卸载必须匹配本任务已装载的样品和设备")
        return row

    @staticmethod
    def validate_in_place(conn, request) -> None:
        requirement = request.operate_in_place
        if requirement:
            row = conn.execute("SELECT * FROM workflow_device_tenancy WHERE device_key=? AND state!='released'",
                               (device_lock_key(requirement.device_id),)).fetchone()
            if (not row or row["task_uuid"] != request.task_uuid or row["material_uuid"] != requirement.material_uuid
                    or row["state"] != "loaded"):
                raise StoreConflict("原位动作要求样品已装载在当前执行设备中")

    @classmethod
    def prepare(cls, conn, request) -> None:
        cls.validate_in_place(conn, request)
        transition = request.device_tenancy
        if not transition:
            return
        source = cls._source(conn, request)
        acquire_uuid = None
        if transition.acquire_device_id:
            key = device_lock_key(transition.acquire_device_id)
            if conn.execute("SELECT 1 FROM workflow_device_tenancy WHERE device_key=? AND state!='released'", (key,)).fetchone():
                raise StoreConflict("装载目标设备已有托管样品")
            others = conn.execute("SELECT tenancy_uuid FROM workflow_device_tenancy WHERE material_uuid=? AND state!='released'",
                                  (transition.material_uuid,)).fetchall()
            if any(not source or row[0] != source["tenancy_uuid"] for row in others):
                raise StoreConflict("样品已被其他设备托管，转移必须声明原设备卸载")
            acquire_uuid = str(uuid4())
            conn.execute("INSERT INTO workflow_device_tenancy "
                         "(tenancy_uuid,task_uuid,material_uuid,device_key,acquired_job_uuid,state,version,created_at) "
                         "VALUES (?,?,?,?,?,'reserved',1,?)", (acquire_uuid, request.task_uuid, transition.material_uuid,
                                                               key, request.owner_uuid, utc_now()))
        conn.execute("INSERT INTO workflow_tenancy_transition(job_uuid,contract,acquire_uuid,release_uuid,state) "
                     "VALUES (?,?,?,?,'prepared')", (request.owner_uuid, _json(transition.model_dump(mode="json")),
                                                     acquire_uuid, source["tenancy_uuid"] if source else None))

    @classmethod
    def require(cls, conn, request, *, for_start: bool) -> None:
        if for_start:
            cls.validate_in_place(conn, request)
            from .resource_intervals import physical_keys
            if cls.blockers(conn, request, physical_keys(request)):
                raise StoreConflict("设备或样品被其他任务托管")
        transition = request.device_tenancy
        row = conn.execute("SELECT * FROM workflow_tenancy_transition WHERE job_uuid=?", (request.owner_uuid,)).fetchone()
        if not transition:
            if row:
                raise StoreConflict("动作托管合同与准入记录不符")
            return
        if not row or row["contract"] != _json(transition.model_dump(mode="json")):
            raise StoreConflict("动作缺少冻结的设备托管转换")
        if for_start and row["state"] != "prepared":
            raise StoreConflict("托管转换已经结束或需要核实")
        for field, device in (("acquire_uuid", transition.acquire_device_id), ("release_uuid", transition.release_device_id)):
            held = conn.execute("SELECT * FROM workflow_device_tenancy WHERE tenancy_uuid=?", (row[field],)).fetchone()
            if not device:
                if row[field]:
                    raise StoreConflict("设备托管转换清单损坏")
                continue
            if (not held or held["task_uuid"] != request.task_uuid or held["material_uuid"] != transition.material_uuid
                    or held["device_key"] != device_lock_key(device)):
                raise StoreConflict("设备托管身份损坏")
            if for_start and held["state"] != ("reserved" if field == "acquire_uuid" else "loaded"):
                raise StoreConflict("设备托管已变化，开始许可失效")

    @staticmethod
    def reconcile(conn, task_uuid: str) -> None:
        rows = conn.execute("SELECT t.*,j.status,j.uncertainty_reason FROM workflow_tenancy_transition t "
                            "JOIN workflow_node_job j ON j.uuid=t.job_uuid WHERE j.workflow_task_uuid=? "
                            "AND t.state IN ('prepared','retained')", (task_uuid,)).fetchall()
        for row in rows:
            if row["status"] == "succeeded":
                if row["acquire_uuid"]:
                    conn.execute("UPDATE workflow_device_tenancy SET state='loaded',version=version+1 "
                                 "WHERE tenancy_uuid=? AND state IN ('reserved','retained')", (row["acquire_uuid"],))
                if row["release_uuid"]:
                    # 必须使用准入时钉住的身份；迟到结果不能释放后来的新托管。
                    conn.execute("UPDATE workflow_device_tenancy SET state='released',version=version+1,released_at=?,release_reason=? "
                                 "WHERE tenancy_uuid=? AND state!='released'", (utc_now(), 'unloaded:' + row["job_uuid"], row["release_uuid"]))
                conn.execute("UPDATE workflow_tenancy_transition SET state='settled' WHERE job_uuid=?", (row["job_uuid"],))
            elif row["status"] in {"failed","canceled","timeout","skipped","execution_unknown","intervention_required"} or row["uncertainty_reason"]:
                for identity in (row["acquire_uuid"], row["release_uuid"]):
                    conn.execute("UPDATE workflow_device_tenancy SET state='retained',version=version+1 "
                                 "WHERE tenancy_uuid=? AND state IN ('reserved','loaded')", (identity,))
                conn.execute("UPDATE workflow_tenancy_transition SET state='retained' WHERE job_uuid=?", (row["job_uuid"],))

    @staticmethod
    def withdraw(conn, job_uuid: str) -> None:
        row = conn.execute("SELECT * FROM workflow_tenancy_transition WHERE job_uuid=? AND state='prepared'", (job_uuid,)).fetchone()
        if not row:
            return
        conn.execute("DELETE FROM workflow_tenancy_transition WHERE job_uuid=?", (job_uuid,))
        conn.execute("DELETE FROM workflow_device_tenancy WHERE tenancy_uuid=? AND state='reserved'", (row["acquire_uuid"],))

    @staticmethod
    def snapshot(conn, task_uuid: str | None = None) -> list[dict]:
        return [dict(row) for row in conn.execute("SELECT * FROM workflow_device_tenancy WHERE state!='released' "
                    + ("AND task_uuid=? " if task_uuid else "") + "ORDER BY created_at,tenancy_uuid", (task_uuid,) if task_uuid else ())]

    @staticmethod
    def confirm_release(conn, tenancy_uuid: str, expected_version: int, reason: str) -> dict:
        row = conn.execute("SELECT * FROM workflow_device_tenancy WHERE tenancy_uuid=?", (tenancy_uuid,)).fetchone()
        if not row:
            raise StoreConflict("设备托管不存在")
        if row["state"] == "released":
            return dict(row)
        if row["version"] != expected_version:
            raise StoreConflict("设备托管已变化，请刷新后重新核实")
        task = conn.execute("SELECT status FROM workflow_task WHERE uuid=?", (row["task_uuid"],)).fetchone()
        if task[0] not in {"succeeded","failed","canceled","timeout"}:
            raise StoreConflict("请先结束或取消原任务，再确认样品已取走")
        if conn.execute("SELECT 1 FROM workflow_tenancy_transition t JOIN workflow_node_job j ON j.uuid=t.job_uuid "
                        "WHERE (t.acquire_uuid=? OR t.release_uuid=?) AND j.status NOT IN ('succeeded','failed','canceled','timeout','skipped')",
                        (tenancy_uuid,tenancy_uuid)).fetchone():
            raise StoreConflict("托管仍有关联的未收敛动作")
        conn.execute("UPDATE workflow_device_tenancy SET state='released',version=version+1,released_at=?,release_reason=? WHERE tenancy_uuid=?",
                     (utc_now(),reason,tenancy_uuid))
        return dict(conn.execute("SELECT * FROM workflow_device_tenancy WHERE tenancy_uuid=?", (tenancy_uuid,)).fetchone())
