"""Typed human replies persisted before the scheduler resumes a local command."""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from unilabos.workflow.human_form import normalize_form_fields, validate_form_answers

PREFIX = "unilabos.workflow.native_commands:HumanCommands."
TERMINAL_TASKS = {
    "succeeded",
    "failed",
    "canceled",
    "canceling",
    "timeout",
    "execution_unknown",
}


def ensure_human_interaction_schema(connection) -> None:
    """幂等创建人工交互运行时表。

    ``connection`` 可以是 SQLite 原生连接，也可以是统一数据库适配器。
    工作流 Store 启动时调用本函数，避免首次请求人工交互时才创建表。
    """

    connection.execute("""CREATE TABLE IF NOT EXISTS workflow_human_interaction (
        uuid TEXT PRIMARY KEY, job_uuid TEXT NOT NULL UNIQUE,
        task_uuid TEXT NOT NULL, kind TEXT NOT NULL, arguments TEXT NOT NULL,
        status TEXT NOT NULL, result TEXT, opened_at TEXT NOT NULL,
        deadline_at TEXT NOT NULL, decided_at TEXT,
        FOREIGN KEY(job_uuid) REFERENCES workflow_node_job(uuid)
    )""")


class HumanInteractionStore:
    def __init__(self, store):
        self.store = store
        with store.transaction() as conn:
            ensure_human_interaction_schema(conn)

    @staticmethod
    def _decode(row):
        result = dict(row)
        for key in ("arguments", "result"):
            result[key] = json.loads(result[key]) if result[key] else None
        return result

    def evaluate(self, event, task_uuid, *, now=None):
        now = now or datetime.now(timezone.utc)
        kind = str(event["command_name"]).removeprefix(PREFIX)
        args = dict(event.get("arguments") or {})
        if kind not in {"choose", "text", "numbers", "sketch", "review", "form"}:
            raise ValueError("未知人工交互类型")
        if kind == "form":
            if (
                not isinstance(args.get("prompt"), str)
                or not args["prompt"].strip()
                or len(args["prompt"]) > 4096
            ):
                raise ValueError("人工判定提示必须是 1–4096 字的非空文本")
            args["fields"] = normalize_form_fields(args.get("fields"))
        timeout = args.get("timeout_seconds", 86400)
        if type(timeout) is not int or not 1 <= timeout <= 86400:
            raise ValueError("人工交互等待时间必须为 1–86400 秒")
        if kind in {"choose", "review"} and (
            not args.get("options")
            or len(args["options"]) != len(args.get("labels", []))
            or len(set(args["options"])) != len(args["options"])
        ):
            raise ValueError("人工选择必须提供不重复的选项和对应标签")
        if kind == "numbers" and (
            not args.get("labels")
            or len(args["labels"]) != len(args.get("defaults", []))
        ):
            raise ValueError("数值输入必须提供对应标签和默认值")
        if kind == "review" and (
            len(args.get("forms", [])) != len(args["options"])
            or any(
                f not in {"none", "text", "numbers", "sketch"} for f in args["forms"]
            )
            or len(args.get("number_labels", []))
            != len(args.get("number_defaults", []))
        ):
            raise ValueError("复核表单与选项或数值默认值不匹配")
        encoded = json.dumps(args, sort_keys=True, ensure_ascii=False, allow_nan=False)
        with self.store.transaction() as conn:
            job = conn.execute(
                "SELECT workflow_task_uuid FROM workflow_node_job WHERE uuid=?",
                (event["job_id"],),
            ).fetchone()
            if job is None or job[0] != task_uuid:
                raise ValueError("人工交互作业不属于本任务")
            conn.execute(
                """INSERT OR IGNORE INTO workflow_human_interaction
                (uuid,job_uuid,task_uuid,kind,arguments,status,opened_at,deadline_at)
                VALUES (?,?,?,?,?,'pending',?,?)""",
                (
                    str(uuid4()),
                    event["job_id"],
                    task_uuid,
                    kind,
                    encoded,
                    now.isoformat(),
                    (now + timedelta(seconds=timeout)).isoformat(),
                ),
            )
            row = conn.execute(
                "SELECT * FROM workflow_human_interaction WHERE job_uuid=?",
                (event["job_id"],),
            ).fetchone()
            if row["arguments"] != encoded or row["kind"] != kind:
                raise ValueError("人工交互重放参数与已冻结请求不一致")
            if row["status"] == "pending" and now >= datetime.fromisoformat(
                row["deadline_at"]
            ):
                conn.execute(
                    "UPDATE workflow_human_interaction SET status='timed_out',decided_at=? WHERE uuid=?",
                    (now.isoformat(), row["uuid"]),
                )
                row = conn.execute(
                    "SELECT * FROM workflow_human_interaction WHERE uuid=?",
                    (row["uuid"],),
                ).fetchone()
        return self._decode(row)

    def expire(self, identity: str, *, now: datetime) -> bool:
        """独立收口已到期请求；暂停和派发门禁不应阻止人工等待超时。"""
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT h.status,h.deadline_at,t.status AS task_status FROM workflow_human_interaction h JOIN workflow_task t ON t.uuid=h.task_uuid WHERE h.uuid=?",
                (identity,),
            ).fetchone()
            if row is None or row["status"] != "pending":
                return False
            if row["task_status"] in TERMINAL_TASKS:
                status = "canceled"
            elif now >= datetime.fromisoformat(row["deadline_at"]):
                status = "timed_out"
            else:
                return False
            conn.execute(
                "UPDATE workflow_human_interaction SET status=?,decided_at=? WHERE uuid=? AND status='pending'",
                (status, now.isoformat(), identity),
            )
        return True

    def list(self, task_uuid):
        with self.store.transaction() as conn:
            conn.execute(
                """UPDATE workflow_human_interaction SET status='canceled'
                WHERE task_uuid=? AND status='pending' AND EXISTS (
                SELECT 1 FROM workflow_task t WHERE t.uuid=task_uuid
                AND t.status IN ('succeeded','failed','canceled','canceling','timeout','execution_unknown'))""",
                (task_uuid,),
            )
            rows = conn.execute(
                "SELECT * FROM workflow_human_interaction WHERE task_uuid=? ORDER BY opened_at",
                (task_uuid,),
            ).fetchall()
        return [self._decode(row) for row in rows]

    @staticmethod
    def validate(kind, args, result):
        if not isinstance(result, dict):
            raise ValueError("人工交互回复必须是对象")
        if kind == "form":
            if set(result) != {"answers"}:
                raise ValueError("人工判定回复只接受 answers")
            validate_form_answers(args["fields"], result["answers"])
            return
        if kind == "review":
            if set(result) != {
                "choice",
                "text",
                "values",
                "x",
                "y",
                "corner_x",
                "corner_y",
            }:
                raise ValueError("复核回复字段不匹配")
            HumanInteractionStore.validate("choose", args, {"choice": result["choice"]})
            HumanInteractionStore.validate("text", args, {"text": result["text"]})
            HumanInteractionStore.validate(
                "numbers",
                {"labels": args["number_labels"]},
                {"values": result["values"]},
            )
            coordinates = {k: result[k] for k in ("x", "y", "corner_x", "corner_y")}
            form = args["forms"][args["options"].index(result["choice"])]
            if form == "sketch":
                HumanInteractionStore.validate("sketch", args, coordinates)
            elif any(v != [] for v in coordinates.values()):
                raise ValueError("此选项不接受手绘坐标")
            return
        expected = {
            "choose": {"choice"},
            "text": {"text"},
            "numbers": {"values"},
            "sketch": {"x", "y", "corner_x", "corner_y"},
        }[kind]
        if set(result) != expected:
            raise ValueError("回复字段与人工交互类型不匹配")
        if kind == "choose":
            if result["choice"] not in args["options"]:
                raise ValueError("回复不是本次请求的选项")
        elif kind == "text":
            if not isinstance(result["text"], str) or len(result["text"]) > 4096:
                raise ValueError("文本回复无效")
        else:
            for values in result.values():
                if (
                    not isinstance(values, list)
                    or len(values) > 4000
                    or any(
                        type(v) not in (int, float) or not math.isfinite(v)
                        for v in values
                    )
                ):
                    raise ValueError("坐标和数值必须是有限数值数组")
            if kind == "numbers" and len(result["values"]) != len(args["labels"]):
                raise ValueError("数值回复数量不匹配")
            if kind == "sketch" and (
                len(result["x"]) < 3
                or len(result["x"]) != len(result["y"])
                or len(result["corner_x"]) not in (0, 4)
                or len(result["corner_x"]) != len(result["corner_y"])
            ):
                raise ValueError("手绘需要至少三个轮廓点及零个或四个板角点")

    def reply(self, identity, result, *, now=None):
        now = now or datetime.now(timezone.utc)
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM workflow_human_interaction WHERE uuid=?", (identity,)
            ).fetchone()
            if row is None:
                raise KeyError(identity)
            request = self._decode(row)
            self.validate(request["kind"], request["arguments"], result)
            if request["status"] == "answered" and request["result"] == result:
                return request
            task = conn.execute(
                "SELECT status FROM workflow_task WHERE uuid=?", (row["task_uuid"],)
            ).fetchone()
            job = conn.execute(
                "SELECT status FROM workflow_node_job WHERE uuid=?", (row["job_uuid"],)
            ).fetchone()
            if (
                request["status"] != "pending"
                or now >= datetime.fromisoformat(row["deadline_at"])
                or task is None
                or task[0] in TERMINAL_TASKS
                or job[0] not in {"pending", "waiting"}
            ):
                raise RuntimeError(
                    "人工交互已结束、超时或任务已取消；不能提交旧轮次回复"
                )
            conn.execute(
                "UPDATE workflow_human_interaction SET status='answered',result=?,decided_at=? WHERE uuid=?",
                (json.dumps(result, allow_nan=False), now.isoformat(), identity),
            )
            row = conn.execute(
                "SELECT * FROM workflow_human_interaction WHERE uuid=?", (identity,)
            ).fetchone()
        return self._decode(row)


def pending_human_interactions(connection) -> list[dict]:
    """读取当前原生交互，保留原有字段与回复协议。"""
    placeholders = ",".join("?" for _ in TERMINAL_TASKS)
    rows = connection.execute(
        "SELECT h.* FROM workflow_human_interaction h "
        "JOIN workflow_node_job j ON j.uuid=h.job_uuid AND j.workflow_task_uuid=h.task_uuid "
        "JOIN workflow_task t ON t.uuid=h.task_uuid "
        "WHERE h.status='pending' AND j.deleted_at IS NULL AND t.deleted_at IS NULL "
        f"AND t.status NOT IN ({placeholders}) "
        "AND j.status NOT IN ('succeeded','failed','canceled','timeout') "
        "ORDER BY h.opened_at,h.uuid",
        tuple(TERMINAL_TASKS),
    ).fetchall()
    return [HumanInteractionStore._decode(row) for row in rows]
