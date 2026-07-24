from typing import Any, Callable, Dict, List, Optional, Tuple
from pylabrobot.resources import Tube, Coordinate
from pylabrobot.resources.well import Well, WellBottomType, CrossSectionType
from pylabrobot.resources.tip import Tip, TipCreator
from pylabrobot.resources.tip_rack import TipRack, TipSpot
from pylabrobot.resources.utils import create_ordered_items_2d
from pylabrobot.resources.height_volume_functions import (
    compute_height_from_volume_rectangle,
    compute_volume_from_height_rectangle,
)

from .prcxi import PRCXI9300Plate, PRCXI9300TipRack, PRCXI9300Trash, PRCXI9300TubeRack, PRCXI9300PlateAdapter

def _make_tip_helper(volume: float, length: float, depth: float) -> Tip:
    """
    PLR 的 Tip 类参数名为: maximal_volume, total_tip_length, fitting_depth
    """
    return Tip(
        has_filter=False, # 默认无滤芯
        maximal_volume=volume,
        total_tip_length=length,
        fitting_depth=depth
    )


# =========================================================================
# Plates
# =========================================================================
def PRCXI_BioER_96_wellplate(name: str) -> PRCXI9300Plate:
    """
    对应 JSON Code: ZX-019-2.2 (2.2ml 深孔板)
    原型: pylabrobot.resources.bioer.BioER_96_wellplate_Vb_2200uL
    """
    return PRCXI9300Plate(
        name=name,
        size_x=127.1,
        size_y=85.0,
        size_z=44.2,
        model="PRCXI_BioER_96_wellplate",
        category="plate",
        material_info={"uuid": "ca877b8b114a4310b429d1de4aae96ee", "id_v4": "655a416b-5cca-40ef-b027-7c88390c3226", "Code": "ZX-019-2.2", "Name": "2.2ml 深孔板", "materialEnum": 0, "SupplyType": 1},
        ordered_items=create_ordered_items_2d(
            Well,
            num_items_x=12,
            num_items_y=8,
            dx=9.5,
            dy=7.5,
            dz=6.0,
            item_dx=9.0,
            item_dy=9.0,
            size_x=8.25,
            size_y=8.25,
            size_z=39.3,
            max_volume=2200.0,
            material_z_thickness=0.8,
            bottom_type=WellBottomType.V,
            cross_section_type=CrossSectionType.RECTANGLE,
        ),
    )
def PRCXI_nest_1_troughplate(name: str) -> PRCXI9300Plate:
    """
    对应 JSON Code: ZX-58-10000 (储液槽)
    原型: pylabrobot.resources.nest.nest_1_troughplate_195000uL_Vb
    """
    well_size_x = 108.0
    well_size_y = 72.0
    well_kwargs = {
        "size_x": well_size_x,
        "size_y": well_size_y,
        "size_z": 26.85,
        "bottom_type": WellBottomType.V,
        "compute_height_from_volume": lambda liquid_volume: compute_height_from_volume_rectangle(
            liquid_volume=liquid_volume, well_length=well_size_x, well_width=well_size_y
        ),
        "compute_volume_from_height": lambda liquid_height: compute_volume_from_height_rectangle(
            liquid_height=liquid_height, well_length=well_size_x, well_width=well_size_y
        ),
        "material_z_thickness": 0.9999999999999973,
    }

    return PRCXI9300Plate(
        name=name,
        size_x=127.76,
        size_y=85.48,
        size_z=31.4,
        lid=None,
        model="PRCXI_Nest_1_troughplate",
        category="plate",
        material_info={"uuid": "04211a2dc93547fe9bf6121eac533650", "id_v4": "715db443-eb6a-49dc-97bc-fbd24fc87053", "Code": "ZX-58-10000", "Name": "储液槽", "materialEnum": 0, "SupplyType": 1},
        ordered_items=create_ordered_items_2d(
            Well,
            num_items_x=1,
            num_items_y=1,
            dx=9.88,
            dy=6.74,
            dz=3.55,
            item_dx=9.0,
            item_dy=9.0,
            **well_kwargs,
        ),
    )
