from unilabos.resources.itemized_carrier import Bottle


def BIOYOND_PolymerStation_Solid_Stock(
    name: str,
    diameter: float = 20.0,
    height: float = 100.0,
    max_volume: float = 30000.0,  # 30mL
    barcode: str = None,
) -> Bottle:
    """创建粉末瓶"""
    return Bottle(
        name=name,
        diameter=diameter,
        height=height,
        max_volume=max_volume,
        barcode=barcode,
        model="BIOYOND_PolymerStation_Solid_Stock",
    )


def BIOYOND_PolymerStation_Solid_Vial(
    name: str,
    diameter: float = 25.0,
    height: float = 60.0,
    max_volume: float = 30000.0,  # 30mL
    barcode: str = None,
) -> Bottle:
    """创建粉末瓶"""
    return Bottle(
        name=name,
        diameter=diameter,
        height=height,
        max_volume=max_volume,
        barcode=barcode,
        model="BIOYOND_PolymerStation_Solid_Vial",
    )


def BIOYOND_PolymerStation_Liquid_Vial(
    name: str,
    diameter: float = 25.0,
    height: float = 60.0,
    max_volume: float = 30000.0,  # 30mL
    barcode: str = None,
) -> Bottle:
    """创建滴定液瓶"""
    return Bottle(
        name=name,
        diameter=diameter,
        height=height,
        max_volume=max_volume,
        barcode=barcode,
        model="BIOYOND_PolymerStation_Liquid_Vial",
    )


def BIOYOND_PolymerStation_Solution_Beaker(
    name: str,
    diameter: float = 60.0,
    height: float = 70.0,
    max_volume: float = 200000.0,  # 200mL
    barcode: str = None,
) -> Bottle:
    """创建溶液烧杯"""
    return Bottle(
        name=name,
        diameter=diameter,
        height=height,
        max_volume=max_volume,
        barcode=barcode,
        model="BIOYOND_PolymerStation_Solution_Beaker",
    )


def BIOYOND_PolymerStation_Reagent_Bottle(
    name: str,
    diameter: float = 70.0,
    height: float = 120.0,
    max_volume: float = 500000.0,  # 500mL
    barcode: str = None,
) -> Bottle:
    """创建试剂瓶"""
    return Bottle(
        name=name,
        diameter=diameter,
        height=height,
        max_volume=max_volume,
        barcode=barcode,
        model="BIOYOND_PolymerStation_Reagent_Bottle",
    )


def BIOYOND_PolymerStation_Reactor(
    name: str,
    diameter: float = 30.0,
    height: float = 80.0,
    max_volume: float = 50000.0,  # 50mL
    barcode: str = None,
) -> Bottle:
    """创建反应器"""
    return Bottle(
        name=name,
        diameter=diameter,
        height=height,
        max_volume=max_volume,
        barcode=barcode,
        model="BIOYOND_PolymerStation_Reactor",
    )


def BIOYOND_PolymerStation_TipBox(
    name: str,
    size_x: float = 127.76,  # 枪头盒宽度
    size_y: float = 85.48,   # 枪头盒长度
    size_z: float = 100.0,   # 枪头盒高度
    barcode: str = None,
):
    """创建4×6枪头盒 (24个枪头)

    Args:
        name: 枪头盒名称
        size_x: 枪头盒宽度 (mm)
        size_y: 枪头盒长度 (mm)
        size_z: 枪头盒高度 (mm)
        barcode: 条形码

    Returns:
        TipBoxCarrier: 包含24个枪头孔位的枪头盒
    """
    from pylabrobot.resources import Container, Coordinate

    # 创建枪头盒容器
    tip_box = Container(
        name=name,
        size_x=size_x,
        size_y=size_y,
        size_z=size_z,
        category="tip_rack",
        model="BIOYOND_PolymerStation_TipBox_4x6",
    )

    # 设置自定义属性
    tip_box.barcode = barcode
    tip_box.tip_count = 24  # 4行×6列
    tip_box.num_items_x = 6  # 6列
    tip_box.num_items_y = 4  # 4行

    # 创建24个枪头孔位 (4行×6列)
    # 假设孔位间距为 9mm
    tip_spacing_x = 9.0  # 列间距
    tip_spacing_y = 9.0  # 行间距
    start_x = 14.38  # 第一个孔位的x偏移
    start_y = 11.24  # 第一个孔位的y偏移

    for row in range(4):  # A, B, C, D
        for col in range(6):  # 1-6
            spot_name = f"{chr(65 + row)}{col + 1}"  # A1, A2, ..., D6
            x = start_x + col * tip_spacing_x
            y = start_y + row * tip_spacing_y

            # 创建枪头孔位容器
            tip_spot = Container(
                name=spot_name,
                size_x=8.0,  # 单个枪头孔位大小
                size_y=8.0,
                size_z=size_z - 10.0,  # 略低于盒子高度
                category="tip_spot",
            )

            # 添加到枪头盒
            tip_box.assign_child_resource(
                tip_spot,
                location=Coordinate(x=x, y=y, z=0)
            )

    return tip_box


def BIOYOND_PolymerStation_Flask(
    name: str,
    diameter: float = 60.0,
    height: float = 70.0,
    max_volume: float = 200000.0,  # 200mL
    barcode: str = None,
) -> Bottle:
    """聚合站-烧杯（统一 Flask 资源到 PolymerStation）"""
    return Bottle(
        name=name,
        diameter=diameter,
        height=height,
        max_volume=max_volume,
        barcode=barcode,
        model="BIOYOND_PolymerStation_Flask",
    )

def BIOYOND_PolymerStation_Measurement_Vial(
    name: str,
    diameter: float = 25.0,
    height: float = 60.0,
    max_volume: float = 20000.0,  # 20mL
    barcode: str = None,
) -> Bottle:
    """创建测量小瓶"""
    return Bottle(
        name=name,
        diameter=diameter,
        height=height,
        max_volume=max_volume,
        barcode=barcode,
        model="BIOYOND_PolymerStation_Measurement_Vial",
    )
