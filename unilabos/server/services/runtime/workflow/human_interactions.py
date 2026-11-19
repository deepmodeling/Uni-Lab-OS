"""V1 结构化人工交互（``/workflow-tasks/{uuid}/human-interactions``、``.../reply``）。

动作 / 工作流命令向操作员要一个选择、文本、数值或手绘轮廓；请求冻结在
``workflow_human_interaction``（runtime.db v9），回复按请求类型严格校验后落库，再唤醒调度器。
dev 的设备动作目前没有发起交互的原生命令；生产者调用 :meth:`HumanInteractions.open`，
它与 V1 ``HumanInteractionStore.evaluate`` 同语义（同一作业重放必须携带相同参数）。
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional
from uuid import uuid4

from unilabos.server.services.runtime.workflow.errors import StoreConflict, StoreNotFound
from unilabos.server.services.runtime.workflow.store import _json, _load

KINDS = frozenset({"choose", "text", "numbers", "sketch", "review"})
_TERMINAL_TASKS = ("succeeded", "failed", "canceled", "canceling", "timeout")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _row(row: Any) -> Dict[str, Any]:
    result = dict(row)
    result["arguments"] = _load(row["arguments"], {})
    result["result"] = _load(row["result"], None) if row["result"] is not None else None
    return result


def validate_arguments(kind: str, args: Mapping[str, Any]) -> None:
    if kind not in KINDS:
        raise ValueError("未知人工交互类型")
    timeout = args.get("timeout_seconds", 86400)
    if type(timeout) is not int or not 1 <= timeout <= 86400:
        raise ValueError("人工交互等待时间必须为 1–86400 秒")
    if kind in {"choose", "review"} and (
        not args.get("options")
        or len(args["options"]) != len(args.get("labels", []))
        or len(set(args["options"])) != len(args["options"])
    ):
        raise ValueError("人工选择必须提供不重复的选项和对应标签")
    if kind == "numbers" and (not args.get("labels") or len(args["labels"]) != len(args.get("defaults", []))):
        raise ValueError("数值输入必须提供对应标签和默认值")
    if kind == "review" and (
        len(args.get("forms", [])) != len(args["options"])
        or any(form not in {"none", "text", "numbers", "sketch"} for form in args["forms"])
        or len(args.get("number_labels", [])) != len(args.get("number_defaults", []))
    ):
        raise ValueError("复核表单与选项或数值默认值不匹配")


def validate_reply(kind: str, args: Mapping[str, Any], result: Mapping[str, Any]) -> None:
    """V1 ``HumanInteractionStore.validate``：回复字段必须与请求类型完全对应。"""

    if kind == "review":
        if set(result) != {"choice", "text", "values", "x", "y", "corner_x", "corner_y"}:
            raise ValueError("复核回复字段不匹配")
        validate_reply("choose", args, {"choice": result["choice"]})
        validate_reply("text", args, {"text": result["text"]})
        validate_reply("numbers", {"labels": args.get("number_labels", [])}, {"values": result["values"]})
        coordinates = {key: result[key] for key in ("x", "y", "corner_x", "corner_y")}
        form = args["forms"][list(args["options"]).index(result["choice"])]
        if form == "sketch":
            validate_reply("sketch", args, coordinates)
        elif any(value != [] for value in coordinates.values()):
            raise ValueError("此选项不接受手绘坐标")
        return
    expected = {"choose": {"choice"}, "text": {"text"}, "numbers": {"values"},
                "sketch": {"x", "y", "corner_x", "corner_y"}}[kind]
    if set(result) != expected:
        raise ValueError("回复字段与人工交互类型不匹配")
    if kind == "choose":
        if result["choice"] not in args.get("options", []):
            raise ValueError("回复不是本次请求的选项")
    elif kind == "text":
        if not isinstance(result["text"], str) or len(result["text"]) > 4096:
            raise ValueError("文本回复无效")
    else:
        for values in result.values():
            if (not isinstance(values, list) or len(values) > 4000
                    or any(type(item) not in (int, float) or not math.isfinite(item) for item in values)):
                raise ValueError("坐标和数值必须是有限数值数组")
        if kind == "numbers" and len(result["values"]) != len(args.get("labels", [])):
            raise ValueError("数值回复数量不匹配")
        if kind == "sketch" and (
            len(result["x"]) < 3 or len(result["x"]) != len(result["y"])
            or len(result["corner_x"]) not in (0, 4) or len(result["corner_x"]) != len(result["corner_y"])
        ):
            raise ValueError("手绘需要至少三个轮廓点及零个或四个板角点")


class HumanInteractions:
    def __init__(self, store: Any) -> None:
        self._store = store

    def open(self, job_uuid: str, *, kind: str, arguments: Dict[str, Any], now: Optional[datetime] = None) -> Dict[str, Any]:
        """作业发起（或重放）一次人工交互；到期未答复的请求收为 ``timed_out``。"""

        now = now or _now()
        args = dict(arguments or {})
        validate_arguments(kind, args)
        encoded = _json(args)
        with self._store.transaction() as conn:
            job = conn.execute("SELECT workflow_task_uuid FROM workflow_node_job WHERE uuid=? AND deleted_at IS NULL",
                               (job_uuid,)).fetchone()
            if job is None:
                raise StoreNotFound(f"workflow node job {job_uuid} not found")
            conn.execute(
                "INSERT OR IGNORE INTO workflow_human_interaction(uuid, job_uuid, task_uuid, kind, arguments, status, "
                "opened_at, deadline_at) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
                (str(uuid4()), job_uuid, job["workflow_task_uuid"], kind, encoded, _iso(now),
                 _iso(now + timedelta(seconds=args.get("timeout_seconds", 86400)))),
            )
            row = conn.execute("SELECT * FROM workflow_human_interaction WHERE job_uuid=?", (job_uuid,)).fetchone()
            if row["arguments"] != encoded or row["kind"] != kind:
                raise StoreConflict("人工交互重放参数与已冻结请求不一致")
            if row["status"] == "pending" and now >= _parse(row["deadline_at"]):
                conn.execute("UPDATE workflow_human_interaction SET status='timed_out', decided_at=? WHERE uuid=?",
                             (_iso(now), row["uuid"]))
                row = conn.execute("SELECT * FROM workflow_human_interaction WHERE uuid=?", (row["uuid"],)).fetchone()
            self._store._append_event(conn, event="workflow.task.changed",
                                      data={"workflow_task_uuid": row["task_uuid"], "human_interaction": row["uuid"]},
                                      now=_iso(now))
        return _row(row)

    def list(self, task_uuid: str) -> List[Dict[str, Any]]:
        marks = ",".join("?" for _ in _TERMINAL_TASKS)
        with self._store.transaction() as conn:
            conn.execute(
                f"UPDATE workflow_human_interaction SET status='canceled' WHERE task_uuid=? AND status='pending' "
                f"AND EXISTS (SELECT 1 FROM workflow_task t WHERE t.uuid=task_uuid AND t.status IN ({marks}))",
                (task_uuid, *_TERMINAL_TASKS),
            )
            rows = conn.execute("SELECT * FROM workflow_human_interaction WHERE task_uuid=? ORDER BY opened_at, uuid",
                                (task_uuid,)).fetchall()
        return [_row(row) for row in rows]

    def reply(self, interaction_uuid: str, result: Dict[str, Any], *, now: Optional[datetime] = None) -> Dict[str, Any]:
        now = now or _now()
        with self._store.transaction() as conn:
            row = conn.execute("SELECT * FROM workflow_human_interaction WHERE uuid=?", (interaction_uuid,)).fetchone()
            if row is None:
                raise StoreNotFound(f"human interaction {interaction_uuid} not found")
            request = _row(row)
            validate_reply(request["kind"], request["arguments"], result)
            if request["status"] == "answered" and request["result"] == result:
                return request
            task = conn.execute("SELECT status FROM workflow_task WHERE uuid=?", (row["task_uuid"],)).fetchone()
            job = conn.execute("SELECT status FROM workflow_node_job WHERE uuid=?", (row["job_uuid"],)).fetchone()
            if (request["status"] != "pending" or now >= _parse(row["deadline_at"]) or task is None
                    or task["status"] in _TERMINAL_TASKS or job is None
                    or job["status"] not in {"pending", "dispatched", "running"}):
                raise StoreConflict("人工交互已结束、超时或任务已取消；不能提交旧轮次回复")
            conn.execute("UPDATE workflow_human_interaction SET status='answered', result=?, decided_at=? WHERE uuid=?",
                         (_json(result), _iso(now), interaction_uuid))
            self._store._append_event(conn, event="workflow.task.changed",
                                      data={"workflow_task_uuid": row["task_uuid"], "human_interaction": interaction_uuid},
                                      now=_iso(now))
            row = conn.execute("SELECT * FROM workflow_human_interaction WHERE uuid=?", (interaction_uuid,)).fetchone()
        return _row(row)


__all__ = ["HumanInteractions", "KINDS", "validate_arguments", "validate_reply"]
