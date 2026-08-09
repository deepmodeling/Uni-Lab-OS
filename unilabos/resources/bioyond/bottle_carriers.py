from pylabrobot.resources import create_homogeneous_resources, Coordinate, ResourceHolder, create_ordered_items_2d, Container

from unilabos.resources.itemized_carrier import BottleCarrier
from unilabos.resources.bioyond.bottles import (
    BIOYOND_PolymerStation_Solid_Stock,
    BIOYOND_PolymerStation_Solid_Vial,
    BIOYOND_PolymerStation_Liquid_Vial,
    BIOYOND_PolymerStation_Solution_Beaker,
    BIOYOND_PolymerStation_Reagent_Bottle,
    BIOYOND_PolymerStation_Flask,
)


def BIOYOND_PolymerStation_Tip(name: str, size_x: float = 8.0, size_y: float = 8.0, size_z: float = 50.0) -> Container:
    """创建单个枪头资源

    Args:
        name: 枪头名称
        size_x: 枪头宽度 (mm)
        size_y: 枪头长度 (mm)
        size_z: 枪头高度 (mm)

    Returns:
        Container: 枪头容器
    """
    return Container(
        name=name,
        size_x=size_x,
        size_y=size_y,
        size_z=size_z,
        category="tip",
        model="BIOYOND_PolymerStation_Tip",
    )
# 命名约定：试剂瓶-Bottle，烧杯-Beaker，烧瓶-Flask,小瓶-Vial


# ============================================================================
# 聚合站（PolymerStation）载体定义（统一入口）
# ============================================================================

def BIOYOND_PolymerStation_6StockCarrier(name: str) -> BottleCarrier:
    """聚合站-6孔样品板 - 2x3布局

    参数:
    - name: 载架名称前缀

    说明:
    - 统一站点命名为 PolymerStation，使用 PolymerStation 的 Vial 资源类
    - A行（PLR y=0，对应 Bioyond 位置A01~A03）使用 Liquid_Vial（10% 分装小瓶）
    - B行（PLR y=1，对应 Bioyond 位置B01~B03）使用 Solid_Vial（90% 分装小瓶）
    """

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
        model="BIOYOND_PolymerStation_6StockCarrier",
    )
    carrier.num_items_x = 3
    carrier.num_items_y = 2
    carrier.num_items_z = 1

    # 布局说明：
    # - num_items_x=3, num_items_y=2 表示 3列×2行
    # - create_ordered_items_2d 按先y后x的顺序创建(列优先)
    # - 索引顺序: 0=A1(x=0,y=0), 1=B1(x=0,y=1), 2=A2(x=1,y=0), 3=B2(x=1,y=1), 4=A3(x=2,y=0), 5=B3(x=2,y=1)
    #
    # Bioyond坐标映射: PLR(x,y) → Bioyond(y+1,x+1)
    # - A行(PLR y=0) → Bioyond x=1 → 10%分装小瓶
    # - B行(PLR y=1) → Bioyond x=2 → 90%分装小瓶

    ordering = ["A1", "B1", "A2", "B2", "A3", "B3"]
    for col in range(3):  # 3列
        for row in range(2):  # 2行
            idx = col * 2 + row  # 计算索引: 列优先顺序
            if row == 0:  # A行 (PLR y=0 → Bioyond x=1)
                carrier[idx] = BIOYOND_PolymerStation_Liquid_Vial(f"{ordering[idx]}")
            else:  # B行 (PLR y=1 → Bioyond x=2)
                carrier[idx] = BIOYOND_PolymerStation_Solid_Vial(f"{ordering[idx]}")
    return carrier


def BIOYOND_PolymerStation_8StockCarrier(name: str) -> BottleCarrier:
    """聚合站-8孔样品板 - 2x4布局

    参数:
    - name: 载架名称前缀

    说明:
    - 统一站点命名为 PolymerStation，使用 PolymerStation 的 Solid_Stock 资源类
    """

    # 载架尺寸 (mm)
    carrier_size_x = 128.0
    carrier_size_y = 85.5
    carrier_size_z = 50.0

    # 瓶位尺寸
    bottle_diameter = 20.0
    bottle_spacing_x = 30.0  # X方向间距
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
        model="BIOYOND_PolymerStation_8StockCarrier",
    )
    carrier.num_items_x = 4
    carrier.num_items_y = 2
    carrier.num_items_z = 1
    ordering = ["A1", "B1", "A2", "B2", "A3", "B3", "A4", "B4"]
    for i in range(8):
        carrier[i] = BIOYOND_PolymerStation_Solid_Stock(f"{name}_vial_{ordering[i]}")
    return carrier


def BIOYOND_PolymerStation_1BottleCarrier(name: str) -> BottleCarrier:
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
        model="BIOYOND_PolymerStation_1BottleCarrier",
    )
    carrier.num_items_x = 1
    carrier.num_items_y = 1
    carrier.num_items_z = 1
    # 统一后缀采用 "flask_1" 命名（可按需调整）
    carrier[0] = BIOYOND_PolymerStation_Reagent_Bottle(f"{name}_flask_1")
    return carrier


