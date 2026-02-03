from pylabrobot.resources import create_homogeneous_resources, Coordinate, ResourceHolder, create_ordered_items_2d

from unilabos.resources.itemized_carrier import Bottle, BottleCarrier
from unilabos.resources.bioyond.YB_bottles import (
    YB_jia_yang_tou_da,
    YB_ye_Bottle,
    YB_ye_100ml_Bottle,
    YB_gao_nian_ye_Bottle,
    YB_5ml_fenyeping,
    YB_20ml_fenyeping,
    YB_pei_ye_xiao_Bottle,
    YB_pei_ye_da_Bottle,
    YB_qiang_tou,
)
# 命名约定：试剂瓶-Bottle，烧杯-Beaker，烧瓶-Flask，小瓶-Vial


def BIOYOND_Electrolyte_6VialCarrier(name: str) -> BottleCarrier:
    """6瓶载架 - 2x3布局"""

    # 载架尺寸 (mm)
    carrier_size_x = 127.8
    carrier_size_y = 85.5
    carrier_size_z = 50.0

    # 瓶位尺寸
    bottle_diameter = 30.0
    bottle_spacing_x = 42.0  # X方向间距
    bottle_spacing_y = 35.0  # Y方向间距

    # 计算起始位置 (居中排列)
    start_x = (carrier_size_x - (3 - 1) * bottle_spacing_x - bottle_diameter) / 2
    start_y = (carrier_size_y - (2 - 1) * bottle_spacing_y - bottle_diameter) / 2

    sites = create_ordered_items_2d(
        klass=ResourceHolder,
        num_items_x=3,
        num_items_y=2,
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
        model="Electrolyte_6VialCarrier",
    )
    carrier.num_items_x = 3
    carrier.num_items_y = 2
    carrier.num_items_z = 1
    # for i in range(6):
    #     carrier[i] = YB_Solid_Vial(f"{name}_vial_{i+1}")
    return carrier


def BIOYOND_Electrolyte_1BottleCarrier(name: str) -> BottleCarrier:
    """1瓶载架 - 单个中央位置"""

    # 载架尺寸 (mm)
    carrier_size_x = 127.8
    carrier_size_y = 85.5
    carrier_size_z = 100.0

    # 烧杯尺寸
    beaker_diameter = 80.0

    # 计算中央位置
    center_x = (carrier_size_x - beaker_diameter) / 2
    center_y = (carrier_size_y - beaker_diameter) / 2
    center_z = 5.0

    carrier = BottleCarrier(
        name=name,
        size_x=carrier_size_x,
        size_y=carrier_size_y,
        size_z=carrier_size_z,
        sites=create_homogeneous_resources(
            klass=ResourceHolder,
            locations=[Coordinate(center_x, center_y, center_z)],
            resource_size_x=beaker_diameter,
            resource_size_y=beaker_diameter,
            name_prefix=name,
        ),
        model="Electrolyte_1BottleCarrier",
    )
    carrier.num_items_x = 1
    carrier.num_items_y = 1
    carrier.num_items_z = 1
    # carrier[0] = YB_Solution_Beaker(f"{name}_beaker_1")
    return carrier


def YB_6StockCarrier(name: str) -> BottleCarrier:
    """6瓶载架 - 2x3布局"""

    # 载架尺寸 (mm)
    carrier_size_x = 127.8
    carrier_size_y = 85.5
    carrier_size_z = 50.0

    # 瓶位尺寸
    bottle_diameter = 20.0
    bottle_spacing_x = 42.0  # X方向间距
    bottle_spacing_y = 35.0  # Y方向间距

    # 计算起始位置 (居中排列)
    start_x = (carrier_size_x - (3 - 1) * bottle_spacing_x - bottle_diameter) / 2
    start_y = (carrier_size_y - (2 - 1) * bottle_spacing_y - bottle_diameter) / 2

    sites = create_ordered_items_2d(
        klass=ResourceHolder,
        num_items_x=3,
        num_items_y=2,
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
        model="6StockCarrier",
    )
    carrier.num_items_x = 3
    carrier.num_items_y = 2
    carrier.num_items_z = 1
    ordering = ["A1", "A2", "A3", "B1", "B2", "B3"]  # 自定义顺序
    # for i in range(6):
    #     carrier[i] = YB_Solid_Stock(f"{name}_vial_{ordering[i]}")
    return carrier


