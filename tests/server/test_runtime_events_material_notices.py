"""/api/v1/events 的物料与日志通知：权威进程合并、限速后写进 frontend_event，与工作流事件同一序列。

每一帧都带整数 id：断线期间的物料变化在重连（Last-Event-ID）后补发，过期 / 超前游标照常 stream.reset。
通知列出这段账本里变了的物料，前端只重读这些（空列表：没有物料变化）；列不准时省略，前端整体重读。
账本由一个物料权威一条的共享轮询线程读，写入器是它的常驻订阅者。
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from unilabos.protocol.runtime.logs import RuntimeLogNotice
from unilabos.server.api.runtime import events
from unilabos.server.api.runtime.workflow import create_workflow_app
from unilabos.server.services.materials.ledger_watch import LedgerBatch, LedgerWatch
from unilabos.server.services.runtime.workflow.frontend_notices import (
    MATERIAL_UUIDS_LIMIT,
    FrontendNoticeWriter,
    material_notice_data,
)
from unilabos.server.services.runtime.workflow.service import WorkflowService

NOW = "2026-11-04T00:00:00Z"


class _Ledger:
    """最小物料权威：账本末尾、账本条目、Site → 所属物料。"""

    def __init__(self, tail: int, rows=(), sites=None):
        self.tail = tail
        self.rows = []
        self.sites = dict(sites or {})
        self.add(*rows)

    def add(self, *rows) -> None:
        for row in rows:
            self.rows.append(SimpleNamespace(sequence=row[0], aggregate_type=row[1], aggregate_uuid=row[2],
                                             delta=row[3] if len(row) > 3 else {}))
            self.tail = max(self.tail, row[0])

    def latest_ledger_sequence(self) -> int:
        return self.tail

    def changes(self, *, after_sequence: int = 0, limit: int = 100):
        return [row for row in self.rows if row.sequence > after_sequence][:limit]

    def get_site(self, site_uuid: str, *, include_deleted: bool = False):
        owner = self.sites.get(site_uuid)
        return SimpleNamespace(owner_material_uuid=owner) if owner else None


class _NoLogs:
    def subscribe(self, listener):
        return lambda: None


class _Hub:
    listener = None

    def subscribe(self, listener):
        self.listener = listener
        return lambda: setattr(self, "listener", None)


class _ManualWatch(LedgerWatch):
    """手动投递账本批次：记下订阅起点与回调，由测试决定每一批什么时候到。"""

    def __init__(self) -> None:
        super().__init__(None)
        self.after: object = "unset"
        self.deliver = None

    def subscribe(self, after, deliver):
        self.after, self.deliver = after, deliver
        return lambda: None


@pytest.fixture
def workflow(monkeypatch):
    monkeypatch.setattr(events, "shutting_down", lambda: False)
    service = WorkflowService(":memory:")
    yield service
    service.close()


def _rows(service: WorkflowService, event: str | None = None) -> list[dict]:
    items = service.list_events(after_id=0, limit=1000)["items"]
    return [item for item in items if event is None or item["event"] == event]


def _wait(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("等待超时")
        time.sleep(0.02)


@contextmanager
def _writer(service: WorkflowService, *, hub=None, materials=None, interval: float = 0.1):
    writer = FrontendNoticeWriter(service, materials=materials, hub=hub or _NoLogs(), interval=interval)
    writer.start()
    if isinstance(materials, LedgerWatch) and not isinstance(materials, _ManualWatch):
        _wait(lambda: materials._subscribers)  # 订阅在轮询线程上登记
    try:
        yield writer
    finally:
        writer.stop()


def _material_rows_after(service: WorkflowService, recorded: int, ledger: _Ledger) -> list[dict]:
    """先记下一条“上次写到 recorded”的物料通知，再启动写入器，返回它续写出的物料通知。"""

    service.append_frontend_event("material.changed", {"sequence": recorded})
    with _writer(service, materials=LedgerWatch(ledger, interval=0.05)):
        _wait(lambda: len(_rows(service, "material.changed")) >= 2)
    return [row["data"] for row in _rows(service, "material.changed")[1:]]


async def _read(app, headers: list[tuple[bytes, bytes]], steps) -> str:
    """直接驱动 ASGI 应用读 SSE：依次等待 steps 中每个 (predicate, action)，文本满足 predicate 后执行 action。"""

    chunks: list[str] = []
    changed = asyncio.Event()
    disconnect = asyncio.Event()

    async def receive():
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body":
            chunks.append(message.get("body", b"").decode("utf-8"))
            changed.set()

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
        "scheme": "http", "path": "/api/v1/events", "raw_path": b"/api/v1/events", "query_string": b"",
        "root_path": "", "headers": headers, "client": ("127.0.0.1", 1), "server": ("testserver", 80),
    }
    task = asyncio.create_task(app(scope, receive, send))
    try:
        for predicate, action in steps:
            while not predicate("".join(chunks)):
                changed.clear()
                await asyncio.wait_for(changed.wait(), 5.0)
            if action is not None:
                await asyncio.to_thread(action)
    finally:
        disconnect.set()
        await asyncio.wait_for(task, 5.0)
    return "".join(chunks)


def _frame(text: str, sequence: int) -> dict:
    block = next(item for item in text.split("\n\n") if item.startswith(f"id: {sequence}\nevent:"))
    lines = dict(line.split(": ", 1) for line in block.split("\n"))
    return {"event": lines["event"], "data": json.loads(lines["data"])}


# ── 物料通知 ─────────────────────────────────────────────────────────


def test_a_material_change_while_disconnected_arrives_after_reconnect(workflow):
    """断线期间账本前进：重连带 Last-Event-ID 后收到带 id 的 material.changed，不靠 REST 补读兜底。"""

    workflow.append_frontend_event("workflow.task.changed", {"workflow_task_uuid": "t"})
    ledger = _Ledger(3)
    app = create_workflow_app(workflow)
    with _writer(workflow, materials=LedgerWatch(ledger, interval=0.05)):
        first = asyncio.run(_read(app, [], [(lambda body: "id: 1\n\n" in body, None)]))
        assert "material.changed" not in first
        # 客户端已断开；这时物料变了
        ledger.add((4, "material", "plate"))
        _wait(lambda: _rows(workflow, "material.changed"))
        again = asyncio.run(_read(app, [(b"last-event-id", b"1")], [(lambda body: "id: 2\nevent:" in body, None)]))
    assert _frame(again, 2) == {"event": "material.changed", "data": {"sequence": 4, "material_uuids": ["plate"]}}
    assert "stream.reset" not in again


def test_ledger_window_names_the_changed_materials(workflow):
    ledger = _Ledger(
        7,
        rows=[(5, "material", "old"), (6, "site", "site-1"), (6, "material", "plate"), (7, "lot", "lot-1"), (7, "material", "plate")],
        sites={"site-1": "deck"},
    )
    # 区间 (5, 7]：Site 归到所属台面，批次不进物料图，重复的物料去重
    assert _material_rows_after(workflow, 5, ledger) == [{"sequence": 7, "material_uuids": ["deck", "plate"]}]


@pytest.mark.parametrize("recorded,ledger,expected", [
    # 模板变化：列不准，整体重读
    (7, _Ledger(9, rows=[(8, "material", "plate"), (9, "resource_template", "tpl")]), {"sequence": 9}),
    # 只动了批次 / 预留：空列表（没有物料要重读），不是省略（整体重读）
    (7, _Ledger(9, rows=[(8, "lot", "lot-1"), (9, "reservation", "r-1")]), {"sequence": 9, "material_uuids": []}),
    # 拓扑边：列出两端物料
    (3, _Ledger(4, rows=[(4, "link", "l-1", {"source_material_uuid": "pump", "target_material_uuid": "flask"})]),
     {"sequence": 4, "material_uuids": ["flask", "pump"]}),
    # 账本比上次记下的还短（库被重置）：整体重读
    (40, _Ledger(2), {"sequence": 2}),
])
def test_material_uuids_semantics(workflow, recorded, ledger, expected):
    assert _material_rows_after(workflow, recorded, ledger) == [expected]


def test_more_materials_than_the_limit_ask_for_a_full_reload():
    many = [f"m{index:04d}" for index in range(MATERIAL_UUIDS_LIMIT + 1)]
    assert material_notice_data(LedgerBatch(after=0, upto=9, changed=many)) == {"sequence": 9}
    assert material_notice_data(LedgerBatch(after=0, upto=9, changed=many[:3])) == {
        "sequence": 9, "material_uuids": many[:3],
    }


def test_batches_inside_the_interval_merge_into_one_row(workflow):
    watch = _ManualWatch()
    with _writer(workflow, materials=watch, interval=0.4):
        watch.deliver(LedgerBatch(after=0, upto=1, changed=["a"]))
        # 空闲后的第一批立即写
        _wait(lambda: len(_rows(workflow, "material.changed")) == 1, timeout=0.35)
        watch.deliver(LedgerBatch(after=1, upto=2, changed=["b"]))
        watch.deliver(LedgerBatch(after=2, upto=3, changed=["c", "b"]))
        _wait(lambda: len(_rows(workflow, "material.changed")) == 2)
        watch.deliver(LedgerBatch(after=3, upto=4, changed=["d"]))
        watch.deliver(LedgerBatch(after=4, upto=5, changed=None))
        _wait(lambda: len(_rows(workflow, "material.changed")) == 3)
        time.sleep(0.5)
    assert [row["data"] for row in _rows(workflow, "material.changed")] == [
        {"sequence": 1, "material_uuids": ["a"]},
        {"sequence": 3, "material_uuids": ["b", "c"]},
        {"sequence": 5},  # 合并进来的一批列不准，整批整体重读
    ]


def test_writer_resumes_after_the_recorded_ledger_sequence(workflow):
    watch = _ManualWatch()
    with _writer(workflow, materials=watch):
        pass
    assert watch.after is None  # 没有记下的物料通知：从当前账本末尾开始
    workflow.append_frontend_event("material.changed", {"sequence": 12, "material_uuids": []})
    with _writer(workflow, materials=watch):
        pass
    assert watch.after == 12


def test_a_failed_write_is_retried_without_losing_the_window(workflow, monkeypatch):
    watch = _ManualWatch()
    real = workflow.append_frontend_event
    failures = iter([RuntimeError("database is locked")])

    def flaky(event, data):
        error = next(failures, None)
        if error is not None:
            raise error
        return real(event, data)

    monkeypatch.setattr(workflow, "append_frontend_event", flaky)
    with _writer(workflow, materials=watch, interval=0.05):
        watch.deliver(LedgerBatch(after=0, upto=1, changed=["a"]))
        time.sleep(0.1)
        watch.deliver(LedgerBatch(after=1, upto=2, changed=["b"]))
        _wait(lambda: _rows(workflow, "material.changed"))
    assert [row["data"] for row in _rows(workflow, "material.changed")] == [
        {"sequence": 2, "material_uuids": ["a", "b"]},
    ]


def test_a_broken_ledger_does_not_stop_log_notices(workflow):
    class Broken(_Ledger):
        def latest_ledger_sequence(self) -> int:
            raise RuntimeError("materials.db is locked")

    hub = _Hub()
    writer = FrontendNoticeWriter(workflow, materials=LedgerWatch(Broken(0), interval=0.05), hub=hub, interval=0.1)
    writer.start()
    try:
        hub.listener(RuntimeLogNotice(source_ids=["host"]))
        _wait(lambda: _rows(workflow, "runtime.logs.changed"))
    finally:
        writer.stop()


# ── 日志通知 ─────────────────────────────────────────────────────────


def test_log_notices_get_ids_and_are_coalesced(workflow):
    hub = _Hub()
    app = create_workflow_app(workflow)
    with _writer(workflow, hub=hub, interval=0.3):
        for _ in range(100):
            hub.listener(RuntimeLogNotice(source_ids=["host"]))
            hub.listener(RuntimeLogNotice(source_ids=["slave:a"], sources_changed=True))
        time.sleep(0.8)
        rows = _rows(workflow, "runtime.logs.changed")
        text = asyncio.run(_read(app, [(b"last-event-id", b"0")], [
            (lambda body: f"id: {rows[-1]['id']}\nevent:" in body, None),
        ]))
    # 200 条通知最多两行：空闲后的第一条立即写，其余在间隔内并进下一行
    assert 1 <= len(rows) <= 2
    assert set().union(*(row["data"]["source_ids"] for row in rows)) == {"host", "slave:a"}
    assert any(row["data"]["sources_changed"] for row in rows)
    for row in rows:
        assert _frame(text, row["id"]) == {"event": "runtime.logs.changed", "data": row["data"]}


def test_log_rows_are_rate_limited(workflow):
    hub = _Hub()
    with _writer(workflow, hub=hub, interval=0.3):
        started = time.monotonic()
        while time.monotonic() - started < 1.0:
            hub.listener(RuntimeLogNotice(source_ids=["host"]))
            time.sleep(0.01)
        time.sleep(0.4)
    rows = _rows(workflow, "runtime.logs.changed")
    # 1 秒持续刷日志、间隔 0.3 秒：约 4 行，而不是上百行
    assert 2 <= len(rows) <= 6


# ── 同一序列：续传、重置与唤醒 ────────────────────────────────────────


def test_stale_and_ahead_cursors_reset_over_notice_rows(workflow):
    for index in range(3):
        workflow.append_frontend_event("material.changed", {"sequence": index + 1, "material_uuids": ["m"]})
    for _ in range(2):
        workflow.append_frontend_event("runtime.logs.changed", RuntimeLogNotice(source_ids=["host"]).model_dump())
    # 保留窗口 4..5（清理一视同仁地越过物料与日志通知）
    workflow.prune_frontend_events(before="0001-01-01T00:00:00.000000Z", max_count=2, batch_size=1000)
    app = create_workflow_app(workflow)

    expired = asyncio.run(_read(app, [(b"last-event-id", b"1")], [
        (lambda body: "event: stream.reset" in body,
         lambda: workflow.append_frontend_event("material.changed", {"sequence": 9})),
        (lambda body: "id: 6\nevent: material.changed" in body, None),
    ]))
    reset = next(block for block in expired.split("\n\n") if "event: stream.reset" in block)
    assert reset.startswith("id: 5\n") and json.loads(reset.split("data: ", 1)[1]) == {"reason": "cursor_expired"}
    assert "id: 4\nevent:" not in expired

    ahead = asyncio.run(_read(app, [(b"last-event-id", b"99")], [(lambda body: "event: stream.reset" in body, None)]))
    reset = next(block for block in ahead.split("\n\n") if "event: stream.reset" in block)
    assert reset.startswith("id: 6\n") and json.loads(reset.split("data: ", 1)[1]) == {"reason": "cursor_ahead"}


def test_every_frame_on_the_stream_has_an_id(workflow):
    workflow.append_frontend_event("workflow.task.changed", {"workflow_task_uuid": "t"})
    workflow.append_frontend_event("material.changed", {"sequence": 4, "material_uuids": ["m"]})
    workflow.append_frontend_event("runtime.logs.changed", RuntimeLogNotice(source_ids=["host"]).model_dump())
    app = create_workflow_app(workflow)
    text = asyncio.run(_read(app, [(b"last-event-id", b"0")], [(lambda body: "id: 3\nevent:" in body, None)]))
    blocks = [block for block in text.split("\n\n") if block and not block.startswith(("retry:", ":"))]
    assert [block.split("\n", 1)[0] for block in blocks] == ["id: 1", "id: 2", "id: 3"]
    assert [_frame(text, index)["event"] for index in (1, 2, 3)] == [
        "workflow.runtime.changed", "material.changed", "runtime.logs.changed",
    ]


def test_a_written_notice_wakes_the_stream_at_once(workflow):
    """写入器写完一行就唤醒本进程的连接，不等约 1 秒一轮的轮询。"""

    hub = _Hub()
    app = create_workflow_app(workflow)
    marks: dict[str, float] = {}

    def notify() -> None:
        time.sleep(0.1)  # 让连接先完成首轮读取，进入等待
        marks["sent"] = time.monotonic()
        hub.listener(RuntimeLogNotice(source_ids=["host"]))

    def arrived(body: str) -> bool:
        if "event: runtime.logs.changed" in body:
            marks.setdefault("arrived", time.monotonic())
            return True
        return False

    with _writer(workflow, hub=hub, interval=0.1):
        asyncio.run(_read(app, [], [(lambda body: "id: 0\n\n" in body, notify), (arrived, None)]))
    assert marks["arrived"] - marks["sent"] < 0.5


# ── 账本共享轮询（物料通知与 /api/v1/materials/events 共用） ─────────────


def test_real_authority_names_the_placed_material_and_the_site_owner(tmp_path):
    from uuid import uuid4

    from unilabos.client.materials import LocalMaterialsClient
    from unilabos.protocol.materials import (
        InventoryMutation,
        MaterialIdentityWrite,
        MaterialNodeCreate,
        MaterialPlacement,
        MaterialTreeCreate,
        ResourceTemplateWrite,
    )
    from unilabos.resources.adapters.device_site import apply_device_authority_state
    from unilabos.resources.resource_tracker import ResourceDict, ResourceDictInstance
    from unilabos.server.services.materials import MaterialsService

    def mutation(operation: str) -> InventoryMutation:
        command = str(uuid4())
        return InventoryMutation(
            command_uuid=command, effect_key=f"{operation}:{command}", operation=operation,
            actor_type="test", actor_uuid="material-notices",
        )

    service = MaterialsService(tmp_path / "materials.db")
    try:
        service.put_template(
            mutation("put_template"), ResourceTemplateWrite(name="sample_tpl", display_name="sample", class_name="Resource")
        )
        device_uuid = str(uuid4())
        device = ResourceDictInstance(ResourceDict.model_validate({
            "id": "notice-device", "uuid": device_uuid, "name": "notice-device", "type": "device",
            "class": "notice_device", "template_name": "notice_device", "config": {}, "data": {}, "extra": {},
            "sites": None, "sites_initialized": False,
        }))
        apply_device_authority_state(
            device, {"available_sites": [{"index": 1, "label": "s1"}]}, "notice_device",
            gateway=LocalMaterialsClient(service),
        )
        [site] = service.get_material(device_uuid).sites
        notices = LedgerWatch(service)
        before = notices.latest()

        created = service.create_tree(
            mutation("create_material_tree"),
            MaterialTreeCreate(
                nodes=[MaterialNodeCreate(client_ref="sample", identity=MaterialIdentityWrite(
                    resource_id=f"sample-{uuid4().hex[:8]}", template_name="sample_tpl", config={"type": "Container"},
                ))],
                placement=MaterialPlacement(parent_material_uuid=device_uuid, site=site.site_uuid),
            ),
        )
        sample_uuid = created.data.root_material_uuid
        after = notices.latest()
        assert after > before
        assert set(notices.read(before, after).changed) == {sample_uuid, device_uuid}

        # 拓扑边也进账本：通知列出两端物料
        service.upsert_link(source_material_uuid=sample_uuid, target_material_uuid=device_uuid, link_type="fluid")
        linked = notices.latest()
        assert linked == after + 1
        assert notices.read(after, linked).changed == sorted({sample_uuid, device_uuid})
    finally:
        service.close()


def test_many_connections_share_one_ledger_read_per_tick():
    """N 个订阅者共用一次读：账本末尾的读取次数与连接数无关。"""

    import threading

    class _Counting(_Ledger):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.reads = 0
            self.lock = threading.Lock()

        def latest_ledger_sequence(self) -> int:
            with self.lock:
                self.reads += 1
            return self.tail

    ledger = _Counting(3)
    watch = LedgerWatch(ledger, interval=0.1)
    received: list[list] = [[] for _ in range(20)]
    releases = [watch.subscribe(3, received[index].append) for index in range(20)]
    time.sleep(0.3)
    ledger.add((4, "material", "m"))
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not all(batches for batches in received):
        time.sleep(0.02)
    reads_with_twenty = ledger.reads

    assert all([batch.changed for batch in batches] == [["m"]] for batches in received)
    # 1 次建立基线 + 每 0.1 秒一轮；20 个连接各读一次的话会远多于这个数
    assert reads_with_twenty < 20
    for release in releases:
        release()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and watch._thread is not None:
        time.sleep(0.02)
    assert watch._thread is None  # 没有订阅者，轮询线程退出


def test_a_late_subscriber_catches_up_from_its_own_cursor():
    ledger = _Ledger(5, rows=[(4, "material", "a"), (5, "material", "b")])
    watch = LedgerWatch(ledger, interval=0.05)
    first: list = []
    release = watch.subscribe(5, first.append)
    late: list = []
    release_late = watch.subscribe(3, late.append)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not late:
        time.sleep(0.02)
    release()
    release_late()
    assert first == []
    assert [(batch.after, batch.upto, batch.changed) for batch in late] == [(3, 5, ["a", "b"])]


def test_a_subscriber_without_a_cursor_starts_at_the_shared_progress():
    ledger = _Ledger(5, rows=[(4, "material", "a"), (5, "material", "b")])
    watch = LedgerWatch(ledger, interval=0.05)
    received: list = []
    release = watch.subscribe(None, received.append)
    try:
        _wait(lambda: watch._subscribers)
        assert received == []  # 不补读订阅前的账本
        ledger.add((6, "material", "c"))
        _wait(lambda: received)
        assert [(batch.after, batch.upto, batch.changed) for batch in received] == [(5, 6, ["c"])]
    finally:
        release()


# ── 装配：写入器在持有 Workflow Authority 的进程里 ────────────────────────


def test_the_authority_writes_host_log_notices_and_material_notices(tmp_path, monkeypatch):
    from unittest.mock import Mock
    from uuid import uuid4

    from unilabos.config.config import BasicConfig
    from unilabos.protocol.materials import InventoryMutation, ResourceTemplateWrite
    from unilabos.server.database import ServerDatabasePaths
    from unilabos.server.edge_dispatch import EdgeControlService
    from unilabos.server.services.materials.ledger_watch import ledger_watch
    from unilabos.server.execution_composition import (
        reset_for_test,
        setup_execution_backend,
        setup_local_scheduler,
        setup_materials_service,
    )

    paths = ServerDatabasePaths.resolve(tmp_path)
    monkeypatch.setattr(BasicConfig, "backend", "hostlink")
    monkeypatch.setattr(BasicConfig, "machine_name", "test-host")
    monkeypatch.setattr(BasicConfig, "server_database_paths", paths)
    try:
        materials = setup_materials_service(database_paths=paths)
        backend = setup_execution_backend(database_paths=paths)
        workflow_service = setup_local_scheduler(backend=backend)
        _wait(lambda: ledger_watch(materials)._subscribers)  # 写入器常驻订阅物料账本

        # Host 的日志通知经 runtime.v1 runtime_logs_changed 到达权威
        EdgeControlService(payload_client=Mock()).handle_message("runtime_logs_changed", {"source_ids": ["slave:a"]})
        _wait(lambda: _rows(workflow_service, "runtime.logs.changed"))
        assert "slave:a" in _rows(workflow_service, "runtime.logs.changed")[0]["data"]["source_ids"]

        command = str(uuid4())
        materials.put_template(
            InventoryMutation(command_uuid=command, effect_key=f"put_template:{command}", operation="put_template",
                              actor_type="test", actor_uuid="frontend-notices"),
            ResourceTemplateWrite(name="notice_tpl", display_name="notice", class_name="Resource"),
        )
        _wait(lambda: _rows(workflow_service, "material.changed"))
        # 模板变化：列不准，整体重读；sequence 是物料账本末尾
        assert _rows(workflow_service, "material.changed")[0]["data"] == {
            "sequence": materials.latest_ledger_sequence(),
        }
    finally:
        reset_for_test()
