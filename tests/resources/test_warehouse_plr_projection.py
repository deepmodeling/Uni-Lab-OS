"""验证仓库资源进入 PLR 与设备驱动时的安全投影边界。"""

from unilabos.resources.itemized_carrier import BottleCarrier
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


def test_resource_slot_parent_tree_skips_non_plr_device_sibling() -> None:
    deck = _deployment_deck_node()
    warehouse = _warehouse_node(logical_mount=False)
    device = ResourceDictInstance.get_resource_instance_from_dict({
        "id": "mounted-device", "name": "mounted-device", "type": "device",
        "uuid": "00000000-0000-4000-8000-000000000009", "class": "test.device",
    })
    for child in (warehouse, device):
        child.res_content.parent = deck.res_content
        deck.children.append(child)
    tree = ResourceTreeSet([ResourceTreeInstance(deck)])
    projected, = tree.to_plr_resources()
    assert [item.unilabos_uuid for item in projected.children] == [warehouse.res_content.uuid]
    assert projected.children[0].parent is projected
    assert device in deck.children


def test_package_warehouse_fqid_projects_to_generic_plr_resource() -> None:
    """领域仓库 FQID 在没有对应 PLR 子类时必须降级为通用 Resource。"""

    warehouse_uuid = "00000000-0000-4000-8000-000000000011"
    warehouse_node = ResourceDictInstance.get_resource_instance_from_dict(
        {
            "id": warehouse_uuid,
            "uuid": warehouse_uuid,
            "name": "苏州双枪头盒交换位",
            "type": "community.szlab_poly_studio.szlab_tip_exchange_warehouse",
            "class": "community.szlab_poly_studio.szlab_tip_exchange_warehouse",
            "barcode": "UNILAB-GRAPH-tip_exchange",
            "position": {
                "size": {"width": 280, "height": 150, "depth": 150},
            },
            "config": {
                "size_x": 280,
                "size_y": 150,
                "size_z": 150,
                "sites": [{"label": "BOX1", "name": "BOX1"}],
            },
            "data": {},
            "extra": {
                "unilabos_resource_class": (
                    "community.szlab_poly_studio.szlab_tip_exchange_warehouse"
                )
            },
        }
    )

    projected_resources = ResourceTreeSet(
        [ResourceTreeInstance(warehouse_node)]
    ).to_plr_resources()

    assert len(projected_resources) == 1
    assert projected_resources[0].name == "苏州双枪头盒交换位"
    assert projected_resources[0].unilabos_uuid == warehouse_uuid


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


def test_bottle_carrier_keeps_sites_when_config_declares_plr_type() -> None:
    """登记类型 bottle_carrier 且 config.type=BottleCarrier 时必须保留 sites。"""

    carrier_uuid = "00000000-0000-4000-8000-000000000021"
    carrier_node = ResourceDictInstance.get_resource_instance_from_dict(
        {
            "id": carrier_uuid,
            "uuid": carrier_uuid,
            "name": "carrier-repro",
            "type": "bottle_carrier",
            "class": "test.bottle_carrier",
            "position": {
                "size": {"width": 100, "height": 100, "depth": 20},
            },
            "config": {
                "type": "BottleCarrier",
                "size_x": 100,
                "size_y": 100,
                "size_z": 20,
                "category": "bottle_carrier",
                "sites": [
                    {
                        "label": "L1B1",
                        "name": "L1B1",
                        "visible": True,
                        "occupied_by": None,
                        "position": {"x": 0, "y": 0, "z": 0},
                        "size": {"width": 10, "height": 10, "depth": 10},
                        "content_type": ["container"],
                    },
                    {
                        "label": "L1B2",
                        "name": "L1B2",
                        "visible": True,
                        "occupied_by": None,
                        "position": {"x": 20, "y": 0, "z": 0},
                        "size": {"width": 10, "height": 10, "depth": 10},
                        "content_type": ["container"],
                    },
                    {
                        "label": "L1B3",
                        "name": "L1B3",
                        "visible": True,
                        "occupied_by": None,
                        "position": {"x": 40, "y": 0, "z": 0},
                        "size": {"width": 10, "height": 10, "depth": 10},
                        "content_type": ["container"],
                    },
                ],
            },
            "data": {},
        }
    )

    projected_resources = ResourceTreeSet(
        [ResourceTreeInstance(carrier_node)]
    ).to_plr_resources()

    assert len(projected_resources) == 1
    carrier = projected_resources[0]
    assert isinstance(carrier, BottleCarrier)
    assert carrier.name == "carrier-repro"
    assert [site["label"] for site in carrier.serialize()["sites"]] == [
        "L1B1",
        "L1B2",
        "L1B3",
    ]


def test_site_address_table_and_logical_mount_stay_out_of_plr_constructor() -> None:
    """库位地址表与 logical_mount 只给 OS 用：留在 ResourceDict.config，转成 PLR 时不进构造器。"""
    deck_node = _deployment_deck_node()
    deck_node.res_content.config.update({
        "logical_mount": True,
        "site_addresses": [{"name": "position_x01_y01", "target": {"node": "nest", "site": "Pos:1"}}],
    })

    projected, = ResourceTreeSet([ResourceTreeInstance(deck_node)]).to_plr_resources()

    assert projected.name == "deck-repro"
    assert deck_node.res_content.config["site_addresses"][0]["name"] == "position_x01_y01"
