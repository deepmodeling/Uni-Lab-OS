"""任务物料准入、绑定与占有的本地持久写模型。"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import uuid4

from unilabos.workflow.json_codec import encode_json
from unilabos.workflow.store import StoreConflict, utc_now

_FLOW_ROLES = frozenset(
    {"primary_sample", "aliquot_sample", "reagent", "consumable"}
)
_CUSTODY_POLICIES = frozenset({"task_exclusive", "shared_source"})


def record_blocked_admission(
    connection: sqlite3.Connection,
    *,
    task_uuid: str,
    reason: str,
    wait_reason: Mapping[str, Any],
) -> None:
    """记录一次可重试的任务物料准入受阻判定。

    参数：``connection`` 是调用方持有的工作流写事务；``task_uuid`` 是稳定任务
    身份；``reason`` 是面向人的受阻原因；``wait_reason`` 是稳定机器可读详情。
    返回无。异常：原因为空或等待详情不能编码成 JSON 时抛 ``StoreConflict``。
    相同任务每次重新评估都会推进 attempt/revision，与 Backend 行为一致。
    """

    normalized_reason = str(reason or "").strip()
    if not normalized_reason:
        raise StoreConflict("任务物料准入受阻原因不能为空")
    wait_reason_json = _json_text(wait_reason, field="wait_reason")
    now = utc_now()
    admission = _admission_row(connection, task_uuid)
    if admission is None:
        connection.execute(
            """
            INSERT INTO workflow_task_material_admission(
                uuid, create_time, update_time, deleted_at, description,
                meta_data, workflow_task_uuid, status, attempt, revision,
                reason, wait_reason, evaluated_at, admitted_at
            ) VALUES (?, ?, ?, NULL, NULL, '{}', ?, 'blocked', 1, 1,
                      ?, ?, ?, NULL)
            """,
            (
                str(uuid4()),
                now,
                now,
                task_uuid,
                normalized_reason,
                wait_reason_json,
                now,
            ),
        )
    else:
        connection.execute(
            """
            UPDATE workflow_task_material_admission
            SET status = 'blocked', attempt = attempt + 1,
                revision = revision + 1, reason = ?, wait_reason = ?,
                evaluated_at = ?, admitted_at = NULL, update_time = ?
            WHERE uuid = ? AND deleted_at IS NULL
            """,
            (
                normalized_reason,
                wait_reason_json,
                now,
                now,
                admission["uuid"],
            ),
        )
    connection.execute(
        "UPDATE workflow_task SET wait_reason = ?, update_time = ? WHERE uuid = ?",
        (wait_reason_json, now, task_uuid),
    )


def record_admitted_materials(
    connection: sqlite3.Connection,
    *,
    task_uuid: str,
    source_jobs: Sequence[sqlite3.Row],
    bindings: Mapping[str, Mapping[str, str | None]],
    incremental: bool = False,
) -> None:
    """原子记录成功准入的来源绑定与任务物料占有。

    参数：``connection`` 是调用方持有的同一工作流事务；``task_uuid`` 是任务
    身份；``source_jobs`` 是物料来源解析作业；``bindings`` 按来源节点给出模板、
    物料、库位、流角色和占用策略。返回无。异常：字段非法、重放载荷冲突或同一
    独占物料已被另一活跃任务占有时抛 ``StoreConflict``，调用方整笔回滚。
    """

    jobs_by_node = {str(row["workflow_node_uuid"]): row for row in source_jobs}
    if not jobs_by_node or set(jobs_by_node) != set(bindings):
        raise StoreConflict(f"物料来源持久绑定集合不完整：{task_uuid}")
    normalized = {
        node_uuid: _normalize_binding(raw_binding)
        for node_uuid, raw_binding in bindings.items()
    }
    admission = _admission_row(connection, task_uuid)
    if not incremental and admission is not None and admission["status"] == "admitted":
        _verify_replayed_facts(
            connection,
            task_uuid=task_uuid,
            jobs_by_node=jobs_by_node,
            bindings=normalized,
        )
        return

    now = utc_now()
    for node_uuid, binding in normalized.items():
        job_uuid = str(jobs_by_node[node_uuid]["uuid"])
        _insert_or_verify_binding(
            connection,
            task_uuid=task_uuid,
            node_uuid=node_uuid,
            job_uuid=job_uuid,
            binding=binding,
            now=now,
        )
        if binding["custody_policy"] == "task_exclusive":
            _insert_or_verify_claim(
                connection,
                task_uuid=task_uuid,
                node_uuid=node_uuid,
                job_uuid=job_uuid,
                material_uuid=str(binding["material_uuid"]),
                now=now,
            )

    if not incremental:
        record_admitted_admission(connection, task_uuid=task_uuid, now=now)


def record_admitted_admission(
    connection: sqlite3.Connection,
    *,
    task_uuid: str,
    now: str | None = None,
) -> None:
    """记录不依赖来源绑定的统一任务物料准入成功事实。

    参数：调用方工作流事务、任务身份和可选统一时间戳。返回无。该入口供仅含
    数量库存需求的任务使用，也由来源绑定入口复用；重复 admitted 为零语义变化。
    """

    admitted_at = now or utc_now()
    admission = _admission_row(connection, task_uuid)
    if admission is not None and admission["status"] == "admitted":
        connection.execute(
            "UPDATE workflow_task SET wait_reason = '{}', update_time = ? WHERE uuid = ?",
            (admitted_at, task_uuid),
        )
        return
    if admission is None:
        connection.execute(
            """
            INSERT INTO workflow_task_material_admission(
                uuid, create_time, update_time, deleted_at, description,
                meta_data, workflow_task_uuid, status, attempt, revision,
                reason, wait_reason, evaluated_at, admitted_at
            ) VALUES (?, ?, ?, NULL, NULL, '{}', ?, 'admitted', 1, 1,
                      NULL, '{}', ?, ?)
            """,
            (
                str(uuid4()),
                admitted_at,
                admitted_at,
                task_uuid,
                admitted_at,
                admitted_at,
            ),
        )
    else:
        connection.execute(
            """
            UPDATE workflow_task_material_admission
            SET status = 'admitted', attempt = attempt + 1,
                revision = revision + 1, reason = NULL, wait_reason = '{}',
                evaluated_at = ?, admitted_at = ?, update_time = ?
            WHERE uuid = ? AND deleted_at IS NULL
            """,
            (admitted_at, admitted_at, admitted_at, admission["uuid"]),
        )
    connection.execute(
        "UPDATE workflow_task SET wait_reason = '{}', update_time = ? WHERE uuid = ?",
        (admitted_at, task_uuid),
    )


def read_material_admission(
    connection: sqlite3.Connection,
    *,
    task_uuid: str,
) -> dict[str, Any] | None:
    """读取任务最近一次物料准入判定。

    参数：``connection`` 是可读连接；``task_uuid`` 是稳定任务身份。返回：不存在
    时为 ``None``，否则返回状态、尝试号、修订和原因。异常：数据库错误原样传播。
    """

    row = _admission_row(connection, task_uuid)
    if row is None:
        return None
    return {
        "uuid": row["uuid"],
        "workflow_task_uuid": row["workflow_task_uuid"],
        "status": row["status"],
        "attempt": row["attempt"],
        "revision": row["revision"],
        "reason": row["reason"],
        "evaluated_at": row["evaluated_at"],
        "admitted_at": row["admitted_at"],
    }


def list_blocked_material_task_uuids(
    connection: sqlite3.Connection,
) -> list[str]:
    """列出可恢复的受阻任务。

    参数：``connection`` 是可读工作流连接。返回：按评估时间和任务身份稳定排序
    的 ``pending`` 任务 UUID；终态或已删除任务不会进入恢复集合。异常：数据库
    错误原样传播。
    """

    rows = connection.execute(
        """
        SELECT admission.workflow_task_uuid
        FROM workflow_task_material_admission AS admission
        JOIN workflow_task AS task ON task.uuid = admission.workflow_task_uuid
        WHERE admission.deleted_at IS NULL
          AND admission.status = 'blocked'
          AND task.deleted_at IS NULL
          AND task.status = 'pending'
        ORDER BY admission.evaluated_at, admission.workflow_task_uuid
        """
    ).fetchall()
    return [str(row["workflow_task_uuid"]) for row in rows]


def find_active_foreign_material_claim(
    connection: sqlite3.Connection,
    *,
    task_uuid: str,
    material_uuids: Sequence[str],
) -> sqlite3.Row | None:
    """查找请求物料上由其他 Task 持有的活动任务级独占。

    参数：当前工作流事务、发起动作的 Task 和动作实际引用的物料集合。返回：按
    取得时间稳定排序的首个外国 TaskMaterialClaim，不存在时返回 ``None``。
    ``shared_source`` 从不写本表，因此不会形成任务级阻塞。
    """

    normalized = tuple(
        sorted({str(value or "").strip() for value in material_uuids} - {""})
    )
    if not normalized:
        return None
    placeholders = ",".join("?" for _ in normalized)
    return connection.execute(
        "SELECT * FROM workflow_task_material_claim "
        "WHERE deleted_at IS NULL AND status='active' "
        "AND workflow_task_uuid<>? "
        f"AND material_uuid IN ({placeholders}) "
        "ORDER BY acquired_at,uuid LIMIT 1",
        (task_uuid, *normalized),
    ).fetchone()


def _admission_row(
    connection: sqlite3.Connection,
    task_uuid: str,
) -> sqlite3.Row | None:
    """读取活跃准入行；参数是连接和任务身份，返回数据库行或 ``None``。"""

    return connection.execute(
        """
        SELECT * FROM workflow_task_material_admission
        WHERE workflow_task_uuid = ? AND deleted_at IS NULL
        """,
        (task_uuid,),
    ).fetchone()


def _normalize_binding(
    raw_binding: Mapping[str, str | None],
) -> dict[str, str | None]:
    """校验一个来源绑定；参数是可疑字段映射，返回规范副本，非法时抛冲突。"""

    material_uuid = str(raw_binding.get("material_uuid") or "").strip()
    template_uuid = str(raw_binding.get("resource_template_uuid") or "").strip()
    flow_role = str(raw_binding.get("flow_role") or "").strip()
    custody_policy = str(raw_binding.get("custody_policy") or "").strip()
    site_uuid = str(raw_binding.get("site_uuid") or "").strip() or None
    if not material_uuid or not template_uuid:
        raise StoreConflict("任务物料绑定身份不能为空")
    if flow_role not in _FLOW_ROLES:
        raise StoreConflict(f"未知物料流角色：{flow_role}")
    if custody_policy not in _CUSTODY_POLICIES:
        raise StoreConflict(f"未知物料占用策略：{custody_policy}")
    return {
        "material_uuid": material_uuid,
        "resource_template_uuid": template_uuid,
        "site_uuid": site_uuid,
        "flow_role": flow_role,
        "custody_policy": custody_policy,
    }


def _insert_or_verify_binding(
    connection: sqlite3.Connection,
    *,
    task_uuid: str,
    node_uuid: str,
    job_uuid: str,
    binding: Mapping[str, str | None],
    now: str,
) -> None:
    """写入或核对一个绑定；完全相同的重放无写入，字段冲突抛 ``StoreConflict``。"""

    existing = connection.execute(
        """
        SELECT * FROM workflow_task_material_binding
        WHERE workflow_task_uuid = ? AND workflow_node_uuid = ?
          AND deleted_at IS NULL
        """,
        (task_uuid, node_uuid),
    ).fetchone()
    expected = (
        job_uuid,
        binding["resource_template_uuid"],
        binding["material_uuid"],
        binding["site_uuid"],
        binding["flow_role"],
        binding["custody_policy"],
    )
    if existing is not None:
        actual = tuple(
            existing[field]
            for field in (
                "workflow_node_job_uuid",
                "resource_template_uuid",
                "material_uuid",
                "site_uuid",
                "flow_role",
                "custody_policy",
            )
        )
        if actual != expected:
            raise StoreConflict(f"任务物料绑定重放冲突：{node_uuid}")
        return
    connection.execute(
        """
        INSERT INTO workflow_task_material_binding(
            uuid, create_time, update_time, deleted_at, description, meta_data,
            workflow_task_uuid, workflow_node_uuid, workflow_node_job_uuid,
            resource_template_uuid, material_uuid, site_uuid, flow_role,
            custody_policy
        ) VALUES (?, ?, ?, NULL, NULL, '{}', ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid4()),
            now,
            now,
            task_uuid,
            node_uuid,
            job_uuid,
            binding["resource_template_uuid"],
            binding["material_uuid"],
            binding["site_uuid"],
            binding["flow_role"],
            binding["custody_policy"],
        ),
    )


