"""
PRCXI 设备使用示例和演示代码

本文件包含 PRCXI9300/9320 设备的使用示例，包括：
- step_mode 单点动作模式示例
- 正常模式批量操作示例
- 布局和物料导出工具
- 其他测试和演示代码
"""

import asyncio
import json
import os
import time
from typing import List, Tuple, Dict, Any

from pylabrobot.resources import Coordinate
from pylabrobot.resources.opentrons.tip_racks import (
    opentrons_96_tiprack_300ul,
    opentrons_96_tiprack_10ul,
    tipone_96_tiprack_200ul,
)
from pylabrobot.resources.opentrons.plates import (
    corning_96_wellplate_360ul_flat,
    nest_96_wellplate_2ml_deep,
)

from unilabos.devices.liquid_handling.prcxi.prcxi import (
    PRCXI9300Deck,
    PRCXI9300Handler,
    PRCXI9300Container,
    PRCXI9300Trash,
    PRCXI9300Api,
    DefaultLayout,
)
from unilabos.resources.graphio import tree_to_list, resource_plr_to_ulab


# ============================================================================
# 工具函数：导出布局和物料列表
# ============================================================================

def export_layout_and_materials(host: str = "192.168.1.201", port: int = 9999, output_file: str = "prcxi_layout_and_materials.json"):
    """获取布局和物料列表并保存为JSON"""
    prcxi_api = PRCXI9300Api(host=host, port=port)
    matrices = prcxi_api.list_matrices()
    materials = prcxi_api.get_all_materials()
    
    # 保存为格式规整的JSON文件
    output_data = {
        "matrices": matrices,
        "materials": materials
    }
    
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(output_data, f, indent=4, ensure_ascii=False)
    
    print(f"布局和物料列表已保存到 {output_file}")
    return output_data


# ============================================================================
# step_mode 使用示例
# ============================================================================
# step_mode 是 PRCXI9320 设备支持的单点动作模式
# 在 step_mode=True 时，每个操作会立即创建协议并执行，适合调试和单步测试
# 在 step_mode=False 时，所有操作会先添加到协议中，最后统一执行，适合生产流程

async def example_step_mode():
    """示例1: 使用 step_mode=True 进行单点动作调试（仅 PRCXI9320 支持）
    
    单点动作模式示例 - 每个操作立即执行
    """
    # 创建 deck
    deck = PRCXI9300Deck(name="PRCXI_Deck_9320", size_x=100, size_y=100, size_z=100)
    
    # 创建资源
    tip_rack = opentrons_96_tiprack_300ul("RackT1")
    plate = corning_96_wellplate_360ul_flat("PlateT2")
    
    # 设置物料信息
    tip_rack.load_state({
        "Material": {
            "uuid": "076250742950465b9d6ea29a225dfb00",
            "Code": "ZX-001-300",
            "Name": "300μL Tip头"
        }
    })
    plate.load_state({
        "Material": {
            "uuid": "57b1e4711e9e4a32b529f3132fc5931f",
            "Code": "ZX-019-2.2",
            "Name": "96深孔板"
        }
    })
    
    deck.assign_child_resource(tip_rack, location=Coordinate(0, 0, 0))
    deck.assign_child_resource(plate, location=Coordinate(50, 0, 0))
    
    # 创建 handler，启用 step_mode（仅 PRCXI9320 支持）
    handler = PRCXI9300Handler(
        deck=deck,
        host="192.168.1.201",
        port=9999,
        timeout=10.0,
        step_mode=True,  # 启用单点动作模式
        is_9320=True,    # 必须是 9320 设备
        setup=True
    )
    
    await handler.setup()
    handler.set_tiprack([tip_rack])
    
    # 在 step_mode 下，每个操作会立即执行
    print("=== 单点动作模式示例 ===")
    print("1. 拾取枪头（立即执行）")
    await handler.pick_up_tips([tip_rack.get_item("A1")], [0])
    
    print("2. 吸取液体（立即执行）")
    await handler.aspirate([plate.get_item("A1")], [50], [0])
    
    print("3. 分液（立即执行）")
    await handler.dispense([plate.get_item("B1")], [50], [0])
    
    print("4. 混合（立即执行）")
    await handler.mix([plate.get_item("B1")], mix_time=3, mix_vol=20)
    
    print("5. 丢弃枪头（立即执行）")
    await handler.discard_tips([0])
    
    print("单点动作模式示例完成")