def PRCXI_BioRad_384_wellplate(name: str) -> PRCXI9300Plate:
    """
    对应 JSON Code: q3 (384板)
    原型: pylabrobot.resources.biorad.BioRad_384_wellplate_50uL_Vb
    """
    return PRCXI9300Plate(
        name=name,
        size_x=127.76,
        size_y=85.48,
        size_z=10.4,
        model="BioRad_384_wellplate_50uL_Vb",
        category="plate",
        material_info={"uuid": "853dcfb6226f476e8b23c250217dc7da", "id_v4": "cdb99ebd-392d-4cc7-8325-90fbed9dd1cf", "Code": "q3", "Name": "384板", "SupplyType": 1},
        ordered_items=create_ordered_items_2d(
            Well,
            num_items_x=24,
            num_items_y=16,
            dx=10.58,
            dy=7.44,
            dz=1.05,
            item_dx=4.5,
            item_dy=4.5,
            size_x=3.1,
            size_y=3.1,
            size_z=9.35,
            max_volume=50.0,
            material_z_thickness=1.0,
            bottom_type=WellBottomType.V,
            cross_section_type=CrossSectionType.CIRCLE,
        ),
    )
def PRCXI_AGenBio_4_troughplate(name: str) -> PRCXI9300Plate:
    """
    对应 JSON Code: sdfrth654 (4道储液槽)
    原型: pylabrobot.resources.agenbio.AGenBio_4_troughplate_75000uL_Vb
    """
    well_size_x = 26.0
    well_size_y = 71.2
    well_kwargs = {
        "size_x": well_size_x,
        "size_y": well_size_y,
        "size_z": 42.55,
        "bottom_type": WellBottomType.FLAT,
        "cross_section_type": CrossSectionType.RECTANGLE,
        "compute_height_from_volume": lambda liquid_volume: compute_height_from_volume_rectangle(
            liquid_volume=liquid_volume, well_length=well_size_x, well_width=well_size_y
        ),
        "compute_volume_from_height": lambda liquid_height: compute_volume_from_height_rectangle(
            liquid_height=liquid_height, well_length=well_size_x, well_width=well_size_y
        ),
        "material_z_thickness": 1.0,
    }

    return PRCXI9300Plate(
        name=name,
        size_x=127.76,
        size_y=85.48,
        size_z=43.8,
        lid=None,
        model="PRCXI_AGenBio_4_troughplate",
        category="plate",
        material_info={"uuid": "01953864f6f140ccaa8ddffd4f3e46f5", "id_v4": "d43d5fe1-49e7-49f0-8ca0-9ccd7d5db81c", "Code": "sdfrth654", "Name": "4道储液槽", "materialEnum": 0, "SupplyType": 1},
        ordered_items=create_ordered_items_2d(
            Well,
            num_items_x=4,
            num_items_y=1,
            dx=9.8,
            dy=7.2,
            dz=0.9,
            item_dx=27.1,
            item_dy=9.0,
            **well_kwargs,
        ),
    )
def PRCXI_nest_12_troughplate(name: str) -> PRCXI9300Plate:
    """
    对应 JSON Code: 12道储液槽 (12道储液槽)
    原型: pylabrobot.resources.nest.nest_12_troughplate_15000uL_Vb
    """
    well_size_x = 8.2
    well_size_y = 71.2
    well_kwargs = {
        "size_x": well_size_x,
        "size_y": well_size_y,
        "size_z": 26.85,
        "bottom_type": WellBottomType.V,
        "compute_height_from_volume": lambda liquid_volume: compute_height_from_volume_rectangle(
            liquid_volume=liquid_volume, well_length=well_size_x, well_width=well_size_y
        ),
        "compute_volume_from_height": lambda liquid_height: compute_volume_from_height_rectangle(
            liquid_height=liquid_height, well_length=well_size_x, well_width=well_size_y
        ),
        "material_z_thickness": 0.9999999999999973,
    }

    return PRCXI9300Plate(
        name=name,
        size_x=127.76,
        size_y=85.48,
        size_z=31.4,
        lid=None,
        model="PRCXI_nest_12_troughplate",
        category="plate",
        material_info={"uuid": "0f1639987b154e1fac78f4fb29a1f7c1", "id_v4": "4958b4b8-f9a7-40f4-83d6-38b8ec9def9c", "Code": "12道储液槽", "Name": "12道储液槽", "materialEnum": 0, "SupplyType": 1},
        ordered_items=create_ordered_items_2d(
            Well,
            num_items_x=12,
            num_items_y=1,
            dx=10.28,
            dy=7.14,
            dz=3.55,
            item_dx=9.0,
            item_dy=9.0,
            **well_kwargs,
        ),
    )
