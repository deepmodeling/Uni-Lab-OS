"""JSON 通用设备动作的物料占位符解析回归。"""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace
from typing import Annotated

import pytest
from pylabrobot.resources import Resource

from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.resources.resource_tracker import JSON_UNILABOS_PARAM, PARAM_SAMPLE_UUIDS
from unilabos.ros.nodes import base_device_node
from unilabos.ros.nodes.base_device_node import BaseROS2DeviceNode
from unilabos.utils.type_check import TypeEncoder


class _Logger:
    def debug(self, *_args, **_kwargs) -> None:
        return

    def error(self, *_args, **_kwargs) -> None:
        return

    def warning(self, *_args, **_kwargs) -> None:
        return


class _Driver:
    def pick(self, resource: ResourceSlot):
        return resource


class _AnnotatedDriver:
    def pick(self, beaker: Annotated[ResourceSlot, "beaker_500ml"]):
        return beaker


class _AsyncDriver:
    def __init__(self) -> None:
        self.received: tuple[object, object] | None = None

    async def transfer(
        self,
        resource: ResourceSlot,
        mount_resource: ResourceSlot,
    ) -> tuple[object, object]:
        self.received = (resource, mount_resource)
        return self.received


class _AsyncCommandDriver:
    async def echo(self, value: str) -> dict[str, str]:
        await asyncio.sleep(0)
        return {"value": value}


def test_json_command_resolves_uuid_only_resource_slot_before_driver_call() -> None:
    """规范 UUID-only 物料引用必须在调用驱动前解析为完整资源实例。"""

    node = object.__new__(BaseROS2DeviceNode)
    node.driver_instance = _Driver()
    node.resource_tracker = None
    node._resolve_driver_method_name = lambda name: name
    node.lab_logger = lambda: _Logger()
    resolved_resource = object()
    observed_uuids: list[str] = []

    def convert(*uuids: str):
        observed_uuids.extend(uuids)
        return [resolved_resource]

    node._convert_resources_sync = convert
    command = {
        "function_name": "pick",
        "function_args": {
            "resource": {"uuid": "50000000-0000-4000-8000-000000000001"}
        },
        JSON_UNILABOS_PARAM: {PARAM_SAMPLE_UUIDS: {}},
    }

    result = node._execute_driver_command(json.dumps(command))

    assert observed_uuids == ["50000000-0000-4000-8000-000000000001"]
    assert result is resolved_resource


