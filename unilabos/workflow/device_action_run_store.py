"""设备单动作运行（DeviceActionRun）的 SQLite 持久化适配器。"""

from __future__ import annotations

import sqlite3
from typing import Any

from unilabos.workflow.json_codec import encode_json
from unilabos.workflow.store import StoreConflict, WorkflowStore, utc_now


def _json(value: Any) -> str:
    """把领域事实编码为稳定 JSON 文本。

    参数：``value`` 是快照、计划或状态数据。返回按键排序的 UTF-8 JSON 文本，
    用于幂等持久化和跨重启恢复。
    """

    return encode_json(value, sort_keys=True).decode("utf-8")


class DeviceActionRunStore:
    """原子创建或复用直接设备动作的 Task/Job 聚合。"""

    def __init__(self, workflow_store: WorkflowStore) -> None:
        """绑定唯一工作流存储。

        参数：``workflow_store`` 是本地工作流任务（WorkflowTask）权威；本适配器
        不创建第二个连接或数据库。
        """

        self._workflow_store = workflow_store

    def create_or_reuse(
        self,
        *,
        task: dict[str, Any],
        job: dict[str, Any],
        idempotency_key: str,
        request_fingerprint: str,
    ) -> dict[str, Any]:
        """在单事务内创建或幂等复用一个直接设备动作聚合。

        参数：``task`` 和 ``job`` 是已校验的持久事实；``idempotency_key`` 标识
        同一逻辑创建命令；``request_fingerprint`` 防止同键改义。
        返回 Backend 形状 ``task/job/created``。同键不同指纹或既有聚合不含唯一
        设备作业时抛出 ``StoreConflict``，且不产生部分写入。
        """

        # ``created`` 区分首次提交与幂等投递重放（DeliveryReplay）。
        created = False
        task_uuid = str(task["uuid"])
        job_uuid = str(job["uuid"])
        try:
            with self._workflow_store.transaction() as connection:
                existing = connection.execute(
                    """
                    SELECT uuid, request_fingerprint
                    FROM workflow_task
                    WHERE execution_kind = 'ad_hoc_device_action'
                      AND idempotency_key = ?
                      AND deleted_at IS NULL
                    """,
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    if existing["request_fingerprint"] != request_fingerprint:
                        raise StoreConflict("设备单动作幂等键已被不同请求使用")
                    task_uuid = str(existing["uuid"])
                    job_rows = connection.execute(
                        """
                        SELECT uuid, executor_kind
                        FROM workflow_node_job
                        WHERE workflow_task_uuid = ? AND deleted_at IS NULL
                        ORDER BY topological_index, create_time, uuid
                        """,
                        (task_uuid,),
                    ).fetchall()
                    if (
                        len(job_rows) != 1
                        or job_rows[0]["executor_kind"] != "device_action"
                    ):
                        raise StoreConflict("设备单动作任务没有唯一设备作业")
                    job_uuid = str(job_rows[0]["uuid"])
                else:
                    self._insert(connection, task=task, job=job)
                    created = True
        except sqlite3.IntegrityError as error:
            raise StoreConflict("设备单动作持久化身份冲突") from error

        return {
            "task": self._workflow_store.get_task(task_uuid),
            "job": self._workflow_store.get_job(job_uuid),
            "created": created,
        }

    @staticmethod
    def _insert(
        connection: sqlite3.Connection,
        *,
        task: dict[str, Any],
        job: dict[str, Any],
    ) -> None:
        """写入一个已校验的直接设备动作 Task/Job 聚合。

        参数：``connection`` 是当前 ``BEGIN IMMEDIATE`` 事务；``task`` 与 ``job``
        共享稳定身份。返回无；任一 INSERT 失败时由外层回滚全部事实。
        """

        now = utc_now()
        connection.execute(
            """
            INSERT INTO workflow_task(
                uuid, create_time, update_time, deleted_at, description,
                meta_data, workflow_uuid, execution_kind, idempotency_key,
                request_fingerprint, status, workflow_snapshot, execution_plan,
                run_mode, target_node_uuid, control_status, cleanup_status,
                trace_context, input, output, error_info
            ) VALUES (?, ?, ?, NULL, ?, ?, NULL, 'ad_hoc_device_action', ?, ?,
                      'pending', ?, ?, 'single_node', ?, 'active', 'none', '{}',
                      '{}', '{}', '[]')
            """,
            (
                task["uuid"],
                now,
                now,
                task.get("description"),
                _json(task.get("meta_data") or {}),
                task["idempotency_key"],
                task["request_fingerprint"],
                _json(task["workflow_snapshot"]),
                _json(task["execution_plan"]),
                task["target_node_uuid"],
            ),
        )
        policy = job.get("execution_policy") or {}
        connection.execute(
            """
            INSERT INTO workflow_node_job(
                uuid, create_time, update_time, deleted_at, description,
                meta_data, workflow_task_uuid, workflow_node_uuid,
                material_uuid, feedback_sequence, topological_index,
                executor_kind, execution_policy, execution_timeout_seconds,
                status, attempt, param, feedback_data, return_info,
                control_data, error_info
            ) VALUES (?, ?, ?, NULL, NULL, '{}', ?, ?, ?, 0, 0,
                      'device_action', ?, ?, 'pending', 1, ?, '{}', '{}',
                      '{}', '[]')
            """,
            (
                job["uuid"],
                now,
                now,
                task["uuid"],
                job["workflow_node_uuid"],
                job["material_uuid"],
                _json(policy),
                int(policy.get("execution_timeout_seconds") or 0),
                _json(job["param"]),
            ),
        )


__all__ = ["DeviceActionRunStore"]
