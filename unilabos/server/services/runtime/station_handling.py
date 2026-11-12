"""V1 工站错误处置（``/api/v1/stations/{station_id}/*``）在 dev 运行库上的实现。

模型与 V1 ``workflow/error_control.py`` 一致：

- 每个等待人工决策的失败作业是一项**工站错误**（``workflow_station_error``，由调度器标记
  决策等待时与工作流干预同一事务开出）；阶段 ``DECISION_REQUIRED → MANUAL_HANDLING →
  DECISION_REQUIRED → APPLYING → RESOLVED``。
- **处置会话**（``workflow_error_session``）在选择"进入手动处理"时创建，工站随之 ``PAUSED``：
  普通派发全部停住，只有本会话的手动设备动作（``execution_source=error_manual`` 的单点任务）
  可以执行。完成会话回到最终选择阶段，工站保持暂停，由操作员确认后恢复。
- 每个写操作是一条**控制命令**（``workflow_station_control_command``）：``Idempotency-Key``
  + 请求指纹 + 操作员身份幂等；工站版本 / 错误批次 / 会话版本 / 决定版本做 CAS。
- 取消 / 重试经与 ``/error-decisions`` 相同的放行通道投递给持有失败的执行面（dev 的
  ``abort`` / ``retry``）；投递失败时命令保持 ``PENDING``（``needs_apply``），可经
  ``control-commands/{uuid}/apply`` 用原身份重放。

状态变化发 ``station.error_handling.changed``（``data.station_id``）。
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from typing import Any, Callable, Dict, List, Mapping, Optional
from uuid import uuid4

from unilabos.server.services.runtime.workflow.store import _json, _load, utc_now

logger = logging.getLogger(__name__)

STATION_EVENT = "station.error_handling.changed"
DECISION_ACTIONS = ("cancel_task", "retry_current_node", "enter_manual_handling")
OPTIONS = [
    {"id": "cancel_task", "label": "取消当前任务"},
    {"id": "retry_current_node", "label": "重试当前节点"},
    {"id": "enter_manual_handling", "label": "进入手动处理"},
]
_MANUAL_ACTIVE = ("pending", "running", "canceling")
_TERMINAL_JOB = ("succeeded", "failed", "skipped", "canceled", "timeout")


class StationNotFound(LookupError):
    """工站 / 错误 / 会话 / 命令 / 手动动作不存在（业务码 4001）。"""


class StationConflict(RuntimeError):
    """版本、阶段、幂等或门禁冲突（业务码 3003）。"""


class StationInvalid(ValueError):
    """请求不合法（业务码 1000）。"""


class StationUnavailable(RuntimeError):
    """执行面 / 设备动作目录暂不可用（业务码 5001）。"""


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


# ── 开出 / 收口工站错误（被 interventions 钩子调用，处于调用方事务内） ─────────


def ensure_station(conn: sqlite3.Connection, station_id: str = "local") -> None:
    conn.execute(
        "INSERT OR IGNORE INTO workflow_station_control(station_id, updated_at) VALUES (?, ?)",
        (station_id, utc_now()),
    )


def emit(conn: sqlite3.Connection, station_id: str, task_uuid: Optional[str] = None) -> None:
    from unilabos.server.services.runtime.workflow.store import WorkflowStore

    now = utc_now()
    WorkflowStore._append_event(conn, event=STATION_EVENT, data={"station_id": station_id}, now=now)
    if task_uuid:
        WorkflowStore._append_event(conn, event="workflow.task.changed",
                                    data={"workflow_task_uuid": task_uuid}, now=now)


def open_station_error(
    conn: sqlite3.Connection, job: sqlite3.Row, report: Mapping[str, Any], intervention_uuid: Optional[str],
    now: str, station_id: str = "local",
) -> None:
    decision_id = str(report.get("decision_id") or "").strip()
    if not decision_id:
        return
    ensure_station(conn, station_id)
    if conn.execute("SELECT 1 FROM workflow_station_error WHERE decision_id=?", (decision_id,)).fetchone():
        return
    revision = conn.execute(
        "SELECT COALESCE(MAX(revision), 0) + 1 FROM workflow_station_error WHERE workflow_node_job_uuid=?",
        (job["uuid"],),
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO workflow_station_error(
            decision_id, station_id, intervention_uuid, workflow_task_uuid, workflow_node_job_uuid, revision,
            stage, status, meta_data, options, opened_at, update_time
        ) VALUES (?, ?, ?, ?, ?, ?, 'DECISION_REQUIRED', 'open', ?, ?, ?, ?)
        """,
        (
            decision_id, station_id, intervention_uuid, job["workflow_task_uuid"], job["uuid"], revision,
            _json({
                "message": str(report.get("error_message") or report.get("exception_type") or "设备动作异常"),
                "source_node_id": job["workflow_node_uuid"],
                "device_id": str(report.get("device_id") or ""),
                "action_name": str(report.get("action_name") or report.get("action") or ""),
            }),
            _json(OPTIONS), now, now,
        ),
    )
    conn.execute(
        "UPDATE workflow_station_control SET error_epoch=error_epoch+1, version=version+1, updated_at=? "
        "WHERE station_id=?",
        (now, station_id),
    )
    emit(conn, station_id, job["workflow_task_uuid"])


def resolve_station_errors(conn: sqlite3.Connection, job_uuid: str, now: str, *, reason: str) -> None:
    """作业已恢复或终态：仍在等待决定 / 手动处理 / 应用中的错误收口，活动会话随之关闭。"""

    rows = conn.execute(
        "SELECT * FROM workflow_station_error WHERE workflow_node_job_uuid=? AND stage!='RESOLVED'",
        (job_uuid,),
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE workflow_station_error SET stage='RESOLVED', status=CASE WHEN status='open' THEN 'superseded' "
            "ELSE status END, revision=revision+1, resolved_at=?, update_time=?, "
            "meta_data=json_set(meta_data, '$.resolved_by', ?) WHERE decision_id=?",
            (now, now, reason, row["decision_id"]),
        )
        if row["session_id"]:
            conn.execute(
                "UPDATE workflow_error_session SET stage='RESOLVED', version=version+1, resolved_at=? "
                "WHERE session_id=? AND stage!='RESOLVED'",
                (now, row["session_id"]),
            )
            conn.execute(
                "UPDATE workflow_station_control SET active_session_id=NULL, version=version+1, updated_at=? "
                "WHERE station_id=? AND active_session_id=?",
                (now, row["station_id"], row["session_id"]),
            )
        emit(conn, row["station_id"], row["workflow_task_uuid"])