def YB_6VialCarrier(name: str) -> BottleCarrier:
    """6瓶载架 - 2x3布局"""

    # 载架尺寸 (mm)
    carrier_size_x = 127.8
    carrier_size_y = 85.5
    carrier_size_z = 50.0

    # 瓶位尺寸
    bottle_diameter = 30.0
    bottle_spacing_x = 42.0  # X方向间距
    bottle_spacing_y = 35.0  # Y方向间距

    # 计算起始位置 (居中排列)
    start_x = (carrier_size_x - (3 - 1) * bottle_spacing_x - bottle_diameter) / 2
    start_y = (carrier_size_y - (2 - 1) * bottle_spacing_y - bottle_diameter) / 2

    sites = create_ordered_items_2d(
        klass=ResourceHolder,
        num_items_x=3,
        num_items_y=2,
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
        model="6VialCarrier",
    )
    carrier.num_items_x = 3
    carrier.num_items_y = 2
    carrier.num_items_z = 1
    ordering = ["A1", "A2", "A3", "B1", "B2", "B3"]  # 自定义顺序
    # for i in range(3):
    #     carrier[i] = YB_Solid_Vial(f"{name}_solidvial_{ordering[i]}")
    # for i in range(3, 6):
    #     carrier[i] = YB_Liquid_Vial(f"{name}_liquidvial_{ordering[i]}")
    return carrier

# 1瓶载架 - 单个中央位置
def YB_ye(name: str) -> BottleCarrier:
    
    # 载架尺寸 (mm)
    carrier_size_x = 127.8
    carrier_size_y = 85.5
    carrier_size_z = 20.0

    # 烧杯尺寸
    beaker_diameter = 60.0

    # 计算中央位置
    center_x = (carrier_size_x - beaker_diameter) / 2
    center_y = (carrier_size_y - beaker_diameter) / 2
    center_z = 5.0

    carrier = BottleCarrier(
        name=name,
        size_x=carrier_size_x,
        size_y=carrier_size_y,
        size_z=carrier_size_z,
        sites=create_homogeneous_resources(
            klass=ResourceHolder,
            locations=[Coordinate(center_x, center_y, center_z)],
            resource_size_x=beaker_diameter,
            resource_size_y=beaker_diameter,
            name_prefix=name,
        ),
        model="YB_ye",
    )
    carrier.num_items_x = 1
    carrier.num_items_y = 1
    carrier.num_items_z = 1
    carrier[0] = YB_ye_Bottle(f"{name}_flask_1")
    return carrier


# 高粘液瓶载架 - 单个中央位置
def YB_gaonianye(name: str) -> BottleCarrier:
    
    # 载架尺寸 (mm)
    carrier_size_x = 127.8
    carrier_size_y = 85.5
    carrier_size_z = 20.0

    # 烧杯尺寸
    beaker_diameter = 60.0

    # 计算中央位置
    center_x = (carrier_size_x - beaker_diameter) / 2
    center_y = (carrier_size_y - beaker_diameter) / 2
    center_z = 5.0

    carrier = BottleCarrier(
        name=name,
        size_x=carrier_size_x,
        size_y=carrier_size_y,
        size_z=carrier_size_z,
        sites=create_homogeneous_resources(
            klass=ResourceHolder,
            locations=[Coordinate(center_x, center_y, center_z)],
            resource_size_x=beaker_diameter,
            resource_size_y=beaker_diameter,
            name_prefix=name,
        ),
        model="YB_gaonianye",
    )
    carrier.num_items_x = 1
    carrier.num_items_y = 1
    carrier.num_items_z = 1
    carrier[0] = YB_gao_nian_ye_Bottle(f"{name}_flask_1")
    return carrier


# 100ml液体瓶载架 - 单个中央位置
def YB_100ml_yeti(name: str) -> BottleCarrier:
    
    # 载架尺寸 (mm)
    carrier_size_x = 127.8
    carrier_size_y = 85.5
    carrier_size_z = 20.0

    # 烧杯尺寸
    beaker_diameter = 60.0

    # 计算中央位置
    center_x = (carrier_size_x - beaker_diameter) / 2
    center_y = (carrier_size_y - beaker_diameter) / 2
    center_z = 5.0

    carrier = BottleCarrier(
        name=name,
        size_x=carrier_size_x,
        size_y=carrier_size_y,
        size_z=carrier_size_z,
        sites=create_homogeneous_resources(
            klass=ResourceHolder,
            locations=[Coordinate(center_x, center_y, center_z)],
            resource_size_x=beaker_diameter,
            resource_size_y=beaker_diameter,
            name_prefix=name,
        ),
        model="YB_100ml_yeti",
    )
    carrier.num_items_x = 1
    carrier.num_items_y = 1
    carrier.num_items_z = 1
    carrier[0] = YB_ye_100ml_Bottle(f"{name}_flask_1")
    return carrier

