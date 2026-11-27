"""后端状态服务共享的事务边界；借用现有设备状态库，不创建执行运行时。"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from contextlib import contextmanager
from typing import Any

from unilabos.runtime.observations import StateError


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class ObservationContext:
    def __init__(self, inventory, state_store, *, clock=time.time, retention=10000):
        if retention < 1:
            raise ValueError("retention 必须为正数")
        self.inventory, self.store = inventory, state_store
        self.db, self.lock = state_store.db, state_store._lock
        self.clock, self.retention = clock, retention
        self.session = str(uuid.uuid4())
        self.descriptor_cache = {}
        self.native_revision = None
        self.starting = True
        with self.transaction():
            self.db.execute(
                "INSERT INTO device_observation_meta(key,value) VALUES ('runtime_session',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (self.session,),
            )
            for row in self.db.execute(
                "SELECT name,body FROM observation_source_group"
            ).fetchall():
                group = json.loads(row["body"])
                group.update(
                    ready=False,
                    ready_members=[],
                    recovery_required=True,
                    runtime_session=self.session,
                )
                self.save_group(row["name"], group)
            self.event("runtime.started", {"runtime_session": self.session})
        self.starting = False

    @contextmanager
    def transaction(self):
        # 固定锁序：库存 → 设备状态；两者均复用 OS 的事务与数据库适配。
        try:
            with self.inventory.transaction() as connection:
                with self.lock, self.db.transaction():
                    if not self.starting:
                        current = self.db.execute(
                            "SELECT value FROM device_observation_meta WHERE key='runtime_session'"
                        ).fetchone()
                        if current is None or current[0] != self.session:
                            raise StateError("runtime_superseded")
                    yield connection
        except BaseException:
            with self.lock:
                self.descriptor_cache.clear()
            raise

    def event(self, kind, value):
        cursor = self.db.insert_returning(
            "INSERT INTO device_observation_event(kind,body) VALUES (?,?)",
            (kind, _json(value)),
            "cursor",
        )
        self.db.execute(
            "DELETE FROM device_observation_event WHERE cursor<=?",
            (cursor - self.retention,),
        )
        return int(cursor)

    def native(self, connection):
        nodes, revision = self.inventory.project(connection)
        if revision != self.native_revision:
            self.descriptor_cache.clear()
            self.native_revision = revision
        fingerprint = f"{revision}:" + hashlib.sha256(_json(nodes).encode()).hexdigest()
        old = self.db.execute(
            "SELECT value FROM device_observation_meta WHERE key='graph'"
        ).fetchone()
        if old is None or old[0] != fingerprint:
            self.db.execute(
                "INSERT INTO device_observation_meta(key,value) VALUES ('graph',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (fingerprint,),
            )
            self.event("scene.invalidated", {"graph_version": fingerprint})
        return nodes

    def group(self, name: str) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT body FROM observation_source_group WHERE name=?", (name,)
        ).fetchone()
        if row is None:
            raise StateError("group_missing")
        return json.loads(row[0])

    def save_group(self, name: str, group: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO observation_source_group(name,body) VALUES (?,?) ON CONFLICT(name) DO UPDATE SET body=excluded.body",
            (name, _json(group)),
        )
