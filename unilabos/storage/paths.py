"""集中解析四类 SQLite 路径，避免各存储适配器自行选择权威文件。"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class RuntimeStorageConflict(RuntimeError):
    """运行时存储路径无法安全收敛。"""


def _read(config: Mapping[str, Any] | object, *names: str) -> Any:
    """按候选名读取映射键或对象属性，找不到时返回 ``None``。"""

    if isinstance(config, Mapping):
        for name in names:
            if name in config:
                return config[name]
        return None
    for name in names:
        if hasattr(config, name):
            return getattr(config, name)
    return None


def _normalize_path(value: Any, *, working_dir: Path, home_dir: Path) -> Path:
    """把配置路径规范化为绝对路径，但不创建文件或目录。"""

    text = str(value).strip()
    if text == "~":
        path = home_dir
    elif text.startswith("~/"):
        path = home_dir / text[2:]
    else:
        path = Path(text)
        if not path.is_absolute():
            path = working_dir / path
    return path.resolve()


def _optional_path(
    value: Any,
    *,
    default: Path,
    working_dir: Path,
    home_dir: Path,
) -> Path | None:
    """解析可关闭的路径；空值用默认路径，``off`` 返回 ``None``。"""

    if value is None:
        return default.resolve()
    text = str(value).strip()
    if not text or text.lower() == "off":
        return None
    return _normalize_path(text, working_dir=working_dir, home_dir=home_dir)


def _sqlite_has_domain_facts(path: Path) -> bool:
    """只读判断数据库是否已有非迁移领域事实，不创建文件。

    ``path`` 不存在或为空时返回 ``False``；数据库不可读时失败关闭，避免
    在无法证明权威归属时选择另一个工作流数据库。
    """

    if not path.is_file() or path.stat().st_size == 0:
        return False
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        try:
            tables = connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                  AND name NOT LIKE 'sqlite_%'
                  AND name != 'schema_migration'
                ORDER BY name
                """
            ).fetchall()
            for (table_name,) in tables:
                quoted_name = '"' + str(table_name).replace('"', '""') + '"'
                if connection.execute(
                    f"SELECT 1 FROM {quoted_name} LIMIT 1"
                ).fetchone():
                    return True
            return False
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise RuntimeStorageConflict(f"无法检查工作流数据库 {path}: {error}") from error


def _deduplicate(paths: list[Path]) -> list[Path]:
    """按输入顺序去重路径，返回新的列表且不修改调用方数据。"""

    result: list[Path] = []
    for path in paths:
        if path not in result:
            result.append(path)
    return result


@dataclass(frozen=True)
class RuntimeStoragePaths:
    """四类 SQLite 的唯一解析结果，由组合根注入各存储适配器。"""

    working_dir: Path
    workflow_db: Path
    inventory_db: Path | None
    device_state_db: Path | None
    edge_control_db: Path
    legacy_workflow_history_enabled: bool = True

    @classmethod
    def resolve(cls, config: Mapping[str, Any] | object) -> RuntimeStoragePaths:
        """一次解析四类路径，并对分叉的工作流权威失败关闭。

        ``config`` 可为启动参数映射或配置对象；返回不可变的统一路径对象。
        本函数只读探测已有 SQLite，不创建、迁移或修改任何数据库。
        """

        raw_working_dir = _read(config, "working_dir")
        if raw_working_dir is None or not str(raw_working_dir).strip():
            raise ValueError("working_dir is required")
        working_dir = Path(str(raw_working_dir)).expanduser().resolve()

        raw_home_dir = _read(config, "home_dir")
        home_dir = (
            Path(str(raw_home_dir)).expanduser().resolve()
            if raw_home_dir is not None and str(raw_home_dir).strip()
            else Path.home().resolve()
        )
        default_root = home_dir / ".unilabos"

        raw_workflow_db = _read(
            config,
            "edge_workflow_history_db",
            "workflow_db",
        )
        workflow_text = "" if raw_workflow_db is None else str(raw_workflow_db).strip()
        workflow_history_enabled = workflow_text.lower() != "off"
        explicit_workflow_db = bool(workflow_text and workflow_text.lower() != "off")
        default_workflow_db = (default_root / "workflow_history.db").resolve()
        preferred_workflow_db = (
            _normalize_path(
                workflow_text,
                working_dir=working_dir,
                home_dir=home_dir,
            )
            if explicit_workflow_db
            else default_workflow_db
        )
        legacy_workflow_candidates = _deduplicate(
            [
                default_workflow_db,
                (working_dir / "workflow_history.db").resolve(),
            ]
        )
        factual_legacy_databases = [
            path
            for path in legacy_workflow_candidates
            if _sqlite_has_domain_facts(path)
        ]
        if len(factual_legacy_databases) > 1:
            joined = ", ".join(str(path) for path in factual_legacy_databases)
            raise RuntimeStorageConflict(
                "检测到两个或更多含领域事实的工作流数据库；"
                f"拒绝自动选择，请先检查并迁移：{joined}"
            )
        if explicit_workflow_db:
            if (
                preferred_workflow_db not in legacy_workflow_candidates
                and _sqlite_has_domain_facts(preferred_workflow_db)
                and factual_legacy_databases
            ):
                raise RuntimeStorageConflict(
                    "显式工作流数据库和旧路径同时包含工作流权威（Workflow Authority）事实"
                )
            workflow_db = preferred_workflow_db
        elif factual_legacy_databases:
            workflow_db = factual_legacy_databases[0]
        else:
            workflow_db = preferred_workflow_db

        inventory_db = _optional_path(
            _read(config, "edge_inventory_db", "inventory_db"),
            default=default_root / "inventory.db",
            working_dir=working_dir,
            home_dir=home_dir,
        )
        device_state_db = _optional_path(
            _read(config, "edge_device_state_db", "device_state_db"),
            default=default_root / "device_state.db",
            working_dir=working_dir,
            home_dir=home_dir,
        )

        raw_edge_control_db = _read(config, "edge_state_db", "edge_control_db")
        edge_control_text = (
            "" if raw_edge_control_db is None else str(raw_edge_control_db).strip()
        )
        if edge_control_text.lower() == "off":
            raise ValueError("edge_control_db cannot be disabled")
        edge_control_db = (
            _normalize_path(
                edge_control_text,
                working_dir=working_dir,
                home_dir=home_dir,
            )
            if edge_control_text
            else (working_dir / "edge_control.db").resolve()
        )

        return cls(
            working_dir=working_dir,
            workflow_db=workflow_db,
            inventory_db=inventory_db,
            device_state_db=device_state_db,
            edge_control_db=edge_control_db,
            legacy_workflow_history_enabled=workflow_history_enabled,
        )


__all__ = ["RuntimeStorageConflict", "RuntimeStoragePaths"]