def test_sync_json_command_waits_for_async_driver_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同步 JSON Action 遇到异步驱动方法时必须等待真实结果。"""

    loop = asyncio.new_event_loop()
    loop_started = threading.Event()

    def run_loop() -> None:
        asyncio.set_event_loop(loop)
        loop_started.set()
        loop.run_forever()

    loop_thread = threading.Thread(target=run_loop, daemon=True)
    loop_thread.start()
    assert loop_started.wait(timeout=2.0)
    monkeypatch.setattr(base_device_node.ROS2DeviceNode, "_asyncio_loop", loop)

    node = object.__new__(BaseROS2DeviceNode)
    node.driver_instance = _AsyncCommandDriver()
    node.resource_tracker = None
    node._resolve_driver_method_name = lambda name: name
    node.lab_logger = lambda: _Logger()
    command = {
        "function_name": "echo",
        "function_args": {"value": "completed"},
        JSON_UNILABOS_PARAM: {PARAM_SAMPLE_UUIDS: {}},
    }

    try:
        result = node._execute_driver_command(json.dumps(command))
    finally:
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=2.0)
        loop.close()

    assert result == {"value": "completed"}


def test_json_command_resolves_annotated_resource_slot_before_driver_call() -> None:
    """带模板约束的物料占位符（ResourceSlot）也必须解析为完整资源实例。"""

    node = object.__new__(BaseROS2DeviceNode)
    node.driver_instance = _AnnotatedDriver()
    node.resource_tracker = None
    node._resolve_driver_method_name = lambda name: name
    node.lab_logger = lambda: _Logger()
    resolved_resource = SimpleNamespace(category="beaker")
    observed_uuids: list[str] = []

    def convert(*uuids: str):
        observed_uuids.extend(uuids)
        return [resolved_resource]

    node._convert_resources_sync = convert
    command = {
        "function_name": "pick",
        "function_args": {
            "beaker": {"uuid": "50000000-0000-4000-8000-000000000008"}
        },
        JSON_UNILABOS_PARAM: {PARAM_SAMPLE_UUIDS: {}},
    }

    result = node._execute_driver_command(json.dumps(command))

    assert observed_uuids == ["50000000-0000-4000-8000-000000000008"]
    assert result is resolved_resource


def test_json_command_maps_inventory_uuid_to_runtime_resource_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """库存稳定 UUID 命中设备运行时别名时仍须返回本地资源实例。"""

    inventory_uuid = "50000000-0000-4000-8000-000000000002"
    runtime_uuid = "50000000-0000-4000-8000-000000000003"
    inventory_resource = SimpleNamespace(
        unilabos_uuid=inventory_uuid,
        children=[],
    )
    runtime_resource = Resource(
        name="local-runtime-beaker",
        size_x=1.0,
        size_y=1.0,
        size_z=1.0,
    )
    runtime_resource.unilabos_uuid = runtime_uuid
    tree_set = SimpleNamespace(
        trees=[
            SimpleNamespace(
                root_node=SimpleNamespace(res_content=inventory_resource)
            )
        ],
        to_plr_resources=lambda: [inventory_resource],
    )
    monkeypatch.setattr(
        base_device_node.ResourceTreeSet,
        "from_raw_dict_list",
        lambda _raw_data: tree_set,
    )

    class _Future:
        def done(self) -> bool:
            return True

        def result(self) -> SimpleNamespace:
            return SimpleNamespace(response=json.dumps([{"uuid": inventory_uuid}]))

    class _Client:
        def call_async(self, _request: object) -> _Future:
            return _Future()

    class _Tracker:
        def figure_resource(self, resource: object, try_mode: bool) -> list[object]:
            assert try_mode is True
            if resource is inventory_resource:
                return [runtime_resource]
            return []

        def loop_find_with_uuid(self, resource: object, target_uuid: str) -> object | None:
            if getattr(resource, "unilabos_uuid", None) == target_uuid:
                return resource
            return None

    node = object.__new__(BaseROS2DeviceNode)
    node._resource_clients = {"c2s_update_resource_tree": _Client()}
    node.resource_tracker = _Tracker()
    node.lab_logger = lambda: _Logger()

    converted = node._convert_resources_sync(inventory_uuid)

    assert converted == [runtime_resource]
    assert runtime_resource.unilabos_uuid == runtime_uuid
    assert runtime_resource.unilabos_reference_uuid == inventory_uuid
    assert json.loads(
        json.dumps({"resource": runtime_resource}, cls=TypeEncoder)
    ) == {"resource": {"uuid": inventory_uuid}}


def test_nested_json_action_preserves_authority_uuid_instead_of_resource_repr() -> None:
    """设备动作再次调用 Host 时必须发送权威 UUID，不能退化为 PLR 字符串。"""

    inventory_uuid = "50000000-0000-4000-8000-000000000131"
    runtime_uuid = "50000000-0000-4000-8000-000000000132"
    resource = Resource(
        name="nested-transfer-beaker",
        size_x=1.0,
        size_y=1.0,
        size_z=1.0,
    )
    resource.unilabos_uuid = runtime_uuid
    resource.unilabos_reference_uuid = inventory_uuid

    command = {
        "function_name": "transfer_resource",
        "function_args": {"resource": resource},
        JSON_UNILABOS_PARAM: {PARAM_SAMPLE_UUIDS: {}},
    }

    encoded = json.loads(json.dumps(command, cls=TypeEncoder))

    assert encoded["function_args"]["resource"] == {"uuid": inventory_uuid}


def test_json_command_preserves_device_root_skipped_by_plr_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """设备型库位父资源被 PLR 投影跳过时仍须保留稳定引用字段。"""

    device_uuid = "50000000-0000-4000-8000-000000000006"
    raw_device = {
        "uuid": device_uuid,
        "id": device_uuid,
        "name": "S09 移液站",
        "type": "device",
        "class": "community.szlab_poly_studio.szlab_mixer_pipetting_station",
    }
    device_content = SimpleNamespace(
        uuid=device_uuid,
        id=device_uuid,
        type="device",
        model_dump=lambda **_kwargs: dict(raw_device),
    )
    tree_set = SimpleNamespace(
        trees=[SimpleNamespace(root_node=SimpleNamespace(res_content=device_content))],
        to_plr_resources=lambda: [],
    )
    monkeypatch.setattr(
        base_device_node.ResourceTreeSet,
        "from_raw_dict_list",
        lambda _raw_data: tree_set,
    )

    class _Future:
        def done(self) -> bool:
            return True

        def result(self) -> SimpleNamespace:
            return SimpleNamespace(response=json.dumps([raw_device]))

    class _Client:
        def call_async(self, _request: object) -> _Future:
            return _Future()

    node = object.__new__(BaseROS2DeviceNode)
    node._resource_clients = {"c2s_update_resource_tree": _Client()}
    node.resource_tracker = SimpleNamespace()
    node.lab_logger = lambda: _Logger()

    assert node._convert_resources_sync(device_uuid) == [raw_device]


def test_json_command_hydrates_each_resource_slot_in_mixed_device_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """批量参数必须逐个水合，不能让设备根吞掉直属物料。

    参数：pytest monkeypatch。返回：无；模拟同一动作同时引用设备直属物料与该
    设备仓库。资源服务的合并查询会形成设备根树，而逐项查询可分别保留物料子树
    与设备稳定引用。
    """

    device_uuid = "50000000-0000-4000-8000-000000000012"
    target_uuid = "50000000-0000-4000-8000-000000000013"
    raw_device = {
        "uuid": device_uuid,
        "id": device_uuid,
        "name": "S08 开关盖",
        "type": "device",
    }
    raw_target = {
        "uuid": target_uuid,
        "id": target_uuid,
        "name": "S082 瓶盖暂存位物料",
        "type": "resource",
        "parent_uuid": device_uuid,
    }
    target_resource = SimpleNamespace(
        unilabos_uuid=target_uuid,
        children=[],
    )
    target_content = SimpleNamespace(
        uuid=target_uuid,
        id=target_uuid,
        type="resource",
    )
    device_content = SimpleNamespace(
        uuid=device_uuid,
        id=device_uuid,
        type="device",
        model_dump=lambda **_kwargs: dict(raw_device),
    )

    def tree_set_from_rows(rows: list[dict[str, object]]) -> SimpleNamespace:
        if rows == [raw_target]:
            return SimpleNamespace(
                trees=[
                    SimpleNamespace(
                        root_node=SimpleNamespace(res_content=target_content)
                    )
                ],
                to_plr_resources=lambda: [target_resource],
            )
        if rows == [raw_device]:
            return SimpleNamespace(
                trees=[
                    SimpleNamespace(
                        root_node=SimpleNamespace(res_content=device_content)
                    )
                ],
                to_plr_resources=list,
            )
        if rows == [raw_device, raw_target]:
            return SimpleNamespace(
                trees=[
                    SimpleNamespace(
                        root_node=SimpleNamespace(res_content=device_content)
                    )
                ],
                to_plr_resources=list,
            )
        raise AssertionError(rows)

    observed_queries: list[list[str]] = []

    def query_nodes(
        _resource_client: object,
        query_uuids: list[str],
        **_kwargs: object,
    ) -> list[dict[str, object]]:
        observed_queries.append(query_uuids)
        if query_uuids == [target_uuid]:
            return [raw_target]
        if query_uuids == [device_uuid]:
            # 第一次是目标物料的父树验证，第二次是设备参数自身的查询。
            if observed_queries.count([device_uuid]) == 1:
                return [raw_device, raw_target]
            return [raw_device]
        if query_uuids == [target_uuid, device_uuid]:
            return [raw_device, raw_target]
        raise AssertionError(query_uuids)

    monkeypatch.setattr(
        base_device_node.ResourceTreeSet,
        "from_raw_dict_list",
        tree_set_from_rows,
    )
    monkeypatch.setattr(base_device_node, "query_resource_nodes_sync", query_nodes)

    node = object.__new__(BaseROS2DeviceNode)
    node._resource_clients = {"c2s_update_resource_tree": object()}
    node.resource_tracker = SimpleNamespace(
        figure_resource=lambda _resource, try_mode: [],
        loop_find_with_uuid=lambda _resource, _uuid: None,
    )
    node.lab_logger = lambda: _Logger()

    converted = node._convert_resources_sync(target_uuid, device_uuid)

    assert observed_queries == [
        [target_uuid],
        [device_uuid],
        [device_uuid],
    ]
    assert converted == [target_resource, raw_device]


def test_backend_control_plane_resource_conversion_does_not_query_local_scheduler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backend ResourceSlot 水合不得访问本地 Scheduler 的 ROS 资源服务。

    参数说明：``monkeypatch`` 替换生产资源投影和资源树装配。返回：无；断言仅查询
    Backend 身份投影，并保持父树水合的两次查询语义。
    """

    target_uuid = "50000000-0000-4000-8000-000000000121"
    parent_uuid = "50000000-0000-4000-8000-000000000122"
    raw_target = {"uuid": target_uuid, "parent_uuid": parent_uuid}
    raw_parent = {"uuid": parent_uuid, "parent_uuid": None}
    parent_resource = SimpleNamespace(unilabos_uuid=parent_uuid, children=[])
    target_resource = SimpleNamespace(
        unilabos_uuid=target_uuid,
        parent=parent_resource,
        children=[],
    )
    parent_resource.children.append(target_resource)
    parent_tree = SimpleNamespace(
        trees=[SimpleNamespace(root_node=SimpleNamespace(res_content=parent_resource))],
        to_plr_resources=lambda: [parent_resource],
    )
    observed_queries: list[list[str]] = []

    def query_production(query_uuids: list[str]) -> list[dict[str, object]]:
        observed_queries.append(query_uuids)
        return [raw_target] if query_uuids == [target_uuid] else [raw_parent, raw_target]

    monkeypatch.setattr(base_device_node.BasicConfig, "control_plane", "backend")
    monkeypatch.setattr(
        base_device_node,
        "query_production_resource_nodes_sync",
        query_production,
    )
    monkeypatch.setattr(
        base_device_node.ResourceTreeSet,
        "from_raw_dict_list",
        lambda _rows: parent_tree,
    )
    node = object.__new__(BaseROS2DeviceNode)
    node._resource_clients = {}
    node.resource_tracker = SimpleNamespace(
        loop_find_with_uuid=lambda root, wanted: (
            target_resource if root is parent_resource and wanted == target_uuid else None
        ),
        figure_resource=lambda resource, try_mode: [resource],
    )
    node.lab_logger = lambda: _Logger()

    converted = node._convert_resources_sync(target_uuid)

    assert converted == [target_resource]
    assert observed_queries == [[target_uuid], [parent_uuid]]


