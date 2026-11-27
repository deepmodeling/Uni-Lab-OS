"""PostgreSQL TimescaleDB 增强。

TimescaleDB 是 PostgreSQL 的可选扩展；SQLite 和未安装扩展的 PostgreSQL
继续使用普通关系表。当前只增强设备状态历史表，不改变设备状态写入语义。
"""

from __future__ import annotations

from typing import Any

DEVICE_PROPERTY_HISTORY = "device_property_history"
DEVICE_PROPERTY_HISTORY_TIME_COLUMN = "recorded_at"
DEVICE_PROPERTY_HISTORY_TIME_INDEX = "idx_dph_device_property_recorded_at"
# recorded_at 使用 Unix epoch 毫秒；按一天切分，避免 TimescaleDB 按 BIGINT
# 使用默认单位不明确的分块间隔。
DEVICE_PROPERTY_HISTORY_CHUNK_INTERVAL_MS = 86_400_000


def _extension_installed(connection: Any) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'"
        ).fetchone()
        is not None
    )


def _is_hypertable(connection: Any) -> bool:
    return (
        connection.execute(
            """
            SELECT 1
            FROM timescaledb_information.hypertables
            WHERE hypertable_schema = current_schema()
              AND hypertable_name = %s
            """,
            (DEVICE_PROPERTY_HISTORY,),
        ).fetchone()
        is not None
    )


def _primary_key(connection: Any) -> tuple[str, list[str]] | None:
    row = connection.execute(
        """
        SELECT constraint_info.conname,
               array_agg(attribute.attname ORDER BY key_position.ordinality)
        FROM pg_constraint AS constraint_info
        JOIN pg_class AS table_info
          ON table_info.oid = constraint_info.conrelid
        JOIN pg_namespace AS namespace_info
          ON namespace_info.oid = table_info.relnamespace
        JOIN unnest(constraint_info.conkey) WITH ORDINALITY AS key_position(
            attnum, ordinality
        )
          ON TRUE
        JOIN pg_attribute AS attribute
          ON attribute.attrelid = table_info.oid
         AND attribute.attnum = key_position.attnum
        WHERE constraint_info.contype = 'p'
          AND namespace_info.nspname = current_schema()
          AND table_info.relname = %s
        GROUP BY constraint_info.conname
        """,
        (DEVICE_PROPERTY_HISTORY,),
    ).fetchone()
    if row is None:
        return None
    return str(row[0]), [str(column) for column in row[1]]


def _ensure_partition_key_constraint(connection: Any) -> None:
    wanted = [DEVICE_PROPERTY_HISTORY_TIME_COLUMN, "id"]
    current = _primary_key(connection)
    if current is not None and current[1] == wanted:
        return

    if current is not None:
        from psycopg import sql

        connection.execute(
            sql.SQL(
                "ALTER TABLE public.device_property_history "
                "DROP CONSTRAINT {}"
            ).format(sql.Identifier(current[0]))
        )
    connection.execute(
        """
        ALTER TABLE public.device_property_history
        ADD CONSTRAINT device_property_history_recorded_at_id_pkey
        PRIMARY KEY (recorded_at, id)
        """
    )


def _ensure_time_index(connection: Any) -> None:
    connection.execute(
        f"""
        CREATE INDEX IF NOT EXISTS {DEVICE_PROPERTY_HISTORY_TIME_INDEX}
        ON public.{DEVICE_PROPERTY_HISTORY}
            (device_id, property, recorded_at DESC, id DESC)
        """
    )


def ensure_device_property_history_hypertable(connection: Any) -> bool:
    """将空的设备属性历史表幂等转换为 TimescaleDB hypertable。

    返回是否已启用 hypertable。扩展未安装时返回 ``False``，由调用方继续
    使用普通 PostgreSQL 表；已有数据时拒绝自动迁移，避免启动阶段长时间锁表。
    """

    if not _extension_installed(connection):
        return False

    if _is_hypertable(connection):
        _ensure_time_index(connection)
        return True

    if connection.execute(
        f"SELECT 1 FROM public.{DEVICE_PROPERTY_HISTORY} LIMIT 1"
    ).fetchone() is not None:
        raise RuntimeError(
            "TimescaleDB 已启用，但 device_property_history 已有数据；"
            "请先执行带停机窗口的数据迁移，再转换为 hypertable"
        )

    _ensure_partition_key_constraint(connection)
    connection.execute(
        f"""
        SELECT create_hypertable(
            'public.device_property_history',
            by_range(
                'recorded_at',
                {DEVICE_PROPERTY_HISTORY_CHUNK_INTERVAL_MS}::bigint
            ),
            if_not_exists => TRUE
        )
        """
    )
    _ensure_time_index(connection)
    return True


__all__ = [
    "DEVICE_PROPERTY_HISTORY",
    "DEVICE_PROPERTY_HISTORY_TIME_COLUMN",
    "DEVICE_PROPERTY_HISTORY_TIME_INDEX",
    "DEVICE_PROPERTY_HISTORY_CHUNK_INTERVAL_MS",
    "ensure_device_property_history_hypertable",
]
