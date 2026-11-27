"""物料占位符（ResourceSlot）父上下文水合的公开接缝测试。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from unilabos.ros.nodes import base_device_node
from unilabos.ros.nodes.base_device_node import BaseROS2DeviceNode
from unilabos.ros.nodes.resource_slot_hydration import (
    PRODUCTION_SOURCE_RUNTIME_UUID_EXTRA,
    ResourceSlotHydrationError,
    hydrate_resource_slot_nodes_sync,
    install_production_resource_lookup,
    install_production_resource_nodes,
    plan_resource_slot_parent_context,
    query_production_resource_by_local_id_sync,
    query_production_resource_nodes_sync,
    upsert_production_resource_nodes,
    validate_resource_slot_parent_context,
)


def _raw_resource(
    resource_uuid: str,
    *,
    parent_uuid: str | None = None,
) -> dict[str, Any]:
    """构造父上下文判定所需的最小物料行。

    参数说明：``resource_uuid`` 是物料（Material）的稳定 UUID；
    ``parent_uuid`` 是其父资源稳定 UUID。返回：模拟资源查询接口的一行数据。
    """

    return {
        "uuid": resource_uuid,
        "id": resource_uuid,
        "parent_uuid": parent_uuid,
    }


class _Logger:
    """提供转换接缝所需的最小日志接口。"""

    def trace(self, *_args: object, **_kwargs: object) -> None:
        """忽略测试无关的跟踪日志。

        参数说明：位置参数和命名参数承接生产日志调用。返回：无。
        """

    def warning(self, *_args: object, **_kwargs: object) -> None:
        """忽略测试无关的告警。

        参数说明：位置参数和命名参数承接生产日志调用。返回：无。
        """


class _Tracker:
    """以稳定 UUID 在测试资源树中定位物料实例。"""

    def figure_resource(self, resource: object, try_mode: bool) -> list[object]:
        """把查询树根视为已解析的本地实例。

        参数说明：``resource`` 是待解析资源；``try_mode`` 必须保持探测模式。
        返回：只含原资源的唯一候选。
        """

        assert try_mode is True
        return [resource]

    def loop_find_with_uuid(
        self,
        resource: object,
        target_uuid: str,
    ) -> object | None:
        """递归查找稳定 UUID 对应的资源。

        参数说明：``resource`` 是查询根；``target_uuid`` 是目标物料稳定 UUID。
        返回：唯一命中的资源，未命中时返回 ``None``。
        """

        if getattr(resource, "unilabos_uuid", None) == target_uuid:
            return resource
        for child in getattr(resource, "children", []):
            found = self.loop_find_with_uuid(child, target_uuid)
            if found is not None:
                return found
        return None


def _resource_instances() -> tuple[str, str, object, object]:
    """构造一个含库位（Site）的父资源和目标物料。

    参数：无。返回：目标 UUID、父 UUID、父资源实例与目标物料实例。
    """

    # ``material_uuid`` 是动作参数引用的目标物料（Material）稳定身份。
    material_uuid = "50000000-0000-4000-8000-000000000101"
    # ``parent_uuid`` 是承载库位（Site）的父资源稳定身份。
    parent_uuid = "50000000-0000-4000-8000-000000000102"
    site = SimpleNamespace(name="A1")
    parent = SimpleNamespace(
        unilabos_uuid=parent_uuid,
        sites={"A1": site},
        children=[],
    )
    material = SimpleNamespace(
        unilabos_uuid=material_uuid,
        parent=parent,
        children=[],
    )
    parent.children.append(material)
    return material_uuid, parent_uuid, parent, material


def _tree_set(
    raw_rows: list[dict[str, Any]],
    root_resource: object,
) -> SimpleNamespace:
    """构造资源查询返回的最小资源树集合。

    参数说明：``raw_rows`` 是用于父关系验证的扁平行；``root_resource`` 是 PLR
    投影后的树根。返回：同时支持 ``dump`` 和 PLR 投影的测试替身。
    """

    return SimpleNamespace(
        trees=[
            SimpleNamespace(
                root_node=SimpleNamespace(res_content=root_resource),
            )
        ],
        dump=lambda: [raw_rows],
        to_plr_resources=lambda: [root_resource],
    )


def test_device_slot_on_a_non_device_parent_keeps_the_requested_device() -> None:
    device = dict(_raw_resource("device-id", parent_uuid="deck-id"), type="device")
    deck = dict(_raw_resource("deck-id"), type="deck")
    assert hydrate_resource_slot_nodes_sync(
        "device-id", [device], query_nodes=lambda _: [deck, device],
    ) == [device]


def test_parent_context_plan_preserves_no_parent_behavior() -> None:
    """无父资源的物料占位符（ResourceSlot）不得触发额外查询。

    参数：无。返回：无；断言计划保留目标身份并明确无父上下文。
    """

    # ``material_uuid`` 是无父资源物料的稳定身份。
    material_uuid = "50000000-0000-4000-8000-000000000103"

    plan = plan_resource_slot_parent_context(
        material_uuid,
        [_raw_resource(material_uuid)],
    )

    assert plan.target_uuid == material_uuid
    assert plan.parent_uuid is None


@pytest.mark.parametrize(
    ("parent_rows", "message"),
    [
        pytest.param([], "父资源查询返回空结果", id="empty-parent-tree"),
        pytest.param(
            [
                _raw_resource("50000000-0000-4000-8000-000000000104"),
                _raw_resource(
                    "50000000-0000-4000-8000-000000000103",
                    parent_uuid="50000000-0000-4000-8000-000000000104",
                ),
                _raw_resource("50000000-0000-4000-8000-000000000105"),
            ],
            "恰好一棵父资源树",
            id="multiple-parent-trees",
        ),
        pytest.param(
            [_raw_resource("50000000-0000-4000-8000-000000000104")],
            "未包含目标物料",
            id="missing-target",
        ),
        pytest.param(
            [
                _raw_resource("50000000-0000-4000-8000-000000000104"),
                _raw_resource(
                    "50000000-0000-4000-8000-000000000103",
                    parent_uuid="50000000-0000-4000-8000-000000000105",
                ),
            ],
            "父关系冲突",
            id="parent-conflict",
        ),
    ],
)
def test_parent_context_validation_fails_closed(
    parent_rows: list[dict[str, Any]],
    message: str,
) -> None:
    """不完整或冲突的父资源树必须失败关闭。

    参数说明：``parent_rows`` 是父查询反例；``message`` 是稳定中文诊断片段。
    返回：无；断言不会猜测库位（Site）上下文或回退为裸物料。
    """

    # ``material_uuid`` 与 ``parent_uuid`` 固定第一次查询已经声明的父关系。
    material_uuid = "50000000-0000-4000-8000-000000000103"
    parent_uuid = "50000000-0000-4000-8000-000000000104"
    plan = plan_resource_slot_parent_context(
        material_uuid,
        [_raw_resource(material_uuid, parent_uuid=parent_uuid)],
    )

    with pytest.raises(ResourceSlotHydrationError, match=message):
        validate_resource_slot_parent_context(plan, parent_rows)


def test_sync_conversion_hydrates_target_inside_parent_resource_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同步普通动作必须经两次查询返回带父库位的目标物料。

    参数说明：``monkeypatch`` 只替换资源树装配边界。返回：无；断言第二次查询按
    父 UUID 取完整子树，且驱动取得的仍是目标物料而不是父资源。
    """

    material_uuid, parent_uuid, parent, material = _resource_instances()
    direct_rows = [_raw_resource(material_uuid, parent_uuid=parent_uuid)]
    parent_rows = [
        _raw_resource(parent_uuid),
        _raw_resource(material_uuid, parent_uuid=parent_uuid),
    ]
    parent_tree = _tree_set(parent_rows, parent)
    monkeypatch.setattr(
        base_device_node.ResourceTreeSet,
        "from_raw_dict_list",
        lambda rows: parent_tree if rows == parent_rows else None,
    )

    # ``requests`` 保存同步资源服务实际收到的两次稳定查询命令。
    requests: list[dict[str, Any]] = []

    class _Future:
        """立即返回一轮资源查询结果。"""

        def __init__(self, rows: list[dict[str, Any]]) -> None:
            """保存本轮查询行。

            参数说明：``rows`` 是资源服务响应。返回：无。
            """

            self._rows = rows

        def done(self) -> bool:
            """报告异步服务调用已经完成。

            参数：无。返回：恒为 ``True``。
            """

            return True

        def result(self) -> SimpleNamespace:
            """返回 JSON 编码的资源服务响应。

            参数：无。返回：含 ``response`` 字段的响应对象。
            """

            return SimpleNamespace(response=json.dumps(self._rows))

    class _Client:
        """按查询 UUID 返回直接物料或完整父资源树。"""

        def call_async(self, request: object) -> _Future:
            """记录查询并返回对应资源行。

            参数说明：``request`` 是 ROS 串行命令请求。返回：已完成 Future。
            """

            command = json.loads(request.command)
            requests.append(command)
            query_uuid = command["data"]["data"][0]
            return _Future(parent_rows if query_uuid == parent_uuid else direct_rows)

    node = object.__new__(BaseROS2DeviceNode)
    node._resource_clients = {"c2s_update_resource_tree": _Client()}
    node.resource_tracker = _Tracker()
    node.lab_logger = lambda: _Logger()

    converted = node._convert_resources_sync(material_uuid)

    assert converted == [material]
    assert material.parent.sites["A1"].name == "A1"
    assert [request["data"]["data"] for request in requests] == [
        [material_uuid],
        [parent_uuid],
    ]
    assert all(request["data"]["with_children"] is True for request in requests)


