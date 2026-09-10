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
    ResourceSlotHydrationError,
    plan_resource_slot_parent_context,
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