async def example_normal_mode():
    """示例2: 使用 step_mode=False 进行批量操作（默认模式）
    
    正常模式示例 - 批量执行所有操作
    """
    # 创建 deck
    deck = PRCXI9300Deck(name="PRCXI_Deck_9320", size_x=100, size_y=100, size_z=100)
    
    # 创建资源
    tip_rack = opentrons_96_tiprack_300ul("RackT1")
    source_plate = corning_96_wellplate_360ul_flat("PlateT2")
    dest_plate = corning_96_wellplate_360ul_flat("PlateT3")
    
    # 设置物料信息
    tip_rack.load_state({
        "Material": {
            "uuid": "076250742950465b9d6ea29a225dfb00",
            "Code": "ZX-001-300",
            "Name": "300μL Tip头"
        }
    })
    source_plate.load_state({
        "Material": {
            "uuid": "57b1e4711e9e4a32b529f3132fc5931f",
            "Code": "ZX-019-2.2",
            "Name": "96深孔板"
        }
    })
    dest_plate.load_state({
        "Material": {
            "uuid": "57b1e4711e9e4a32b529f3132fc5931f",
            "Code": "ZX-019-2.2",
            "Name": "96深孔板"
        }
    })
    
    deck.assign_child_resource(tip_rack, location=Coordinate(0, 0, 0))
    deck.assign_child_resource(source_plate, location=Coordinate(50, 0, 0))
    deck.assign_child_resource(dest_plate, location=Coordinate(100, 0, 0))
    
    # 创建 handler，使用默认的 step_mode=False
    handler = PRCXI9300Handler(
        deck=deck,
        host="192.168.1.201",
        port=9999,
        timeout=10.0,
        step_mode=False,  # 正常模式（默认）
        is_9320=True,
        setup=True
    )
    
    await handler.setup()
    handler.set_tiprack([tip_rack])
    
    # 创建协议
    await handler.create_protocol("批量转移示例")
    
    # 在正常模式下，所有操作先添加到协议中
    print("=== 正常模式示例 ===")
    print("添加操作到协议中...")
    
    # 批量转移：从 A1-A8 转移到 B1-B8
    for i in range(8):
        well_name = f"{chr(65+i)}1"  # A1, B1, C1, ...
        await handler.pick_up_tips([tip_rack.get_item(well_name)], [0])
        await handler.aspirate([source_plate.get_item(well_name)], [100], [0])
        await handler.dispense([dest_plate.get_item(well_name)], [100], [0])
        await handler.discard_tips([0])
    
    print("执行协议...")
    # 最后统一执行所有操作
    await handler.run_protocol()
    
    print("正常模式示例完成")


async def example_mixed_usage():
    """示例3: 混合使用 - 部分操作使用 step_mode，部分使用正常模式
    
    混合使用示例 - 演示如何在不同场景下切换模式
    """
    print("=== 混合使用示例 ===")
    print("注意：step_mode 是在创建 handler 时设置的，不能动态切换")
    print("如果需要混合使用，需要创建两个 handler 实例，或者")
    print("在代码中根据需求选择使用哪个 handler")


# ============================================================================
# 布局推荐示例
# ============================================================================

def example_layout_recommendation():
    """示例：使用 DefaultLayout 推荐布局"""
    with open("prcxi_material.json", "r") as f:
        material_info = json.load(f)

    layout = DefaultLayout("PRCXI9320")
    layout.add_lab_resource(material_info)
    
    MatrixLayout_1, dict_1 = layout.recommend_layout(
        [
            ("reagent_1", "96 细胞培养皿", 3),
            ("reagent_2", "12道储液槽", 1),
            ("reagent_3", "200μL Tip头", 7),
            ("reagent_4", "10μL加长 Tip头", 1),
        ]
    )
    print("推荐布局1:", dict_1)
    
    MatrixLayout_2, dict_2 = layout.recommend_layout(
        [
            ("reagent_1", "96深孔板", 4),
            ("reagent_2", "12道储液槽", 1),
            ("reagent_3", "200μL Tip头", 1),
            ("reagent_4", "10μL加长 Tip头", 1),
        ]
    )
    print("推荐布局2:", dict_2)
    
    return MatrixLayout_1, dict_1, MatrixLayout_2, dict_2


# ============================================================================
# 主入口
# ============================================================================

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="PRCXI 设备使用示例")
    parser.add_argument(
        "--export",
        action="store_true",
        help="导出布局和物料列表到JSON文件"
    )
    parser.add_argument(
        "--host",
        type=str,
        default="192.168.1.201",
        help="PRCXI设备IP地址"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=9999,
        help="PRCXI设备端口"
    )
    parser.add_argument(
        "--example",
        type=str,
        choices=["step_mode", "normal_mode", "mixed", "layout"],
        help="运行指定的示例"
    )
    
    args = parser.parse_args()
    
    if args.export:
        export_layout_and_materials(host=args.host, port=args.port)
    elif args.example == "step_mode":
        asyncio.run(example_step_mode())
    elif args.example == "normal_mode":
        asyncio.run(example_normal_mode())
    elif args.example == "mixed":
        asyncio.run(example_mixed_usage())
    elif args.example == "layout":
        example_layout_recommendation()
    else:
        print("请使用 --help 查看可用选项")
        print("\n示例用法：")
        print("  python -m unilabos.devices.liquid_handling.prcxi.prcxi_examples --export")
        print("  python -m unilabos.devices.liquid_handling.prcxi.prcxi_examples --example step_mode")
        print("  python -m unilabos.devices.liquid_handling.prcxi.prcxi_examples --example normal_mode")