def test_production_resource_projection_uses_backend_identity_and_parent_tree() -> None:
    """生产资源投影必须以 Backend UUID 改写本地图的完整父子关系。

    参数：无。返回：无；断言直接物料查询和父树补查均不依赖本地 Scheduler。
    """

    local_parent_uuid = "50000000-0000-4000-8000-000000000111"
    local_target_uuid = "50000000-0000-4000-8000-000000000112"
    backend_parent_uuid = "50000000-0000-4000-8000-000000000113"
    backend_target_uuid = "50000000-0000-4000-8000-000000000114"
    install_production_resource_nodes(
        [[
            {
                **_raw_resource(local_parent_uuid),
                "id": "warehouse",
                "name": "Warehouse",
                "type": "warehouse",
                "class": "warehouse",
                "barcode": "UNILAB-GRAPH-warehouse",
                "config": {"sites": [{"name": "A1"}]},
                "data": {},
                "extra": {},
            },
            {
                **_raw_resource(local_target_uuid, parent_uuid=local_parent_uuid),
                "id": "sample",
                "name": "Sample",
                "type": "container",
                "class": "sample",
                "barcode": "UNILAB-GRAPH-sample",
                "config": {},
                "data": {},
                "extra": {},
            },
        ]],
        {
            "UNILAB-GRAPH-warehouse": backend_parent_uuid,
            "UNILAB-GRAPH-sample": backend_target_uuid,
        },
    )

    assert query_production_resource_by_local_id_sync("sample")[0]["uuid"] == backend_target_uuid
    assert [row["uuid"] for row in query_production_resource_by_local_id_sync("warehouse", with_children=False)] == [backend_parent_uuid]
    with pytest.raises(ResourceSlotHydrationError, match="未注册"):
        query_production_resource_by_local_id_sync("unknown-local-resource")
    direct_rows = query_production_resource_nodes_sync([backend_target_uuid])
    parent_rows = query_production_resource_nodes_sync([backend_parent_uuid])

    assert [row["uuid"] for row in direct_rows] == [backend_target_uuid]
    assert direct_rows[0]["parent_uuid"] == backend_parent_uuid
    assert direct_rows[0]["extra"][PRODUCTION_SOURCE_RUNTIME_UUID_EXTRA] == (
        local_target_uuid
    )
    assert [row["uuid"] for row in parent_rows] == [
        backend_parent_uuid,
        backend_target_uuid,
    ]


