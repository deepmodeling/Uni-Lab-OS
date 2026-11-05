"""runtime.db v2 → v3：新增 workflow_publication，既有数据原样保留。"""

from __future__ import annotations

import sqlite3
from dataclasses import replace

from unilabos.server.database import DATABASE_SPECS, initialize_database
from unilabos.server.database.tables.runtime import RUNTIME_DATABASE, RUNTIME_MIGRATIONS
from unilabos.server.services.runtime.workflow.service import WorkflowService

WORKFLOW_UUID = "82000000-0000-4000-8000-000000000001"


def test_v2_runtime_database_migrates_to_v3_and_keeps_rows(tmp_path) -> None:
    path = tmp_path / "runtime.db"
    v2 = replace(
        RUNTIME_DATABASE,
        tables=tuple(item for item in RUNTIME_DATABASE.tables if item.name != "workflow_publication"),
        schema_version=2,
        migrations=RUNTIME_MIGRATIONS[:1],
    )
    connection = initialize_database(path, v2)
    with connection:
        connection.execute(
            "INSERT INTO workflow(uuid, create_time, update_time, meta_data, name, tags, revision)"
            " VALUES (?, 't0', 't0', '{}', '旧工作流', '[]', 4)",
            (WORKFLOW_UUID,),
        )
        connection.execute("UPDATE schema_identity SET checksum = 'runtime-v2' WHERE database_key = 'runtime'")
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
    connection.close()

    service = WorkflowService(path)
    try:
        assert service.get_workflow(WORKFLOW_UUID)["revision"] == 4
        assert service.list_workflow_publications(WORKFLOW_UUID) == []
    finally:
        service.close()

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == DATABASE_SPECS["runtime"].schema_version
        assert connection.execute("SELECT checksum FROM schema_identity").fetchone()[0] == (
            DATABASE_SPECS["runtime"].checksum
        )
        history = [tuple(row) for row in connection.execute("SELECT version, description FROM schema_migration")]
        assert history[0] == (3, "新增 workflow_publication：工作流发布的不可变修订快照")
        assert connection.execute("SELECT name FROM workflow").fetchall() == [("旧工作流",)]
    finally:
        connection.close()
    target = DATABASE_SPECS["runtime"].schema_version
    assert list(tmp_path.glob(f"runtime.db.pre-v{target}-*.bak")), "迁移前应留备份"
