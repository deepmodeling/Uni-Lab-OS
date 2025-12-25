from unilabos.resources.itemized_carrier import Bottle


def POST_PROCESS_PolymerStation_Reagent_Bottle(
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
        model="POST_PROCESS_PolymerStation_Reagent_Bottle",
    )

