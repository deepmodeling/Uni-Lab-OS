"""FRAMEWORK_GAPS #26：大物料树挂载不能在提交之后被等待方超时取消，每次取消都要留日志。

``materials.assign`` / Host 下发 ``RESOURCE_APPEND`` 都经 ``run_node_coroutine`` 阻塞等待
``node.append_resource``。挂载以一次根树快照收尾，树大时（1873 个物料的扣电台面）要几十秒。
等待方的超时只能约束提交点之前的阶段（等锁、从权威拉取物料）；本地 PLR 挂上、权威 move
落库之后再取消，本地与权威就各执一词，调用方还拿到一个"失败"。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from uuid import uuid4

import pytest

from unilabos.backend.hostlink.local_runtime import HostLinkDriverSpec, HostLinkLocalRuntime
from unilabos.backend.runtime.resource import AuthorityResourceService
from unilabos.client.materials import LocalMaterialsClient
from unilabos.resources import materials
from unilabos.server.services.materials import MaterialsService
from tests.plr_fixtures import site_deck, tip_rack


class _Bench:
    def __init__(self, **_kwargs) -> None:
        pass


class _SlowAuthority(AuthorityResourceService):
    """把挂载的某一阶段拖慢，模拟大台面（整树快照几十秒）。"""

    def __init__(self, gateway, *, slow_update: float = 0.0, slow_get: float = 0.0) -> None:
        super().__init__(gateway)
        self.slow_update = slow_update
        self.slow_get = slow_get

    def update_resources_sync(self, device_id, device_uuid, resources):
        # append_resource 第 4 步（快照回报）经 materials.update 走同步入口
        if self.slow_update:
            time.sleep(self.slow_update)
        return super().update_resources_sync(device_id, device_uuid, resources)

    async def get_resources(self, device_id, resources_uuid, with_children):
        if self.slow_get:
            await asyncio.sleep(self.slow_get)
        return await super().get_resources(device_id, resources_uuid, with_children)


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def messages(self, level: int) -> list[str]:
        return [item.getMessage() for item in self.records if item.levelno >= level]


@pytest.fixture
def bench(tmp_path):
    service = MaterialsService(tmp_path / "materials.db")
    gateway = LocalMaterialsClient(service)
    authority = _SlowAuthority(gateway)
    runtime = HostLinkLocalRuntime()
    node = runtime.add_driver(HostLinkDriverSpec("bench", _Bench, {}, resource_uuid="bench-uuid"))
    runtime.set_resource_service(authority)
    runtime.start()
    deck_uuid = str(uuid4())
    materials.ensure(site_deck(deck_uuid, "Bench_Deck", ("T1", "T2")), gateway=gateway)
    materials.assign(node, deck_uuid)
    tips = materials.create(tip_rack("tips"), node=node, gateway=gateway)
    records = _Records()
    node.lab_logger().addHandler(records)
    try:
        yield {
            "service": service,
            "authority": authority,
            "node": node,
            "deck_uuid": deck_uuid,
            "tips": tips,
            "records": records,
        }
    finally:
        node.lab_logger().removeHandler(records)
        runtime.stop()
        service.close()


def test_mount_is_not_cancelled_after_the_move_is_committed(bench) -> None:
    """快照回报比等待方的超时还慢：挂载照常走完，调用方拿到成功结果，不留半截状态。"""

    bench["authority"].slow_update = 6.0
    tips = bench["tips"]

    started = time.monotonic()
    result = materials.assign(bench["node"], tips, parent="Bench_Deck", slot="T2", timeout=3.0)

    assert time.monotonic() - started >= 6.0  # 越过超时一直等到快照回报完成

    assert result["created_resource_tree"]
    deck = bench["node"].resource_tracker.uuid_to_resources[bench["deck_uuid"]]
    assert tips.parent is deck
    stored = bench["service"].get_material(tips.unilabos_uuid).material
    assert stored.parent_material_uuid == bench["deck_uuid"]
    warnings = bench["records"].messages(logging.WARNING)
    assert any("提交点" in message for message in warnings), warnings


def test_mount_cancelled_before_commit_is_logged_and_leaves_nothing_behind(bench) -> None:
    """提交点之前超时：取消挂载，本地与权威都不变，取消原因写进日志。"""

    bench["authority"].slow_get = 1.5
    tips = bench["tips"]
    deck_uuid = bench["deck_uuid"]

    with pytest.raises(TimeoutError):
        materials.assign(bench["node"], tips, parent="Bench_Deck", slot="T2", timeout=0.3)
    time.sleep(1.8)  # 让被取消的协程有机会（错误地）继续执行

    assert tips.parent is None
    assert bench["service"].get_material(tips.unilabos_uuid).material.parent_material_uuid != deck_uuid
    errors = bench["records"].messages(logging.ERROR)
    assert any("取消" in message for message in errors), errors


def test_run_node_coroutine_waits_past_the_commit_point() -> None:
    from unilabos.backend.runtime.async_utils import CommitPoint, run_node_coroutine

    class _Node:
        def __init__(self) -> None:
            self.loop = asyncio.new_event_loop()
            self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
            self.thread.start()
            self.logger = logging.getLogger("tests.commit_point")

        def create_task(self, coroutine):
            return asyncio.run_coroutine_threadsafe(coroutine, self.loop)

        def lab_logger(self):
            return self.logger

    node = _Node()
    try:
        committed = CommitPoint()

        async def committing() -> str:
            assert committed.enter()
            await asyncio.sleep(0.5)
            return "done"

        assert run_node_coroutine(node, committing(), 0.1, commit_point=committed) == "done"

        abandoned = CommitPoint()
        reached = threading.Event()

        async def too_late() -> str:
            await asyncio.sleep(0.5)
            reached.set()
            return "late" if abandoned.enter() else "abandoned"

        with pytest.raises(TimeoutError):
            run_node_coroutine(node, too_late(), 0.1, commit_point=abandoned)
        # 等待方放弃之后协程即使还在跑，也进不了提交点
        assert abandoned.enter() is False
    finally:
        node.loop.call_soon_threadsafe(node.loop.stop)
        node.thread.join(timeout=5)