def BIOYOND_PolymerStation_1FlaskCarrier(name: str) -> BottleCarrier:
    """聚合站-单烧杯载架

    说明:
    - 使用 BIOYOND_PolymerStation_Flask 资源类
    - 载架命名与 model 统一为 PolymerStation
    """

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
        model="BIOYOND_PolymerStation_1FlaskCarrier",
    )
    carrier.num_items_x = 1
    carrier.num_items_y = 1
    carrier.num_items_z = 1
    carrier[0] = BIOYOND_PolymerStation_Flask(f"{name}_flask_1")
    return carrier


# ============================================================================
# 其他载体定义
# ============================================================================

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
        model="BIOYOND_Electrolyte_6VialCarrier",
    )
    carrier.num_items_x = 3
    carrier.num_items_y = 2
    carrier.num_items_z = 1
    for i in range(6):
        carrier[i] = BIOYOND_PolymerStation_Solid_Vial(f"{name}_vial_{i+1}")
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
        model="BIOYOND_Electrolyte_1BottleCarrier",
    )
    carrier.num_items_x = 1
    carrier.num_items_y = 1
    carrier.num_items_z = 1
    carrier[0] = BIOYOND_PolymerStation_Solution_Beaker(f"{name}_beaker_1")
    return carrier


def BIOYOND_PolymerStation_TipBox(
    name: str,
    size_x: float = 127.76,  # 枪头盒宽度
    size_y: float = 85.48,   # 枪头盒长度
    size_z: float = 100.0,   # 枪头盒高度
    barcode: str = None,
) -> BottleCarrier:
    """创建4×6枪头盒 (24个枪头) - 使用 BottleCarrier 结构

    Args:
        name: 枪头盒名称
        size_x: 枪头盒宽度 (mm)
        size_y: 枪头盒长度 (mm)
        size_z: 枪头盒高度 (mm)
        barcode: 条形码

    Returns:
        BottleCarrier: 包含24个枪头孔位的枪头盒载架

    布局说明:
        - 4行×6列 (A-D, 1-6)
        - 枪头孔位间距: 18mm (x方向) × 18mm (y方向)
        - 起始位置居中对齐
        - 索引顺序: 列优先 (0=A1, 1=B1, 2=C1, 3=D1, 4=A2, ...)
    """
    # 枪头孔位参数
    num_cols = 6  # 1-6 (x方向)
    num_rows = 4  # A-D (y方向)
    tip_diameter = 8.0  # 枪头孔位直径
    tip_spacing_x = 18.0  # 列间距 (增加到18mm，更宽松)
    tip_spacing_y = 18.0  # 行间距 (增加到18mm，更宽松)

    # 计算起始位置 (居中对齐)
    total_width = (num_cols - 1) * tip_spacing_x + tip_diameter
    total_height = (num_rows - 1) * tip_spacing_y + tip_diameter
    start_x = (size_x - total_width) / 2
    start_y = (size_y - total_height) / 2

    # 使用 create_ordered_items_2d 创建孔位
    # create_ordered_items_2d 返回的 key 是数字索引: 0, 1, 2, ...
    # 顺序是列优先: 先y后x (即 0=A1, 1=B1, 2=C1, 3=D1, 4=A2, 5=B2, ...)
    sites = create_ordered_items_2d(
        klass=ResourceHolder,
        num_items_x=num_cols,
        num_items_y=num_rows,
        dx=start_x,
        dy=start_y,
        dz=5.0,
        item_dx=tip_spacing_x,
        item_dy=tip_spacing_y,
        size_x=tip_diameter,
        size_y=tip_diameter,
        size_z=50.0,  # 枪头深度
    )

    # 更新 sites 中每个 ResourceHolder 的名称
    for k, v in sites.items():
        v.name = f"{name}_{v.name}"

    # 创建枪头盒载架
    # 注意：不设置 category，使用默认的 "bottle_carrier"，这样前端会显示为完整的矩形载架
    tip_box = BottleCarrier(
        name=name,
        size_x=size_x,
        size_y=size_y,
        size_z=size_z,
        sites=sites,  # 直接使用数字索引的 sites
        model="BIOYOND_PolymerStation_TipBox",
    )

    # 设置自定义属性
    tip_box.barcode = barcode
    tip_box.tip_count = 24  # 4行×6列
    tip_box.num_items_x = num_cols
    tip_box.num_items_y = num_rows
    tip_box.num_items_z = 1

    # ⭐ 枪头盒不需要放入子资源
    # 与其他 carrier 不同，枪头盒在 Bioyond 中是一个整体
    # 不需要追踪每个枪头的状态，保持为空的 ResourceHolder 即可
    # 这样前端会显示24个空槽位，可以用于放置枪头

    return tip_box
