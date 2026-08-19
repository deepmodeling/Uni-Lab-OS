"""四库之间只使用逻辑 UUID，不能形成跨 SQLite 外键或附加数据库。"""

from __future__ import annotations

from unilabos.server.database import DATABASE_SPECS, initialize_database


def test_foreign_keys_only_reference_tables_in_the_same_database(tmp_path) -> None:
    for spec in DATABASE_SPECS.values():
        connection = initialize_database(tmp_path / spec.filename, spec)
        try:
            local_tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            for table in local_tables:
                if table.startswith("sqlite_"):
                    continue
                for foreign_key in connection.execute(
                    f'PRAGMA foreign_key_list("{table}")'
                ):
                    assert foreign_key[2] in local_tables
        finally:
            connection.close()


def test_schema_never_attaches_another_database() -> None:
    for spec in DATABASE_SPECS.values():
        statements = "\n".join(spec.statements()).upper()
        assert "ATTACH DATABASE" not in statements


def test_durability_profile_matches_database_write_role() -> None:
    assert DATABASE_SPECS["runtime"].synchronous == "FULL"
    assert DATABASE_SPECS["materials"].synchronous == "FULL"
    assert DATABASE_SPECS["telemetry"].synchronous == "NORMAL"
    assert DATABASE_SPECS["history"].synchronous == "NORMAL"
