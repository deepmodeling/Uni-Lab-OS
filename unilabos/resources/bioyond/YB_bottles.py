from unilabos.resources.itemized_carrier import Bottle, BottleCarrier
# 工厂函数
"""加样头（大）"""
def YB_jia_yang_tou_da(
    name: str,
    diameter: float = 20.0,
    height: float = 100.0,
    max_volume: float = 30000.0,  # 30mL
    barcode: str = None,
) -> Bottle:
    """创建粉末瓶"""
    return Bottle(
        name=name,
        diameter=diameter,# 未知
        height=height,
        max_volume=max_volume,
        barcode=barcode,
        model="YB_jia_yang_tou_da",
    )

"""液1x1"""
def YB_ye_Bottle(
    name: str,
    diameter: float = 40.0,
    height: float = 70.0,
    max_volume: float = 50000.0,  # 50mL
    barcode: str = None,
) -> Bottle:
    """创建液体瓶"""
    return Bottle(
        name=name,
        diameter=diameter,
        height=height,
        max_volume=max_volume,
        barcode=barcode,
        model="YB_ye_Bottle",
    )

"""100ml液体"""
def YB_ye_100ml_Bottle(
    name: str,
    diameter: float = 50.0,
    height: float = 90.0,
    max_volume: float = 100000.0,  # 100mL
    barcode: str = None,
) -> Bottle:
    """创建100ml液体瓶"""
    return Bottle(
        name=name,
        diameter=diameter,
        height=height,
        max_volume=max_volume,
        barcode=barcode,
        model="YB_100ml_yeti",
    )

"""高粘液"""
def YB_gao_nian_ye_Bottle(
    name: str,
    diameter: float = 40.0,
    height: float = 70.0,
    max_volume: float = 50000.0,  # 50mL
    barcode: str = None,
) -> Bottle:
    """创建高粘液瓶"""
    return Bottle(
        name=name,
        diameter=diameter,
        height=height,
        max_volume=max_volume,
        barcode=barcode,
        model="High_Viscosity_Liquid",
    )

"""5ml分液瓶"""
def YB_5ml_fenyeping(
    name: str,
    diameter: float = 20.0,
    height: float = 50.0,
    max_volume: float = 5000.0,  # 5mL
    barcode: str = None,
) -> Bottle:
    """创建5ml分液瓶"""
    return Bottle(
        name=name,
        diameter=diameter,
        height=height,
        max_volume=max_volume,
        barcode=barcode,
        model="YB_5ml_fenyeping",
    )

"""20ml分液瓶"""
def YB_20ml_fenyeping(
    name: str,
    diameter: float = 30.0,
    height: float = 65.0,
    max_volume: float = 20000.0,  # 20mL
    barcode: str = None,
) -> Bottle:
    """创建20ml分液瓶"""
    return Bottle(
        name=name,
        diameter=diameter,
        height=height,
        max_volume=max_volume,
        barcode=barcode,
        model="YB_20ml_fenyeping",
    )

"""配液瓶(小)"""
def YB_pei_ye_xiao_Bottle(
    name: str,
    diameter: float = 35.0,
    height: float = 60.0,
    max_volume: float = 30000.0,  # 30mL
    barcode: str = None,
) -> Bottle:
    """创建配液瓶(小)"""
    return Bottle(
        name=name,
        diameter=diameter,
        height=height,
        max_volume=max_volume,
        barcode=barcode,
        model="YB_pei_ye_xiao_Bottle",
    )

"""配液瓶(大)"""
def YB_pei_ye_da_Bottle(
    name: str,
    diameter: float = 55.0,
    height: float = 100.0,
    max_volume: float = 150000.0,  # 150mL
    barcode: str = None,
) -> Bottle:
    """创建配液瓶(大)"""
    return Bottle(
        name=name,
        diameter=diameter,
        height=height,
        max_volume=max_volume,
        barcode=barcode,
        model="YB_pei_ye_da_Bottle",
    )

"""枪头"""
def YB_qiang_tou(
    name: str,
    diameter: float = 10.0,
    height: float = 50.0,
    max_volume: float = 1000.0,  # 1mL
    barcode: str = None,
) -> Bottle:
    """创建枪头"""
    return Bottle(
        name=name,
        diameter=diameter,
        height=height,
        max_volume=max_volume,
        barcode=barcode,
        model="YB_qiang_tou",
    )    
