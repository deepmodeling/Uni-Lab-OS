"""四个独立 SQLite 文件共用的声明式 schema、版本迁移与建库入口。

每个库声明 ``schema_version`` 与一串线性迁移（Flyway 式）。0.13 发布线上四个库都以
v1 为基线、尚无迁移；之后的结构变化从 v2 起追加迁移。库内版本记在
``PRAGMA user_version``，结构指纹记在 ``schema_identity.checksum``：

- 库内版本不高于代码版本且指纹一致：直接复用；
- 库内版本低于代码版本且迁移连续：先用 SQLite backup API 备份为
  ``<file>.pre-v<N>-<时间戳>.bak``，再在一个事务里依次执行迁移，执行后与当前声明逐表
  比对列、索引与触发器并做外键检查，任何一项不符都整体回滚；
- 没有迁移路径（指纹漂移、版本缺失或高于代码版本）：按 ``on_unmigratable`` 处理。``refuse``
  拒绝启动并保留旧库原样；``rebuild`` 把旧库改名为 ``<file>.bak-<时间戳>`` 后重建空库。
  ``BasicConfig.database_rebuild_on_mismatch`` 可在开发期显式允许 ``refuse`` 库重建。
"""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Literal, Optional, Sequence

logger = logging.getLogger(__name__)

#: 迁移审计表：不属于任何领域声明，也不计入结构指纹。
MIGRATION_HISTORY_TABLE = "schema_migration"
_INFRASTRUCTURE_TABLES = frozenset({MIGRATION_HISTORY_TABLE})


class DatabaseIdentityConflict(RuntimeError):
    """物理 SQLite 文件已属于其他职责，拒绝打开以免误清数据。"""


class DatabaseMigrationRequired(RuntimeError):
    """库内结构与当前代码不一致且没有迁移路径；为保护数据拒绝启动。"""


class DatabaseMigrationFailed(RuntimeError):
    """迁移执行或迁移后的结构校验失败；事务已回滚，库保持迁移前状态。"""


@dataclass(frozen=True)
class TableSpec:
    """一张表及其索引的不可变建表规格。"""

    name: str
    create_sql: str
    indexes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Migration:
    """把库从 ``version - 1`` 升到 ``version`` 的一步迁移。

    ``apply`` 在迁移事务内执行，只能使用 ``connection.execute``：``executescript``
    会先提交当前事务，破坏整体回滚。迁移期间外键检查关闭，结束前统一做
    ``PRAGMA foreign_key_check``。
    """

    version: int
    description: str
    apply: Callable[[sqlite3.Connection], None]


def sql_migration(version: int, description: str, *statements: str) -> Migration:
    """由若干条 SQL 组成的迁移；每条语句单独执行，处于同一事务内。"""

    def apply(connection: sqlite3.Connection) -> None:
        for statement in statements:
            connection.execute(statement)

    return Migration(version=version, description=description, apply=apply)