def test_production_projection_fetches_dynamic_backend_material() -> None:
    """material_io 装入的新物料不在部署图时，应按 Backend UUID 补进投影。"""

    graph_uuid = "50000000-0000-4000-8000-000000000201"
    loaded_uuid = "50000000-0000-4000-8000-000000000202"
    install_production_resource_nodes(
        [
            {
                **_raw_resource(graph_uuid),
                "id": "exchange",
                "name": "Exchange",
                "barcode": "UNILAB-GRAPH-exchange",
                "type": "device",
                "class": "device",
                "config": {},
                "data": {},
                "extra": {},
            }
        ],
        {"UNILAB-GRAPH-exchange": graph_uuid},
    )
    fetched = [
        {
            **_raw_resource(loaded_uuid, parent_uuid=graph_uuid),
            "id": loaded_uuid,
            "name": "Loaded tip box",
            "type": "tip_box",
            "class": "resource",
            "barcode": "",
            "config": {},
            "data": {},
            "extra": {},
        }
    ]
    install_production_resource_lookup(lambda _uuids: fetched)

    rows = query_production_resource_nodes_sync([loaded_uuid])

    assert [row["uuid"] for row in rows] == [loaded_uuid]
    assert rows[0]["parent_uuid"] == graph_uuid
    assert rows[0]["name"] == "Loaded tip box"
    cached = query_production_resource_nodes_sync([loaded_uuid])
    assert cached[0]["uuid"] == loaded_uuid


