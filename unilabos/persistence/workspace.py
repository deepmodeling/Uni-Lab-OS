"""Workspace Host 对配置、只读检查和显式重置使用相同数据库选择。"""

from __future__ import annotations

import json
import os
import runpy
from contextlib import contextmanager
from pathlib import Path

from unilabos.persistence.config import DatabaseSettings


def database_selection_path(workspace: Path) -> Path:
    return workspace / ".unilabos" / "runtime" / "workbench" / "database.json"


def save_workspace_database(workspace: Path, settings: DatabaseSettings) -> None:
    """保存本轮进程共用的选择，供 Host 重启、只读检查及组件恢复使用。"""
    from unilabos.workspace_host.model import atomic_write_json

    atomic_write_json(database_selection_path(workspace), {
        "database_type": settings.database_type,
        "database_url": settings.database_url if settings.database_type == "postgresql" else "",
    })


def workspace_settings(
    workspace: Path, *, active: bool = True, overrides: dict[str, str] | None = None,
) -> DatabaseSettings:
    """活动进程使用已选数据库；新启动按命令行、环境变量、配置文件解析。"""
    selection = database_selection_path(workspace)
    if active and selection.is_file():
        return DatabaseSettings(**json.loads(selection.read_text(encoding="utf-8")))
    overrides = overrides or {}
    path = Path(
        os.environ.get("UNILABOS_BASICCONFIG_CONFIG_PATH")
        or workspace / "deployment" / "local_config.py"
    )
    config = runpy.run_path(str(path)).get("BasicConfig") if path.is_file() else None
    return DatabaseSettings(
        overrides.get("database_type", os.environ.get(
            "UNILABOS_BASICCONFIG_DATABASE_TYPE",
            getattr(config, "database_type", "sqlite"),
        )),
        overrides.get("database_url", os.environ.get(
            "UNILABOS_BASICCONFIG_DATABASE_URL", getattr(config, "database_url", "")
        )),
    )


@contextmanager
def use_workspace_database(workspace: Path):
    from unilabos.persistence.config import use_database_settings

    with use_database_settings(workspace_settings(workspace)):
        yield


def database_environment(workspace: Path) -> dict[str, str]:
    settings = workspace_settings(workspace)
    return {
        "UNILABOS_BASICCONFIG_DATABASE_TYPE": settings.database_type,
        "UNILABOS_BASICCONFIG_DATABASE_URL": settings.database_url,
    }


def migrate_sqlite_backup(source: Path, destination: Path) -> None:
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(source)) as original, closing(
        sqlite3.connect(destination)
    ) as backup:
        original.backup(backup)
    destination.chmod(0o600)


def reset_postgresql_domain(settings: DatabaseSettings) -> list[str]:
    """仅由 local.reset-state 完成安全检查、停止进程后调用。"""
    import psycopg
    from psycopg import sql

    from unilabos.persistence.schema import catalog

    managed = {
        item["name"]
        for scope in ("inventory", "workflow", "history", "device", "authority")
        for item in catalog()[scope]
        if item["type"] == "table"
    }
    managed.update(
        {
            "workflow_node_job_result",
            "workflow_node_job_feedback_history",
            "workflow_manual_confirmation",
            # 这些表由运行时能力按需创建，不会出现在基础 schema catalog 中。
            "published_workflow_contract",
            "workflow_human_interaction",
            "scheduler_timing_observations",
            "station_material_export_identity",
            "station_material_export",
            "station_material_export_item",
            "station_material_boundary_receipt",
        }
    )
    from unilabos.persistence.center_schema import TABLES as CENTER_TABLES

    managed.update(CENTER_TABLES)
    with psycopg.connect(settings.database_url) as connection:
        connection.execute("SELECT pg_advisory_xact_lock(836271945)")
        existing = {
            row[0]
            for row in connection.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='public'"
            )
        }
        tables = sorted(managed & existing)
        if tables:
            # 不使用 CASCADE，防止删除与本应用无关的引用表。
            connection.execute(
                sql.SQL("TRUNCATE {} RESTART IDENTITY").format(
                    sql.SQL(",").join(
                        sql.Identifier("public", table) for table in tables
                    )
                )
            )
    return tables