def PRCXI_CellTreat_96_wellplate(name: str) -> PRCXI9300Plate:
    """
    对应 JSON Code: ZX-78-096 (细菌培养皿)
    原型: pylabrobot.resources.celltreat.CellTreat_96_wellplate_350ul_Fb
    """
    return PRCXI9300Plate(
        name=name,
        size_x=127.61,
        size_y=85.24,
        size_z=14.3,
        model="PRCXI_CellTreat_96_wellplate",
        category="plate",
        material_info={"uuid": "b05b3b2aafd94ec38ea0cd3215ecea8f", "id_v4": "", "Code": "ZX-78-096", "Name": "细菌培养皿", "materialEnum": 4, "SupplyType": 1},
        ordered_items=create_ordered_items_2d(
            Well,
            num_items_x=12,
            num_items_y=8,
            dx=10.83,
            dy=7.67,
            dz=4.05,
            item_dx=9.0,
            item_dy=9.0,
            size_x=6.96,
            size_y=6.96,
            size_z=10.04,
            max_volume=300.0,
            material_z_thickness=1.75,
            cross_section_type=CrossSectionType.CIRCLE,
        ),
    )
def PRCXI_PCR_Plate_200uL_nonskirted(name: str) -> PRCXI9300Plate:
    """
    对应 JSON Code: ZX-023-0.2 (0.2ml PCR 板)
    """
    return PRCXI9300Plate(
        name=name,
        size_x=119.5,
        size_y=80.0,
        size_z=26.0,
        plate_type="non-skirted",
        model="PRCXI_PCR_Plate_200uL_nonskirted",
        category="plate",
        material_info={"uuid": "73bb9b10bc394978b70e027bf45ce2d3", "id_v4": "2e92b9a1-bee6-44dc-a01b-4f5f41455411", "Code": "ZX-023-0.2", "Name": "0.2ml PCR 板", "materialEnum": 0, "SupplyType": 1},
        ordered_items=create_ordered_items_2d(
            Well,
            num_items_x=12,
            num_items_y=8,
            dx=7.0,
            dy=5.0,
            dz=0.0,
            item_dx=9.0,
            item_dy=9.0,
            size_x=6.0,
            size_y=6.0,
            size_z=15.17,
            max_volume=428.92164499461444,
            bottom_type=WellBottomType.V,
            cross_section_type=CrossSectionType.CIRCLE,
        ),
    )
def PRCXI_PCR_Plate_200uL_semiskirted(name: str) -> PRCXI9300Plate:
    """
    对应 JSON Code: ZX-023-0.2 (0.2ml PCR 板)
    """
    return PRCXI9300Plate(
        name=name,
        size_x=126.0,
        size_y=86.0,
        size_z=21.2,
        plate_type="semi-skirted",
        model="PRCXI_PCR_Plate_200uL_semiskirted",
        category="plate",
        material_info={"uuid": "73bb9b10bc394978b70e027bf45ce2d3", "id_v4": "2e92b9a1-bee6-44dc-a01b-4f5f41455411", "Code": "ZX-023-0.2", "Name": "0.2ml PCR 板", "materialEnum": 0, "SupplyType": 1},
        ordered_items=create_ordered_items_2d(
            Well,
            num_items_x=12,
            num_items_y=8,
            dx=11.0,
            dy=8.0,
            dz=0.0,
            item_dx=9.0,
            item_dy=9.0,
            size_x=6.0,
            size_y=6.0,
            size_z=15.17,
            max_volume=428.92164499461444,
            bottom_type=WellBottomType.V,
            cross_section_type=CrossSectionType.CIRCLE,
        ),
    )
def PRCXI_PCR_Plate_200uL_skirted(name: str) -> PRCXI9300Plate:
    """
    对应 JSON Code: ZX-023-0.2 (0.2ml PCR 板)
    """
    return PRCXI9300Plate(
        name=name,
        size_x=127.76,
        size_y=86.0,
        size_z=16.1,
        plate_type="skirted",
        model="PRCXI_PCR_Plate_200uL_skirted",
        category="plate",
        material_info={"uuid": "73bb9b10bc394978b70e027bf45ce2d3", "id_v4": "2e92b9a1-bee6-44dc-a01b-4f5f41455411", "Code": "ZX-023-0.2", "Name": "0.2ml PCR 板", "materialEnum": 0, "SupplyType": 1},
        ordered_items=create_ordered_items_2d(
            Well,
            num_items_x=12,
            num_items_y=8,
            dx=11.0,
            dy=8.49,
            dz=0.8,
            item_dx=9.0,
            item_dy=9.0,
            size_x=6.0,
            size_y=6.0,
            size_z=15.1,
            max_volume=426.94244162285287,
            bottom_type=WellBottomType.V,
            cross_section_type=CrossSectionType.CIRCLE,
        ),
    )