def station_paused(conn: sqlite3.Connection, station_id: str = "local") -> bool:
    row = conn.execute("SELECT mode FROM workflow_station_control WHERE station_id=?", (station_id,)).fetchone()
    return row is not None and row["mode"] == "PAUSED"


# ── 工站处置服务 ──────────────────────────────────────────────────────


class StationHandling:
    """一个工站（默认 ``local``）的错误处置权威。

    依赖通过可替换的回调注入（测试替身 / 生产装配）：
    ``deliver(decision_id, payload) -> bool`` 投递取消 / 重试；``action_catalog(device_id)`` 返回
    设备动作目录；``materials()`` 返回 MaterialsService（库存读取与更正）。
    """

    def __init__(
        self,
        workflow: Any,
        *,
        station_id: str = "local",
        deliver: Optional[Callable[[str, Dict[str, Any]], bool]] = None,
        action_catalog: Optional[Callable[[str], List[Dict[str, Any]]]] = None,
        materials: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.workflow = workflow
        self.station_id = station_id
        self._deliver = deliver
        self._action_catalog = action_catalog
        self._materials = materials
        with workflow.transaction() as conn:
            ensure_station(conn, station_id)

    # 读取 ---------------------------------------------------------------

    def _decision(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "decision_id": row["decision_id"],
            "decision_version": int(row["revision"]),
            "revision": int(row["revision"]),
            "intervention_uuid": row["intervention_uuid"],
            "source_task_uuid": row["workflow_task_uuid"],
            "source_job_uuid": row["workflow_node_job_uuid"],
            "workflow_task_uuid": row["workflow_task_uuid"],
            "workflow_node_job_uuid": row["workflow_node_job_uuid"],
            "stage": row["stage"],
            "status": row["status"],
            "session_id": row["session_id"],
            "initial_action": row["initial_action"],
            "final_action": row["final_action"],
            "options": _load(row["options"], []),
            "meta_data": _load(row["meta_data"], {}),
            "opened_at": row["opened_at"],
            "decided_at": row["decided_at"],
            "resolved_at": row["resolved_at"],
        }

    @staticmethod
    def _session(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def _manual_rows(self, conn: sqlite3.Connection, session_id: Optional[str] = None) -> List[sqlite3.Row]:
        return conn.execute(
            """
            SELECT task.uuid AS task_uuid, task.status AS task_status, task.meta_data AS task_meta,
                   task.trace_context, job.*
            FROM workflow_task AS task
            JOIN workflow_node_job AS job ON job.workflow_task_uuid = task.uuid AND job.deleted_at IS NULL
            WHERE task.deleted_at IS NULL AND json_extract(task.meta_data, '$.execution_source') = 'error_manual'
              AND json_extract(task.meta_data, '$.station_id') = ?
              AND (? IS NULL OR json_extract(task.meta_data, '$.session_id') = ?)
            ORDER BY job.create_time DESC
            """,
            (self.station_id, session_id, session_id),
        ).fetchall()

    @staticmethod
    def _review_version(row: sqlite3.Row) -> str:
        material = f"{row['uuid']}|{row['status']}|{row['finished_at'] or ''}|{row['return_info']}|{row['error_info']}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]

    def _manual_job(self, row: sqlite3.Row) -> Dict[str, Any]:
        meta = _load(row["task_meta"], {})
        control = _load(row["control_data"], {})
        review = control.get("manual_review")
        return {
            "uuid": row["uuid"],
            "workflow_task_uuid": row["task_uuid"],
            "status": row["status"],
            "edge_command_uuid": row["edge_command_uuid"] or meta.get("manual_command_id"),
            "start_state": "START_COMMITTED" if row["started_at"] else "NOT_STARTED",
            "uncertainty_reason": row["uncertainty_reason"],
            "review_version": self._review_version(row),
            "result_reviewed": bool(review and review.get("review_version") == self._review_version(row)),
            "manual_review": review,
            "execution_source": "error_manual",
            "action_name": meta.get("action_name"),
            "device_id": meta.get("device_id"),
            "control_data": {**control, "dispatch_payload": {"action": meta.get("action_name"),
                                                             "device_id": meta.get("device_id")}},
            "param": _load(row["param"], {}),
            "return_info": _load(row["return_info"], {}),
            "error_info": _load(row["error_info"], []),
            "feedback_data": _load(row["feedback_data"], {}),
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "meta_data": {
                "session_id": meta.get("session_id"),
                "source_task_uuid": meta.get("source_task_uuid"),
                "source_job_uuid": meta.get("source_job_uuid"),
                "source_node_id": meta.get("source_node_id"),
                "manual_command_id": meta.get("manual_command_id"),
            },
        }

    def _snapshot(self, conn: sqlite3.Connection) -> Dict[str, Any]:
        row = conn.execute("SELECT * FROM workflow_station_control WHERE station_id=?", (self.station_id,)).fetchone()
        station = dict(row)
        station["station_version"] = station.pop("version")
        station["snapshot_id"] = f"{self.station_id}:{station['station_version']}:{station['error_epoch']}"
        station["active_session"] = None
        if station["active_session_id"]:
            session = conn.execute("SELECT * FROM workflow_error_session WHERE session_id=?",
                                   (station["active_session_id"],)).fetchone()
            station["active_session"] = self._session(session) if session else None
        station["errors"] = [
            self._decision(item)
            for item in conn.execute(
                "SELECT * FROM workflow_station_error WHERE station_id=? AND stage!='RESOLVED' "
                "ORDER BY opened_at, decision_id",
                (self.station_id,),
            ).fetchall()
        ]
        station["manual_actions"] = [self._manual_job(item) for item in self._manual_rows(conn)][:100]
        commands = []
        for item in conn.execute(
            "SELECT command_uuid, operation, status FROM workflow_station_control_command WHERE station_id=? "
            "ORDER BY created_at DESC LIMIT 100",
            (self.station_id,),
        ).fetchall():
            commands.append({"command_uuid": item["command_uuid"], "operation": item["operation"],
                             "status": item["status"], "needs_apply": item["status"] == "PENDING"})
        station["control_commands"] = commands
        station["event_cursor"] = conn.execute("SELECT COALESCE(MAX(sequence), 0) FROM frontend_event").fetchone()[0]
        return station

    def snapshot(self) -> Dict[str, Any]:
        with self.workflow.write_lock:
            return self._snapshot(self.workflow.connection)

    def _read_error(self, conn: sqlite3.Connection, decision_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM workflow_station_error WHERE decision_id=? AND station_id=?",
                           (decision_id, self.station_id)).fetchone()
        if row is None:
            raise StationNotFound("错误决定不存在")
        return row

    def _read_session(self, conn: sqlite3.Connection, session_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM workflow_error_session WHERE session_id=? AND station_id=?",
                           (session_id, self.station_id)).fetchone()
        if row is None:
            raise StationNotFound("处置会话不存在")
        return row

    def get_session(self, session_id: str) -> Dict[str, Any]:
        with self.workflow.write_lock:
            conn = self.workflow.connection
            session = self._session(self._read_session(conn, session_id))
            session["decision"] = self._decision(self._read_error(conn, session["decision_id"]))
            session["manual_actions"] = [self._manual_job(row) for row in self._manual_rows(conn, session_id)]
        return session

    # 控制命令 -------------------------------------------------------------

    def _command(self, conn: sqlite3.Connection, row: sqlite3.Row) -> Dict[str, Any]:
        return {"command_uuid": row["command_uuid"], "status": row["status"], "operation": row["operation"],
                "result": _load(row["snapshot_json"], {}), "station": self._snapshot(conn)}

    def get_command(self, command_uuid: str) -> Dict[str, Any]:
        with self.workflow.write_lock:
            conn = self.workflow.connection
            row = conn.execute("SELECT * FROM workflow_station_control_command WHERE command_uuid=? AND station_id=?",
                               (command_uuid, self.station_id)).fetchone()
            if row is None:
                raise StationNotFound("控制命令不存在")
            return self._command(conn, row)

    def _existing(self, conn: sqlite3.Connection, operation: str, key: str, request: Dict[str, Any],
                  operator: str) -> Optional[Dict[str, Any]]:
        if not key.strip() or len(key) > 255 or not operator.strip():
            raise StationInvalid("缺少有效幂等键或操作者")
        row = conn.execute(
            "SELECT * FROM workflow_station_control_command WHERE station_id=? AND operation=? AND idempotency_key=?",
            (self.station_id, operation, key),
        ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != _hash(request) or row["operator_id"] != operator:
            raise StationConflict("idempotency_conflict")
        return self._command(conn, row)

    def _check_station(self, conn: sqlite3.Connection, request: Mapping[str, Any]) -> Dict[str, Any]:
        station = self._snapshot(conn)
        if request.get("expected_station_version") != station["station_version"]:
            raise StationConflict("station_version_conflict")
        if "observed_error_epoch" in request and request["observed_error_epoch"] is not None and \
                request["observed_error_epoch"] != station["error_epoch"]:
            raise StationConflict("station_version_conflict")
        return station

    @staticmethod
    def _confirm_resume(station: Mapping[str, Any], request: Mapping[str, Any]) -> None:
        if request.get("operator_confirmed") is not True or request.get("confirm_snapshot_id") != station["snapshot_id"]:
            raise StationConflict("operator_confirmation_required")

    def _manual_unfinished(self, conn: sqlite3.Connection, session_id: Optional[str] = None,
                           *, require_review: bool = True) -> None:
        for row in self._manual_rows(conn, session_id):
            if row["task_status"] in _MANUAL_ACTIVE or row["status"] not in _TERMINAL_JOB:
                raise StationConflict("manual_action_in_progress")
            if require_review and (row["status"] != "succeeded" or row["uncertainty_reason"]):
                review = _load(row["control_data"], {}).get("manual_review")
                if not review or review.get("review_version") != self._review_version(row):
                    raise StationConflict("manual_action_review_required")

    def _save(self, conn: sqlite3.Connection, *, operation: str, key: str, request: Dict[str, Any],
              operator: str, result: Dict[str, Any], applied: bool) -> Dict[str, Any]:
        identity, now = str(uuid4()), utc_now()
        conn.execute(
            "UPDATE workflow_station_control SET version=version+1, last_control_command_uuid=?, updated_at=? "
            "WHERE station_id=?",
            (identity, now, self.station_id),
        )
        station = self._snapshot(conn)
        conn.execute(
            """
            INSERT INTO workflow_station_control_command(
                command_uuid, station_id, operation, idempotency_key, request_hash, expected_version,
                applied_version, error_epoch, operator_id, operator_confirmed, snapshot_json, status,
                created_at, applied_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (identity, self.station_id, operation, key, _hash(request),
             int(request.get("expected_station_version") or station["station_version"] - 1),
             station["station_version"], station["error_epoch"], operator,
             int(request.get("operator_confirmed") is True), _json(result),
             "APPLIED" if applied else "PENDING", now, now if applied else None),
        )
        emit(conn, self.station_id, result.get("source_task_uuid"))
        row = conn.execute("SELECT * FROM workflow_station_control_command WHERE command_uuid=?", (identity,)).fetchone()
        return self._command(conn, row)

    def _finish(self, command_uuid: str, *, status: str, result_update: Dict[str, Any]) -> None:
        with self.workflow.transaction() as conn:
            row = conn.execute("SELECT * FROM workflow_station_control_command WHERE command_uuid=?",
                               (command_uuid,)).fetchone()
            result = {**_load(row["snapshot_json"], {}), **result_update}
            conn.execute(
                "UPDATE workflow_station_control_command SET status=?, snapshot_json=?, applied_at=? "
                "WHERE command_uuid=?",
                (status, _json(result), utc_now() if status == "APPLIED" else None, command_uuid),
            )
            emit(conn, self.station_id, result.get("source_task_uuid"))

    def _wake_all(self) -> None:
        controller = getattr(self.workflow, "_task_controller", None)
        if controller is None:
            return
        with self.workflow.write_lock:
            tasks = [row[0] for row in self.workflow.connection.execute(
                "SELECT uuid FROM workflow_task WHERE deleted_at IS NULL AND status IN ('pending', 'running')"
            ).fetchall()]
        for task_uuid in tasks:
            try:
                controller(task_uuid)
            except Exception:  # noqa: BLE001 - 恢复派发是尽力唤醒，调度器下一轮会重新判定
                logger.exception("工站恢复后唤醒任务 %s 失败", task_uuid)

    # 决定 -----------------------------------------------------------------

    def decide(self, decision_id: str, request: Dict[str, Any], *, idempotency_key: str,
               operator_id: str) -> Dict[str, Any]:
        operation = f"decision:{decision_id}"
        with self.workflow.transaction() as conn:
            existing = self._existing(conn, operation, idempotency_key, request, operator_id)
            if existing is not None:
                return existing
            station = self._check_station(conn, request)
            error = self._read_error(conn, decision_id)
            if request.get("expected_decision_version") != error["revision"] or error["stage"] != "DECISION_REQUIRED":
                raise StationConflict("decision_conflict")
            action = request.get("action")
            if action not in DECISION_ACTIONS:
                raise StationInvalid("不支持的错误处理动作")
            result: Dict[str, Any] = {
                "decision_id": decision_id, "source_task_uuid": error["workflow_task_uuid"],
                "source_job_uuid": error["workflow_node_job_uuid"], "action": action, "application_started": False,
            }
            now = utc_now()
            if action == "enter_manual_handling":
                if station["active_session_id"] or error["session_id"]:
                    raise StationConflict("active_session_conflict")
                session_id = str(uuid4())
                conn.execute(
                    "INSERT INTO workflow_error_session(session_id, station_id, decision_id, stage, created_by, "
                    "created_at) VALUES (?, ?, ?, 'MANUAL_HANDLING', ?, ?)",
                    (session_id, self.station_id, decision_id, operator_id, now),
                )
                conn.execute(
                    "UPDATE workflow_station_error SET stage='MANUAL_HANDLING', initial_action=?, session_id=?, "
                    "revision=revision+1, update_time=? WHERE decision_id=?",
                    (action, session_id, now, decision_id),
                )
                conn.execute("UPDATE workflow_station_control SET mode='PAUSED', active_session_id=? WHERE station_id=?",
                             (session_id, self.station_id))
                result["session_id"] = session_id
                return self._save(conn, operation=operation, key=idempotency_key, request=request,
                                  operator=operator_id, result=result, applied=True)
            if error["session_id"]:
                session = self._read_session(conn, error["session_id"])
                if (request.get("session_id") != session["session_id"]
                        or request.get("expected_session_version") != session["version"]
                        or session["stage"] != "DECISION_REQUIRED"):
                    raise StationConflict("session_version_conflict")
                self._manual_unfinished(conn, session["session_id"])
            elif station["active_session_id"]:
                raise StationConflict("active_session_conflict")
            if request.get("resume_requested", False):
                self._confirm_resume(station, request)
                self._manual_unfinished(conn, require_review=False)
            result["resume_requested"] = request.get("resume_requested") is True
            result["reason"] = str(request.get("reason") or "")
            conn.execute(
                "UPDATE workflow_station_error SET stage='APPLYING', status='selected', "
                "initial_action=COALESCE(initial_action, ?), final_action=?, revision=revision+1, update_time=?, "
                "decided_at=? WHERE decision_id=?",
                (action, action, now, now, decision_id),
            )
            accepted = self._save(conn, operation=operation, key=idempotency_key, request=request,
                                  operator=operator_id, result=result, applied=False)
        return self.apply_command(accepted["command_uuid"])

    def apply_command(self, command_uuid: str) -> Dict[str, Any]:
        """应用已受理但未完成的取消 / 重试（只复用保存的身份，不新建选择）。"""

        command = self.get_command(command_uuid)
        if command["status"] != "PENDING":
            return command
        result = command["result"]
        decision_id = result.get("decision_id")
        action = result.get("action")
        if not command["operation"].startswith("decision:") or action not in {"cancel_task", "retry_current_node"}:
            return command
        payload = {"action": "abort" if action == "cancel_task" else "retry",
                   "reason": result.get("reason") or f"工站错误处置：{action}",
                   "operator_id": "station:" + self.station_id}
        error_text = None
        try:
            if self._deliver is None:
                raise StationUnavailable("错误处置投递通道未装配")
            if not self._deliver(str(decision_id), payload):
                error_text = "执行面没有接受这项决定（决策已不在等待或执行面离线）"
        except Exception as exc:  # noqa: BLE001 - 命令保持 PENDING，可用原身份重放
            error_text = str(exc) or type(exc).__name__
        if error_text is not None:
            response = self.get_command(command_uuid)
            response["application_error"] = error_text
            return response
        if action == "cancel_task":
            try:
                self.workflow.command_workflow_task(
                    result["source_task_uuid"], command_type="cancel",
                    idempotency_key=f"station-cancel:{command_uuid}",
                    description="工站错误处置：取消当前任务", meta_data={"station_command_uuid": command_uuid},
                )
            except Exception:  # noqa: BLE001 - 决策已放行，任务按失败收口
                logger.exception("工站取消任务 %s 失败", result["source_task_uuid"])
        now = utc_now()
        with self.workflow.transaction() as conn:
            error = self._read_error(conn, str(decision_id))
            if error["stage"] != "RESOLVED":
                conn.execute(
                    "UPDATE workflow_station_error SET stage='RESOLVED', revision=revision+1, resolved_at=?, "
                    "update_time=? WHERE decision_id=?",
                    (now, now, decision_id),
                )
            if error["session_id"]:
                conn.execute("UPDATE workflow_error_session SET stage='RESOLVED', version=version+1, resolved_at=? "
                             "WHERE session_id=? AND stage!='RESOLVED'", (now, error["session_id"]))
                conn.execute("UPDATE workflow_station_control SET active_session_id=NULL WHERE station_id=? "
                             "AND active_session_id=?", (self.station_id, error["session_id"]))
            if result.get("resume_requested"):
                conn.execute("UPDATE workflow_station_control SET mode='RUNNING' WHERE station_id=?", (self.station_id,))
            conn.execute(
                "UPDATE workflow_station_control_command SET status='APPLIED', applied_at=?, snapshot_json=? "
                "WHERE command_uuid=?",
                (now, _json({**result, "application_started": True}), command_uuid),
            )
            conn.execute("UPDATE workflow_station_control SET version=version+1, updated_at=? WHERE station_id=?",
                         (now, self.station_id))
            emit(conn, self.station_id, result.get("source_task_uuid"))
        if result.get("resume_requested"):
            self._wake_all()
        return self.get_command(command_uuid)

    # 会话 -----------------------------------------------------------------

    def _session_transition(self, session_id: str, request: Dict[str, Any], *, idempotency_key: str,
                            operator_id: str, returning: bool) -> Dict[str, Any]:
        operation = f"{'return' if returning else 'complete'}:{session_id}"
        with self.workflow.transaction() as conn:
            existing = self._existing(conn, operation, idempotency_key, request, operator_id)
            if existing is not None:
                return existing
            station = self._check_station(conn, request)
            session = self._read_session(conn, session_id)
            expected = "DECISION_REQUIRED" if returning else "MANUAL_HANDLING"
            if (station["active_session_id"] != session_id or session["stage"] != expected
                    or request.get("expected_session_version") != session["version"]):
                raise StationConflict("session_version_conflict")
            error = self._read_error(conn, session["decision_id"])
            if error["stage"] == "APPLYING":
                raise StationConflict("decision_conflict")
            if not returning:
                self._manual_unfinished(conn, session_id)
            target = "MANUAL_HANDLING" if returning else "DECISION_REQUIRED"
            now = utc_now()
            conn.execute(
                "UPDATE workflow_error_session SET stage=?, version=version+1, summary=?, completed_by=?, "
                "completed_at=? WHERE session_id=?",
                (target, request.get("summary", session["summary"]), operator_id, now, session_id),
            )
            conn.execute("UPDATE workflow_station_error SET stage=?, revision=revision+1, update_time=? "
                         "WHERE decision_id=?", (target, now, session["decision_id"]))
            return self._save(conn, operation=operation, key=idempotency_key, request=request, operator=operator_id,
                              result={"session_id": session_id, "source_task_uuid": error["workflow_task_uuid"]},
                              applied=True)

    def complete(self, session_id: str, request: Dict[str, Any], *, idempotency_key: str,
                 operator_id: str) -> Dict[str, Any]:
        """关闭新手动提交并返回同一会话的最终选择阶段，工站保持暂停。"""

        return self._session_transition(session_id, request, idempotency_key=idempotency_key,
                                        operator_id=operator_id, returning=False)

    def return_to_manual(self, session_id: str, request: Dict[str, Any], *, idempotency_key: str,
                         operator_id: str) -> Dict[str, Any]:
        """最终决定前返回手动处理。"""

        return self._session_transition(session_id, request, idempotency_key=idempotency_key,
                                        operator_id=operator_id, returning=True)

    def resume(self, request: Dict[str, Any], *, idempotency_key: str, operator_id: str) -> Dict[str, Any]:
        with self.workflow.transaction() as conn:
            existing = self._existing(conn, "resume", idempotency_key, request, operator_id)
            if existing is not None:
                return existing
            station = self._check_station(conn, request)
            self._confirm_resume(station, request)
            if station["active_session_id"]:
                raise StationConflict("active_session_conflict")
            self._manual_unfinished(conn, require_review=False)
            conn.execute("UPDATE workflow_station_control SET mode='RUNNING' WHERE station_id=?", (self.station_id,))
            result = self._save(conn, operation="resume", key=idempotency_key, request=request,
                                operator=operator_id, result={"resume_requested": True}, applied=True)
        self._wake_all()
        return result

    # 手动设备动作 ---------------------------------------------------------

    def manual_actions(self, session_id: str, device_id: str) -> List[Dict[str, Any]]:
        with self.workflow.write_lock:
            self._read_session(self.workflow.connection, session_id)
        if self._action_catalog is None:
            raise StationUnavailable("设备动作目录暂不可用")
        return self._action_catalog(device_id)

    def execute_manual(self, session_id: str, request: Dict[str, Any], *, idempotency_key: str,
                       operator_id: str) -> Dict[str, Any]:
        operation = f"manual:{session_id}"
        catalog = None
        with self.workflow.transaction() as conn:
            existing = self._existing(conn, operation, idempotency_key, request, operator_id)
            if existing is not None:
                return {**existing, "manual_action": self.manual_action(existing["result"]["manual_command_id"])}
            station = self._check_station(conn, request)
            session = self._read_session(conn, session_id)
            if (station["active_session_id"] != session_id or session["stage"] != "MANUAL_HANDLING"
                    or request.get("expected_session_version") != session["version"]):
                raise StationConflict("session_version_conflict")
            error = self._read_error(conn, session["decision_id"])
        if self._action_catalog is None:
            raise StationUnavailable("设备动作目录暂不可用")
        catalog = {item["action_id"]: item for item in self._action_catalog(str(request.get("device_id") or ""))}
        action = catalog.get(str(request.get("action_id") or ""))
        if action is None:
            raise StationInvalid("设备没有该动作")
        if action.get("action_version") != request.get("action_version"):
            raise StationConflict("action_version_conflict")
        command_id = str(uuid4())
        meta = {
            "execution_source": "error_manual", "station_id": self.station_id, "session_id": session_id,
            "source_task_uuid": error["workflow_task_uuid"], "source_job_uuid": error["workflow_node_job_uuid"],
            "source_node_id": _load(error["meta_data"], {}).get("source_node_id"),
            "manual_command_id": command_id, "device_id": request["device_id"], "action_name": action["name"],
            "operator_id": operator_id,
        }
        task = self.workflow.create_ad_hoc_device_action_task(
            device_id=str(request["device_id"]), action_name=str(action["name"]),
            action_type=str(action.get("action_type") or ""), param=dict(request.get("parameters") or {}),
            # 工站已暂停、失败作业仍持有设备动作锁：会话内的手动动作是操作员显式接管设备，免动作锁
            execution_policy={"always_free": True},
            idempotency_key=f"station-manual:{command_id}", description="工站手动处理动作", meta_data=meta,
        )
        with self.workflow.transaction() as conn:
            existing = self._existing(conn, operation, idempotency_key, request, operator_id)
            if existing is not None:
                return {**existing, "manual_action": self.manual_action(existing["result"]["manual_command_id"])}
            accepted = self._save(conn, operation=operation, key=idempotency_key, request=request,
                                  operator=operator_id, applied=True, result={
                                      "manual_command_id": command_id, "execution_task_uuid": task["uuid"],
                                      "source_task_uuid": error["workflow_task_uuid"], "session_id": session_id,
                                  })
        return {**accepted, "dispatch_error": None, "manual_action": self.manual_action(command_id)}

    def _manual_row(self, conn: sqlite3.Connection, command_id: str) -> sqlite3.Row:
        for row in self._manual_rows(conn):
            if _load(row["task_meta"], {}).get("manual_command_id") == command_id:
                return row
        raise StationNotFound("手动动作不存在")

    def manual_action(self, command_id: str) -> Dict[str, Any]:
        with self.workflow.write_lock:
            row = self._manual_row(self.workflow.connection, command_id)
            job = self._manual_job(row)
        return {"execution_job": job, "execution_task": {"uuid": row["task_uuid"], "status": row["task_status"],
                                                         "trace_context": _load(row["trace_context"], {})},
                "source_task_uuid": job["meta_data"]["source_task_uuid"]}

    def stop_manual(self, command_id: str, request: Dict[str, Any], *, idempotency_key: str,
                    operator_id: str) -> Dict[str, Any]:
        operation = f"stop:{command_id}"
        with self.workflow.transaction() as conn:
            existing = self._existing(conn, operation, idempotency_key, request, operator_id)
            if existing is not None:
                return {**existing, "manual_action": self.manual_action(command_id)}
            row = self._manual_row(conn, command_id)
        cancel = self.workflow.command_workflow_task(
            row["task_uuid"], command_type="cancel", idempotency_key=f"station-stop:{command_id}",
            description=str(request.get("reason") or "用户停止手动动作"), meta_data={"manual_command_id": command_id},
        )
        with self.workflow.transaction() as conn:
            existing = self._existing(conn, operation, idempotency_key, request, operator_id)
            if existing is None:
                existing = self._save(conn, operation=operation, key=idempotency_key, request=request,
                                      operator=operator_id, applied=True, result={
                                          "manual_command_id": command_id, "execution_task_uuid": row["task_uuid"],
                                          "cancel_status": cancel["status"], "cancel": cancel.get("result", {}),
                                      })
        return {**existing, "manual_action": self.manual_action(command_id)}

    def _review(self, conn: sqlite3.Connection, row: sqlite3.Row, request: Mapping[str, Any], operator_id: str) -> None:
        if row["task_status"] in _MANUAL_ACTIVE or row["status"] not in _TERMINAL_JOB:
            raise StationConflict("manual_action_in_progress")
        if request.get("operator_confirmed") is not True:
            raise StationConflict("operator_confirmation_required")
        if request.get("expected_review_version") != self._review_version(row):
            raise StationConflict("review_version_conflict")
        control = _load(row["control_data"], {})
        control["manual_review"] = {"review_version": self._review_version(row), "operator_id": operator_id,
                                    "reviewed_at": utc_now(), "reason": str(request.get("reason") or "")}
        conn.execute("UPDATE workflow_node_job SET control_data=?, update_time=? WHERE uuid=?",
                     (_json(control), utc_now(), row["uuid"]))

    def review_manual(self, command_id: str, request: Dict[str, Any], *, idempotency_key: str,
                      operator_id: str) -> Dict[str, Any]:
        operation = f"review:{command_id}"
        with self.workflow.transaction() as conn:
            existing = self._existing(conn, operation, idempotency_key, request, operator_id)
            if existing is not None:
                return existing
            station = self._check_station(conn, request)
            session = self._read_session(conn, str(request.get("session_id") or ""))
            if request.get("expected_session_version") != session["version"] or \
                    station["active_session_id"] != session["session_id"]:
                raise StationConflict("session_version_conflict")
            row = self._manual_row(conn, command_id)
            if _load(row["task_meta"], {}).get("session_id") != session["session_id"]:
                raise StationConflict("session_version_conflict")
            self._review(conn, row, request, operator_id)
            return self._save(conn, operation=operation, key=idempotency_key, request=request, operator=operator_id,
                              applied=True, result={"manual_command_id": command_id, "job_uuid": row["uuid"]})

    def _job_row(self, conn: sqlite3.Connection, job_uuid: str) -> sqlite3.Row:
        for row in self._manual_rows(conn):
            if row["uuid"] == job_uuid:
                return row
        raise StationNotFound("手动动作作业不存在")

    def manual_job_review_context(self, job_uuid: str) -> Dict[str, Any]:
        with self.workflow.write_lock:
            conn = self.workflow.connection
            row = self._job_row(conn, job_uuid)
            job = self._manual_job(row)
            session = self._read_session(conn, job["meta_data"]["session_id"])
        return {"station_id": self.station_id, "session_id": session["session_id"], "session_stage": session["stage"],
                "job": job, "review_version": job["review_version"], "result_reviewed": job["result_reviewed"],
                "manual_review": job["manual_review"]}

    def review_manual_job(self, job_uuid: str, request: Dict[str, Any], *, idempotency_key: str,
                          operator_id: str) -> Dict[str, Any]:
        operation = f"job-review:{job_uuid}"
        with self.workflow.transaction() as conn:
            existing = self._existing(conn, operation, idempotency_key, request, operator_id)
            if existing is None:
                row = self._job_row(conn, job_uuid)
                self._review(conn, row, request, operator_id)
                existing = self._save(conn, operation=operation, key=idempotency_key, request=request,
                                      operator=operator_id, applied=True, result={"job_uuid": job_uuid})
        return {**existing, "review_context": self.manual_job_review_context(job_uuid)}

    # 库存与资源 -----------------------------------------------------------

    def _materials_service(self) -> Any:
        service = self._materials() if self._materials is not None else None
        if service is None:
            raise StationUnavailable("物料权威暂不可用")
        return service

    def inventory(self, session_id: str) -> Dict[str, Any]:
        with self.workflow.write_lock:
            self._read_session(self.workflow.connection, session_id)
        materials = self._materials_service()
        instances, sites = [], []
        for aggregate in materials.list_materials():
            identity = aggregate.material
            instances.append({"edge_uuid": identity.material_uuid, "name": identity.name,
                              "parent_uuid": getattr(identity, "parent_material_uuid", None) or "",
                              "version": aggregate.position_version})
            for site in aggregate.sites:
                sites.append({"uuid": site.site_uuid, "material_uuid": site.owner_material_uuid,
                              "name": getattr(site, "label", None) or str(site.site_index),
                              "occupied_material_uuid": getattr(site, "occupant_material_uuid", None),
                              "update_time": str(site.updated_at_ms), "version": site.version})
        return {"instances": instances, "sites": sites}

    def inventory_correction(self, session_id: str, request: Dict[str, Any], *, idempotency_key: str,
                             operator_id: str) -> Dict[str, Any]:
        """登记人工确认的上下料与库位更正：经物料权威的 ``move_material`` 逐项应用。"""

        from unilabos.protocol.materials import ACTOR_HUMAN, InventoryMutation, MaterialMove

        operation = f"inventory:{session_id}"
        with self.workflow.transaction() as conn:
            existing = self._existing(conn, operation, idempotency_key, request, operator_id)
            if existing is not None:
                return existing
            station = self._check_station(conn, request)
            session = self._read_session(conn, session_id)
            if (station["active_session_id"] != session_id or session["stage"] != "MANUAL_HANDLING"
                    or request.get("expected_session_version") != session["version"]):
                raise StationConflict("session_version_conflict")
            error = self._read_error(conn, session["decision_id"])
        materials = self._materials_service()
        expected = request.get("expected_inventory_versions") or {}
        applied = []
        for index, change in enumerate(request.get("changes") or []):
            material_uuid = str(change.get("material_uuid") or "")
            current = materials.get_material(material_uuid)
            if material_uuid in expected and str(expected[material_uuid]) != str(current.position_version):
                raise StationConflict("inventory_version_conflict")
            moved = materials.move_material(
                InventoryMutation(command_uuid=f"station-inventory:{idempotency_key}:{index}",
                                  effect_key=f"station-inventory:{idempotency_key}:{index}",
                                  operation="move_material", actor_type=ACTOR_HUMAN, actor_uuid=operator_id),
                MaterialMove(material_uuid=material_uuid, destination_site_uuid=change.get("site_uuid") or None,
                             parent_material_uuid=change.get("parent_uuid") or None),
            )
            applied.append({"material_uuid": material_uuid, "result": getattr(moved, "status", "applied")})
        with self.workflow.transaction() as conn:
            existing = self._existing(conn, operation, idempotency_key, request, operator_id)
            if existing is not None:
                return existing
            return self._save(conn, operation=operation, key=idempotency_key, request=request, operator=operator_id,
                              applied=True, result={"session_id": session_id, "changes": applied,
                                                    "source_task_uuid": error["workflow_task_uuid"],
                                                    "reason": request.get("reason")})

    def occupancies(self) -> List[Dict[str, Any]]:
        """当前逻辑资源占用：执行锁（Claim + 租约）、区间占用与设备托管。"""

        from unilabos.server.services.runtime.workflow.task_lifecycle import TaskObservability

        with self.workflow.write_lock:
            conn = self.workflow.connection
            result: List[Dict[str, Any]] = []
            for row in conn.execute(
                "SELECT claim.*, lease.lock_key, lease.token FROM workflow_execution_claim AS claim "
                "JOIN workflow_execution_lease AS lease ON lease.claim_uuid = claim.claim_uuid "
                "WHERE claim.state != 'released' ORDER BY claim.acquired_at"
            ).fetchall():
                result.append({
                    "uuid": TaskObservability.lease_uuid(row["claim_uuid"], row["lock_key"]),
                    "create_time": row["acquired_at"], "lock_key": row["lock_key"], "state": row["state"],
                    "occupancy_kind": "execution_lock", "version": f"{row['claim_uuid']}:{row['token']}",
                    "workflow_task_uuid": row["task_uuid"], "workflow_node_job_uuid": row["job_uuid"],
                    "claim_uuid": row["claim_uuid"], "fencing_token": row["token"],
                })
            for row in conn.execute("SELECT * FROM workflow_resource_interval WHERE state!='released'").fetchall():
                record = dict(row)
                result.append({
                    "uuid": record["owner_uuid"], "lock_key": record["lock_key"], "state": record["state"],
                    "occupancy_kind": "resource_interval", "version": str(record["version"]),
                    "workflow_task_uuid": record["task_uuid"], "task_uuid": record["task_uuid"],
                })
            for row in conn.execute("SELECT * FROM workflow_device_tenancy WHERE state!='released'").fetchall():
                result.append({
                    "uuid": row["tenancy_uuid"], "create_time": row["created_at"], "lock_key": row["device_key"],
                    "state": row["state"], "occupancy_kind": "device_tenancy", "version": str(row["version"]),
                    "workflow_task_uuid": row["task_uuid"], "workflow_node_job_uuid": row["acquired_job_uuid"],
                    "material_uuid": row["material_uuid"], "device_id": row["device_key"],
                })
        return result

    def resource_usage(self) -> Dict[str, Any]:
        with self.workflow.write_lock:
            conn = self.workflow.connection
            jobs = []
            for row in conn.execute(
                """
                SELECT job.uuid, job.workflow_task_uuid, job.workflow_node_uuid, job.status, task.status AS task_status,
                       task.workflow_snapshot, run.material_uuid
                FROM workflow_node_job AS job
                JOIN workflow_task AS task ON task.uuid = job.workflow_task_uuid
                LEFT JOIN workflow_node_run AS run ON run.uuid = job.workflow_node_run_uuid
                WHERE job.deleted_at IS NULL AND task.deleted_at IS NULL
                  AND job.status IN ('pending', 'dispatched', 'running', 'cancel_requested', 'intervention_required',
                                     'execution_unknown')
                ORDER BY job.create_time
                """
            ).fetchall():
                snapshot = _load(row["workflow_snapshot"], {})
                node = next((item for item in snapshot.get("nodes") or []
                             if isinstance(item, dict) and item.get("uuid") == row["workflow_node_uuid"]), {})
                meta = node.get("meta_data") if isinstance(node.get("meta_data"), dict) else {}
                jobs.append({
                    "uuid": row["uuid"], "task_uuid": row["workflow_task_uuid"], "task_status": row["task_status"],
                    "task_name": (snapshot.get("workflow") or {}).get("name", ""),
                    "node_uuid": row["workflow_node_uuid"],
                    "node_name": node.get("name") or node.get("action_name") or row["workflow_node_uuid"],
                    "status": row["status"], "device_id": meta.get("target_device_id"),
                    "material_uuid": row["material_uuid"], "expected_change_set": {},
                })
        return {"captured_at": utc_now(), "occupancies": self.occupancies(), "material_bindings": [], "jobs": jobs}

    def resource_disposition(self, request: Dict[str, Any], *, idempotency_key: str,
                             operator_id: str) -> Dict[str, Any]:
        """释放用户选中的逻辑占用（只限异常决定下的资源；带版本 CAS）。"""

        from unilabos.server.services.runtime.workflow.device_tenancy import DeviceTenancies
        from unilabos.server.services.runtime.workflow.resource_intervals import ResourceIntervals

        decision_id = str(request.get("decision_id") or "")
        operation = f"resource:{decision_id}"
        with self.workflow.transaction() as conn:
            existing = self._existing(conn, operation, idempotency_key, request, operator_id)
            if existing is not None:
                return existing
            self._check_station(conn, request)
            error = self._read_error(conn, decision_id)
            if error["stage"] == "RESOLVED":
                raise StationConflict("decision_conflict")
            if request.get("session_id"):
                session = self._read_session(conn, str(request["session_id"]))
                if request.get("expected_session_version") != session["version"]:
                    raise StationConflict("session_version_conflict")
        current = {item["uuid"]: item for item in self.occupancies()}
        versions = request.get("expected_versions") or {}
        selected = []
        for identity in request.get("occupancy_ids") or []:
            item = current.get(identity)
            if item is None:
                raise StationNotFound(f"逻辑占用不存在：{identity}")
            if str(versions.get(identity)) != item["version"]:
                raise StationConflict("occupancy_version_conflict")
            selected.append(item)
        reason = str(request.get("reason") or "")
        released = []
        for item in selected:
            if item["occupancy_kind"] == "resource_interval":
                ResourceIntervals(self.workflow).confirm_release(item["uuid"], expected_version=int(item["version"]),
                                                                 reason=reason)
            elif item["occupancy_kind"] == "device_tenancy":
                with self.workflow.transaction() as conn:
                    DeviceTenancies.confirm_release(conn, item["uuid"], int(item["version"]), reason)
            else:
                with self.workflow.transaction() as conn:
                    conn.execute("DELETE FROM workflow_execution_lease WHERE claim_uuid=?", (item["claim_uuid"],))
                    conn.execute("UPDATE workflow_execution_claim SET state='released', released_at=? "
                                 "WHERE claim_uuid=?", (utc_now(), item["claim_uuid"]))
                releaser = getattr(self.workflow, "_task_resource_releaser", None)
                if releaser is not None:
                    try:
                        releaser(item["workflow_task_uuid"], [item["workflow_node_job_uuid"]])
                    except Exception:  # noqa: BLE001
                        logger.exception("执行锁已释放，调度器内存占用释放失败")
            released.append(item["uuid"])
        listener = getattr(self.workflow, "_resource_change_listener", None)
        if listener is not None and released:
            try:
                listener()
            except Exception:  # noqa: BLE001
                logger.exception("资源处置已持久化，等待调度器恢复")
        with self.workflow.transaction() as conn:
            existing = self._existing(conn, operation, idempotency_key, request, operator_id)
            if existing is not None:
                return existing
            return self._save(conn, operation=operation, key=idempotency_key, request=request, operator=operator_id,
                              applied=True, result={"decision_id": decision_id, "released": released,
                                                    "source_task_uuid": error["workflow_task_uuid"], "reason": reason})

    # 事件 -----------------------------------------------------------------

    def events(self, after: int = 0) -> Dict[str, Any]:
        """保留的工站通知；游标越界或早于保留窗口时要求重读快照。"""

        with self.workflow.write_lock:
            conn = self.workflow.connection
            bounds = conn.execute("SELECT COALESCE(MIN(sequence), 0), COALESCE(MAX(sequence), 0) FROM frontend_event"
                                  ).fetchone()
            oldest, latest = int(bounds[0]), int(bounds[1])
            rows = conn.execute(
                "SELECT sequence, type, payload FROM frontend_event WHERE sequence > ? AND type = ? "
                "AND json_extract(payload, '$.station_id') = ? ORDER BY sequence LIMIT 200",
                (after, STATION_EVENT, self.station_id),
            ).fetchall()
        reset = after > latest or (after != 0 and after < oldest - 1)
        return {
            "items": [] if reset else [{"id": row["sequence"], "event": row["type"], "data": _load(row["payload"], {})}
                                       for row in rows],
            "next_cursor": latest if reset else (rows[-1]["sequence"] if rows else after),
            "reset_required": reset,
        }


__all__ = [
    "DECISION_ACTIONS",
    "OPTIONS",
    "STATION_EVENT",
    "StationConflict",
    "StationHandling",
    "StationInvalid",
    "StationNotFound",
    "StationUnavailable",
    "open_station_error",
    "resolve_station_errors",
    "station_paused",
]
