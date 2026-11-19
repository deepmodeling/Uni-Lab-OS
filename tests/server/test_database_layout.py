"""四个数据库职责必须由组合根解析到互不相同的物理文件。"""

from __future__ import annotations

import pytest

from unilabos.server.database import DatabaseLayoutConflict, ServerDatabasePaths


def test_database_paths_are_resolved_once_from_root(tmp_path) -> None:
    paths = ServerDatabasePaths.resolve(
        tmp_path,
        {"telemetry": "high-write/device-telemetry.db"},
    )

    assert paths.runtime_db == (tmp_path / "runtime.db").resolve()
    assert paths.materials_db == (tmp_path / "materials.db").resolve()
    assert paths.telemetry_db == (tmp_path / "high-write/device-telemetry.db").resolve()
    assert paths.history_db == (tmp_path / "history.db").resolve()
    assert len(set(paths.as_mapping().values())) == 4


def test_database_roles_cannot_share_one_file(tmp_path) -> None:
    with pytest.raises(DatabaseLayoutConflict, match="same physical file"):
        ServerDatabasePaths.resolve(
            tmp_path,
            {"runtime": "shared.db", "materials": "shared.db"},
        )


def test_unknown_database_role_is_rejected(tmp_path) -> None:
    with pytest.raises(ValueError, match="unknown database path override"):
        ServerDatabasePaths.resolve(tmp_path, {"artifact": "artifact.db"})
