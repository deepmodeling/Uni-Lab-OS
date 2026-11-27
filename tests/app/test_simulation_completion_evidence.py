"""证据聚合与关系读回的公开合同：OS 汇总证据，但不提升未观测的结论。"""
import dataclasses
import sqlite3
from contextlib import contextmanager

import pytest

from unilabos.app.scheduler.inventory.runtime_view import InventoryView
from unilabos.simulation.completion import EvidenceBundle, EvidenceRejected, build_evidence_bundle
from unilabos.simulation.motion import CollisionStatus, EvidenceLevel


def bundle(**overrides):
    values = dict(
        action_id="action-1",
        evidence_level=EvidenceLevel.PHYSICS_OBSERVED,
        collision_status=CollisionStatus.CLEAR,
        settlement_receipt="receipt:1",
        observation_refs=("readback:1",),
        failed_reason=None,
    )
    values.update(overrides)
    return build_evidence_bundle(**values)


def test_complete_evidence_bundle_is_accepted():
    evidence = bundle()
    assert isinstance(evidence, EvidenceBundle)
    assert evidence.action_id == "action-1"
    assert evidence.evidence_level is EvidenceLevel.PHYSICS_OBSERVED
    assert evidence.collision_status is CollisionStatus.CLEAR
    assert evidence.settlement_receipt == "receipt:1"
    assert evidence.observation_refs == ("readback:1",)
    assert evidence.failed_reason is None


def test_evidence_bundle_is_frozen():
    evidence = bundle()
    with pytest.raises(dataclasses.FrozenInstanceError):
        evidence.failed_reason = "late_change"


@pytest.mark.parametrize(
    "level",
    [EvidenceLevel.KINEMATIC_REPLAY, EvidenceLevel.PHYSICS_OBSERVED, EvidenceLevel.PHYSICAL_QUALIFIED],
)
def test_settled_evidence_requires_settlement_receipt(level):
    with pytest.raises(EvidenceRejected):
        bundle(evidence_level=level, settlement_receipt=None)


def test_physical_qualified_requires_observation_refs():
    with pytest.raises(EvidenceRejected):
        bundle(evidence_level=EvidenceLevel.PHYSICAL_QUALIFIED, observation_refs=())


@pytest.mark.parametrize("status", list(CollisionStatus))
def test_physical_qualified_requires_clear_collision(status):
    if status is CollisionStatus.CLEAR:
        assert bundle(evidence_level=EvidenceLevel.PHYSICAL_QUALIFIED).collision_status is status
        return
    with pytest.raises(EvidenceRejected):
        bundle(evidence_level=EvidenceLevel.PHYSICAL_QUALIFIED, collision_status=status)


def test_failed_evidence_cannot_be_physical_qualified():
    with pytest.raises(EvidenceRejected):
        bundle(evidence_level=EvidenceLevel.PHYSICAL_QUALIFIED, failed_reason="readback_timeout")


class _Store:
    """最小库存替身：真实 SQLite `material` 表与只读事务，不引入 OS 重依赖。"""

    def __init__(self, connection):
        self.connection = connection

    def query_all(self, sql, parameters=()):
        return []

    @contextmanager
    def transaction(self):
        yield self.connection


class _Cursor:
    """真实游标的代理，记录被读取的关系查询语句。"""

    def __init__(self, cursor, statements, sql):
        self._cursor = cursor
        self._statements = statements
        self._sql = sql

    def fetchone(self):
        self._statements.append(self._sql)
        return self._cursor.fetchone()


class _RecordingConnection:
    """包装真实连接以记录关系查询语句，供测试断言读回只发 SELECT。"""

    def __init__(self, connection):
        self._connection = connection
        self.statements = []

    def execute(self, sql, parameters=()):
        return _Cursor(self._connection.execute(sql, parameters), self.statements, sql)


def inventory_store():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE material (uuid TEXT PRIMARY KEY, parent_uuid TEXT, meta_data TEXT, deleted_at TEXT)")
    connection.executemany(
        "INSERT INTO material VALUES (?,?,?,?)",
        [
            ("scene-rooted", None, "{}", None),
            ("db-rooted", "db-parent", "{}", None),
            ("deleted", None, "{}", "2026-10-25"),
        ],
    )
    return _Store(_RecordingConnection(connection)), connection


def test_relations_merge_scene_parent_without_overwriting_database_parent():
    store, connection = inventory_store()
    scene = {"scene-rooted": "scene-cell", "db-rooted": "scene-other"}
    view = InventoryView(store)

    current = view.relations(["scene-rooted", "db-rooted"], scene_parent=scene.get)

    assert current["scene-rooted"] == "scene-cell"
    assert current["db-rooted"] == "db-parent"
    assert connection.execute("SELECT parent_uuid FROM material WHERE uuid=?", ("db-rooted",)).fetchone()[0] == "db-parent"
    assert connection.total_changes == 3


def test_relations_do_not_resurrect_deleted_or_invent_roots():
    store, connection = inventory_store()
    scene = {"deleted": "scene-cell", "scene-rooted": None}
    view = InventoryView(store)

    current = view.relations(["deleted", "scene-rooted"], scene_parent=scene.get)

    assert "deleted" not in current
    assert current["scene-rooted"] is None
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in store.connection.statements)
    assert connection.total_changes == 3