def PRCXI_96_DeepWell(name: str) -> PRCXI9300Plate:
    """
    对应 JSON Code: q2 (96深孔板)
    """
    return PRCXI9300Plate(
        name=name,
        size_x=127.3,
        size_y=85.35,
        size_z=45.0,
        model="PRCXI_96_DeepWell",
        category="plate",
        material_info={"uuid": "57b1e4711e9e4a32b529f3132fc5931f", "id_v4": "b186e62b-8564-4313-962c-17ec53996d85", "Code": "q2", "Name": "96深孔板", "materialEnum": 0},
        ordered_items=create_ordered_items_2d(
            Well,
            num_items_x=12,
            num_items_y=8,
            dx=10.9,
            dy=8.25,
            dz=2.0,
            item_dx=9.0,
            item_dy=9.0,
            size_x=8.2,
            size_y=8.2,
            size_z=42.0,
            max_volume=2200.0,
            bottom_type=WellBottomType.UNKNOWN,
            cross_section_type=CrossSectionType.CIRCLE,
        ),
    )
def PRCXI_48_DeepWell(name: str) -> PRCXI9300Plate:
    """
    Code: 22 (48孔深孔板)
    """
    return PRCXI9300Plate(
        name=name,
        size_x=127.0,
        size_y=85.0,
        size_z=44.0,
        model="PRCXI_48_DeepWell",
        category="plate",
        material_info={"uuid": "026c5d5cf3d94e56b4e16b7fb53a995b", "id_v4": "246f3047-9615-4ee4-90eb-14b307b0024b", "Code": "22", "Name": "48孔深孔板", "SupplyType": 1},
        ordered_items=create_ordered_items_2d(
            Well,
            num_items_x=6,
            num_items_y=8,
            dx=10.0,
            dy=10.0,
            dz=1.0,
            item_dx=18.5,
            item_dy=9.0,
            size_x=8.0,
            size_y=8.0,
            size_z=40.0,
            max_volume=2010.6192982974676,
            bottom_type=WellBottomType.UNKNOWN,
            cross_section_type=CrossSectionType.CIRCLE,
        ),
    )
# =========================================================================
# Tip Racks
# =========================================================================
def PRCXI_10ul_eTips(name: str) -> PRCXI9300TipRack:
    """
    对应 JSON Code: ZX-001-10+
    """
    return PRCXI9300TipRack(
        name=name,
        size_x=127.76,
        size_y=85.48,
        size_z=58.0,
        model="PRCXI_10ul_eTips",
        material_info={"uuid": "068b3815e36b4a72a59bae017011b29f", "id_v4": "7ea82dbc-6549-42d0-b43d-7ef6246114f7", "Code": "ZX-001-10+", "Name": "10μL加长 Tip头", "SupplyType": 1},
        ordered_items=create_ordered_items_2d(
            TipSpot,
            num_items_x=12,
            num_items_y=8,
            dx=10.63,
            dy=7.49,
            dz=14.6,
            item_dx=9.0,
            item_dy=9.0,
            size_x=7.5,
            size_y=7.5,
            size_z=52.0,
            make_tip=lambda: _make_tip_helper(volume=10.0, length=52.0, depth=8.2)
        )
    )
def PRCXI_300ul_Tips(name: str) -> PRCXI9300TipRack:
    """
    对应 JSON Code: ZX-001-300
    吸头盒通常比较特殊，需要定义 Tip 对象
    """
    return PRCXI9300TipRack(
        name=name,
        size_x=127.76,
        size_y=85.48,
        size_z=58.0,
        model="PRCXI_300ul_Tips",
        material_info={"uuid": "076250742950465b9d6ea29a225dfb00", "id_v4": "5e655766-9888-4d7f-aa43-0a8dff606cb8", "Code": "ZX-001-300", "Name": "300μL Tip头", "SupplyType": 1},
        ordered_items=create_ordered_items_2d(
            TipSpot,
            num_items_x=12,
            num_items_y=8,
            dx=10.63,
            dy=7.49,
            dz=6.6,
            item_dx=9.0,
            item_dy=9.0,
            size_x=7.5,
            size_y=7.5,
            size_z=60.0,
            make_tip=lambda: _make_tip_helper(volume=300.0, length=60.0, depth=8.2)
        )
    )
def PRCXI_1250uL_Tips(name: str) -> PRCXI9300TipRack:
    """
    Code: ZX-001-1250
    """
    return PRCXI9300TipRack(
        name=name,
        size_x=127.76,
        size_y=85.48,
        size_z=98.0,
        model="PRCXI_1250uL_Tips",
        material_info={"uuid": "7960f49ddfe9448abadda89bd1556936", "id_v4": "a50356db-80ee-484d-b350-b56ad15b486c", "Code": "ZX-001-1250", "Name": "1250μL Tip头", "SupplyType": 1},
        ordered_items=create_ordered_items_2d(
            TipSpot,
            num_items_x=12,
            num_items_y=8,
            dx=10.63,
            dy=7.49,
            dz=6.6,
            item_dx=9.0,
            item_dy=9.0,
            size_x=7.5,
            size_y=7.5,
            size_z=100.0,
            make_tip=lambda: _make_tip_helper(volume=1250.0, length=100.0, depth=8.2)
        )
    )
