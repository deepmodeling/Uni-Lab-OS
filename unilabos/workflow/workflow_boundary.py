"""工作流输入/输出纯数据边界作业与结果记录。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid5

from unilabos.app.scheduler.param_resolver import json_get_exists
from unilabos.workflow.frozen_json_cache import decode_large_json_text
from unilabos.workflow.json_codec import decode_json_bytes, encode_json


class WorkflowBoundaryError(RuntimeError):
    """冻结边界绑定或持久作业事实无法形成唯一结果。"""


@dataclass(frozen=True, slots=True)
class WorkflowBoundaryProjection:
    """一次输出边界投影产生的可审计状态变化。"""

    output_job_uuid: str | None = None
    output_changed: bool = False
    task_completed: bool = False
    result: dict[str, Any] | None = None


def ensure_workflow_boundary_schema(connection: sqlite3.Connection) -> None:
    """幂等创建每个任务唯一的工作流结果记录。"""

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS workflow_result (
            uuid TEXT PRIMARY KEY,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            workflow_task_uuid TEXT NOT NULL UNIQUE,
            workflow_node_job_uuid TEXT NOT NULL UNIQUE,
            result TEXT NOT NULL,
            FOREIGN KEY(workflow_task_uuid) REFERENCES workflow_task(uuid),
            FOREIGN KEY(workflow_node_job_uuid) REFERENCES workflow_node_job(uuid)
        )
        """
    )


