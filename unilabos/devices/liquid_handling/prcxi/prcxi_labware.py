from typing import Optional
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
# 标准品 参照 PLR 标准库的参数，但是用 PRCXI9300Plate 实例化，并注入 UUID
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
        lid=None,
        model="PRCXI_BioER_96_wellplate", 
        category="plate",
        material_info={
            "uuid": "ca877b8b114a4310b429d1de4aae96ee",
            "Code": "ZX-019-2.2",
            "Name": "2.2ml 深孔板",
            "materialEnum": 0,
            "SupplyType": 1
        },

        ordered_items=create_ordered_items_2d(
            Well,
            size_x=8.25,  
            size_y=8.25, 
            size_z=39.3,  # 修改过
            dx=9.5,  
            dy=7.5, 
            dz=6, 
            material_z_thickness=0.8,
            item_dx=9.0,
            item_dy=9.0,
            num_items_x=12, 
            num_items_y=8,
            cross_section_type=CrossSectionType.RECTANGLE,
            bottom_type=WellBottomType.V,  # 是否需要修改？
            max_volume=2200, 
        ),
    )
def PRCXI_nest_1_troughplate(name: str) -> PRCXI9300Plate: 
    """
    对应 JSON Code: ZX-58-10000 (储液槽)
    原型: pylabrobot.resources.nest.nest_1_troughplate_195000uL_Vb
    """
    well_size_x = 127.76 - (14.38 - 9 / 2) * 2  
    well_size_y = 85.48 - (11.24 - 9 / 2) * 2   
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
        "material_z_thickness": 31.4 - 26.85 - 3.55,
    }

    return PRCXI9300Plate(
        name=name,
        size_x=127.76,
        size_y=85.48,
        size_z=31.4,
        lid=None,
        model="PRCXI_Nest_1_troughplate", 
        category="plate",
        material_info={
            "uuid": "04211a2dc93547fe9bf6121eac533650",
            "Code": "ZX-58-10000",
            "Name": "储液槽",
            "materialEnum": 0,
            "SupplyType": 1
        },

        ordered_items=create_ordered_items_2d(
            Well,
            num_items_x=1,
            num_items_y=1,
            dx=14.38 - 9 / 2, 
            dy=11.24 - 9 / 2,  
            dz=3.55,        
            item_dx=9.0,       
            item_dy=9.0,
            **well_kwargs,     # 传入上面计算好的孔参数
        ),
    )
def PRCXI_BioRad_384_wellplate(name: str) -> PRCXI9300Plate: 
    """
    对应 JSON Code: q3 (384板)
    原型: pylabrobot.resources.biorad.BioRad_384_wellplate_50uL_Vb
    """
    return PRCXI9300Plate(
        name=name,
        # 直接抄录 PLR 标准品的物理尺寸
        size_x=127.76,
        size_y=85.48,
        size_z=10.40,
        model="BioRad_384_wellplate_50uL_Vb",
        category="plate",
        # 2. 注入 Unilab 必须的 UUID 信息
        material_info={
            "uuid": "853dcfb6226f476e8b23c250217dc7da",
            "Code": "q3",
            "Name": "384板",
            "SupplyType": 1,
        },
        # 3. 定义孔的排列 (抄录标准参数)
        ordered_items=create_ordered_items_2d(
            Well,
            num_items_x=24,
            num_items_y=16,
            dx=10.58,   # A1 左边缘距离板子左边缘 需要进一步测量
            dy=7.44,    # P1 下边缘距离板子下边缘 需要进一步测量
            dz=1.05,
            item_dx=4.5,
            item_dy=4.5,
            size_x=3.10,
            size_y=3.10,
            size_z=9.35,
            max_volume=50,
            material_z_thickness=1,
            bottom_type=WellBottomType.V,
            cross_section_type=CrossSectionType.CIRCLE,
        )
    )