def PRCXI_10uL_Tips(name: str) -> PRCXI9300TipRack:
    """
    Code: ZX-001-10
    """
    return PRCXI9300TipRack(
        name=name,
        size_x=127.76,
        size_y=85.48,
        size_z=58.0,
        model="PRCXI_10uL_Tips",
        material_info={"uuid": "45f2ed3ad925484d96463d675a0ebf66", "id_v4": "7ea82dbc-6549-42d0-b43d-7ef6246114f7", "Code": "ZX-001-10", "Name": "10μL Tip头", "SupplyType": 1},
        ordered_items=create_ordered_items_2d(
            TipSpot,
            num_items_x=12,
            num_items_y=8,
            dx=10.63,
            dy=7.49,
            dz=14.6,
            item_dx=9.0,
            item_dy=9.0,
            size_x=7.5,
            size_y=7.5,
            size_z=52.0,
            make_tip=lambda: _make_tip_helper(volume=10.0, length=52.0, depth=8.2)
        )
    )
def PRCXI_1000uL_Tips(name: str) -> PRCXI9300TipRack:
    """
    Code: ZX-001-1000
    """
    return PRCXI9300TipRack(
        name=name,
        size_x=127.76,
        size_y=85.48,
        size_z=98.0,
        model="PRCXI_1000uL_Tips",
        material_info={"uuid": "80652665f6a54402b2408d50b40398df", "id_v4": "ea707dab-b7d6-40d9-80b6-1f1167997259", "Code": "ZX-001-1000", "Name": "1000μL Tip头", "SupplyType": 1},
        ordered_items=create_ordered_items_2d(
            TipSpot,
            num_items_x=12,
            num_items_y=8,
            dx=10.63,
            dy=7.49,
            dz=6.6,
            item_dx=9.0,
            item_dy=9.0,
            size_x=7.5,
            size_y=7.5,
            size_z=100.0,
            make_tip=lambda: _make_tip_helper(volume=1000.0, length=100.0, depth=8.2)
        )
    )
def PRCXI_200uL_Tips(name: str) -> PRCXI9300TipRack:
    """
    Code: ZX-001-200
    """
    return PRCXI9300TipRack(
        name=name,
        size_x=120.98,
        size_y=82.12,
        size_z=66.9,
        model="PRCXI_200uL_Tips",
        material_info={"uuid": "7a73bb9e5c264515a8fcbe88aed0e6f7", "id_v4": "0ee5ccee-557f-4ecf-bc58-72460240292a", "Code": "ZX-001-200", "Name": "200μL Tip头", "SupplyType": 1},
        ordered_items=create_ordered_items_2d(
            TipSpot,
            num_items_x=12,
            num_items_y=8,
            dx=8.24,
            dy=6.81,
            dz=2.0,
            item_dx=9.0,
            item_dy=9.0,
            size_x=7.0,
            size_y=7.0,
            size_z=60.0,
            make_tip=lambda: _make_tip_helper(volume=300.0, length=60.0, depth=51.0)
        )
    )
def PRCXI_50uL_tips(name: str) -> PRCXI9300TipRack:
    """
    Code: 
    """
    return PRCXI9300TipRack(
        name=name,
        size_x=127.76,
        size_y=85.48,
        size_z=58.0,
        model="PRCXI_50uL_tips",
        material_info={"uuid": "", "id_v4": "32103665-0677-4004-8e43-5108c44278ff", "Code": "", "Name": "", "SupplyType": 1},
        ordered_items=create_ordered_items_2d(
            TipSpot,
            num_items_x=12,
            num_items_y=8,
            dx=10.63,
            dy=7.49,
            dz=13.6,
            item_dx=9.0,
            item_dy=9.0,
            size_x=7.5,
            size_y=7.5,
            size_z=53.0,
            make_tip=lambda: _make_tip_helper(volume=50.0, length=53.0, depth=8.2)
        )
    )
# =========================================================================
# Trash
# =========================================================================
def PRCXI_trash(name: str = "trash") -> PRCXI9300Trash:
    """
    对应 JSON Code: q1 (废弃槽)
    """
    return PRCXI9300Trash(
        name="trash",
        size_x=126.59,
        size_y=84.87,
        size_z=89.5,
        category="trash",
        model="PRCXI_trash",
        material_info={"uuid": "730067cf07ae43849ddf4034299030e9", "id_v4": "238c27e6-0ad7-4718-81cc-03f80b993de7", "Code": "q1", "Name": "废弃槽", "materialEnum": 0, "SupplyType": 1}
    )
