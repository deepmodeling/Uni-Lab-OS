"""公开目录的显式下架意图；只保存准入决定，发布文件仍是唯一合同权威。"""

from __future__ import annotations

from unilabos.workflow.store import WorkflowStore


class CatalogWithdrawals:
    def __init__(self, store: WorkflowStore) -> None:
        self._store = store
        with store.transaction() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS workflow_catalog_withdrawal (
                    workflow_uuid TEXT PRIMARY KEY, reason TEXT NOT NULL)"""
            )

    def withdraw(self, workflow_uuid: str) -> None:
        """在删除源码前停止中心新准入；删除失败时意图仍保留，供用户重试。"""
        with self._store.transaction() as connection:
            connection.execute(
                """INSERT INTO workflow_catalog_withdrawal(workflow_uuid,reason)
                   VALUES (?,?) ON CONFLICT(workflow_uuid) DO NOTHING""",
                (workflow_uuid, "工站用户已请求删除工作流，停止新任务准入"),
            )

    def republish(self, workflow_uuid: str) -> None:
        with self._store.transaction() as connection:
            connection.execute("DELETE FROM workflow_catalog_withdrawal WHERE workflow_uuid=?", (workflow_uuid,))

    def contains(self, workflow_uuid: str) -> bool:
        with self._store.read() as connection:
            return connection.execute(
                "SELECT 1 FROM workflow_catalog_withdrawal WHERE workflow_uuid=?", (workflow_uuid,)
            ).fetchone() is not None

    def snapshot(self) -> list[dict[str, str]]:
        with self._store.read() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT workflow_uuid,reason FROM workflow_catalog_withdrawal ORDER BY workflow_uuid"
            ).fetchall()]
