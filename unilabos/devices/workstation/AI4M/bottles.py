from unilabos.resources.itemized_carrier import Bottle


def Hydrogel_Powder_Containing_Bottle(
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
        model="Hydrogel_Powder_Containing_Bottle",
    )

def Hydrogel_Clean_Bottle(
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
        model="Hydrogel_Clean_Bottle",
    )

def Hydrogel_Waste_Bottle(
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
        model="Hydrogel_Waste_Bottle",
    )