def PRCXI_trash_x2(name: str = "trash") -> PRCXI9300Trash:
    """
    加长废弃槽
    """
    return PRCXI9300Trash(
        name="trash",
        size_x=127.0,
        size_y=180.0,
        size_z=89.5,
        category="trash",
        model="PRCXI_trash_x2",
        material_info={"uuid": "1111", "id_v4": "238c27e6-0ad7-4718-81cc-03f80b993de7", "Code": "1111", "Name": "废弃槽X2", "materialEnum": 0, "SupplyType": 1}
    )
# =========================================================================
# Tube Racks
# =========================================================================
def PRCXI_EP_Adapter(name: str) -> PRCXI9300TubeRack:
    """
    对应 JSON Code: 1 (ep适配器)
    这是一个 4x6 的 EP 管架，适配 1.5mL/2.0mL 离心管
    """
    return PRCXI9300TubeRack(
        name=name,
        size_x=128.04,
        size_y=85.8,
        size_z=42.66,
        model="PRCXI_EP_Adapter",
        category="tube_rack",
        material_info={"uuid": "e146697c395e4eabb3d6b74f0dd6aaf7", "id_v4": "b9bfc405-53a2-4501-84a0-06f9afc9c7d3", "Code": "1", "Name": "ep适配器", "materialEnum": 0, "SupplyType": 1},
        ordered_items=create_ordered_items_2d(
            Tube,
            num_items_x=6,
            num_items_y=4,
            dx=3.54,
            dy=10.7,
            dz=4.58,
            item_dx=21.0,
            item_dy=18.0,
            size_x=10.6,
            size_y=10.6,
            size_z=40.0,
            max_volume=1500.0
        )
    )


def PRCXI_2_Reagent_Rack_50mL(name: str) -> PRCXI9300TubeRack:
    """
    对应 JSON Code: zx-004-50 (试剂2)
    这是一个 1x2 的 50mL 试剂架。
    """
    return PRCXI9300TubeRack(
        name=name,
        size_x=127.76,
        size_y=85.48,
        size_z=0.0,
        model="PRCXI_2_Reagent_Rack_50mL",
        category="tube_rack",
        material_info={
            "uuid": "094e9130a0a24913bdebb8a2bdcf457a", "id_v4": "",
            "Code": "zx-004-50",
            "SupplyType": 1,
            "Name": "试剂2",
            "SummaryName": None,
            "Factory": None,
            "LengthNum": None,
            "WidthNum": None,
            "HeightNum": 0.0,
            "DepthNum": 0.0,
            "PipetteHeight": None,
            "HoleDiameter": None,
            "Margins_X": None,
            "Margins_Y": None,
            "HoleColum": 2,
            "HoleRow": 1,
            "Volume": 50,
            "ImagePath": "C:\\Program Files\\Pipetting workstation chip",
            "CreateTime": None,
            "UpdateTime": None,
            "XSpacing": 64.0,
            "YSpacing": 0.0,
            "materialEnum": 0,
        },
        ordered_items=create_ordered_items_2d(
            Tube,
            num_items_x=2,
            num_items_y=1,
            dx=0.0,
            dy=0.0,
            dz=0.0,
            item_dx=64.0,
            item_dy=0.0,
            size_x=30.0,
            size_y=30.0,
            size_z=0.0,
            max_volume=50000.0,
        ),
    )


def PRCXI_8_Reagent_Rack_10mL(name: str) -> PRCXI9300TubeRack:
    """
    对应 JSON Code: zx-003-10 (试剂1)
    这是一个 2x4 的 10mL 试剂架。
    """
    return PRCXI9300TubeRack(
        name=name,
        size_x=127.76,
        size_y=85.48,
        size_z=0.0,
        model="PRCXI_8_Reagent_Rack_10mL",
        category="tube_rack",
        material_info={
            "uuid": "6086fe7ae1434a0e80fa4194ef3eb0e1", "id_v4": "",
            "Code": "zx-003-10",
            "SupplyType": 1,
            "Name": "试剂1",
            "SummaryName": None,
            "Factory": None,
            "LengthNum": None,
            "WidthNum": None,
            "HeightNum": 0.0,
            "DepthNum": 0.0,
            "PipetteHeight": None,
            "HoleDiameter": None,
            "Margins_X": None,
            "Margins_Y": None,
            "HoleColum": 4,
            "HoleRow": 2,
            "Volume": 10,
            "ImagePath": "C:\\Program Files\\Pipetting workstation chip",
            "CreateTime": None,
            "UpdateTime": None,
            "XSpacing": 42.0,
            "YSpacing": 32.0,
            "materialEnum": 0,
        },
        ordered_items=create_ordered_items_2d(
            Tube,
            num_items_x=4,
            num_items_y=2,
            dx=0.0,
            dy=0.0,
            dz=0.0,
            item_dx=42.0,
            item_dy=32.0,
            size_x=16.0,
            size_y=16.0,
            size_z=0.0,
            max_volume=10000.0,
        ),
    )