def test_upsert_production_nodes_are_queryable_without_lookup() -> None:
    """已并入投影的动态物料必须能直接按 Backend UUID 查询。"""

    graph_uuid = "50000000-0000-4000-8000-000000000211"
    loaded_uuid = "50000000-0000-4000-8000-000000000212"
    install_production_resource_nodes(
        [
            {
                **_raw_resource(graph_uuid),
                "id": "exchange",
                "name": "Exchange",
                "barcode": "UNILAB-GRAPH-exchange",
                "type": "device",
                "class": "device",
                "config": {},
                "data": {},
                "extra": {},
            }
        ],
        {"UNILAB-GRAPH-exchange": graph_uuid},
    )
    upsert_production_resource_nodes(
        [
            {
                **_raw_resource(loaded_uuid, parent_uuid=graph_uuid),
                "id": loaded_uuid,
                "name": "Loaded tip box",
            }
        ]
    )

    rows = query_production_resource_nodes_sync([loaded_uuid])
    assert [row["uuid"] for row in rows] == [loaded_uuid]


def test_production_resource_projection_uses_backend_site_identity() -> None:
    """PLR well/tip spot nodes use Site UUIDs while their owner remains Material."""

    local_plate_uuid = "50000000-0000-4000-8000-000000000121"
    local_well_uuid = "50000000-0000-4000-8000-000000000122"
    backend_plate_uuid = "50000000-0000-4000-8000-000000000123"
    backend_site_uuid = "50000000-0000-4000-8000-000000000124"
    install_production_resource_nodes(
        [[
            {
                **_raw_resource(local_plate_uuid),
                "id": "plate",
                "name": "Plate",
                "type": "plate",
                "class": "plate",
                "barcode": "UNILAB-GRAPH-plate",
                "config": {},
                "data": {},
                "extra": {},
            },
            {
                **_raw_resource(local_well_uuid, parent_uuid=local_plate_uuid),
                "id": "plate_well_A1",
                "name": "plate_well_A1",
                "type": "well",
                "class": "",
                "barcode": "UNILAB-GRAPH-plate_well_A1",
                "config": {},
                "data": {},
                "extra": {},
            },
        ]],
        {"UNILAB-GRAPH-plate": backend_plate_uuid},
        resource_uuids_by_local_id={"plate_well_A1": backend_site_uuid},
    )

    rows = query_production_resource_nodes_sync([backend_plate_uuid])

    assert [row["uuid"] for row in rows] == [
        backend_plate_uuid,
        backend_site_uuid,
    ]
    assert rows[1]["parent_uuid"] == backend_plate_uuid
    assert rows[1]["extra"][PRODUCTION_SOURCE_RUNTIME_UUID_EXTRA] == (
        local_well_uuid
    )


