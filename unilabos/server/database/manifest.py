"""从真实 v1 SQLite 数据库导出稳定、只读的 schema manifest。"""

from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Sequence

from unilabos.server.database.schema import DatabaseSpec, initialize_database
from unilabos.server.database.tables import (
    HISTORY_DATABASE,
    MATERIALS_DATABASE,
    RUNTIME_DATABASE,
    TELEMETRY_DATABASE,
)


SCHEMA_MANIFEST_VERSION = 1
DATABASE_SPEC_ORDER = (
    RUNTIME_DATABASE,
    MATERIALS_DATABASE,
    TELEMETRY_DATABASE,
    HISTORY_DATABASE,
)


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _table_manifest(
    connection: sqlite3.Connection,
    table_name: str,
) -> dict[str, Any]:
    rows = connection.execute(
        f"PRAGMA table_info({_quote_identifier(table_name)})"
    ).fetchall()
    if not rows:
        raise RuntimeError(f"SQLite table {table_name!r} has no columns")
    columns = [
        {
            "name": str(row[1]),
            "type": str(row[2]),
            "nullable": not bool(row[3]),
            "default": row[4],
            "pk_position": int(row[5]),
        }
        for row in rows
    ]
    primary_key = [
        str(column["name"])
        for column in sorted(
            (column for column in columns if int(column["pk_position"]) > 0),
            key=lambda column: int(column["pk_position"]),
        )
    ]
    return {
        "name": table_name,
        "columns": columns,
        "primary_key": primary_key,
    }


def _database_manifest(
    database_path: Path,
    spec: DatabaseSpec,
) -> dict[str, Any]:
    connection = initialize_database(database_path, spec)
    try:
        table_names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        expected_names = set(spec.table_names)
        if table_names != expected_names:
            raise RuntimeError(
                f"{spec.key} sqlite_master tables differ from DatabaseSpec: "
                f"expected={sorted(expected_names)!r}, actual={sorted(table_names)!r}"
            )
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        migration = connection.execute(
            "SELECT name,checksum FROM schema_migration "
            "WHERE database_key=? AND version=?",
            (spec.key, user_version),
        ).fetchone()
        if migration is None:
            raise RuntimeError(f"{spec.key} has no schema_migration identity")
        return {
            "key": spec.key,
            "filename": spec.filename,
            "role": spec.role,
            "schema_version": user_version,
            "synchronous": spec.synchronous,
            "migration_name": str(migration[0]),
            "schema_checksum": str(migration[1]),
            "tables": [
                _table_manifest(connection, table_name)
                for table_name in spec.table_names
            ],
        }
    finally:
        connection.close()


def export_schema_manifest(
    specs: Sequence[DatabaseSpec] = DATABASE_SPEC_ORDER,
) -> dict[str, Any]:
    """创建临时数据库并从 SQLite 元数据导出四库 schema。"""

    with tempfile.TemporaryDirectory(prefix="unilabos-schema-manifest-") as directory:
        root = Path(directory)
        return {
            "manifest_version": SCHEMA_MANIFEST_VERSION,
            "source": "unilabos.server.database.migrations.v1",
            "databases": [
                _database_manifest(root / spec.filename, spec) for spec in specs
            ],
        }


def render_schema_manifest(manifest: dict[str, Any] | None = None) -> str:
    """以可复现格式渲染 manifest JSON。"""

    return json.dumps(
        manifest if manifest is not None else export_schema_manifest(),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"


def write_schema_manifest(path: str | Path) -> Path:
    """把只读 schema manifest 写入显式目标文件。"""

    output_path = Path(path).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_schema_manifest(), encoding="utf-8")
    return output_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        help="输出 JSON 文件；省略时写入 stdout",
    )
    args = parser.parse_args(argv)
    if args.output is None:
        print(render_schema_manifest(), end="")
    else:
        print(write_schema_manifest(args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DATABASE_SPEC_ORDER",
    "SCHEMA_MANIFEST_VERSION",
    "export_schema_manifest",
    "render_schema_manifest",
    "write_schema_manifest",
]
