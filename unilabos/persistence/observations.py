"""设备状态库的连续采样与来源租约模式，独立于标量变化保留策略。"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS observation_descriptor (
 material_uuid TEXT PRIMARY KEY, generation TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS observation_source_group (
 name TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS observation_source_operation (
 id TEXT PRIMARY KEY, request TEXT NOT NULL, result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS device_observation_latest (
 material_uuid TEXT NOT NULL, property TEXT NOT NULL, body TEXT NOT NULL,
 received_at REAL NOT NULL, PRIMARY KEY(material_uuid,property));
CREATE TABLE IF NOT EXISTS device_observation_event (
 cursor INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS device_observation_record (
 id INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS observation_legacy_hold (job_id TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS device_observation_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

LEGACY_TABLES = {
    "twin_descriptor": "observation_descriptor",
    "twin_group": "observation_source_group",
    "twin_operation": "observation_source_operation",
    "twin_latest": "device_observation_latest",
    "twin_event": "device_observation_event",
    "twin_record": "device_observation_record",
    "twin_action": "observation_legacy_hold",
    "twin_meta": "device_observation_meta",
}


def migrate_legacy_observations(db):
    """原子保留描述、完整采样、游标及未决阻断；禁止借迁移重发动作。"""
    with db.transaction():
        for old, new in LEGACY_TABLES.items():
            if not db.table_exists(old):
                continue
            # 新旧数据同时存在时拒绝覆盖，交由运维处理库合并。
            if db.execute(f"SELECT 1 FROM {new} LIMIT 1").fetchone():
                raise ValueError("观测数据迁移冲突: " + new)
            db.execute(f"INSERT INTO {new} SELECT * FROM {old}")
            db.execute(f"DROP TABLE {old}")