def rebuild_table_migration(
    version: int,
    description: str,
    *,
    table: str,
    create_sql: str,
    indexes: Sequence[str] = (),
) -> Migration:
    """SQLite 改 CHECK / 列约束的标准重建：建新表 → 拷贝同名列 → 删旧表 → 改名 → 重建索引。

    ``create_sql`` / ``indexes`` 必须是该版本**冻结**的语句（字面量，不要引用会继续演进的
    表规格），表名为 ``table``。依赖本表的视图与本表的触发器按迁移前的定义原样重建
    （删表后 ``ALTER TABLE RENAME`` 不允许存在引用缺失表的视图）。迁移期间外键检查已关闭，
    引用本表的外键改名后照常指向新表；结束前统一 ``foreign_key_check``。旧行不满足新约束
    时插入失败，整体回滚。
    """

    pattern = re.compile(rf"CREATE TABLE IF NOT EXISTS {re.escape(table)}\s*\(")
    if len(pattern.findall(create_sql)) != 1:
        raise ValueError(f"create_sql 必须恰好包含一条 CREATE TABLE IF NOT EXISTS {table}")
    staging = f"{table}__migrating"
    staged_sql = pattern.sub(f'CREATE TABLE "{staging}" (', create_sql, count=1)

    def apply(connection: sqlite3.Connection) -> None:
        views = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'view' AND sql IS NOT NULL ORDER BY rowid"
        ).fetchall()
        triggers = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ? AND sql IS NOT NULL "
            "ORDER BY rowid",
            (table,),
        ).fetchall()
        for name, _sql in views:
            connection.execute(f'DROP VIEW "{name}"')
        old_columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]
        connection.execute(f'DROP TABLE IF EXISTS "{staging}"')
        connection.execute(staged_sql)
        new_columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{staging}")')]
        shared = ", ".join(f'"{column}"' for column in new_columns if column in old_columns)
        connection.execute(f'INSERT INTO "{staging}" ({shared}) SELECT {shared} FROM "{table}"')
        connection.execute(f'DROP TABLE "{table}"')
        connection.execute(f'ALTER TABLE "{staging}" RENAME TO "{table}"')
        for statement in indexes:
            connection.execute(statement)
        for (sql,) in triggers:
            connection.execute(sql)
        for _name, sql in views:
            connection.execute(sql)

    return Migration(version=version, description=description, apply=apply)


@dataclass(frozen=True)
class DatabaseSpec:
    """一个物理 SQLite 文件的完整 schema。

    ``contract_version`` 表示表结构之外的行内容契约（JSON 列的形状、幂等键的
    派生规则、枚举取值等）；它计入结构指纹。``schema_version`` 是迁移序号，从 1
    （无迁移的基线）开始：DDL 或 ``contract_version`` 变化时把它 +1，并在
    ``migrations`` 里给出对应迁移。
    """

    key: str
    filename: str
    role: str
    synchronous: str
    tables: tuple[TableSpec, ...]
    contract_version: int = 1
    schema_version: int = 1
    migrations: tuple[Migration, ...] = field(default=())
    on_unmigratable: Literal["refuse", "rebuild"] = "rebuild"

    def __post_init__(self) -> None:
        if self.schema_version < 1:
            raise ValueError(f"{self.key}: schema_version must be >= 1")
        versions = [migration.version for migration in self.migrations]
        if versions != list(range(2, 2 + len(versions))) or (versions and versions[-1] != self.schema_version):
            raise ValueError(
                f"{self.key}: migrations must cover versions 2..{self.schema_version} in order, got {versions}"
            )
        if self.on_unmigratable not in ("refuse", "rebuild"):
            raise ValueError(f"{self.key}: on_unmigratable must be 'refuse' or 'rebuild'")

    @property
    def table_names(self) -> tuple[str, ...]:
        return tuple(table.name for table in self.tables)

    def statements(self) -> Iterable[str]:
        for table in self.tables:
            yield table.create_sql.strip()
            yield from (index.strip() for index in table.indexes)

    @property
    def checksum(self) -> str:
        canonical = "\n\n".join(
            (f"-- contract_version={self.contract_version}", *self.statements())
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()


SCHEMA_IDENTITY_TABLE = TableSpec(
    name="schema_identity",
    create_sql="""
        CREATE TABLE IF NOT EXISTS schema_identity (
            database_key TEXT PRIMARY KEY CHECK (TRIM(database_key) <> ''),
            checksum TEXT NOT NULL CHECK (TRIM(checksum) <> ''),
            applied_at_ms INTEGER NOT NULL CHECK (applied_at_ms >= 0)
        )
    """,
)

_MIGRATION_HISTORY_DDL = f"""
    CREATE TABLE IF NOT EXISTS {MIGRATION_HISTORY_TABLE} (
        version INTEGER PRIMARY KEY CHECK (version >= 2),
        description TEXT NOT NULL,
        from_checksum TEXT NOT NULL,
        to_checksum TEXT NOT NULL,
        applied_at_ms INTEGER NOT NULL CHECK (applied_at_ms >= 0)
    )
"""


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        if not str(row[0]).startswith("sqlite_")
    }