def project_ready_workflow_output(
    connection: sqlite3.Connection,
    *,
    task_uuid: str,
    now: str,
    complete_task: bool,
) -> WorkflowBoundaryProjection:
    """在显式输出依赖成功后幂等完成输出 Job 并写入结果记录。

    参数：调用方持有的工作流写事务、任务身份、统一时间以及是否在本函数内完成
    纯数据任务。返回状态变化摘要；依赖尚未成功时零写入。异常：多输出 Job、
    绑定损坏或已提交结果漂移时抛 ``WorkflowBoundaryError`` 并由外层回滚。
    """

    task = connection.execute(
        "SELECT * FROM workflow_task WHERE uuid = ? AND deleted_at IS NULL",
        (task_uuid,),
    ).fetchone()
    if task is None:
        raise WorkflowBoundaryError("工作流边界任务不存在")
    output_jobs = connection.execute(
        """
        SELECT * FROM workflow_node_job
        WHERE workflow_task_uuid = ? AND executor_kind = 'workflow_output' AND current_attempt=1
          AND deleted_at IS NULL
        ORDER BY topological_index, uuid
        """,
        (task_uuid,),
    ).fetchall()
    if not output_jobs:
        return WorkflowBoundaryProjection()
    if len(output_jobs) != 1:
        raise WorkflowBoundaryError("一个任务只能有一个工作流输出作业")
    output_job = output_jobs[0]
    if str(output_job["status"]) == "succeeded":
        return WorkflowBoundaryProjection(
            output_job_uuid=str(output_job["uuid"]),
            result=_decode(str(output_job["return_info"]), {}),
        )
    if str(output_job["status"]) != "pending":
        return WorkflowBoundaryProjection(output_job_uuid=str(output_job["uuid"]))

    plan = _decode(str(task["execution_plan"]), {})
    task_input = _decode(str(task["input"]), {})
    output_node = next(
        (
            node
            for node in plan.get("nodes", [])
            if node.get("uuid") == output_job["workflow_node_uuid"]
            and node.get("kind") == "workflow_output"
        ),
        None,
    )
    if not isinstance(output_node, dict):
        raise WorkflowBoundaryError("输出作业缺少冻结边界节点")
    bindings = output_node.get("output_bindings")
    if not isinstance(bindings, dict):
        raise WorkflowBoundaryError("输出节点绑定不是对象")
    if not bindings:
        # 空输出合同也有真实结果边界；等待全部当前 Job 成功或被条件分支跳过。
        unfinished = connection.execute(
            """SELECT COUNT(*) FROM workflow_node_job
               WHERE workflow_task_uuid=? AND uuid!=? AND current_attempt=1
                 AND deleted_at IS NULL AND status NOT IN ('succeeded','skipped')""",
            (task_uuid, output_job["uuid"]),
        ).fetchone()[0]
        if unfinished:
            return WorkflowBoundaryProjection(output_job_uuid=str(output_job["uuid"]))
    result: dict[str, Any] = {}
    for output_name, binding in bindings.items():
        if not isinstance(output_name, str) or not isinstance(binding, dict):
            raise WorkflowBoundaryError("输出绑定身份或形状非法")
        completion_nodes = binding.get("completion_node_uuids", [])
        if not isinstance(completion_nodes, list) or any(
            not isinstance(identity, str) or not identity for identity in completion_nodes
        ):
            raise WorkflowBoundaryError("工作流输出完成边界无效")
        for identity in completion_nodes:
            completion = connection.execute(
                """SELECT status FROM workflow_node_job
                   WHERE workflow_task_uuid = ? AND workflow_node_uuid = ?
                     AND current_attempt = 1 AND deleted_at IS NULL""",
                (task_uuid, identity),
            ).fetchone()
            if completion is None:
                raise WorkflowBoundaryError("工作流输出完成边界缺少当前作业")
            completed_statuses = {"succeeded"} if binding.get("kind") == "literal" else {"succeeded", "skipped"}
            if str(completion["status"]) not in completed_statuses:
                return WorkflowBoundaryProjection(output_job_uuid=str(output_job["uuid"]))
        if binding.get("kind") == "workflow_input":
            parameter = binding.get("parameter")
            if not isinstance(parameter, str) or parameter not in task_input:
                raise WorkflowBoundaryError("输出绑定引用不存在的规范输入")
            result[output_name] = task_input[parameter]
            continue
        if binding.get("kind") == "literal":
            if "value" not in binding or not completion_nodes:
                raise WorkflowBoundaryError("固定输出缺少值或组合操作完成边界")
            result[output_name] = binding["value"]
            continue
        if binding.get("kind") != "node_output":
            raise WorkflowBoundaryError("输出绑定种类非法")
        source_node_uuid = str(binding.get("workflow_node_uuid") or "")
        source_job = connection.execute(
            """
            SELECT * FROM workflow_node_job
            WHERE workflow_task_uuid = ? AND workflow_node_uuid = ? AND current_attempt=1
              AND deleted_at IS NULL
            """,
            (task_uuid, source_node_uuid),
        ).fetchone()
        if source_job is None:
            raise WorkflowBoundaryError("输出绑定引用不存在的来源作业")
        if str(source_job["status"]) != "succeeded":
            return WorkflowBoundaryProjection(output_job_uuid=str(output_job["uuid"]))
        return_info = _decode(str(source_job["return_info"]), {})
        source_value = (
            return_info.get("return_value", return_info)
            if isinstance(return_info, dict)
            else return_info
        )
        source_handle_uuid = str(binding.get("source_handle_uuid") or "")
        handle = next(
            (
                item
                for item in plan.get("handles", [])
                if item.get("node_uuid") == source_node_uuid
                and item.get("template_handle_uuid") == source_handle_uuid
            ),
            None,
        )
        if not isinstance(handle, dict):
            raise WorkflowBoundaryError("输出绑定引用的来源连接点不在冻结计划中")
        data_key = str(handle.get("data_key") or "")
        exists, value = json_get_exists(source_value, data_key)
        if not exists:
            raise WorkflowBoundaryError("来源作业结果缺少工作流输出值")
        result[output_name] = value

    result_json = _encode(result)
    updated = connection.execute(
        """
        UPDATE workflow_node_job
        SET status = 'succeeded', return_info = ?, error_info = '[]',
            finished_at = ?, update_time = ?
        WHERE uuid = ? AND status = 'pending' AND deleted_at IS NULL
        """,
        (result_json, now, now, output_job["uuid"]),
    ).rowcount
    if updated != 1:
        raise WorkflowBoundaryError("输出作业状态发生并发变化")
    result_uuid = str(uuid5(UUID(task_uuid), "workflow-result"))
    existing = connection.execute(
        "SELECT result FROM workflow_result WHERE workflow_task_uuid = ?",
        (task_uuid,),
    ).fetchone()
    if existing is None:
        connection.execute(
            """
            INSERT INTO workflow_result(
                uuid,create_time,update_time,workflow_task_uuid,
                workflow_node_job_uuid,result
            ) VALUES (?,?,?,?,?,?)
            """,
            (result_uuid, now, now, task_uuid, output_job["uuid"], result_json),
        )
    elif _decode(str(existing["result"]), {}) != result:
        raise WorkflowBoundaryError("工作流结果记录发生漂移")
    connection.execute(
        "UPDATE workflow_task SET output = ?, update_time = ? WHERE uuid = ?",
        (result_json, now, task_uuid),
    )
    task_completed = False
    if complete_task:
        unfinished = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM workflow_node_job
                WHERE workflow_task_uuid = ? AND deleted_at IS NULL AND current_attempt=1
                  AND status != 'succeeded'
                """,
                (task_uuid,),
            ).fetchone()[0]
        )
        if unfinished == 0 and str(task["status"]) in {"pending", "running"}:
            task_completed = (
                connection.execute(
                    """
                    UPDATE workflow_task
                    SET status = 'succeeded', finished_at = ?, update_time = ?
                    WHERE uuid = ? AND status IN ('pending', 'running')
                    """,
                    (now, now, task_uuid),
                ).rowcount
                == 1
            )
    return WorkflowBoundaryProjection(
        output_job_uuid=str(output_job["uuid"]),
        output_changed=True,
        task_completed=task_completed,
        result=result,
    )


def _encode(value: Any) -> str:
    """把结果值编码为稳定 JSON 文本。"""

    return encode_json(value, sort_keys=True).decode("utf-8")


def _decode_text(value: str) -> Any:
    return decode_json_bytes(value.encode("utf-8"))


def _decode(value: str, fallback: Any) -> Any:
    """恢复持久 JSON；空文本使用调用方默认值。

    冻结执行计划这类大字段按内容缓存解码结果（见 frozen_json_cache）：就绪输出
    边界投影在每个作业完成时都会读整份计划，不能每次重新解码。
    """

    if not value:
        return fallback
    return decode_large_json_text(value, _decode_text)


__all__ = [
    "WorkflowBoundaryError",
    "WorkflowBoundaryProjection",
    "ensure_workflow_boundary_schema",
    "project_ready_workflow_output",
]