# 5ml分液瓶板 - 4x2布局，8个位置
def YB_5ml_fenyepingban(name: str) -> BottleCarrier:
    

    # 载架尺寸 (mm)
    carrier_size_x = 127.8
    carrier_size_y = 85.5
    carrier_size_z = 50.0

    # 瓶位尺寸
    bottle_diameter = 15.0
    bottle_spacing_x = 42.0  # X方向间距
    bottle_spacing_y = 35.0  # Y方向间距

    # 计算起始位置 (居中排列)
    start_x = (carrier_size_x - (4 - 1) * bottle_spacing_x - bottle_diameter) / 2
    start_y = (carrier_size_y - (2 - 1) * bottle_spacing_y - bottle_diameter) / 2

    sites = create_ordered_items_2d(
        klass=ResourceHolder,
        num_items_x=4,
        num_items_y=2,
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
        model="YB_5ml_fenyepingban",
    )
    carrier.num_items_x = 4
    carrier.num_items_y = 2
    carrier.num_items_z = 1
    ordering = ["A1", "A2", "A3", "A4", "B1", "B2", "B3", "B4"]
    for i in range(8):
        carrier[i] = YB_5ml_fenyeping(f"{name}_vial_{ordering[i]}")
    return carrier

# 20ml分液瓶板 - 4x2布局，8个位置
def YB_20ml_fenyepingban(name: str) -> BottleCarrier:


    # 载架尺寸 (mm)
    carrier_size_x = 127.8
    carrier_size_y = 85.5
    carrier_size_z = 70.0

    # 瓶位尺寸
    bottle_diameter = 20.0
    bottle_spacing_x = 42.0  # X方向间距
    bottle_spacing_y = 35.0  # Y方向间距

    # 计算起始位置 (居中排列)
    start_x = (carrier_size_x - (4 - 1) * bottle_spacing_x - bottle_diameter) / 2
    start_y = (carrier_size_y - (2 - 1) * bottle_spacing_y - bottle_diameter) / 2

    sites = create_ordered_items_2d(
        klass=ResourceHolder,
        num_items_x=4,
        num_items_y=2,
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
        model="YB_20ml_fenyepingban",
    )
    carrier.num_items_x = 4
    carrier.num_items_y = 2
    carrier.num_items_z = 1
    ordering = ["A1", "A2", "A3", "A4", "B1", "B2", "B3", "B4"]
    for i in range(8):
        carrier[i] = YB_20ml_fenyeping(f"{name}_vial_{ordering[i]}")
    return carrier

# 配液瓶(小)板 - 4x2布局，8个位置
def YB_peiyepingxiaoban(name: str) -> BottleCarrier:
    

    # 载架尺寸 (mm)
    carrier_size_x = 127.8
    carrier_size_y = 85.5
    carrier_size_z = 65.0

    # 瓶位尺寸
    bottle_diameter = 35.0
    bottle_spacing_x = 42.0  # X方向间距
    bottle_spacing_y = 35.0  # Y方向间距

    # 计算起始位置 (居中排列)
    start_x = (carrier_size_x - (4 - 1) * bottle_spacing_x - bottle_diameter) / 2
    start_y = (carrier_size_y - (2 - 1) * bottle_spacing_y - bottle_diameter) / 2

    sites = create_ordered_items_2d(
        klass=ResourceHolder,
        num_items_x=4,
        num_items_y=2,
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
        model="YB_peiyepingxiaoban",
    )
    carrier.num_items_x = 4
    carrier.num_items_y = 2
    carrier.num_items_z = 1
    ordering = ["A1", "A2", "A3", "A4", "B1", "B2", "B3", "B4"]
    for i in range(8):
        carrier[i] = YB_pei_ye_xiao_Bottle(f"{name}_bottle_{ordering[i]}")
    return carrier


