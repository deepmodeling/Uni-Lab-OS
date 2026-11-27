"""工作流事务内事件和运行日志的唯一公开写入模块。"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any

from unilabos.workflow.json_codec import encode_json


def append_frontend_event(
    connection: sqlite3.Connection,
    *,
    event: str,
    data: Mapping[str, Any],
    now: str,
) -> int:
    """在调用方事务内追加一个前端失效通知。

    参数：``connection`` 是工作流存储拥有的当前事务；``event`` 是稳定事件名；
    ``data`` 是小型事实引用；``now`` 是调用方统一时间。返回：新事件的单调游标。
    异常：SQLite 或 JSON 编码错误原样传播，由外层事务整体回滚。
    """

    cursor = connection.execute(
        "INSERT INTO frontend_event(event, data, create_time) VALUES (?, ?, ?)",
        (event, _json(data), now),
    )
    return int(cursor.lastrowid)


def append_runtime_event(
    connection: sqlite3.Connection,
    *,
    task_uuid: str,
    kind: str,
    now: str,
    job_uuid: str | None = None,
    command_uuid: str | None = None,
    from_status: str | None = None,
    to_status: str | None = None,
    data: Mapping[str, Any] | None = None,
) -> int:
    """在调用方事务内追加一个可重放的工作流运行事实。

    参数：``connection`` 是当前工作流写事务；Task/Job/Command UUID 标识聚合与
    可选执行身份；``kind``、前后状态、``data`` 与 ``now`` 描述状态变化。返回：
    任务内严格递增的运行日志序号。异常：身份、外键、SQLite 或 JSON 错误原样
    传播，调用方不得在事务外补写日志。
    """

    cursor = connection.execute(
        """
        INSERT INTO workflow_runtime_journal(
            workflow_task_uuid, workflow_node_job_uuid,
            workflow_task_command_uuid, kind, from_status, to_status,
            data, create_time
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_uuid,
            job_uuid,
            command_uuid,
            kind,
            from_status,
            to_status,
            _json(data or {}),
            now,
        ),
    )
    if kind == "task_transition" and to_status in {"succeeded", "failed", "canceled", "timeout"}:
        from unilabos.workflow.center_boundary import record_center_task_outcome

        # 终态事实与中心 outbox 使用当前事务；写入失败必须回滚整个状态变化。
        record_center_task_outcome(connection, task_uuid=task_uuid)
    return int(cursor.lastrowid)


def _json(value: Mapping[str, Any]) -> str:
    """把事件对象编码为键稳定 JSON。

    参数：``value`` 是事件的结构化小对象。返回：UTF-8 JSON 文本。异常：值不可
    编码时由统一 JSON 编码器原样抛出，禁止持久化部分事件。
    """

    return encode_json(dict(value), sort_keys=True).decode("utf-8")


__all__ = ["append_frontend_event", "append_runtime_event"]
