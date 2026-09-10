"""验证仓库资源进入 PLR 与设备驱动时的安全投影边界。"""

from unilabos.resources.resource_tracker import (
    DeviceNodeResourceTracker,
    ResourceDictInstance,
    ResourceTreeInstance,
    ResourceTreeSet,
)
from unilabos.ros.utils.driver_creator import DeviceClassCreator


def _warehouse_node(*, logical_mount: bool) -> ResourceDictInstance:
    """构造最小仓库资源节点。

    Args:
        logical_mount: 是否只承担库存（Inventory）与库位（Site）投影。

    Returns:
        可直接送入资源树或设备资源附着接缝的仓库节点。
    """
    # 固定 UUID 是本测试仓库资源的稳定身份，避免随机身份掩盖往返错误。
    warehouse_uuid = "00000000-0000-4000-8000-000000000001"
    return ResourceDictInstance.get_resource_instance_from_dict(
        {
            "id": "warehouse-repro",
            "uuid": warehouse_uuid,
            "name": "warehouse-repro",
            "type": "warehouse",
            "class": "test.warehouse",
            "position": {
                "size": {"width": 10, "height": 10, "depth": 10},
            },
            "config": {
                "size_x": 10,
                "size_y": 10,
                "size_z": 10,
                "category": "warehouse",
                "logical_mount": logical_mount,
                "sites": [{"label": "S01", "name": "S01"}],
            },
            "data": {},
        }
    )


def _deployment_deck_node() -> ResourceDictInstance:
    """构造带部署期初始化开关的最小工作台资源节点。"""
    return ResourceDictInstance.get_resource_instance_from_dict(
        {
            "id": "deck-repro",
            "uuid": "00000000-0000-4000-8000-000000000002",
            "name": "deck-repro",
            "type": "deck",
            "class": "test.deck",
            "position": {
                "size": {"width": 100, "height": 100, "depth": 10},
            },
            # setup 只控制部署类构造，不属于 PLR Deck 的序列化合同。
            "config": {"setup": False},
            "data": {},
        }
    )


def _site_declaring_container_node() -> ResourceDictInstance:
    """构造带部署期库位声明的最小容器资源节点。"""
    return ResourceDictInstance.get_resource_instance_from_dict(
        {
            "id": "container-repro",
            "uuid": "00000000-0000-4000-8000-000000000003",
            "name": "container-repro",
            "type": "container",
            "class": "test.container",
            "position": {
                "size": {"width": 10, "height": 10, "depth": 10},
            },
            "config": {
                "category": "container",
                "sites": [{"label": "C01", "name": "C01"}],
            },
            "data": {},
        }
    )


def test_deployment_site_declaration_does_not_reach_plr_constructor() -> None:
    """证明已展开的库位声明不会泄漏给任意 PLR 资源构造器。"""
    container_node = _site_declaring_container_node()

    projected_resources = ResourceTreeSet(
        [ResourceTreeInstance(container_node)]
    ).to_plr_resources()

    assert len(projected_resources) == 1
    assert projected_resources[0].name == "container-repro"


def test_deployment_only_deck_setup_does_not_reach_plr_constructor() -> None:
    """证明部署期工作台初始化开关不会泄漏给 PLR 构造器。"""
    deck_node = _deployment_deck_node()

    projected_resources = ResourceTreeSet(
        [ResourceTreeInstance(deck_node)]
    ).to_plr_resources()

    assert len(projected_resources) == 1
    assert projected_resources[0].name == "deck-repro"


def test_nested_warehouse_layout_metadata_does_not_reach_plr_constructor() -> None:
    """证明工作台下仓库的库位元数据不会泄漏给 PLR 构造器。"""
    deck_node = _deployment_deck_node()
    warehouse_node = _warehouse_node(logical_mount=False)
    # 公开资源树用双向父子关系表达工作台下挂的仓库资源。
    warehouse_node.res_content.parent = deck_node.res_content
    deck_node.children = [warehouse_node]

    projected_resources = ResourceTreeSet(
        [ResourceTreeInstance(deck_node)]
    ).to_plr_resources()

    assert len(projected_resources[0].children) == 1
    assert projected_resources[0].children[0].name == "warehouse-repro"


def test_plain_warehouse_projects_to_generic_plr_resource() -> None:
    """证明普通仓库能降级为通用 PLR 资源且保留稳定 UUID。"""
    # 普通仓库节点代表需要进入设备资源跟踪器的实际资源树根。
    warehouse_node = _warehouse_node(logical_mount=False)

    projected_resources = ResourceTreeSet(
        [ResourceTreeInstance(warehouse_node)]
    ).to_plr_resources()

    assert len(projected_resources) == 1
    assert projected_resources[0].name == "warehouse-repro"
    assert (
        projected_resources[0].unilabos_uuid
        == "00000000-0000-4000-8000-000000000001"
    )


def test_logical_mount_warehouse_never_attaches_to_device_driver() -> None:
    """证明只承担库存与库位投影的仓库不会被误当成驱动物理资源。"""
    # 逻辑挂载仓库只提供公共物料图中的库存与库位事实。
    logical_warehouse_node = _warehouse_node(logical_mount=True)
    # 独立跟踪器用于观测设备附着完成后的公开资源集合。
    resource_tracker = DeviceNodeResourceTracker()
    creator = DeviceClassCreator(
        object,
        [logical_warehouse_node],
        resource_tracker,
    )
    creator.device_instance = object()

    creator.attach_resource()

    assert resource_tracker.resources == []