# =========================================================================
# Plate Adapters
# =========================================================================
def PRCXI_Tip1250_Adapter(name: str) -> PRCXI9300PlateAdapter:
    """ Code: ZX-58-1250 """
    return PRCXI9300PlateAdapter(
        name=name,
        size_x=128.0,
        size_y=85.0,
        size_z=20.0,
        material_info={"uuid": "3b6f33ffbf734014bcc20e3c63e124d4", "id_v4": "", "Code": "ZX-58-1250", "Name": "Tip头适配器 1250uL", "SupplyType": 2}
    )
def PRCXI_Tip300_Adapter(name: str) -> PRCXI9300PlateAdapter:
    """ Code: ZX-58-300 """
    return PRCXI9300PlateAdapter(
        name=name,
        size_x=127.0,
        size_y=85.0,
        size_z=81.0,
        material_info={"uuid": "7c822592b360451fb59690e49ac6b181", "id_v4": "", "Code": "ZX-58-300", "Name": "ZHONGXI 适配器 300uL", "SupplyType": 2}
    )
def PRCXI_Tip10_Adapter(name: str) -> PRCXI9300PlateAdapter:
    """ Code: ZX-58-10 """
    return PRCXI9300PlateAdapter(
        name=name,
        size_x=128.0,
        size_y=85.0,
        size_z=72.3,
        material_info={"uuid": "8cc3dce884ac41c09f4570d0bcbfb01c", "id_v4": "", "Code": "ZX-58-10", "Name": "吸头10ul 适配器", "SupplyType": 2}
    )
def PRCXI_PCR_Adapter(name: str) -> PRCXI9300PlateAdapter:
    """ 对应 JSON Code: ZX-58-0001 (全裙边 PCR适配器) """
    return PRCXI9300PlateAdapter(
        name=name,
        size_x=127.76,
        size_y=85.48,
        size_z=21.69,
        model="PRCXI_PCR_Adapter",
        material_info={"uuid": "4a043a07c65a4f9bb97745e1f129b165", "id_v4": "", "Code": "ZX-58-0001", "Name": "全裙边 PCR适配器", "materialEnum": 3, "SupplyType": 2}
    )
def PRCXI_Reservoir_Adapter(name: str) -> PRCXI9300PlateAdapter:
    """ Code: ZX-ADP-001 """
    return PRCXI9300PlateAdapter(
        name=name,
        size_x=133.0,
        size_y=91.8,
        size_z=70.0,
        material_info={"uuid": "6bdfdd7069df453896b0806df50f2f4d", "id_v4": "", "Code": "ZX-ADP-001", "Name": "储液槽 适配器", "SupplyType": 2}
    )
def PRCXI_Deep300_Adapter(name: str) -> PRCXI9300PlateAdapter:
    """ Code: ZX-002-300 """
    return PRCXI9300PlateAdapter(
        name=name,
        size_x=136.4,
        size_y=93.8,
        size_z=96.0,
        material_info={"uuid": "9a439bed8f3344549643d6b3bc5a5eb4", "id_v4": "", "Code": "ZX-002-300", "Name": "300ul深孔板适配器", "SupplyType": 2}
    )
def PRCXI_Deep10_Adapter(name: str) -> PRCXI9300PlateAdapter:
    """ Code: ZX-002-10 """
    return PRCXI9300PlateAdapter(
        name=name,
        size_x=136.5,
        size_y=93.8,
        size_z=121.5,
        material_info={"uuid": "4dc8d6ecfd0449549683b8ef815a861b", "id_v4": "", "Code": "ZX-002-10", "Name": "10ul专用深孔板适配器", "SupplyType": 2}
    )
def PRCXI_Adapter(name: str) -> PRCXI9300PlateAdapter:
    """ Code: Fhh478 """
    return PRCXI9300PlateAdapter(
        name=name,
        size_x=120.0,
        size_y=90.0,
        size_z=86.0,
        material_info={"uuid": "adfabfffa8f24af5abfbba67b8d0f973", "id_v4": "", "Code": "Fhh478", "Name": "适配器", "SupplyType": 2}
    )