def PRCXI_AGenBio_4_troughplate(name: str) -> PRCXI9300Plate:
  """
    对应 JSON Code: sdfrth654 (4道储液槽)
    原型: pylabrobot.resources.agenbio.AGenBio_4_troughplate_75000uL_Vb
  """
  INNER_WELL_WIDTH = 26.1  
  INNER_WELL_LENGTH = 71.2 
  well_kwargs = {
    "size_x": 26,
    "size_y": 71.2,  
    "size_z": 42.55,
    "bottom_type": WellBottomType.FLAT,
    "cross_section_type": CrossSectionType.RECTANGLE,
    "compute_height_from_volume": lambda liquid_volume: compute_height_from_volume_rectangle(
      liquid_volume,
      INNER_WELL_LENGTH,
      INNER_WELL_WIDTH,
    ),
    "compute_volume_from_height": lambda liquid_height: compute_volume_from_height_rectangle(
      liquid_height,
      INNER_WELL_LENGTH,
      INNER_WELL_WIDTH,
    ),
    "material_z_thickness": 1,
  }

  return PRCXI9300Plate(
    name=name,
    size_x=127.76,
    size_y=85.48,
    size_z=43.80,
    model="PRCXI_AGenBio_4_troughplate",
    category="plate",
    material_info={
        "uuid": "01953864f6f140ccaa8ddffd4f3e46f5",
        "Code": "sdfrth654",
        "Name": "4道储液槽",
        "materialEnum": 0,
        "SupplyType": 1
    },
        
    ordered_items=create_ordered_items_2d(
      Well,
      num_items_x=4,
      num_items_y=1,
      dx=9.8,
      dy=7.2,
      dz=0.9, 
      item_dx=INNER_WELL_WIDTH + 1,  # 1 mm wall thickness
      item_dy=INNER_WELL_LENGTH,
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
    "material_z_thickness": 31.4 - 26.85 - 3.55,
  }

  return PRCXI9300Plate(
    name=name,
    size_x=127.76, 
    size_y=85.48, 
    size_z=31.4,
    lid=None,
    model="PRCXI_nest_12_troughplate",
    category="plate",
    material_info={
        "uuid": "0f1639987b154e1fac78f4fb29a1f7c1",
        "Code": "12道储液槽",
        "Name": "12道储液槽",
        "materialEnum": 0,
        "SupplyType": 1
    },
    ordered_items=create_ordered_items_2d(
      Well,
      num_items_x=12,
      num_items_y=1,
      dx=14.38 - 8.2 / 2, 
      dy=(85.48 - 71.2) / 2, 
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
  well_kwargs = {
    "size_x": 6.96,
    "size_y": 6.96,
    "size_z": 10.04,
    "bottom_type": WellBottomType.FLAT,
    "material_z_thickness": 1.75,
    "cross_section_type": CrossSectionType.CIRCLE,
    "max_volume": 300,
  }

  return PRCXI9300Plate(
    name=name,
    size_x=127.61,
    size_y=85.24,
    size_z=14.30, 
    lid=None,
    model="PRCXI_CellTreat_96_wellplate",
    category="plate",
    material_info={
        "uuid": "b05b3b2aafd94ec38ea0cd3215ecea8f",
        "Code": "ZX-78-096",
        "Name": "细菌培养皿",
        "materialEnum": 4,
        "SupplyType": 1
    },
    ordered_items=create_ordered_items_2d(
      Well,
      num_items_x=12,
      num_items_y=8,
      dx=10.83, 
      dy=7.67, 
      dz=4.05, 
      item_dx=9,
      item_dy=9,
      **well_kwargs,
    ),
  )
# =========================================================================
# 自定义/需测量品 (Custom Measurement)
# =========================================================================
def PRCXI_10ul_eTips(name: str) -> PRCXI9300TipRack:
    """
    对应 JSON Code: ZX-001-10+
    """
    return PRCXI9300TipRack(
        name=name,
        size_x=122.11,
        size_y=85.48, #修改
        size_z=58.23,
        model="PRCXI_10ul_eTips",
        material_info={
            "uuid": "068b3815e36b4a72a59bae017011b29f",
            "Code": "ZX-001-10+",
            "Name": "10μL加长 Tip头",
            "SupplyType": 1
        },
        ordered_items=create_ordered_items_2d(
            TipSpot,
            num_items_x=12,
            num_items_y=8,
            dx=7.97,  #需要修改
            dy=5.0,   #需修改
            dz=2.0,   #需修改
            item_dx=9.0,
            item_dy=9.0,
            size_x=7.0,
            size_y=7.0,
            size_z=0,
            make_tip=lambda: _make_tip_helper(volume=10, length=52.0, depth=45.1)
        )
    )
def PRCXI_300ul_Tips(name: str) -> PRCXI9300TipRack:
    """
    对应 JSON Code: ZX-001-300
    吸头盒通常比较特殊，需要定义 Tip 对象
    """
    return PRCXI9300TipRack(
        name=name,
        size_x=122.11,
        size_y=85.48, #修改
        size_z=58.23,
        model="PRCXI_300ul_Tips",
        material_info={
            "uuid": "076250742950465b9d6ea29a225dfb00",
            "Code": "ZX-001-300",
            "Name": "300μL Tip头",
            "SupplyType": 1
        },
        ordered_items=create_ordered_items_2d(
            TipSpot,
            num_items_x=12,
            num_items_y=8,
            dx=7.97,  #需要修改
            dy=5.0,   #需修改
            dz=2.0,   #需修改
            item_dx=9.0,
            item_dy=9.0,
            size_x=7.0,
            size_y=7.0,
            size_z=0,
            make_tip=lambda: _make_tip_helper(volume=300, length=60.0, depth=51.0)
        )
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
    model="PRCXI_PCR_Plate_200uL_nonskirted",
    plate_type="non-skirted",
    category="plate",
    material_info={
        "uuid": "73bb9b10bc394978b70e027bf45ce2d3",
        "Code": "ZX-023-0.2",
        "Name": "0.2ml PCR 板",
        "materialEnum": 0,
        "SupplyType": 1
    },
    ordered_items=create_ordered_items_2d(
      Well,
      num_items_x=12,
      num_items_y=8,
      dx=7,
      dy=5,
      dz=0.0,
      item_dx=9,
      item_dy=9,
      size_x=6,
      size_y=6,
      size_z=15.17,
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
    size_x=126,
    size_y=86,
    size_z=21.2,
    model="PRCXI_PCR_Plate_200uL_semiskirted",
    plate_type="semi-skirted",
    category="plate",
    material_info={
        "uuid": "73bb9b10bc394978b70e027bf45ce2d3",
        "Code": "ZX-023-0.2",
        "Name": "0.2ml PCR 板",
        "materialEnum": 0,
        "SupplyType": 1
    },
    ordered_items=create_ordered_items_2d(
      Well,
      num_items_x=12,
      num_items_y=8,
      dx=11,
      dy=8,
      dz=0.0,
      item_dx=9,
      item_dy=9,
      size_x=6,
      size_y=6,
      size_z=15.17,
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
    size_y=86,
    size_z=16.1,
    model="PRCXI_PCR_Plate_200uL_skirted",
    plate_type="skirted",
    category="plate",
    material_info={
        "uuid": "73bb9b10bc394978b70e027bf45ce2d3",
        "Code": "ZX-023-0.2",
        "Name": "0.2ml PCR 板",
        "materialEnum": 0,
        "SupplyType": 1
    },
    ordered_items=create_ordered_items_2d(
      Well,
      num_items_x=12,
      num_items_y=8,
      dx=11,
      dy=8.49,
      dz=0.8,
      item_dx=9,
      item_dy=9,
      size_x=6,
      size_y=6,
      size_z=15.1,
      bottom_type=WellBottomType.V,
      cross_section_type=CrossSectionType.CIRCLE,
    ),
)
def PRCXI_trash(name: str = "trash") -> PRCXI9300Trash:
    """
    对应 JSON Code: q1 (废弃槽)
    """
    return PRCXI9300Trash(
        name="trash",
        size_x=126.59,
        size_y=84.87, 
        size_z=89.5,  # 修改
        category="trash",
        model="PRCXI_trash",
        material_info={
            "uuid": "730067cf07ae43849ddf4034299030e9",
            "Code": "q1",
            "Name": "废弃槽",
            "materialEnum": 0,
            "SupplyType": 1
        }
    )
def PRCXI_96_DeepWell(name: str) -> PRCXI9300Plate:
    """
    对应 JSON Code: q2 (96深孔板)
    """ 
    return PRCXI9300Plate(
        name=name,
        size_x=127.3,
        size_y=85.35,
        size_z=45.0,   #修改
        model="PRCXI_96_DeepWell",
        material_info={
            "uuid": "57b1e4711e9e4a32b529f3132fc5931f", # 对应 q2 uuid
            "Code": "q2",
            "Name": "96深孔板",
            "materialEnum": 0
        },
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
            max_volume=2200
        )
    )
def PRCXI_EP_Adapter(name: str) -> PRCXI9300TubeRack:
    """
    对应 JSON Code: 1 (ep适配器)
    这是一个 4x6 的 EP 管架，适配 1.5mL/2.0mL 离心管
    """
    ep_tube_prototype = Tube(
        name="EP_Tube_1.5mL",
        size_x=10.6,
        size_y=10.6,
        size_z=40.0,  # 管子本身的高度，通常比架子孔略高或持平
        max_volume=1500,
        model="EP_Tube_1.5mL"
    )

    # 计算 PRCXI9300TubeRack 中孔的起始位置 dx, dy
    dy_calc = 85.8 - 10.5 - (3 * 18) - 10.6 
    dx_calc = 3.54
    return PRCXI9300TubeRack(
        name=name,
        size_x=128.04,
        size_y=85.8,
        size_z=42.66,
        model="PRCXI_EP_Adapter",
        category="tube_rack",
        material_info={
            "uuid": "e146697c395e4eabb3d6b74f0dd6aaf7",
            "Code": "1",
            "Name": "ep适配器",
            "materialEnum": 0,
            "SupplyType": 1
        },
        ordered_items=create_ordered_items_2d(
            Tube, 
            num_items_x=6,
            num_items_y=4,
            dx=dx_calc, 
            dy=dy_calc, 
            dz=42.66 - 38.08, # 架高 - 孔深
            item_dx=21.0,  
            item_dy=18.0, 
            size_x=10.6,
            size_y=10.6,
            size_z=40.0,
            max_volume=1500
        )
    )
# =========================================================================
# 无实物，需要测量
# =========================================================================
def PRCXI_Tip1250_Adapter(name: str) -> PRCXI9300PlateAdapter:
    """ Code: ZX-58-1250 """
    return PRCXI9300PlateAdapter(
        name=name, 
        size_x=128, 
        size_y=85, 
        size_z=20,
        material_info={
            "uuid": "3b6f33ffbf734014bcc20e3c63e124d4", 
            "Code": "ZX-58-1250", 
            "Name": "Tip头适配器 1250uL", 
            "SupplyType": 2
            }
    )
def PRCXI_Tip300_Adapter(name: str) -> PRCXI9300PlateAdapter:
    """ Code: ZX-58-300 """
    return PRCXI9300PlateAdapter(
        name=name, 
        size_x=127, 
        size_y=85, 
        size_z=81,
        material_info={
            "uuid": "7c822592b360451fb59690e49ac6b181", 
            "Code": "ZX-58-300", 
            "Name": "ZHONGXI 适配器 300uL", 
            "SupplyType": 2
            }
    )
def PRCXI_Tip10_Adapter(name: str) -> PRCXI9300PlateAdapter:
    """ Code: ZX-58-10 """
    return PRCXI9300PlateAdapter(
        name=name, 
        size_x=128, 
        size_y=85, 
        size_z=72.3,
        material_info={
            "uuid": "8cc3dce884ac41c09f4570d0bcbfb01c", 
            "Code": "ZX-58-10", 
            "Name": "吸头10ul 适配器", 
            "SupplyType": 2
            }
    )
def PRCXI_1250uL_Tips(name: str) -> PRCXI9300TipRack:
    """ Code: ZX-001-1250 """
    return PRCXI9300TipRack(
        name=name,
        size_x=118.09, 
        size_y=80.7, 
        size_z=107.67,
        model="PRCXI_1250uL_Tips",
        material_info={
            "uuid": "7960f49ddfe9448abadda89bd1556936", 
            "Code": "ZX-001-1250", 
            "Name": "1250μL Tip头", 
            "SupplyType": 1
            },
        ordered_items=create_ordered_items_2d(
            TipSpot, 
            num_items_x=12, 
            num_items_y=8,
            dx=9.545 - 7.95/2, 
            dy=8.85 - 7.95/2, 
            dz=2.0,
            item_dx=9, 
            item_dy=9,
            size_x=7.0,
            size_y=7.0,
            size_z=0,
            make_tip=lambda: _make_tip_helper(volume=1250, length=107.67, depth=8)
        )
    )
def PRCXI_10uL_Tips(name: str) -> PRCXI9300TipRack:
    """ Code: ZX-001-10 """
    return PRCXI9300TipRack(
        name=name,
        size_x=120.98, 
        size_y=82.12, 
        size_z=67,
        model="PRCXI_10uL_Tips",
        material_info={
            "uuid": "45f2ed3ad925484d96463d675a0ebf66", 
            "Code": "ZX-001-10", 
            "Name": "10μL Tip头", 
            "SupplyType": 1
            },
        ordered_items=create_ordered_items_2d(
            TipSpot, 
            num_items_x=12, 
            num_items_y=8,
            dx=10.99 - 5/2, 
            dy=9.56 - 5/2, 
            dz=2.0,
            item_dx=9, 
            item_dy=9,
            size_x=7.0,
            size_y=7.0,
            size_z=0,
            make_tip=lambda: _make_tip_helper(volume=1250, length=52.0, depth=5)
        )
    )
def PRCXI_1000uL_Tips(name: str) -> PRCXI9300TipRack:
    """ Code: ZX-001-1000 """
    return PRCXI9300TipRack(
        name=name,
        size_x=128.09, 
        size_y=85.8, 
        size_z=98,
        model="PRCXI_1000uL_Tips",
        material_info={
            "uuid": "80652665f6a54402b2408d50b40398df", 
            "Code": "ZX-001-1000", 
            "Name": "1000μL Tip头", 
            "SupplyType": 1
            },
        ordered_items=create_ordered_items_2d(
            TipSpot, 
            num_items_x=12, 
            num_items_y=8,
            dx=14.5 - 7.95/2, 
            dy=7.425, 
            dz=2.0,
            item_dx=9, 
            item_dy=9,
            size_x=7.0,
            size_y=7.0,
            size_z=0,
            make_tip=lambda: _make_tip_helper(volume=1000, length=55.0, depth=8)
        )
    )
def PRCXI_200uL_Tips(name: str) -> PRCXI9300TipRack:
    """ Code: ZX-001-200 """
    return PRCXI9300TipRack(
        name=name,
        size_x=120.98, 
        size_y=82.12, 
        size_z=66.9,
        model="PRCXI_200uL_Tips",
        material_info={
            "uuid": "7a73bb9e5c264515a8fcbe88aed0e6f7", 
            "Code": "ZX-001-200", 
            "Name": "200μL Tip头", 
            "SupplyType": 1},
        ordered_items=create_ordered_items_2d(
            TipSpot, 
            num_items_x=12, 
            num_items_y=8,
            dx=10.99 - 5.5/2, 
            dy=9.56 - 5.5/2, 
            dz=2.0,
            item_dx=9, 
            item_dy=9,
            size_x=7.0,
            size_z=0,
            size_y=7.0,
            make_tip=lambda: _make_tip_helper(volume=200, length=52.0, depth=5)
        )
    )
def PRCXI_PCR_Adapter(name: str) -> PRCXI9300PlateAdapter:
    """
    对应 JSON Code: ZX-58-0001 (全裙边 PCR适配器)
    """
    return PRCXI9300PlateAdapter(
        name=name,
        size_x=127.76,
        size_y=85.48,
        size_z=21.69,
        model="PRCXI_PCR_Adapter",
        material_info={
            "uuid": "4a043a07c65a4f9bb97745e1f129b165",
            "Code": "ZX-58-0001",
            "Name": "全裙边 PCR适配器",
            "materialEnum": 3,
            "SupplyType": 2
        }
    )
def PRCXI_Reservoir_Adapter(name: str) -> PRCXI9300PlateAdapter:
    """ Code: ZX-ADP-001 """
    return PRCXI9300PlateAdapter(
        name=name, 
        size_x=133, 
        size_y=91.8, 
        size_z=70,
        material_info={
            "uuid": "6bdfdd7069df453896b0806df50f2f4d", 
            "Code": "ZX-ADP-001", 
            "Name": "储液槽 适配器", 
            "SupplyType": 2
            }
    )
def PRCXI_Deep300_Adapter(name: str) -> PRCXI9300PlateAdapter:
    """ Code: ZX-002-300 """
    return PRCXI9300PlateAdapter(
        name=name, 
        size_x=136.4, 
        size_y=93.8, 
        size_z=96,
        material_info={
            "uuid": "9a439bed8f3344549643d6b3bc5a5eb4", 
            "Code": "ZX-002-300", 
            "Name": "300ul深孔板适配器", 
            "SupplyType": 2
            }
    )
def PRCXI_Deep10_Adapter(name: str) -> PRCXI9300PlateAdapter:
    """ Code: ZX-002-10 """
    return PRCXI9300PlateAdapter(
        name=name, 
        size_x=136.5, 
        size_y=93.8, 
        size_z=121.5,
        material_info={
            "uuid": "4dc8d6ecfd0449549683b8ef815a861b", 
            "Code": "ZX-002-10", 
            "Name": "10ul专用深孔板适配器", 
            "SupplyType": 2
            }
    )
def PRCXI_Adapter(name: str) -> PRCXI9300PlateAdapter:
    """ Code: Fhh478 """
    return PRCXI9300PlateAdapter(
        name=name, 
        size_x=120, 
        size_y=90, 
        size_z=86,
        material_info={
            "uuid": "adfabfffa8f24af5abfbba67b8d0f973", 
            "Code": "Fhh478", 
            "Name": "适配器", 
            "SupplyType": 2
            }
    )
def PRCXI_48_DeepWell(name: str) -> PRCXI9300Plate:
    """ Code: 22 (48孔深孔板) """
    print("Warning: Code '22' (48孔深孔板) dimensions are null in JSON.")
    return PRCXI9300Plate(
        name=name, 
        size_x=127, 
        size_y=85, 
        size_z=44, 
        model="PRCXI_48_DeepWell",
        material_info={
            "uuid": "026c5d5cf3d94e56b4e16b7fb53a995b", 
            "Code": "22", 
            "Name": "48孔深孔板", 
            "SupplyType": 1
            },
        ordered_items=create_ordered_items_2d(
            Well, 
            num_items_x=6, 
            num_items_y=8, 
            dx=10, 
            dy=10, 
            dz=1, 
            item_dx=18.5, 
            item_dy=9, 
            size_x=8, 
            size_y=8, 
            size_z=40
        )
    )
def PRCXI_30mm_Adapter(name: str) -> PRCXI9300PlateAdapter:
    """ Code: ZX-58-30 """
    return PRCXI9300PlateAdapter(
        name=name, 
        size_x=132, 
        size_y=93.5, 
        size_z=30,
        material_info={
            "uuid": "a0757a90d8e44e81a68f306a608694f2", 
            "Code": "ZX-58-30", 
            "Name": "30mm适配器", 
            "SupplyType": 2
            }
    )