from pylabrobot.resources import create_homogeneous_resources, Coordinate, ResourceHolder, create_ordered_items_2d

from unilabos.resources.itemized_carrier import BottleCarrier
from unilabos.devices.workstation.AI4M.bottles import Hydrogel_Powder_Containing_Bottle, Hydrogel_Clean_Bottle, Hydrogel_Waste_Bottle

# 命名约定：试剂瓶-Bottle，烧杯-Beaker，烧瓶-Flask,小瓶-Vial


# ============================================================================
# 聚合站（PolymerStation）载体定义（统一入口）
# ============================================================================

def Hydrogel_Powder_Containing_1BottleCarrier(name: str) -> BottleCarrier:
    """聚合站-单试剂瓶载架

    参数:
    - name: 载架名称前缀
    """

    # 载架尺寸 (mm)
    carrier_size_x = 127.8
    carrier_size_y = 85.5
    carrier_size_z = 20.0

    # 烧杯/试剂瓶占位尺寸（使用圆形占位）
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
        model="Hydrogel_Powder_Containing_1BottleCarrier",
    )
    carrier.num_items_x = 1
    carrier.num_items_y = 1
    carrier.num_items_z = 1
    # 统一后缀采用 "flask_1" 命名（可按需调整）
    carrier[0] = Hydrogel_Powder_Containing_Bottle(f"{name}_flask_1")
    return carrier

def Hydrogel_Clean_1BottleCarrier(name: str) -> BottleCarrier:
    """聚合站-单试剂瓶载架

    参数:
    - name: 载架名称前缀
    """

    # 载架尺寸 (mm)
    carrier_size_x = 127.8
    carrier_size_y = 85.5
    carrier_size_z = 20.0

    # 烧杯/试剂瓶占位尺寸（使用圆形占位）
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
        model="Hydrogel_Clean_1BottleCarrier",
    )
    carrier.num_items_x = 1
    carrier.num_items_y = 1
    carrier.num_items_z = 1
    # 统一后缀采用 "flask_1" 命名（可按需调整）
    carrier[0] = Hydrogel_Clean_Bottle(f"{name}_flask_1")
    return carrier

def Hydrogel_Waste_1BottleCarrier(name: str) -> BottleCarrier:
    """聚合站-单试剂瓶载架

    参数:
    - name: 载架名称前缀
    """

    # 载架尺寸 (mm)
    carrier_size_x = 127.8
    carrier_size_y = 85.5
    carrier_size_z = 20.0

    # 烧杯/试剂瓶占位尺寸（使用圆形占位）
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
        model="Hydrogel_Waste_1BottleCarrier",
    )
    carrier.num_items_x = 1
    carrier.num_items_y = 1
    carrier.num_items_z = 1
    # 统一后缀采用 "flask_1" 命名（可按需调整）
    carrier[0] = Hydrogel_Waste_Bottle(f"{name}_flask_1")
    return carrier