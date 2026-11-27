"""容器内容（试剂、样品、当前内容物）共享的不变量。"""

from __future__ import annotations

import json
import sqlite3

from unilabos.app.scheduler.inventory.backend_contract import (
    INVALID_PARAMETER,
    MATERIAL_NOT_FOUND,
    RESOURCE_DATA_CONFLICT,
    BackendContractError,
)


def require_container(conn: sqlite3.Connection, material_uuid: str) -> None:
    """确认物料存在、活动且模板声明了 ``container`` 标签。"""

    row = conn.execute(
        """SELECT material.uuid,resource_template.tags FROM material
        JOIN resource_template
          ON resource_template.uuid=material.resource_template_uuid
        WHERE material.uuid=? AND material.deleted_at IS NULL
          AND resource_template.deleted_at IS NULL""",
        (material_uuid,),
    ).fetchone()
    if row is None:
        raise BackendContractError(MATERIAL_NOT_FOUND, "material does not exist")
    try:
        tags = json.loads(str(row["tags"]))
    except (TypeError, ValueError):
        tags = []
    if "container" not in {str(tag).strip().lower() for tag in tags}:
        raise BackendContractError(
            INVALID_PARAMETER, "selected material is not a container"
        )


def ensure_container_empty(conn: sqlite3.Connection, material_uuid: str) -> None:
    """保证同一容器最多登记一种活动内容。"""

    occupied = conn.execute(
        """SELECT 1 FROM reagent
        WHERE material_uuid=? AND deleted_at IS NULL
        UNION ALL
        SELECT 1 FROM sample
        WHERE material_uuid=? AND deleted_at IS NULL
        UNION ALL
        SELECT 1 FROM current_substance
        WHERE material_uuid=? AND deleted_at IS NULL
        LIMIT 1""",
        (material_uuid, material_uuid, material_uuid),
    ).fetchone()
    if occupied is not None:
        raise BackendContractError(
            RESOURCE_DATA_CONFLICT,
            "selected container already has other content",
        )


__all__ = ["ensure_container_empty", "require_container"]
