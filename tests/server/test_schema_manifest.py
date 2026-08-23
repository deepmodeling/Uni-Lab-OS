"""四库 schema manifest 必须来自真实 SQLite 元数据。"""

from __future__ import annotations

import json

from unilabos.server.database import DATABASE_SPECS
from unilabos.server.database.manifest import (
    SCHEMA_MANIFEST_VERSION,
    export_schema_manifest,
    render_schema_manifest,
    write_schema_manifest,
)


def test_schema_manifest_matches_all_v1_database_specs() -> None:
    manifest = export_schema_manifest()
    assert manifest["manifest_version"] == SCHEMA_MANIFEST_VERSION
    assert manifest["source"] == "unilabos.server.database.migrations.v1"
    assert [database["key"] for database in manifest["databases"]] == [
        "runtime",
        "materials",
        "telemetry",
        "history",
    ]

    for database in manifest["databases"]:
        spec = DATABASE_SPECS[database["key"]]
        assert database["filename"] == spec.filename
        assert database["schema_version"] == spec.version
        assert database["schema_checksum"] == spec.checksum
        assert [table["name"] for table in database["tables"]] == list(
            spec.table_names
        )
        for table in database["tables"]:
            assert table["columns"]
            assert table["primary_key"]
            column_names = [column["name"] for column in table["columns"]]
            assert set(table["primary_key"]).issubset(column_names)


def test_schema_manifest_render_and_file_export_are_deterministic(tmp_path) -> None:
    first = render_schema_manifest()
    second = render_schema_manifest()
    assert first == second
    assert json.loads(first)["manifest_version"] == 1

    output = write_schema_manifest(tmp_path / "nested" / "server-v1-schema.json")
    assert output.read_text(encoding="utf-8") == first
