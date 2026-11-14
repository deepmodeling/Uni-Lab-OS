"""A03 ``order_sensitive``（与 V1 对齐）：顺序敏感的动作与同一资源上的兄弟动作必须显式排序。

``ctx.parallel`` / 互不依赖的分支只保证同一设备上的动作不同时执行，不保证先后；动作声明
``resource_contract={"order_sensitive": True}`` 后，这种写法在发布和创建任务时被拒绝。
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from unilabos.registry.typed_contract import validate_action_values
from unilabos.registry.workflows import upsert_workflow
from unilabos.server.services.runtime.workflow.errors import StoreConflict
from unilabos.server.services.runtime.workflow.service import WorkflowError, WorkflowService

SENSITIVE = {"lid_open", "lid_close"}


def _schema(sensitive: bool) -> dict:
    contract = {"order_sensitive": True} if sensitive else {}
    return {"x-unilabos-action-contract": {"resource_contract": contract},
            "properties": {"goal": {"type": "object", "properties": {}}, "result": {"type": "object", "properties": {}}}}


@pytest.fixture
def service(tmp_path):
    value = WorkflowService(tmp_path / "runtime.db")
    value.configure_preflight(action_schema_resolver=lambda _device, action: _schema(action in SENSITIVE))
    yield value
    value.close()


def _workflow(service, steps, *, ordered: bool):
    """steps: [(名称, 设备, 动作)]；ordered=False 时全部并列（互不依赖）。"""
    nodes = []
    for name, device, action in steps:
        nodes.append({"uuid": str(uuid4()), "name": name, "type": "device_action", "material_uuid": str(uuid4()),
                      "action_name": action, "param": {}, "meta_data": {"target_device_id": device},
                      "execution_policy": {"depends_on": [nodes[-1]["uuid"]] if ordered and nodes else []}})
    return upsert_workflow(service, {"workflow_uuid": str(uuid4()), "name": "开关盖", "description": "", "tags": [],
                                     "nodes": nodes, "edges": []})


def _start(service, record):
    return service.create_workflow_task(workflow_uuid=record["uuid"], run_mode="normal", target_node_uuid=None,
                                        input_value={}, description=None, meta_data={})


@pytest.mark.parametrize("steps", [
    [("开盖", "robot", "lid_open"), ("关盖", "robot", "lid_close")],
    [("开盖", "robot", "lid_open"), ("拍照", "robot", "photo")],  # 只要一方顺序敏感就要排序（V1 同规则）
], ids=["both-sensitive", "one-sensitive"])
def test_unordered_siblings_on_one_device_are_rejected_at_publish_and_start(service, steps) -> None:
    record = _workflow(service, steps, ordered=False)
    with pytest.raises(WorkflowError, match="必须显式排序") as published:
        service.publish_workflow(record["uuid"], revision=record["revision"])
    assert "「开盖」" in str(published.value) and "改成顺序执行" in str(published.value)
    with pytest.raises((WorkflowError, StoreConflict), match="必须显式排序"):
        _start(service, record)  # 没发布直接运行同样受约束


@pytest.mark.parametrize(("steps", "ordered"), [
    ([("开盖", "robot", "lid_open"), ("关盖", "robot", "lid_close")], True),  # 显式先后
    ([("开盖", "robot-a", "lid_open"), ("关盖", "robot-b", "lid_close")], False),  # 不同设备
    ([("拍照", "robot", "photo"), ("称重", "robot", "weigh")], False),  # 都不敏感：顺序无所谓
], ids=["ordered", "different-devices", "insensitive"])
def test_ordered_or_independent_actions_are_accepted(service, steps, ordered) -> None:
    record = _workflow(service, steps, ordered=ordered)
    assert service.publish_workflow(record["uuid"], revision=record["revision"])
    assert _start(service, record)["uuid"]


def test_declaring_order_sensitive_no_longer_blocks_dispatch_validation() -> None:
    # 以前 typed_contract 把 order_sensitive 当作未启用的执行合同直接拒绝；它只约束计划
    assert validate_action_values(_schema(True), {}) == {}
    assert validate_action_values(_schema(True), {}, managed_resources=True) == {}