def test_production_projection_resolves_exact_local_runtime_resource(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backend identities must be translated back only at the driver boundary."""

    local_uuid = "50000000-0000-4000-8000-000000000131"
    backend_uuid = "50000000-0000-4000-8000-000000000132"
    projected = SimpleNamespace(
        unilabos_uuid=backend_uuid,
        unilabos_extra={PRODUCTION_SOURCE_RUNTIME_UUID_EXTRA: local_uuid},
        children=[],
    )
    local_resource = SimpleNamespace(unilabos_uuid=local_uuid, children=[])
    tree_set = SimpleNamespace(
        trees=[SimpleNamespace(root_node=SimpleNamespace(res_content=projected))],
        to_plr_resources=lambda: [projected],
    )

    monkeypatch.setattr(base_device_node.BasicConfig, "control_plane", "backend")
    monkeypatch.setattr(base_device_node.BasicConfig, "process_role", "edge_runtime")
    monkeypatch.setattr(
        base_device_node,
        "query_production_resource_nodes_sync",
        lambda _uuids: [{"uuid": backend_uuid, "parent_uuid": None}],
    )
    monkeypatch.setattr(
        base_device_node.ResourceTreeSet,
        "from_raw_dict_list",
        lambda _rows: tree_set,
    )

    class _RuntimeTracker:
        resources = [local_resource]

        def figure_resource(self, _resource: object, try_mode: bool) -> list[object]:
            assert try_mode is True
            return []

        def loop_find_with_uuid(self, root: object, wanted: str) -> object | None:
            if root is local_resource and wanted == local_uuid:
                return local_resource
            return None

    node = object.__new__(BaseROS2DeviceNode)
    node._resource_clients = {}
    node.resource_tracker = _RuntimeTracker()
    node.lab_logger = lambda: _Logger()

    assert node._convert_resources_sync(backend_uuid) == [local_resource]


def test_async_production_projection_resolves_exact_local_runtime_resource(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """异步动作也必须把 Backend UUID 映射回唯一的本地运行时实例。"""

    local_uuid = "50000000-0000-4000-8000-000000000133"
    backend_uuid = "50000000-0000-4000-8000-000000000134"
    projected = SimpleNamespace(
        unilabos_uuid=backend_uuid,
        unilabos_extra={PRODUCTION_SOURCE_RUNTIME_UUID_EXTRA: local_uuid},
        children=[],
    )
    local_resource = SimpleNamespace(unilabos_uuid=local_uuid, children=[])
    tree_set = SimpleNamespace(
        trees=[SimpleNamespace(root_node=SimpleNamespace(res_content=projected))],
        to_plr_resources=lambda: [projected],
    )

    monkeypatch.setattr(base_device_node.BasicConfig, "control_plane", "backend")
    monkeypatch.setattr(base_device_node.BasicConfig, "process_role", "edge_runtime")
    monkeypatch.setattr(
        base_device_node,
        "query_production_resource_nodes_sync",
        lambda _uuids: [{"uuid": backend_uuid, "parent_uuid": None}],
    )
    monkeypatch.setattr(
        base_device_node.ResourceTreeSet,
        "from_raw_dict_list",
        lambda _rows: tree_set,
    )

    class _RuntimeTracker:
        resources = [local_resource]

        def figure_resource(self, _resource: object, try_mode: bool) -> list[object]:
            assert try_mode is True
            return []

        def loop_find_with_uuid(self, root: object, wanted: str) -> object | None:
            if root is local_resource and wanted == local_uuid:
                return local_resource
            return None

    node = object.__new__(BaseROS2DeviceNode)
    node.resource_tracker = _RuntimeTracker()
    node.lab_logger = lambda: _Logger()

    assert asyncio.run(node._convert_resource_async({"uuid": backend_uuid})) is (
        local_resource
    )


def test_managed_local_edge_uses_backend_material_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Managed Local Edge 也必须使用 Backend 权威物料投影。

    参数说明：``monkeypatch`` 隔离全局运行角色和资源装配。返回：无；断言 Edge
    Runtime 不会回退到已拆除的本地 Scheduler/ROS 物料查询。
    """

    material_uuid = "50000000-0000-4000-8000-000000000115"
    material = SimpleNamespace(unilabos_uuid=material_uuid, children=[])
    direct_rows = [_raw_resource(material_uuid)]
    install_production_resource_nodes(
        [
            {
                **direct_rows[0],
                "id": "managed-local-sample",
                "name": "Managed local sample",
                "barcode": "UNILAB-GRAPH-managed-local-sample",
                "type": "container",
                "class": "sample",
                "config": {},
                "data": {},
                "extra": {},
            }
        ],
        {"UNILAB-GRAPH-managed-local-sample": material_uuid},
    )
    monkeypatch.setattr(base_device_node.BasicConfig, "control_plane", "local")
    monkeypatch.setattr(base_device_node.BasicConfig, "process_role", "edge_runtime")
    monkeypatch.setattr(
        base_device_node.ResourceTreeSet,
        "from_raw_dict_list",
        lambda _rows: _tree_set(direct_rows, material),
    )

    class _ForbiddenClient:
        def call_async(self, _request: object) -> object:
            raise AssertionError("Edge Runtime must not query legacy ROS material service")

    node = object.__new__(BaseROS2DeviceNode)
    node._resource_clients = {"c2s_update_resource_tree": _ForbiddenClient()}
    node.resource_tracker = _Tracker()
    node.lab_logger = lambda: _Logger()

    assert node._convert_resources_sync(material_uuid) == [material]


def test_managed_local_native_action_resource_query_uses_backend_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """原生动作读取物料时也必须使用 Backend 权威投影。

    参数说明：``monkeypatch`` 隔离运行角色、资源装配与遗留 ROS 服务。返回：
    无；断言 Edge Runtime 的公开 ``get_resource`` 接缝不访问同进程 Host 的旧
    物料服务。异常：生产投影缺失或资源身份未知时保持失败关闭。
    """

    # ``material_uuid`` 是 Backend 调度参数携带的物料稳定身份。
    material_uuid = "50000000-0000-4000-8000-000000000117"
    # ``material`` 是原生 ROS 动作最终应取得的投影实例。
    material = SimpleNamespace(unilabos_uuid=material_uuid, children=[])
    direct_rows = [_raw_resource(material_uuid)]
    install_production_resource_nodes(
        [
            {
                **direct_rows[0],
                "id": "managed-local-native-sample",
                "name": "Managed local native sample",
                "barcode": "UNILAB-GRAPH-managed-local-native-sample",
                "type": "container",
                "class": "sample",
                "config": {},
                "data": {},
                "extra": {},
            }
        ],
        {"UNILAB-GRAPH-managed-local-native-sample": material_uuid},
    )
    monkeypatch.setattr(base_device_node.BasicConfig, "control_plane", "local")
    monkeypatch.setattr(base_device_node.BasicConfig, "process_role", "edge_runtime")
    monkeypatch.setattr(
        base_device_node.ResourceTreeSet,
        "from_raw_dict_list",
        lambda _rows: _tree_set(direct_rows, material),
    )

    class _ForbiddenClient:
        """拒绝 Edge Runtime 访问遗留 ROS 物料服务。"""

        def call_async(self, _request: object) -> object:
            """若公开查询越过 Backend 投影则立即报告测试失败。

            参数：``_request`` 是不应发送的遗留请求。返回：永不返回。
            异常：始终抛出 ``AssertionError``。
            """

            raise AssertionError("Edge Runtime must not query legacy ROS material service")

    node = object.__new__(BaseROS2DeviceNode)
    node._resource_clients = {"c2s_update_resource_tree": _ForbiddenClient()}
    node.lab_logger = lambda: _Logger()

    result = asyncio.run(node.get_resource([material_uuid]))

    assert result.to_plr_resources() == [material]


def test_managed_local_edge_async_action_uses_backend_material_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HostNode 异步动作也不得回退到 Edge 内的旧物料服务。

    参数说明：``monkeypatch`` 隔离运行角色、资源装配与网络接缝。返回：无；断言
    ``transfer_resource`` 一类异步动作直接使用已注册的 Backend 投影。
    """

    material_uuid = "50000000-0000-4000-8000-000000000116"
    material = SimpleNamespace(unilabos_uuid=material_uuid, children=[])
    direct_rows = [_raw_resource(material_uuid)]
    install_production_resource_nodes(
        [
            {
                **direct_rows[0],
                "id": "managed-local-async-sample",
                "name": "Managed local async sample",
                "barcode": "UNILAB-GRAPH-managed-local-async-sample",
                "type": "container",
                "class": "sample",
                "config": {},
                "data": {},
                "extra": {},
            }
        ],
        {"UNILAB-GRAPH-managed-local-async-sample": material_uuid},
    )
    monkeypatch.setattr(base_device_node.BasicConfig, "control_plane", "local")
    monkeypatch.setattr(base_device_node.BasicConfig, "process_role", "edge_runtime")
    monkeypatch.setattr(
        base_device_node.ResourceTreeSet,
        "from_raw_dict_list",
        lambda _rows: _tree_set(direct_rows, material),
    )

    node = object.__new__(BaseROS2DeviceNode)
    node.resource_tracker = _Tracker()

    async def forbidden_get_resource(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("Edge Runtime must not query legacy async material service")

    node.get_resource = forbidden_get_resource

    assert asyncio.run(node._convert_resource_async({"uuid": material_uuid})) is material


def test_sync_conversion_without_parent_keeps_single_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无父资源的同步物料转换必须保持旧的一次查询行为。

    参数说明：``monkeypatch`` 替换资源树装配边界。返回：无；断言不增加父查询。
    """

    # ``material_uuid`` 是本轮独立物料稳定身份。
    material_uuid = "50000000-0000-4000-8000-000000000106"
    material = SimpleNamespace(unilabos_uuid=material_uuid, children=[])
    direct_rows = [_raw_resource(material_uuid)]
    direct_tree = _tree_set(direct_rows, material)
    monkeypatch.setattr(
        base_device_node.ResourceTreeSet,
        "from_raw_dict_list",
        lambda _rows: direct_tree,
    )
    # ``requests`` 统计同步资源服务调用次数。
    requests: list[dict[str, Any]] = []

    class _Future:
        """立即返回独立物料查询结果。"""

        def done(self) -> bool:
            """报告查询完成。

            参数：无。返回：恒为 ``True``。
            """

            return True

        def result(self) -> SimpleNamespace:
            """返回独立物料 JSON 响应。

            参数：无。返回：含 ``response`` 字段的响应对象。
            """

            return SimpleNamespace(response=json.dumps(direct_rows))

    class _Client:
        """记录一次资源服务查询。"""

        def call_async(self, request: object) -> _Future:
            """保存查询命令并返回完成 Future。

            参数说明：``request`` 是 ROS 串行命令请求。返回：查询 Future。
            """

            requests.append(json.loads(request.command))
            return _Future()

    node = object.__new__(BaseROS2DeviceNode)
    node._resource_clients = {"c2s_update_resource_tree": _Client()}
    node.resource_tracker = _Tracker()
    node.lab_logger = lambda: _Logger()

    assert node._convert_resources_sync(material_uuid) == [material]
    assert len(requests) == 1


def test_async_conversion_hydrates_target_inside_parent_resource_tree() -> None:
    """异步普通动作也必须复用同一父上下文验证并返回目标物料。

    参数：无。返回：无；断言 JSON 异步接缝执行两次查询且保留父库位关系。
    """

    material_uuid, parent_uuid, parent, material = _resource_instances()
    direct_rows = [_raw_resource(material_uuid, parent_uuid=parent_uuid)]
    parent_rows = [
        _raw_resource(parent_uuid),
        _raw_resource(material_uuid, parent_uuid=parent_uuid),
    ]
    direct_tree = _tree_set(direct_rows, material)
    parent_tree = _tree_set(parent_rows, parent)
    # ``queries`` 保存异步查询使用的 UUID 与子树开关。
    queries: list[tuple[list[str], bool]] = []
    node = object.__new__(BaseROS2DeviceNode)

    async def get_resource(
        resource_uuids: list[str],
        *,
        with_children: bool,
    ) -> SimpleNamespace:
        """按稳定 UUID 返回直接物料或父资源树。

        参数说明：``resource_uuids`` 是查询身份；``with_children`` 要求完整子树。
        返回：测试资源树集合。
        """

        queries.append((resource_uuids, with_children))
        return parent_tree if resource_uuids == [parent_uuid] else direct_tree

    node.get_resource = get_resource
    node.resource_tracker = _Tracker()
    node.lab_logger = lambda: _Logger()

    converted = asyncio.run(node._convert_resource_async({"uuid": material_uuid}))

    assert converted is material
    assert material.parent.sites["A1"].name == "A1"
    assert queries == [([material_uuid], True), ([parent_uuid], True)]