def test_async_resource_conversion_preserves_device_root_skipped_by_plr_projection() -> None:
    """异步 Host 动作也必须保留设备型库位父资源的原始映射。

    参数：无。返回：无；断言无父资源的设备引用仍走一次查询，并由既有设备根
    映射返回稳定引用，不误触发物料父上下文补查。
    """

    device_uuid = "50000000-0000-4000-8000-000000000007"
    raw_device = {
        "uuid": device_uuid,
        "id": device_uuid,
        "name": "S08 开关盖",
        "type": "device",
        "class": "community.szlab_poly_studio.szlab_s08_cap_station",
    }
    device_content = SimpleNamespace(
        uuid=device_uuid,
        id=device_uuid,
        type="device",
        model_dump=lambda **_kwargs: dict(raw_device),
    )
    tree_set = SimpleNamespace(
        trees=[SimpleNamespace(root_node=SimpleNamespace(res_content=device_content))],
        dump=lambda: [[raw_device]],
        to_plr_resources=list,
    )
    node = object.__new__(BaseROS2DeviceNode)

    async def get_resource(*_args: object, **_kwargs: object) -> SimpleNamespace:
        """返回无父资源的设备根查询夹具。

        参数说明：位置参数和命名参数承接既有资源查询签名。返回：带 ``dump``
        能力的设备资源树集合。
        """

        return tree_set

    node.get_resource = get_resource
    node.resource_tracker = SimpleNamespace()
    node.lab_logger = lambda: _Logger()

    converted = asyncio.run(node._convert_resource_async({"uuid": device_uuid}))

    assert converted == raw_device