def _insert_or_verify_claim(
    connection: sqlite3.Connection,
    *,
    task_uuid: str,
    node_uuid: str,
    job_uuid: str,
    material_uuid: str,
    now: str,
) -> None:
    """写入或核对独占 claim；参数是任务/节点/作业/物料身份，冲突时失败关闭。"""

    active_foreign_use = connection.execute(
        "SELECT workflow_task_uuid,workflow_node_job_uuid "
        "FROM execution_lock_lease "
        "WHERE material_uuid=? AND deleted_at IS NULL "
        "AND state IN ('reserved','running','uncertain') "
        "AND workflow_task_uuid<>? "
        "ORDER BY acquired_at,uuid LIMIT 1",
        (material_uuid, task_uuid),
    ).fetchone()
    if active_foreign_use is not None:
        raise StoreConflict(
            f"物料正在被其他任务的动作使用：{material_uuid} "
            f"({active_foreign_use['workflow_task_uuid']})"
        )
    existing = connection.execute(
        """
        SELECT * FROM workflow_task_material_claim
        WHERE workflow_task_uuid = ? AND workflow_node_uuid = ?
          AND deleted_at IS NULL
        """,
        (task_uuid, node_uuid),
    ).fetchone()
    if existing is not None:
        if (
            existing["workflow_node_job_uuid"] != job_uuid
            or existing["material_uuid"] != material_uuid
        ):
            raise StoreConflict(f"任务物料占有重放冲突：{node_uuid}")
        return
    try:
        connection.execute(
            """
            INSERT INTO workflow_task_material_claim(
                uuid, create_time, update_time, deleted_at, description,
                meta_data, workflow_task_uuid, workflow_node_uuid,
                workflow_node_job_uuid, material_uuid, status, revision,
                acquired_at, released_at
            ) VALUES (?, ?, ?, NULL, NULL, '{}', ?, ?, ?, ?, 'active', 1,
                      ?, NULL)
            """,
            (
                str(uuid4()),
                now,
                now,
                task_uuid,
                node_uuid,
                job_uuid,
                material_uuid,
                now,
            ),
        )
    except sqlite3.IntegrityError as error:
        raise StoreConflict(f"物料已被其他任务独占：{material_uuid}") from error