def _open_connection(
    path: Path, spec: DatabaseSpec, timeout: float
) -> sqlite3.Connection:
    connection = sqlite3.connect(
        str(path),
        timeout=timeout,
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute(f"PRAGMA synchronous = {spec.synchronous}")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


@dataclass(frozen=True)
class _SchemaState:
    kind: Literal["empty", "current", "migratable", "unmigratable"]
    stored_version: int = 0
    stored_checksum: str = ""
    reason: str = ""


def _user_version(connection: sqlite3.Connection) -> int:
    return int(connection.execute("PRAGMA user_version").fetchone()[0])


def _inspect(connection: sqlite3.Connection, spec: DatabaseSpec) -> _SchemaState:
    """判断现有库能否直接复用、能否迁移；身份属于其他库时抛错兜底。"""

    tables = _table_names(connection) - _INFRASTRUCTURE_TABLES
    if not tables:
        return _SchemaState("empty")
    if "schema_identity" not in tables:
        return _SchemaState("unmigratable", reason="缺少 schema_identity，无法确认库的职责与结构")
    rows = connection.execute(
        "SELECT database_key, checksum FROM schema_identity"
    ).fetchall()
    keys = {str(row[0]) for row in rows}
    if keys - {spec.key}:
        conflict = ", ".join(sorted(keys))
        raise DatabaseIdentityConflict(
            f"database file belongs to {conflict!r}, cannot open it as {spec.key!r}"
        )
    if not rows:
        if tables - {"schema_identity"}:
            return _SchemaState("unmigratable", reason="有领域表但缺少身份行，初始化不完整")
        return _SchemaState("empty")
    stored_checksum = str(rows[0][1])
    stored_version = _user_version(connection)
    if stored_version > spec.schema_version:
        # 指纹一致也不复用：库由更新的代码或版本基线压平前的开发版本写入，残留的
        # schema_migration 记录会与之后同号的迁移冲突。
        return _SchemaState(
            "unmigratable", stored_version, stored_checksum, "库内版本高于代码版本（更新的代码或压平前的开发库）"
        )
    if stored_checksum == spec.checksum:
        return _SchemaState("current", stored_version, stored_checksum)
    if 1 <= stored_version < spec.schema_version:
        return _SchemaState("migratable", stored_version, stored_checksum)
    if stored_version == 0:
        reason = "库内没有迁移版本且结构指纹不同（迁移机制之前的旧结构）"
    elif stored_version == spec.schema_version:
        reason = "库内版本与代码版本相同，但结构指纹不同"
    else:
        reason = "结构指纹不同且没有迁移路径"
    return _SchemaState("unmigratable", stored_version, stored_checksum, reason)


def _retire_database_files(path: Path) -> Optional[Path]:
    """把旧库改名为 ``<file>.bak-<时间戳>`` 而不是直接删除：schema 漂移重建不应该悄悄丢数据。

    WAL / SHM 跟着主文件一起改名（保持 ``<file>-wal`` 后缀约定），旧库仍可用 sqlite 直接打开。
    返回备份主文件路径；没有旧文件时返回 None。
    """

    if not path.exists():
        for suffix in ("-wal", "-shm"):
            stale = Path(f"{path}{suffix}")
            if stale.exists():
                stale.unlink()
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = Path(f"{path}.bak-{stamp}")
    counter = 1
    while backup.exists():
        backup = Path(f"{path}.bak-{stamp}-{counter}")
        counter += 1
    for suffix in ("", "-wal", "-shm"):
        source = Path(f"{path}{suffix}")
        if source.exists():
            source.replace(Path(f"{backup}{suffix}"))
    return backup


def _backup_before_migration(connection: sqlite3.Connection, path: Path, target_version: int) -> Path:
    """用 SQLite backup API 做一致性快照（包含 WAL 中未检查点的页）。"""

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = Path(f"{path}.pre-v{target_version}-{stamp}.bak")
    counter = 1
    while backup.exists():
        backup = Path(f"{path}.pre-v{target_version}-{stamp}-{counter}.bak")
        counter += 1
    target = sqlite3.connect(str(backup))
    try:
        connection.backup(target)
    finally:
        target.close()
    return backup


def _normalize_sql(sql: Optional[str]) -> str:
    text = re.sub(r"\s+", " ", sql or "").strip().rstrip(";")
    return re.sub(r"\s*([(),])\s*", r"\1", text).lower().replace("if not exists ", "")


def _structure(connection: sqlite3.Connection, table_names: Iterable[str]) -> dict[str, object]:
    """列（名称、类型、非空、默认值、主键位）与索引 / 触发器定义；与列顺序无关。"""

    result: dict[str, object] = {}
    for table in sorted(table_names):
        columns = sorted(
            (str(row[1]), str(row[2]).upper(), int(row[3]), row[4], int(row[5]))
            for row in connection.execute(f"PRAGMA table_xinfo('{table}')")
        )
        others = sorted(
            (str(row[0]), str(row[1]), _normalize_sql(row[2]))
            for row in connection.execute(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE tbl_name = ? AND type IN ('index', 'trigger') AND sql IS NOT NULL",
                (table,),
            )
        )
        result[table] = (columns, others)
    return result


def _views(connection: sqlite3.Connection) -> list[tuple[str, str]]:
    return sorted(
        (str(row[0]), _normalize_sql(row[1]))
        for row in connection.execute("SELECT name, sql FROM sqlite_master WHERE type = 'view'")
    )


def _verify_structure(connection: sqlite3.Connection, spec: DatabaseSpec) -> None:
    """与按当前声明新建的内存库比对。CHECK 等表级约束不在比对范围内，由迁移作者负责。"""

    reference = sqlite3.connect(":memory:")
    try:
        for statement in spec.statements():
            reference.execute(statement)
        expected = _structure(reference, spec.table_names)
        expected_views = _views(reference)
    finally:
        reference.close()
    if _views(connection) != expected_views:
        raise DatabaseMigrationFailed(f"{spec.key}: 迁移后视图与当前声明不一致")
    present = _table_names(connection) - _INFRASTRUCTURE_TABLES
    extra = sorted(present - set(spec.table_names))
    missing = sorted(set(spec.table_names) - present)
    if extra or missing:
        raise DatabaseMigrationFailed(f"{spec.key}: 迁移后表集合不符，多出 {extra}，缺少 {missing}")
    actual = _structure(connection, spec.table_names)
    different = [table for table in spec.table_names if actual[table] != expected[table]]
    if different:
        raise DatabaseMigrationFailed(f"{spec.key}: 迁移后结构与当前声明不一致：{different}")


def _migrate(connection: sqlite3.Connection, path: Path, spec: DatabaseSpec, state: _SchemaState) -> None:
    chain = [migration for migration in spec.migrations if migration.version > state.stored_version]
    backup = _backup_before_migration(connection, path, spec.schema_version)
    logger.warning(
        "数据库 %s 从版本 %s 迁移到 %s（%s）；迁移前备份 %s",
        path, state.stored_version, spec.schema_version,
        "；".join(f"v{item.version} {item.description}" for item in chain), backup,
    )
    if connection.in_transaction:
        connection.commit()
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(_MIGRATION_HISTORY_DDL)
            checksum = state.stored_checksum
            for migration in chain:
                migration.apply(connection)
                if not connection.in_transaction:
                    raise DatabaseMigrationFailed(
                        f"{spec.key} v{migration.version}: 迁移提前提交了事务（不要使用 executescript）"
                    )
                connection.execute(
                    f"INSERT INTO {MIGRATION_HISTORY_TABLE}"
                    "(version, description, from_checksum, to_checksum, applied_at_ms) VALUES (?, ?, ?, ?, ?)",
                    (migration.version, migration.description, checksum, spec.checksum, int(time.time() * 1000)),
                )
            _verify_structure(connection, spec)
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise DatabaseMigrationFailed(f"{spec.key}: 迁移后存在外键违例 {len(violations)} 条")
            connection.execute(
                "UPDATE schema_identity SET checksum = ?, applied_at_ms = ? WHERE database_key = ?",
                (spec.checksum, int(time.time() * 1000), spec.key),
            )
            connection.execute(f"PRAGMA user_version = {int(spec.schema_version)}")
            connection.commit()
        except BaseException as exc:
            if connection.in_transaction:
                connection.rollback()
            if isinstance(exc, DatabaseMigrationFailed):
                raise
            raise DatabaseMigrationFailed(f"{spec.key}: 迁移失败，已回滚（备份 {backup}）：{exc}") from exc
    finally:
        connection.execute("PRAGMA foreign_keys = ON")


def _rebuild_allowed() -> bool:
    try:
        from unilabos.config.config import BasicConfig
    except Exception:  # noqa: BLE001 - 配置不可用时按默认拒绝
        return False
    value = getattr(BasicConfig, "database_rebuild_on_mismatch", False)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _apply_schema(connection: sqlite3.Connection, spec: DatabaseSpec) -> None:
    with connection:
        for statement in spec.statements():
            connection.execute(statement)
        connection.execute(
            """
            INSERT OR IGNORE INTO schema_identity(
                database_key, checksum, applied_at_ms
            ) VALUES (?, ?, CAST(strftime('%s', 'now') AS INTEGER) * 1000)
            """,
            (spec.key, spec.checksum),
        )
        if _user_version(connection) != spec.schema_version:
            connection.execute(f"PRAGMA user_version = {int(spec.schema_version)}")


def initialize_database(
    path: str | Path,
    spec: DatabaseSpec,
    *,
    timeout: float = 30.0,
) -> sqlite3.Connection:
    """创建或打开一个领域数据库；结构变化时迁移，没有迁移路径时按库的策略拒绝或重建。"""

    database_path = Path(path).expanduser().resolve()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = _open_connection(database_path, spec, timeout)
    try:
        state = _inspect(connection, spec)
        if state.kind == "migratable":
            _migrate(connection, database_path, spec, state)
    except BaseException:
        connection.close()
        raise
    if state.kind == "unmigratable":
        connection.close()
        if spec.on_unmigratable == "refuse" and not _rebuild_allowed():
            raise DatabaseMigrationRequired(
                f"数据库 {database_path}（{spec.key}：{spec.role}）与当前代码的结构不一致且无法迁移："
                f"{state.reason}（库内版本 {state.stored_version}，代码版本 {spec.schema_version}）。"
                "为避免丢失数据已拒绝启动，旧库保持原样。可选：升级到包含对应迁移的版本；"
                f"或用 --{spec.key}_db / --server_database_root 指向新位置；确认旧数据不再需要时，"
                "设置 BasicConfig.database_rebuild_on_mismatch = True"
                "（或环境变量 UNILABOS_BASICCONFIG_DATABASE_REBUILD_ON_MISMATCH=true），旧库会改名备份后重建。"
            )
        backup = _retire_database_files(database_path)
        logger.warning(
            "数据库 %s 的 schema 与当前代码不一致（%s），已重建；旧库保留为 %s（确认不需要后可删除）",
            database_path,
            state.reason,
            backup or "（无旧文件）",
        )
        connection = _open_connection(database_path, spec, timeout)
    try:
        _apply_schema(connection, spec)
        return connection
    except BaseException:
        connection.close()
        raise
