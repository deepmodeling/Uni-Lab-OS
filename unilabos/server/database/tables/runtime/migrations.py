"""``runtime.db`` 的版本迁移（v2 起）。

迁移里的 DDL 是该版本冻结的字面量，不引用会继续演进的表规格。
"""

from unilabos.server.database.schema import sql_migration

# v2：资源等待记录加入队时间，准入按老化优先级 + 入队时间排队（SC-N4）。等待记录是瞬态事实：
# 调度器启动时会对所有未派发的作业重新申请准入并重建记录，所以旧记录（没有入队时间）直接清空。
_V2_RESOURCE_WAITER_ENQUEUED_AT = sql_migration(
    2,
    "workflow_resource_waiter 增加 enqueued_at（持久等待顺序），清空旧等待记录",
    "DROP TABLE workflow_resource_waiter",
    """
        CREATE TABLE workflow_resource_waiter (
            job_uuid TEXT PRIMARY KEY REFERENCES workflow_node_job(uuid),
            task_uuid TEXT NOT NULL REFERENCES workflow_task(uuid),
            lock_keys TEXT NOT NULL CHECK(json_valid(lock_keys) AND json_type(lock_keys)='array'),
            blockers TEXT NOT NULL CHECK(json_valid(blockers) AND json_type(blockers)='array'),
            enqueued_at TEXT NOT NULL CHECK(enqueued_at <> '')
        )
    """,
)

RUNTIME_MIGRATIONS = (_V2_RESOURCE_WAITER_ENQUEUED_AT,)

#: 已发布版本的结构指纹：只有真正的 0.13 基线（v1）走迁移；同号不同指纹的旧开发库按 refuse 处理。
RUNTIME_RELEASED_CHECKSUMS = (
    (1, "68bbaa4dbedafb58de6a87d5f3a0153ce27165920c3797a9206237605a7e46ed"),
)
