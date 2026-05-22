from pylabrobot.resources import create_homogeneous_resources, Coordinate, ResourceHolder, create_ordered_items_2d

from unilabos.resources.itemized_carrier import Bottle, BottleCarrier
from unilabos.resources.bioyond.YB_bottles import (
    YB_pei_ye_xiao_Bottle,
)
# 命名约定：试剂瓶-Bottle，烧杯-Beaker，烧瓶-Flask，小瓶-Vial


def YIHUA_Electrolyte_12VialCarrier(name: str) -> BottleCarrier:
    """12瓶载架 - 2x6布局"""
    # 载架尺寸 (mm)
    carrier_size_x = 120.0
    carrier_size_y = 250.0
    carrier_size_z = 50.0

    # 瓶位尺寸
    bottle_diameter = 35.0
    bottle_spacing_x = 35.0  # X方向间距
    bottle_spacing_y = 35.0  # Y方向间距

    # 计算起始位置 (居中排列)
    start_x = (carrier_size_x - (2 - 1) * bottle_spacing_x - bottle_diameter) / 2
    start_y = (carrier_size_y - (6 - 1) * bottle_spacing_y - bottle_diameter) / 2

    sites = create_ordered_items_2d(
        klass=ResourceHolder,
        num_items_x=2,
        num_items_y=6,
        dx=start_x,
        dy=start_y,
        dz=5.0,
        item_dx=bottle_spacing_x,
        item_dy=bottle_spacing_y,

        size_x=bottle_diameter,
        size_y=bottle_diameter,
        size_z=carrier_size_z,
    )
    for k, v in sites.items():
        v.name = f"{name}_{v.name}"

    carrier = BottleCarrier(
        name=name,
        size_x=carrier_size_x,
        size_y=carrier_size_y,
        size_z=carrier_size_z,
        sites=sites,
        model="Electrolyte_12VialCarrier",
    )
    carrier.num_items_x = 2
    carrier.num_items_y = 6
    carrier.num_items_z = 1
    for i in range(12):
        carrier[i] = YB_pei_ye_xiao_Bottle(f"{name}_vial_{i+1}")
    return carrier
