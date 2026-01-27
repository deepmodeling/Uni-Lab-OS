from pylabrobot.resources import (
    ResourceHolder, 
    Coordinate, 
    create_ordered_items_2d
)
from unilabos.resources.itemized_carrier import BottleCarrier
from unilabos.devices.eit_synthesis_station.config.constants import TraySpec, ResourceCode
import uuid
from typing import Callable, Optional
from unilabos.resources.eit_synthesis_station.items import (
    EIT_REAGENT_BOTTLE_2ML,
    EIT_REAGENT_BOTTLE_8ML,
    EIT_REAGENT_BOTTLE_40ML,
    EIT_REAGENT_BOTTLE_125ML,
    EIT_FLASH_FILTER_INNER_BOTTLE,
    EIT_FLASH_FILTER_OUTER_BOTTLE,
    EIT_TEST_TUBE_MAGNET_2ML,
    EIT_REACTION_SEAL_CAP,
    EIT_REACTION_TUBE_2ML,
    EIT_POWDER_BUCKET_30ML
)

def _create_eit_tray(
        name: str,
        tray_type_enum: str,
        size: tuple,
        model_code: str,
        item_factory: Optional[Callable[[str], object]] = None,
        prefill_items: bool = True) -> BottleCarrier:
    """通用的 EIT 托盘创建工厂函数"""
    cols, rows = getattr(TraySpec, tray_type_enum) # 从 TraySpec 获取 (8, 6) 等规格
    
    size_x, size_y, size_z = size
    margin = 6.0
    min_cell_x = size_x / max(cols, 1)
    min_cell_y = size_y / max(rows, 1)
    bottle_diameter = 0.6 * min(min_cell_x, min_cell_y)
    if cols <= 1 and rows <= 1:
        bottle_diameter = 0.6 * min(size_x, size_y)
    spacing_x = (size_x - 2 * margin - bottle_diameter) / (cols - 1) if cols > 1 else 0.0
    spacing_y = (size_y - 2 * margin - bottle_diameter) / (rows - 1) if rows > 1 else 0.0
    offset_x = (size_x - (cols - 1) * spacing_x - bottle_diameter) / 2.0
    offset_y = (size_y - (rows - 1) * spacing_y - bottle_diameter) / 2.0
    site_size_z = min(10.0, size_z * 0.5)
    carrier_size_z = size_z

    sites = create_ordered_items_2d(
            klass=ResourceHolder,
            num_items_x=cols,
            num_items_y=rows,
            item_dx=spacing_x,           
            item_dy=spacing_y,
            dz=site_size_z,                
            dx=offset_x,
            dy=offset_y,
            size_x=bottle_diameter,
            size_y=bottle_diameter,
            size_z=carrier_size_z,
        )
    for key, site in sites.items():
        site.name = str(key)

    carrier = BottleCarrier(
        name=name,
        size_x=size[0],
        size_y=size[1],
        size_z=size[2],
        sites=sites,
        model=str(int(model_code)),
        category="bottle_carrier",
        hide_label=True,
    )

    carrier.unilabos_uuid = str(uuid.uuid4())
    for site in carrier.children:
        site.unilabos_uuid = str(uuid.uuid4())

    carrier.num_items_x = cols #cols列
    carrier.num_items_y = rows #rows行
    carrier.num_items_z = 1
    
    if item_factory and prefill_items:
        LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        ordering = []

        # 外层循环遍历列 (x 方向)
        for x in range(cols):
            # 内层循环遍历行 (y 方向)
            for y in range(rows):
                # LETTERS[y] 将 0->A, 1->B...
                # x + 1 将列索引转换为从 1 开始的数字
                label = f"{LETTERS[y]}{x + 1}"
                ordering.append(label)
        for c in range(cols):
            for r in range(rows): 
                idx = c * rows + r
                carrier[idx] = item_factory(f"{ordering[idx]}@{name}")

    return carrier

def EIT_REAGENT_BOTTLE_TRAY_2ML(name: str, prefill_items: bool = True) -> BottleCarrier:
    """创建 2 mL 试剂瓶托盘"""
    return _create_eit_tray(
        name=name,
        tray_type_enum="REAGENT_BOTTLE_TRAY_2ML",
        size=(127.8, 85.5, 20.0),
        model_code=ResourceCode.REAGENT_BOTTLE_TRAY_2ML,
        item_factory=EIT_REAGENT_BOTTLE_2ML,
        prefill_items=prefill_items,
    )

def EIT_REAGENT_BOTTLE_TRAY_8ML(name: str, prefill_items: bool = True) -> BottleCarrier:
    """创建 8 mL 试剂瓶托盘"""
    return _create_eit_tray(
        name=name,
        tray_type_enum="REAGENT_BOTTLE_TRAY_8ML",
        size=(127.8, 85.5, 20.0),
        model_code=ResourceCode.REAGENT_BOTTLE_TRAY_8ML,
        item_factory=EIT_REAGENT_BOTTLE_8ML,
        prefill_items=prefill_items,
    )