def test_async_resource_conversion_preserves_resource_directly_mounted_on_device() -> None:
    """设备根只提供归属上下文时，动作仍应收到其直接挂载的物料。

    参数：无。返回：无；模拟库存先按目标 UUID 返回完整物料子树，再按父 UUID
    返回以设备为根的完整树。设备根不会被投影为 PLR Resource，但不能因此丢弃
    已经由第一次查询完整取得的目标仓库。
    """

    device_uuid = "50000000-0000-4000-8000-000000000010"
    target_uuid = "50000000-0000-4000-8000-000000000011"
    raw_device = {
        "uuid": device_uuid,
        "id": device_uuid,
        "name": "S07 固体加料",
        "type": "device",
    }
    raw_target = {
        "uuid": target_uuid,
        "id": target_uuid,
        "name": "S07 固体加料转盘仓",
        "type": "resource",
        "parent_uuid": device_uuid,
    }
    target_resource = SimpleNamespace(
        unilabos_uuid=target_uuid,
        children=[],
    )
    direct_tree = SimpleNamespace(
        trees=[
            SimpleNamespace(
                root_node=SimpleNamespace(res_content=target_resource)
            )
        ],
        dump=lambda: [[raw_target]],
        to_plr_resources=lambda: [target_resource],
    )
    device_content = SimpleNamespace(
        uuid=device_uuid,
        id=device_uuid,
        type="device",
        model_dump=lambda **_kwargs: dict(raw_device),
    )
    parent_tree = SimpleNamespace(
        trees=[SimpleNamespace(root_node=SimpleNamespace(res_content=device_content))],
        dump=lambda: [[raw_device, raw_target]],
        to_plr_resources=list,
    )
    observed_queries: list[str] = []
    node = object.__new__(BaseROS2DeviceNode)

    async def get_resource(resources_uuid: list[str], **_kwargs: object) -> object:
        observed_queries.extend(resources_uuid)
        if resources_uuid == [target_uuid]:
            return direct_tree
        if resources_uuid == [device_uuid]:
            return parent_tree
        raise AssertionError(resources_uuid)

    node.get_resource = get_resource
    node.resource_tracker = SimpleNamespace(
        figure_resource=lambda _resource, try_mode: [],
        loop_find_with_uuid=lambda _resource, _uuid: None,
    )
    node.lab_logger = lambda: _Logger()

    converted = asyncio.run(node._convert_resource_async({"uuid": target_uuid}))

    assert observed_queries == [target_uuid, device_uuid]
    assert converted is target_resource


