from unilabos.resources.presets.itemized_carrier import Bottle


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
    from pylabrobot.resources import Tip, TipRack, TipSpot, create_ordered_items_2d

    def make_tip() -> Tip:
        return Tip(
            has_filter=False,
            total_tip_length=size_z - 10.0,
            maximal_volume=1000.0,
            fitting_depth=8.0,
        )

    # 枪头盒必须使用 PLR 的 TipRack/TipSpot 语义，不能把普通 Container
    # 伪装成 tip_spot，否则模板身份和 tip state 都无法正确往返。
    tip_spots = create_ordered_items_2d(
        TipSpot,
        num_items_x=6,
        num_items_y=4,
        dx=14.38,
        dy=11.24,
        dz=0.0,
        item_dx=9.0,
        item_dy=9.0,
        size_x=8.0,
        size_y=8.0,
        size_z=size_z - 10.0,
        make_tip=make_tip,
    )
    tip_box = TipRack(
        name=name,
        size_x=size_x,
        size_y=size_y,
        size_z=size_z,
        ordered_items=tip_spots,
        category="tip_rack",
        model="BIOYOND_PolymerStation_TipBox_4x6",
        with_tips=False,
    )

    # 设置自定义属性
    tip_box.barcode = barcode
    tip_box.tip_count = 24

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