def EIT_REAGENT_BOTTLE_TRAY_40ML(name: str, prefill_items: bool = True) -> BottleCarrier:
    """创建 40 mL 试剂瓶托盘"""
    return _create_eit_tray(
        name=name,
        tray_type_enum="REAGENT_BOTTLE_TRAY_40ML",
        size=(127.8, 85.5, 30.0),
        model_code=ResourceCode.REAGENT_BOTTLE_TRAY_40ML,
        item_factory=EIT_REAGENT_BOTTLE_40ML,
        prefill_items=prefill_items,
    )

def EIT_REAGENT_BOTTLE_TRAY_125ML(name: str, prefill_items: bool = True) -> BottleCarrier:
    """创建 125 mL 试剂瓶托盘"""
    return _create_eit_tray(
        name=name,
        tray_type_enum="REAGENT_BOTTLE_TRAY_125ML",
        size=(127.8, 85.5, 40.0),
        model_code=ResourceCode.REAGENT_BOTTLE_TRAY_125ML,
        item_factory=EIT_REAGENT_BOTTLE_125ML,
        prefill_items=prefill_items,
    )

def EIT_POWDER_BUCKET_TRAY_30ML(name: str, prefill_items: bool = True) -> BottleCarrier:
    """创建 30 mL 粉桶托盘"""
    return _create_eit_tray(
        name=name,
        tray_type_enum="POWDER_BUCKET_TRAY_30ML",
        size=(127.8, 85.5, 30.0),
        model_code=ResourceCode.POWDER_BUCKET_TRAY_30ML,
        item_factory=EIT_POWDER_BUCKET_30ML,
        prefill_items=prefill_items,
    )

def EIT_TIP_TRAY_1ML(name: str) -> BottleCarrier:
    """创建 1 mL Tip 头托盘"""
    return _create_eit_tray(
        name=name,
        tray_type_enum="TIP_TRAY_1ML",
        size=(127.8, 85.5, 40.0),
        model_code=ResourceCode.TIP_TRAY_1ML
    )

def EIT_TIP_TRAY_5ML(name: str) -> BottleCarrier:
    """创建 5 mL Tip 头托盘"""
    return _create_eit_tray(
        name=name,
        tray_type_enum="TIP_TRAY_5ML",
        size=(127.8, 85.5, 40.0),
        model_code=ResourceCode.TIP_TRAY_5ML
    )  

def EIT_TIP_TRAY_50UL(name: str) -> BottleCarrier:
    """创建 50 μL Tip 头托盘"""
    return _create_eit_tray(
        name=name,
        tray_type_enum="TIP_TRAY_50UL",
        size=(127.8, 85.5, 40.0),
        model_code=ResourceCode.TIP_TRAY_50UL
    )

def EIT_REACTION_TUBE_TRAY_2ML(name: str, prefill_items: bool = True) -> BottleCarrier:
    """创建 2 mL 反应试管托盘"""
    return _create_eit_tray(
        name=name,
        tray_type_enum="REACTION_TUBE_TRAY_2ML",
        size=(127.8, 85.5, 30.0),
        model_code=ResourceCode.REACTION_TUBE_TRAY_2ML,
        item_factory=EIT_REACTION_TUBE_2ML,
        prefill_items=prefill_items,
    )  

def EIT_TEST_TUBE_MAGNET_TRAY_2ML(name: str, prefill_items: bool = True) -> BottleCarrier:
    """创建 2 mL 试管磁子托盘"""
    return _create_eit_tray(
        name=name,
        tray_type_enum="TEST_TUBE_MAGNET_TRAY_2ML",
        size=(127.8, 85.5, 30.0),
        model_code=ResourceCode.TEST_TUBE_MAGNET_TRAY_2ML,
        item_factory=EIT_TEST_TUBE_MAGNET_2ML,
        prefill_items=prefill_items,
    )

def EIT_REACTION_SEAL_CAP_TRAY(name: str, prefill_items: bool = True) -> BottleCarrier:
    """创建 反应密封盖托盘"""
    return _create_eit_tray(
        name=name,
        tray_type_enum="REACTION_SEAL_CAP_TRAY",
        size=(127.8, 85.5, 20.0),
        model_code=ResourceCode.REACTION_SEAL_CAP_TRAY,
        item_factory=EIT_REACTION_SEAL_CAP,
        prefill_items=prefill_items,
    )

def EIT_FLASH_FILTER_INNER_BOTTLE_TRAY(name: str, prefill_items: bool = True) -> BottleCarrier:
    """创建 闪滤瓶内瓶托盘"""
    return _create_eit_tray(
        name=name,
        tray_type_enum="FLASH_FILTER_INNER_BOTTLE_TRAY",
        size=(127.8, 85.5, 30.0),
        model_code=ResourceCode.FLASH_FILTER_INNER_BOTTLE_TRAY,
        item_factory=EIT_FLASH_FILTER_INNER_BOTTLE,
        prefill_items=prefill_items,
    )

def EIT_FLASH_FILTER_OUTER_BOTTLE_TRAY(name: str, prefill_items: bool = True) -> BottleCarrier:
    """创建 闪滤瓶外瓶托盘"""
    return _create_eit_tray(
        name=name,
        tray_type_enum="FLASH_FILTER_OUTER_BOTTLE_TRAY",
        size=(127.8, 85.5, 30.0),
        model_code=ResourceCode.FLASH_FILTER_OUTER_BOTTLE_TRAY,
        item_factory=EIT_FLASH_FILTER_OUTER_BOTTLE,
        prefill_items=prefill_items,
    )
