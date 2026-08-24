"""微后端四个 SQLite 文件的集中路径解析与隔离校验。"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from unilabos.server.database.tables import (
    HISTORY_DATABASE,
    MATERIALS_DATABASE,
    RUNTIME_DATABASE,
    TELEMETRY_DATABASE,
)


class DatabaseLayoutConflict(ValueError):
    """两个数据库职责被配置到了同一个物理文件。"""


_SPECS = (
    RUNTIME_DATABASE,
    MATERIALS_DATABASE,
    TELEMETRY_DATABASE,
    HISTORY_DATABASE,
)


def _resolve_path(value: str | Path, *, root: Path, key: str) -> Path:
    text = str(value).strip()
    if not text:
        raise ValueError(f"database path for {key!r} cannot be empty")
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def _same_physical_path(left: Path, right: Path) -> bool:
    if os.path.normcase(str(left)) == os.path.normcase(str(right)):
        return True
    return left.exists() and right.exists() and os.path.samefile(left, right)


def validate_distinct_database_paths(paths: Mapping[str, Path]) -> None:
    """拒绝职责复用同一 SQLite 文件，包括已存在文件的硬链接。"""

    items = tuple(paths.items())
    for index, (left_key, left_path) in enumerate(items):
        for right_key, right_path in items[index + 1 :]:
            if _same_physical_path(left_path, right_path):
                raise DatabaseLayoutConflict(
                    f"database roles {left_key!r} and {right_key!r} "
                    f"resolve to the same physical file: {left_path}"
                )


@dataclass(frozen=True)
class ServerDatabasePaths:
    """由组合根一次解析并注入各数据库 writer 的四库路径。"""

    root: Path
    runtime_db: Path
    materials_db: Path
    telemetry_db: Path
    history_db: Path

    @classmethod
    def resolve(
        cls,
        root: str | Path,
        overrides: Mapping[str, str | Path] | None = None,
    ) -> "ServerDatabasePaths":
        root_text = str(root).strip()
        if not root_text:
            raise ValueError("database root cannot be empty")
        root_path = Path(root_text).expanduser().resolve()
        configured = dict(overrides or {})
        expected_keys = {spec.key for spec in _SPECS}
        unknown_keys = set(configured) - expected_keys
        if unknown_keys:
            unknown = ", ".join(sorted(unknown_keys))
            raise ValueError(f"unknown database path override(s): {unknown}")

        resolved = {
            spec.key: _resolve_path(
                configured.get(spec.key, spec.filename),
                root=root_path,
                key=spec.key,
            )
            for spec in _SPECS
        }
        validate_distinct_database_paths(resolved)
        return cls(
            root=root_path,
            runtime_db=resolved["runtime"],
            materials_db=resolved["materials"],
            telemetry_db=resolved["telemetry"],
            history_db=resolved["history"],
        )

    def as_mapping(self) -> dict[str, Path]:
        return {
            "runtime": self.runtime_db,
            "materials": self.materials_db,
            "telemetry": self.telemetry_db,
            "history": self.history_db,
        }


__all__ = [
    "DatabaseLayoutConflict",
    "ServerDatabasePaths",
    "validate_distinct_database_paths",
]
