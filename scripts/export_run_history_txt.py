"""将运行历史 SQLite 数据库导出为便于检索的 UTF-8 文本。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


TABLES = (
    "run_sessions",
    "action_records",
    "action_events",
    "scheduler_incidents",
    "scheduler_timings",
)


def _display_value(name: str, value: Any) -> str:
    if value is None:
        return ""
    if name.endswith("_at") or name == "timestamp":
        try:
            readable = datetime.fromtimestamp(int(value) / 1000).astimezone().isoformat(
                timespec="milliseconds"
            )
            return f"{value} ({readable})"
        except (TypeError, ValueError, OSError):
            pass
    if name.endswith("_json"):
        try:
            return json.dumps(json.loads(value), ensure_ascii=False, indent=2)
        except (TypeError, json.JSONDecodeError):
            pass
    return str(value)


def export_database(database: Path, output: Path) -> dict[str, int]:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    counts: dict[str, int] = {}
    try:
        with output.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(f"Uni-Lab 运行历史导出\n数据库: {database}\n\n")
            for table in TABLES:
                rows = connection.execute(f'SELECT * FROM "{table}"').fetchall()
                counts[table] = len(rows)
                handle.write(f"{'=' * 80}\n{table} ({len(rows)} 条)\n{'=' * 80}\n")
                for index, row in enumerate(rows, 1):
                    handle.write(f"\n--- {table} #{index} ---\n")
                    for name in row.keys():
                        handle.write(f"{name}: {_display_value(name, row[name])}\n")
                handle.write("\n")
    finally:
        connection.close()
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    counts = export_database(args.database, args.output)
    print(json.dumps({"output": str(args.output), "counts": counts}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