# 配液瓶(大)板 - 2x2布局，4个位置
def YB_peiyepingdaban(name: str) -> BottleCarrier:

    # 载架尺寸 (mm)
    carrier_size_x = 127.8
    carrier_size_y = 85.5
    carrier_size_z = 95.0

    # 瓶位尺寸
    bottle_diameter = 55.0
    bottle_spacing_x = 60.0  # X方向间距
    bottle_spacing_y = 60.0  # Y方向间距

    # 计算起始位置 (居中排列)
    start_x = (carrier_size_x - (2 - 1) * bottle_spacing_x - bottle_diameter) / 2
    start_y = (carrier_size_y - (2 - 1) * bottle_spacing_y - bottle_diameter) / 2

    sites = create_ordered_items_2d(
        klass=ResourceHolder,
        num_items_x=2,
        num_items_y=2,
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
        model="YB_peiyepingdaban",
    )
    carrier.num_items_x = 2
    carrier.num_items_y = 2
    carrier.num_items_z = 1
    ordering = ["A1", "A2", "B1", "B2"]
    for i in range(4):
        carrier[i] = YB_pei_ye_da_Bottle(f"{name}_bottle_{ordering[i]}")
    return carrier

# 加样头(大)板 - 1x1布局，1个位置
def YB_jia_yang_tou_da_Carrier(name: str) -> BottleCarrier:
    
    # 载架尺寸 (mm)
    carrier_size_x = 127.8
    carrier_size_y = 85.5
    carrier_size_z = 95.0

    # 瓶位尺寸
    bottle_diameter = 35.0
    bottle_spacing_x = 42.0  # X方向间距
    bottle_spacing_y = 35.0  # Y方向间距

    # 计算起始位置 (居中排列)
    start_x = (carrier_size_x - (1 - 1) * bottle_spacing_x - bottle_diameter) / 2
    start_y = (carrier_size_y - (1 - 1) * bottle_spacing_y - bottle_diameter) / 2

    sites = create_ordered_items_2d(
        klass=ResourceHolder,
        num_items_x=1,
        num_items_y=1,
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
        model="YB_jia_yang_tou_da_Carrier",
    )
    carrier.num_items_x = 1
    carrier.num_items_y = 1
    carrier.num_items_z = 1
    carrier[0] = YB_jia_yang_tou_da(f"{name}_head_1")
    return carrier


def YB_shi_pei_qi_kuai(name: str) -> BottleCarrier:
    """适配器块 - 单个中央位置"""

    # 载架尺寸 (mm)
    carrier_size_x = 127.8
    carrier_size_y = 85.5
    carrier_size_z = 30.0

    # 适配器尺寸
    adapter_diameter = 80.0

    # 计算中央位置
    center_x = (carrier_size_x - adapter_diameter) / 2
    center_y = (carrier_size_y - adapter_diameter) / 2
    center_z = 0.0

    carrier = BottleCarrier(
        name=name,
        size_x=carrier_size_x,
        size_y=carrier_size_y,
        size_z=carrier_size_z,
        sites=create_homogeneous_resources(
            klass=ResourceHolder,
            locations=[Coordinate(center_x, center_y, center_z)],
            resource_size_x=adapter_diameter,
            resource_size_y=adapter_diameter,
            name_prefix=name,
        ),
        model="YB_shi_pei_qi_kuai",
    )
    carrier.num_items_x = 1
    carrier.num_items_y = 1
    carrier.num_items_z = 1
    # 适配器块本身不包含瓶子，只是一个支撑结构
    return carrier


def YB_qiang_tou_he(name: str) -> BottleCarrier:
    """枪头盒 - 8x12布局，96个位置"""

    # 载架尺寸 (mm)
    carrier_size_x = 127.8
    carrier_size_y = 85.5
    carrier_size_z = 55.0

    # 枪头尺寸
    tip_diameter = 10.0
    tip_spacing_x = 9.0  # X方向间距
    tip_spacing_y = 9.0  # Y方向间距

    # 计算起始位置 (居中排列)
    start_x = (carrier_size_x - (12 - 1) * tip_spacing_x - tip_diameter) / 2
    start_y = (carrier_size_y - (8 - 1) * tip_spacing_y - tip_diameter) / 2

    sites = create_ordered_items_2d(
        klass=ResourceHolder,
        num_items_x=12,
        num_items_y=8,
        dx=start_x,
        dy=start_y,
        dz=5.0,
        item_dx=tip_spacing_x,
        item_dy=tip_spacing_y,
        size_x=tip_diameter,
        size_y=tip_diameter,
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
        model="YB_qiang_tou_he",
    )
    carrier.num_items_x = 12
    carrier.num_items_y = 8
    carrier.num_items_z = 1
    # 创建96个枪头
    for i in range(96):
        row = chr(65 + i // 12)  # A-H
        col = (i % 12) + 1  # 1-12
        carrier[i] = YB_qiang_tou(f"{name}_tip_{row}{col}")
    return carrier