def _verify_replayed_facts(
    connection: sqlite3.Connection,
    *,
    task_uuid: str,
    jobs_by_node: Mapping[str, sqlite3.Row],
    bindings: Mapping[str, Mapping[str, str | None]],
) -> None:
    """核对 admitted 重放事实；参数是连接、任务、作业与绑定，冲突时失败关闭。"""

    for node_uuid, binding in bindings.items():
        _insert_or_verify_binding(
            connection,
            task_uuid=task_uuid,
            node_uuid=node_uuid,
            job_uuid=str(jobs_by_node[node_uuid]["uuid"]),
            binding=binding,
            now=utc_now(),
        )
        if binding["custody_policy"] == "task_exclusive":
            _insert_or_verify_claim(
                connection,
                task_uuid=task_uuid,
                node_uuid=node_uuid,
                job_uuid=str(jobs_by_node[node_uuid]["uuid"]),
                material_uuid=str(binding["material_uuid"]),
                now=utc_now(),
            )


def _json_text(value: Any, *, field: str) -> str:
    """编码 JSON；参数是值和字段名，返回稳定文本，非法值抛 ``StoreConflict``。"""

    try:
        return encode_json(value, sort_keys=True).decode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise StoreConflict(f"{field} 不是合法 JSON") from error


__all__ = [
    "find_active_foreign_material_claim",
    "list_blocked_material_task_uuids",
    "read_material_admission",
    "record_admitted_admission",
    "record_admitted_materials",
    "record_blocked_admission",
]