def PRCXI_30mm_Adapter(name: str) -> PRCXI9300PlateAdapter:
    """ Code: ZX-58-30 """
    return PRCXI9300PlateAdapter(
        name=name,
        size_x=132.0,
        size_y=93.5,
        size_z=30.0,
        material_info={"uuid": "a0757a90d8e44e81a68f306a608694f2", "id_v4": "", "Code": "ZX-58-30", "Name": "30mm适配器", "SupplyType": 2}
    )

PRCXI_TEMPLATE_FACTORY_KINDS: List[Tuple[Callable[..., Any], str]] = [
    (PRCXI_BioER_96_wellplate, "plate"),
    (PRCXI_nest_1_troughplate, "plate"),
    (PRCXI_BioRad_384_wellplate, "plate"),
    (PRCXI_AGenBio_4_troughplate, "plate"),
    (PRCXI_nest_12_troughplate, "plate"),
    (PRCXI_CellTreat_96_wellplate, "plate"),
    (PRCXI_10ul_eTips, "tip_rack"),
    (PRCXI_300ul_Tips, "tip_rack"),
    (PRCXI_PCR_Plate_200uL_nonskirted, "plate"),
    (PRCXI_PCR_Plate_200uL_semiskirted, "plate"),
    (PRCXI_PCR_Plate_200uL_skirted, "plate"),
    (PRCXI_trash, "trash"),
    (PRCXI_96_DeepWell, "plate"),
    (PRCXI_EP_Adapter, "tube_rack"),
    (PRCXI_2_Reagent_Rack_50mL, "tube_rack"),
    (PRCXI_8_Reagent_Rack_10mL, "tube_rack"),
    (PRCXI_1250uL_Tips, "tip_rack"),
    (PRCXI_10uL_Tips, "tip_rack"),
    (PRCXI_1000uL_Tips, "tip_rack"),
    (PRCXI_200uL_Tips, "tip_rack"),
    (PRCXI_48_DeepWell, "plate"),
]


# ---------------------------------------------------------------------------
# 协议上传 / workflow 用：与设备端耗材字典字段对齐的模板描述（供 common 自动匹配）
# ---------------------------------------------------------------------------

_PRCXI_TEMPLATE_SPECS_CACHE: Optional[List[Dict[str, Any]]] = None


def _probe_prcxi_resource(factory: Callable[..., Any]) -> Any:
    probe = "__unilab_template_probe__"
    if factory.__name__ == "PRCXI_trash":
        return factory()
    return factory(probe)


def _first_child_capacity_for_match(resource: Any) -> float:
    """Well max_volume 或 Tip 的 maximal_volume，用于与设备端 Volume 类似的打分。"""
    ch = getattr(resource, "children", None) or []
    if not ch:
        return 0.0
    c0 = ch[0]
    mv = getattr(c0, "max_volume", None)
    if mv is not None:
        return float(mv)
    tip = getattr(c0, "tip", None)
    if tip is not None:
        mv2 = getattr(tip, "maximal_volume", None)
        if mv2 is not None:
            return float(mv2)
    return 0.0


def get_prcxi_labware_template_specs() -> List[Dict[str, Any]]:
    """返回与 ``prcxi._match_and_create_matrix`` 中耗材字段兼容的模板列表，用于按孔数+容量打分。"""
    global _PRCXI_TEMPLATE_SPECS_CACHE
    if _PRCXI_TEMPLATE_SPECS_CACHE is not None:
        return _PRCXI_TEMPLATE_SPECS_CACHE

    out: List[Dict[str, Any]] = []
    for factory, kind in PRCXI_TEMPLATE_FACTORY_KINDS:
        try:
            r = _probe_prcxi_resource(factory)
        except Exception:
            continue
        nx = int(getattr(r, "num_items_x", None) or 0)
        ny = int(getattr(r, "num_items_y", None) or 0)
        nchild = len(getattr(r, "children", []) or [])
        hole_count = nx * ny if nx > 0 and ny > 0 else nchild
        hole_row = ny if nx > 0 and ny > 0 else 0
        hole_col = nx if nx > 0 and ny > 0 else 0
        mi = getattr(r, "material_info", None) or {}
        vol = _first_child_capacity_for_match(r)
        menum = mi.get("materialEnum")
        if menum is None and kind == "tip_rack":
            menum = 1
        elif menum is None and kind == "trash":
            menum = 6
        out.append(
            {
                "class_name": factory.__name__,
                "kind": kind,
                "materialEnum": menum,
                "HoleRow": hole_row,
                "HoleColum": hole_col,
                "Volume": vol,
                "hole_count": hole_count,
                "material_uuid": mi.get("uuid"),
                "material_code": mi.get("Code"),
            }
        )

    _PRCXI_TEMPLATE_SPECS_CACHE = out
    return out
