"""人工装入 / 取走 / 补量写库后的台面同步：变化投影到挂载物料所属设备，失败不回滚库存。

设备 = 从挂载物料往上最近的设备物料。load / unload 走 transfer 同一条 ``MaterialDeviceSync``，refill 走资源树
``update``（``notify-device`` 同一路径）；下行经 ``downlink``：Host 进程里本地设备直调，调度权威经 Host 中继。
这里的设备是 hostlink 本进程设备表里的替身，下行链路本身是真实的。
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any, Dict, List
from uuid import uuid4

import pytest

from tests.backend.hostlink.test_downlink import _FakeNode
from tests.server.test_material_io_inventory import _identity, container, deck
from tests.server.test_material_io_scheduler import (
    _decide,
    _material_io_jobs,
    _pending,
    _restart,
    _scheduler,
    _stack,
    _status,
    _task,
    _tip_boxes,
)
from tests.server.test_scheduler_inventory_authority import _mutation
from tests.server.test_workflow_parallel_execution import Executor, wait_for
from unilabos.backend.hostlink import downlink, main_hostlink_run
from unilabos.config.config import BasicConfig
from unilabos.protocol.materials import (
    MaterialIdentityWrite,
    MaterialMove,
    MaterialNodeCreate,
    MaterialTreeCreate,
    SiteCreate,
)
from unilabos.server.backend.scheduler.material_io import sync_material_io_device
from unilabos.server.services.materials import MaterialsService

DEVICE = "liquid_handler"


class _DeckDevice(_FakeNode):
    """设备节点替身：按真实 ``DeviceNode`` 的合同记账——同一 ``transfer_uuid:action`` 只应用一次，
    load 把物料放上台面（已有同 UUID 先替换），unload 只移除还在的；资源树 update 记下收到的 UUID。"""

    def __init__(self) -> None:
        super().__init__(DEVICE)
        self.syncs: List[Dict[str, Any]] = []
        self.deck: Dict[str, Any] = {}
        self.applied: set[str] = set()
        self.fail_sync = ""
        self.fail_update = ""

    async def material_sync(self, command: Dict[str, Any]) -> Dict[str, Any]:
        self.syncs.append(command)
        if self.fail_sync:
            raise ValueError(self.fail_sync)
        key = f"{command['transfer_uuid']}:{command['action']}"
        replayed = key in self.applied
        if not replayed:
            for index, material in enumerate(command["material_uuids"]):
                if command["action"] == "load":
                    self.deck[material] = command["destination_site_uuids"][index]
                else:
                    self.deck.pop(material, None)
            self.applied.add(key)
        return {"success": True, "action": command["action"], "replayed": replayed}

    async def apply_resource_tree_update(self, operations: List[Dict[str, Any]]) -> Dict[str, Any]:
        self.tree_sync_calls.append(operations)
        if self.fail_update:
            return {"results": [{"success": False, "action": "update", "error": self.fail_update}], "total": 1}
        return {"results": [{"success": True, "action": "update"}], "total": len(operations)}


@pytest.fixture
def device(monkeypatch) -> _DeckDevice:
    """Host 进程形态：设备在本进程 hostlink 设备表里，没有 HostLink server，也不是权威中继。"""

    node = _DeckDevice()
    runtime = SimpleNamespace(local=SimpleNamespace(get_device=lambda identity: node if identity == DEVICE else None))
    monkeypatch.setattr(BasicConfig, "backend", "hostlink")
    monkeypatch.setattr(main_hostlink_run, "get_runtime", lambda: runtime)
    monkeypatch.setattr(downlink, "get_hostlink_server", lambda: None)
    monkeypatch.setattr(downlink, "_remote_device_relay", False)
    return node


def device_deck(materials: MaterialsService, device_id: str = DEVICE) -> tuple[str, dict[str, str]]:
    """设备物料下挂一块两库位台面（A1、B1）：台面的所属设备是 ``device_id``。"""

    tree = materials.create_tree(_mutation("create_material_tree"), MaterialTreeCreate(nodes=[
        MaterialNodeCreate(client_ref="device", identity=MaterialIdentityWrite(
            resource_id=device_id, name=device_id, template_name="liquid_handler_device",
            resource_type="device", class_name="LiquidHandler")),
        MaterialNodeCreate(client_ref="deck", parent_client_ref="device", identity=_identity("deck", "deck"), sites=[
            SiteCreate(template_name="slot", site_index=0, label="A1"),
            SiteCreate(template_name="slot", site_index=1, label="B1")]),
    ])).data
    mount = tree.client_ref_map["deck"]
    return mount, {site.label: site.site_uuid for site in materials.get_material(mount).sites}


def _outputs(stack, task) -> list[dict]:
    return [run["return_info"]["return_value"] for run in sorted(
        stack.workflow.list_workflow_node_runs(task["uuid"]), key=lambda run: run["topological_index"])]


def test_load_projects_the_new_material_onto_the_owning_device(tmp_path, device) -> None:
    stack, executor = _stack(tmp_path), Executor()
    mount, sites = device_deck(stack.materials)
    scheduler = _scheduler(stack, executor)
    try:
        task = _task(stack, f'ctx.material_io(mode="load", mount="{mount}", site="A1", resource_template="tip_box")')
        confirmation = _pending(stack, task)
        assert device.syncs == []  # 确认前不改库存，也不碰台面
        _decide(stack, confirmation)
        wait_for(lambda: _status(stack, task) == "succeeded")
        [box] = _tip_boxes(stack)
        new = box.material.material_uuid
        [job] = _material_io_jobs(stack, task)
        # 设备从权威拉取新物料挂到同一库位；命令身份 = attempt，与库存命令同一个
        [sync] = device.syncs
        assert (sync["device_id"], sync["action"], sync["material_uuids"], sync["destination_site_uuids"]) == (
            DEVICE, "load", [new], [sites["A1"]])
        assert sync["transfer_uuid"] == f"workflow.material_io:{job['uuid']}"
        assert device.deck == {new: sites["A1"]}
        assert job["return_info"]["return_value"]["device_sync"] == {
            "status": "ok", "device_id": DEVICE, "action": "load", "material_uuids": [new]}
        assert device.tree_sync_calls == [] and executor.dispatched == []
    finally:
        scheduler.stop()
        stack.close()


def test_refill_updates_the_container_then_unload_removes_it_from_the_deck(tmp_path, device) -> None:
    stack, executor = _stack(tmp_path), Executor()
    mount, sites = device_deck(stack.materials)
    bottle = container(stack.materials, mount, sites["B1"], water_ml=20)
    device.deck[bottle] = sites["B1"]
    scheduler = _scheduler(stack, executor)
    try:
        task = _task(stack, f'''
            refilled = ctx.material_io(mode="refill", mount="{mount}", site="B1", material="{bottle}",
                                       reagent={{"quantity": 5, "quantity_unit": "mL"}})
            ctx.material_io(mode="unload", mount="{mount}", site="B1", material=refilled.material,
                            disposition="soft_delete", scope="material_with_contents")
        ''')
        _decide(stack, _pending(stack, task))
        unload = _pending(stack, task, 1)
        # refill：设备按权威当前状态重载这只容器（update 载入绝对数量，不是增量）
        assert device.tree_sync_calls == [[{"action": "update", "data": [bottle]}]] and device.syncs == []
        _decide(stack, unload)
        wait_for(lambda: _status(stack, task) == "succeeded")
        [sync] = device.syncs
        assert (sync["action"], sync["material_uuids"], sync["destination_site_uuids"]) == ("unload", [bottle], [])
        assert device.deck == {}
        refill, removal = _outputs(stack, task)
        assert refill["device_sync"] == {"status": "ok", "device_id": DEVICE, "action": "update",
                                         "material_uuids": [bottle]}
        assert removal["device_sync"] == {"status": "ok", "device_id": DEVICE, "action": "unload",
                                          "material_uuids": [bottle]}
    finally:
        scheduler.stop()
        stack.close()


def test_a_plain_shelf_has_no_device_and_is_skipped(tmp_path, device, caplog) -> None:
    stack, executor = _stack(tmp_path), Executor()
    mount, _sites = deck(stack.materials)  # 根物料，没有设备祖先，也没登记所属设备
    scheduler = _scheduler(stack, executor)
    try:
        task = _task(stack, f'ctx.material_io(mode="load", mount="{mount}", site="A1", resource_template="tip_box")')
        with caplog.at_level(logging.WARNING):
            _decide(stack, _pending(stack, task))
            wait_for(lambda: _status(stack, task) == "succeeded")
        [job] = _material_io_jobs(stack, task)
        result = job["return_info"]["return_value"]["device_sync"]
        assert result["status"] == "skipped" and "不属于任何设备" in result["reason"]
        assert device.syncs == [] and device.tree_sync_calls == [] and len(_tip_boxes(stack)) == 1
        assert "台面同步" not in caplog.text  # 普通货架安静跳过
    finally:
        scheduler.stop()
        stack.close()


def test_a_failed_sync_keeps_the_inventory_and_reports_a_warning(tmp_path, device, caplog) -> None:
    stack, executor = _stack(tmp_path), Executor()
    mount, sites = device_deck(stack.materials)
    bottle = container(stack.materials, mount, sites["B1"], water_ml=20)
    device.fail_sync = "设备台面上找不到挂载物料"
    device.fail_update = "容器不在台面上"
    scheduler = _scheduler(stack, executor)
    try:
        task = _task(stack, f'''
            ctx.material_io(mode="load", mount="{mount}", site="A1", resource_template="tip_box")
            ctx.material_io(mode="refill", mount="{mount}", site="B1", material="{bottle}",
                            reagent={{"quantity": 5, "quantity_unit": "mL"}})
        ''')
        with caplog.at_level(logging.WARNING):
            _decide(stack, _pending(stack, task))
            _decide(stack, _pending(stack, task, 1))
            wait_for(lambda: _status(stack, task) == "succeeded")
        load, refill = _outputs(stack, task)
        # 库存是权威：同步失败不回滚，节点照样成功，输出带失败原因
        [box] = _tip_boxes(stack)
        assert stack.materials.occupied_site(box.material.material_uuid).site_uuid == sites["A1"]
        assert load["material_uuid"] == box.material.material_uuid
        assert load["device_sync"]["status"] == "failed" and load["device_sync"]["device_id"] == DEVICE
        assert "设备台面上找不到挂载物料" in load["device_sync"]["reason"]
        assert [(item.quantity, item.quantity_unit) for item in stack.materials.get_material(bottle).data.substances] == [
            (25, "mL")]
        assert refill["device_sync"]["status"] == "failed" and "没有确认内容物更新" in refill["device_sync"]["reason"]
        assert caplog.text.count("台面同步") == 2 and "库存不回滚" in caplog.text
        assert device.deck == {}
    finally:
        scheduler.stop()
        stack.close()


def test_an_unreachable_device_fails_the_sync_but_not_the_step(tmp_path, device) -> None:
    stack, executor = _stack(tmp_path), Executor()
    mount, _sites = device_deck(stack.materials, device_id="offline_handler")  # 不在本进程，也没有 HostLink
    scheduler = _scheduler(stack, executor)
    try:
        task = _task(stack, f'ctx.material_io(mode="load", mount="{mount}", site="A1", resource_template="tip_box")')
        _decide(stack, _pending(stack, task))
        wait_for(lambda: _status(stack, task) == "succeeded")
        [job] = _material_io_jobs(stack, task)
        result = job["return_info"]["return_value"]["device_sync"]
        assert (result["status"], result["device_id"]) == ("failed", "offline_handler")
        assert "HostLink server 未启动" in result["reason"] and len(_tip_boxes(stack)) == 1
    finally:
        scheduler.stop()
        stack.close()


def test_replay_after_a_crash_resends_the_same_idempotent_sync_once(tmp_path, device) -> None:
    """写库与同步都完成、终态还没落库就崩溃：重启重放同一命令再送一次同一 ``transfer_uuid``，设备只应用一次；
    终态落库后同一决策再次送达不再重发。"""

    stack, executor = _stack(tmp_path), Executor()
    mount, sites = device_deck(stack.materials)
    scheduler = _scheduler(stack, executor)
    real = stack.workflow.record_workflow_node_job_terminal
    lost: list[str] = []

    def crash_before_terminal(job_uuid, **kwargs):
        if not lost:
            lost.append(job_uuid)
            raise ConnectionError("终态写入前进程崩溃")
        return real(job_uuid, **kwargs)

    stack.workflow.record_workflow_node_job_terminal = crash_before_terminal
    task = _task(stack, f'ctx.material_io(mode="load", mount="{mount}", site="A1", resource_template="tip_box")')
    confirmation = _pending(stack, task)
    stack.workflow.decide_workflow_manual_confirmation(confirmation["uuid"], action="approve", confirmed_by="op",
                                                       decision_idempotency_key="approve-once")
    assert lost and len(device.syncs) == 1 and _status(stack, task) == "running"
    stack, executor, scheduler = _restart(stack, scheduler, tmp_path)
    try:
        wait_for(lambda: _status(stack, task) == "succeeded")
        [box] = _tip_boxes(stack)  # 库存命令重放：只有一件
        new = box.material.material_uuid
        first, replay = device.syncs
        assert first == replay and first["transfer_uuid"] == f"workflow.material_io:{lost[0]}"
        assert device.deck == {new: sites["A1"]}  # 台面上也只有一件
        [job] = _material_io_jobs(stack, task)
        assert job["return_info"]["return_value"]["device_sync"]["status"] == "ok"

        # 批准请求按同一幂等键重发：决策已落库、节点已终态，不再重发台面同步
        stack.workflow.decide_workflow_manual_confirmation(confirmation["uuid"], action="approve", confirmed_by="op",
                                                           decision_idempotency_key="approve-once")
        assert len(device.syncs) == 2
        assert _material_io_jobs(stack, task)[0]["return_info"] == job["return_info"]
    finally:
        scheduler.stop()
        stack.close()


def test_a_stale_replay_does_not_project_an_overwritten_change(tmp_path, device) -> None:
    """重放时权威已经变了（物料被移走 / 又放回）：不再投影旧变化。"""

    stack = _stack(tmp_path)
    mount, sites = device_deck(stack.materials)
    bottle = container(stack.materials, mount, sites["A1"])
    base = {"mount_uuid": mount, "site_uuid": sites["A1"], "site_label": "A1", "job_uuid": str(uuid4())}
    try:
        # load / refill 的物料已不在库位上
        stack.materials.move_material(_mutation("move_material"),
                                      MaterialMove(material_uuid=bottle, destination_site_uuid=sites["B1"]))
        for mode in ("load", "refill"):
            result = sync_material_io_device(stack.client, {**base, "mode": mode, "material_uuid": bottle},
                                             {"material_uuid": bottle}, command_id="replayed")
            assert result["status"] == "skipped" and "已不在库位 A1" in result["reason"]
        # unload 的物料又回到了库位上
        stack.materials.move_material(_mutation("move_material"),
                                      MaterialMove(material_uuid=bottle, destination_site_uuid=sites["A1"]))
        result = sync_material_io_device(stack.client, {**base, "mode": "unload", "material_uuid": bottle},
                                         {"material_uuid": bottle}, command_id="replayed")
        assert result["status"] == "skipped" and "又回到了库位 A1" in result["reason"]
        assert device.syncs == [] and device.tree_sync_calls == []
    finally:
        stack.close()


def test_the_authority_relays_through_the_host(tmp_path, monkeypatch) -> None:
    """调度权威与 Host 分开时：同步经控制面交给 Host 的 ``/api/v1/hostlink/*`` 中继，权威不查本进程设备表。"""

    posted: list[tuple[str, dict]] = []

    def relay(path: str, payload: dict, timeout: float) -> dict:
        posted.append((path, payload))
        if path.endswith("/material-sync"):
            return {"success": True, "action": payload["action"], "replayed": False}
        return {"notified": True}

    def no_local_lookup(_device_id):
        raise AssertionError("调度权威没有设备运行时，不能查本进程设备表")

    monkeypatch.setattr(downlink, "_remote_device_relay", True)
    monkeypatch.setattr(downlink, "_relay_post", relay)
    monkeypatch.setattr(downlink, "get_local_device_node", no_local_lookup)
    stack = _stack(tmp_path)
    mount, sites = device_deck(stack.materials)
    bottle = container(stack.materials, mount, sites["A1"], water_ml=10)
    plan = {"mount_uuid": mount, "site_uuid": sites["A1"], "site_label": "A1", "job_uuid": "job-1"}
    try:
        load = sync_material_io_device(stack.client, {**plan, "mode": "load"}, {"material_uuid": bottle},
                                       command_id="workflow.material_io:job-1")
        refill = sync_material_io_device(stack.client, {**plan, "mode": "refill", "material_uuid": bottle}, {},
                                         command_id="workflow.material_io:job-2")
        assert load["status"] == refill["status"] == "ok"
        (sync_path, sync), (notify_path, notify) = posted
        assert sync_path == "/api/v1/hostlink/material-sync"
        assert (sync["device_id"], sync["action"], sync["material_uuids"], sync["destination_site_uuids"],
                sync["transfer_uuid"]) == (DEVICE, "load", [bottle], [sites["A1"]], "workflow.material_io:job-1")
        assert notify_path == "/api/v1/hostlink/notify-device"
        assert notify == {"device_id": DEVICE, "action": "update", "resource_uuids": [bottle]}
    finally:
        stack.close()
