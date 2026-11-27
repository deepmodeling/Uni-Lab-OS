"""验证一个动作的关联物料占位符（ResourceSlot）共用 PLR 对象树。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.resources.resource_tracker import JSON_UNILABOS_PARAM, PARAM_SAMPLE_UUIDS
from unilabos.ros.nodes import base_device_node
from unilabos.ros.nodes.base_device_node import BaseROS2DeviceNode


class _Logger:
    """提供 JSON 动作转换边界需要的最小日志接口。"""

    def error(self, *_args: object, **_kwargs: object) -> None:
        """忽略测试预期内的错误日志。

        参数：位置参数和命名参数承接生产日志调用。返回：无。
        """

    def warning(self, *_args: object, **_kwargs: object) -> None:
        """忽略本地资源未预载的预期告警。

        参数：位置参数和命名参数承接生产日志调用。返回：无。
        """


class _ParentAwarePickDriver:
    """模拟要求物料与来源载架来自同一棵 PLR 对象树的机械臂动作。"""

    def pick(
        self,
        resource: ResourceSlot,
        warehouse: ResourceSlot,
        site: str | None = None,
    ) -> tuple[object, object, str | None]:
        """核验动作入口已经保留真实父子对象身份。

        参数：``resource`` 是待取物料，``warehouse`` 是来源载架，``site`` 是
        可选设备局部库位。返回：驱动实际收到的三个参数。

        异常：两个物料占位符（ResourceSlot）被分别水合为不同对象树时失败。
        """

        if getattr(resource, "parent", None) is not warehouse:
            raise ValueError("物料与来源载架不在同一棵 PLR 对象树")
        return resource, warehouse, site


def test_json_command_batches_related_resource_slots_into_one_plr_tree() -> None:
    """JSON 动作入口必须一次水合物料与来源载架。

    参数：无。返回：无；断言两个物料占位符（ResourceSlot）的
    UUID 按动作参数顺序交给同一次转换，驱动看到的
    ``warehouse`` 与 ``resource.parent`` 是同一 PLR 实例。
    """

    material_uuid = "50000000-0000-4000-8000-000000000010"
    warehouse_uuid = "50000000-0000-4000-8000-000000000011"
    shared_warehouse = SimpleNamespace(unilabos_uuid=warehouse_uuid)
    shared_material = SimpleNamespace(
        unilabos_uuid=material_uuid,
        parent=shared_warehouse,
    )
    # ``conversion_calls`` 记录动作入口是批量还是逐参数转换。
    conversion_calls: list[tuple[str, ...]] = []

    def convert(*uuids: str) -> list[object]:
        """根据调用粒度模拟共享或分裂的 PLR 树。

        参数：``uuids`` 是本次转换的稳定身份。返回：批量调用
        返回同一父子树；单项调用故意返回不同父实例。
        """

        conversion_calls.append(uuids)
        if uuids == (material_uuid, warehouse_uuid):
            return [shared_material, shared_warehouse]
        if uuids == (material_uuid,):
            return [
                SimpleNamespace(
                    unilabos_uuid=material_uuid,
                    parent=SimpleNamespace(unilabos_uuid=warehouse_uuid),
                )
            ]
        if uuids == (warehouse_uuid,):
            return [SimpleNamespace(unilabos_uuid=warehouse_uuid)]
        raise AssertionError(f"未预期的转换请求: {uuids}")

    node = object.__new__(BaseROS2DeviceNode)
    node.driver_instance = _ParentAwarePickDriver()
    node.resource_tracker = None
    node._resolve_driver_method_name = lambda name: name
    node.lab_logger = lambda: _Logger()
    node._convert_resources_sync = convert
    command = {
        "function_name": "pick",
        "function_args": {
            "resource": {"uuid": material_uuid},
            "warehouse": {"uuid": warehouse_uuid},
            "site": "L1B1",
        },
        JSON_UNILABOS_PARAM: {PARAM_SAMPLE_UUIDS: {}},
    }

    result = node._execute_driver_command(json.dumps(command))

    assert conversion_calls == [(material_uuid, warehouse_uuid)]
    assert result == (shared_material, shared_warehouse, "L1B1")


def test_batch_resource_conversion_preserves_shared_parent_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """批量转换后子物料的父资源必须就是同批返回的载架。

    参数：``monkeypatch`` 替换资源树装配边界。返回：无；断言
    ``_convert_resources_sync(material, warehouse)`` 从同一棵 PLR 树按顺序
    取出子物料和父载架，且保持 Python 对象身份。
    """

    material_uuid = "50000000-0000-4000-8000-000000000012"
    warehouse_uuid = "50000000-0000-4000-8000-000000000013"
    warehouse = SimpleNamespace(unilabos_uuid=warehouse_uuid, children=[])
    material = SimpleNamespace(
        unilabos_uuid=material_uuid,
        parent=warehouse,
        children=[],
    )
    warehouse.children.append(material)
    tree_set = SimpleNamespace(
        trees=[SimpleNamespace(root_node=SimpleNamespace(res_content=warehouse))],
        to_plr_resources=lambda: [warehouse],
    )
    monkeypatch.setattr(
        base_device_node.ResourceTreeSet,
        "from_raw_dict_list",
        lambda _raw_data: tree_set,
    )

    class _Future:
        """立即返回同一父子树的资源查询结果。"""

        def done(self) -> bool:
            """报告查询已完成。

            参数：无。返回：恒为 ``True``。
            """

            return True

        def result(self) -> SimpleNamespace:
            """返回批量查询的 JSON 行。

            参数：无。返回：先父后子的资源事实。
            """

            return SimpleNamespace(
                response=json.dumps(
                    [
                        {"uuid": warehouse_uuid},
                        {"uuid": material_uuid, "parent_uuid": warehouse_uuid},
                    ]
                )
            )

    class _Client:
        """提供批量资源查询的最小客户端。"""

        def call_async(self, _request: object) -> _Future:
            """接收 ROS 查询并返回已完成结果。

            参数：``_request`` 是本测试不需解码的串行命令。
            返回：共享父子树响应。
            """

            return _Future()

    class _Tracker:
        """在本轮无预载资源时保留新装配的 PLR 树。"""

        def figure_resource(self, _resource: object, try_mode: bool) -> list[object]:
            """报告本地资源跟踪器未命中。

            参数：``_resource`` 是待映射资源，``try_mode`` 必须为探测模式。
            返回：空候选，使转换使用新建树。
            """

            assert try_mode is True
            return []

        def loop_find_with_uuid(
            self,
            resource: object,
            target_uuid: str,
        ) -> object | None:
            """在共享 PLR 树中按稳定 UUID 递归定位。

            参数：``resource`` 是当前根，``target_uuid`` 是目标身份。
            返回：命中实例，未命中时为 ``None``。
            """

            if getattr(resource, "unilabos_uuid", None) == target_uuid:
                return resource
            for child in getattr(resource, "children", []):
                found = self.loop_find_with_uuid(child, target_uuid)
                if found is not None:
                    return found
            return None

    node = object.__new__(BaseROS2DeviceNode)
    node._resource_clients = {"c2s_update_resource_tree": _Client()}
    node.resource_tracker = _Tracker()
    node.lab_logger = lambda: _Logger()

    converted_material, converted_warehouse = node._convert_resources_sync(
        material_uuid,
        warehouse_uuid,
    )

    assert converted_material is material
    assert converted_warehouse is warehouse
    assert converted_material.parent is converted_warehouse