def test_transfer_accepts_uuid_mapping_as_target_resource() -> None:
    """转运记账必须接受设备型父资源的稳定 UUID 原始映射。"""

    source = SimpleNamespace(unilabos_uuid="50000000-0000-4000-8000-000000000008")
    target = {"uuid": "50000000-0000-4000-8000-000000000009", "type": "device"}

    class _UnavailableClient:
        def wait_for_service(self, timeout_sec: float) -> bool:
            assert timeout_sec == 5.0
            return False

    node = object.__new__(BaseROS2DeviceNode)
    node.device_id = "host_node"
    node.create_client = lambda *_args: _UnavailableClient()
    node.lab_logger = lambda: _Logger()

    with pytest.raises(ValueError, match="Service .* not available"):
        asyncio.run(
            node.transfer_resource_to_another(
                [source],
                "szlab_mixer_pipetting_station",
                [target],
                ["BEAKER1"],
            )
        )


def test_async_json_command_resolves_uuid_and_runtime_uuid_resource_slots() -> None:
    """异步设备动作必须解析两种规范资源身份并真正等待驱动协程。"""

    inventory_uuid = "50000000-0000-4000-8000-000000000004"
    mount_uuid = "50000000-0000-4000-8000-000000000005"
    resolved_resource = object()
    resolved_mount = object()
    resolved_by_uuid = {
        inventory_uuid: resolved_resource,
        mount_uuid: resolved_mount,
    }
    observed_uuids: list[str] = []
    driver = _AsyncDriver()
    node = object.__new__(BaseROS2DeviceNode)
    node.driver_instance = driver
    node.resource_tracker = None
    node._resolve_driver_method_name = lambda name: name
    node.lab_logger = lambda: _Logger()

    async def convert(resource_data: dict[str, object]) -> object:
        identity = str(
            resource_data.get("uuid") or resource_data.get("unilabos_uuid") or ""
        )
        observed_uuids.append(identity)
        return resolved_by_uuid[identity]

    node._convert_resource_async = convert
    command = {
        "function_name": "transfer",
        "function_args": {
            "resource": {
                "unilabos_uuid": inventory_uuid,
                "name": "已解析物料",
            },
            "mount_resource": {"uuid": mount_uuid},
        },
        JSON_UNILABOS_PARAM: {PARAM_SAMPLE_UUIDS: {}},
    }

    result = asyncio.run(node._execute_driver_command_async(json.dumps(command)))

    assert observed_uuids == [inventory_uuid, mount_uuid]
    assert driver.received == (resolved_resource, resolved_mount)
    assert result == (resolved_resource, resolved_mount)


def test_json_start_guard_runs_after_resource_hydration_and_before_driver() -> None:
    """开始屏障不能在阻塞的资源解析之前提前签发；拒绝时驱动不得被调用。"""
    from unilabos.app.edge_control.execution_start import ExecutionStartDeferred
    observed = []
    class Driver:
        def pick(self, resource: ResourceSlot):
            observed.append("driver")
            return resource
    node = object.__new__(BaseROS2DeviceNode)
    node.driver_instance = Driver()
    node.resource_tracker = None
    node._resolve_driver_method_name = lambda name: name
    node.lab_logger = lambda: _Logger()
    def hydrate(*_ids):
        observed.append("hydrated")
        return [object()]
    def begin():
        observed.append("guard")
        raise ExecutionStartDeferred()
    node._convert_resources_sync = hydrate
    command = {"function_name": "pick", "function_args": {"resource": {"uuid": "50000000-0000-4000-8000-000000000001"}}, JSON_UNILABOS_PARAM: {PARAM_SAMPLE_UUIDS: {}}}
    with pytest.raises(ExecutionStartDeferred):
        node._execute_driver_command(json.dumps(command), _execution_start=begin)
    assert observed == ["hydrated", "guard"]
