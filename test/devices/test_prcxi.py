import pytest
import json
import os
import asyncio
import collections
from typing import List, Dict, Any

from pylabrobot.resources import Coordinate
from pylabrobot.resources.opentrons.tip_racks import opentrons_96_tiprack_300ul, opentrons_96_tiprack_10ul
from pylabrobot.resources.opentrons.plates import corning_96_wellplate_360ul_flat, nest_96_wellplate_2ml_deep

from unilabos.devices.liquid_handling.prcxi.prcxi import (
    PRCXI9300Deck,
    PRCXI9300Container,
    PRCXI9300Trash,
    PRCXI9300Handler,
    PRCXI9300Backend,
    DefaultLayout,
    Material,
    WorkTablets,
    MatrixInfo
)


@pytest.fixture
def prcxi_materials() -> Dict[str, Any]:
    """加载 PRCXI 物料数据"""
    print("加载 PRCXI 物料数据...")
    material_path = os.path.join(os.path.dirname(__file__), "..", "..", "unilabos", "devices", "liquid_handling", "prcxi", "prcxi_material.json")
    with open(material_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    print(f"加载了 {len(data)} 条物料数据")
    return data


@pytest.fixture
def prcxi_9300_deck() -> PRCXI9300Deck:
    """创建 PRCXI 9300 工作台"""
    return PRCXI9300Deck(name="PRCXI_Deck_9300", size_x=100, size_y=100, size_z=100, model="9300")


@pytest.fixture
def prcxi_9320_deck() -> PRCXI9300Deck:
    """创建 PRCXI 9320 工作台"""
    return PRCXI9300Deck(name="PRCXI_Deck_9320", size_x=100, size_y=100, size_z=100, model="9320")


@pytest.fixture
def prcxi_9300_handler(prcxi_9300_deck) -> PRCXI9300Handler:
    """创建 PRCXI 9300 处理器（模拟模式）"""
    return PRCXI9300Handler(
        deck=prcxi_9300_deck,
        host="192.168.1.201",
        port=9999,
        timeout=10.0,
        channel_num=8,
        axis="Left",
        setup=False,
        debug=True,
        simulator=True,
        matrix_id="test-matrix-9300"
    )


@pytest.fixture
def prcxi_9320_handler(prcxi_9320_deck) -> PRCXI9300Handler:
    """创建 PRCXI 9320 处理器（模拟模式）"""
    return PRCXI9300Handler(
        deck=prcxi_9320_deck,
        host="192.168.1.201",
        port=9999,
        timeout=10.0,
        channel_num=1,
        axis="Right",
        setup=False,
        debug=True,
        simulator=True,
        matrix_id="test-matrix-9320",
        is_9320=True
    )


@pytest.fixture
def tip_rack_300ul(prcxi_materials) -> PRCXI9300Container:
    """创建 300μL 枪头盒"""
    tip_rack = PRCXI9300Container(
        name="tip_rack_300ul",
        size_x=50,
        size_y=50,
        size_z=10,
        category="tip_rack",
        ordering=collections.OrderedDict()
    )
    tip_rack.load_state({
        "Material": {
            "uuid": prcxi_materials["300μL Tip头"]["uuid"],
            "Code": "ZX-001-300",
            "Name": "300μL Tip头"
        }
    })
    return tip_rack


@pytest.fixture
def tip_rack_10ul(prcxi_materials) -> PRCXI9300Container:
    """创建 10μL 枪头盒"""
    tip_rack = PRCXI9300Container(
        name="tip_rack_10ul",
        size_x=50,
        size_y=50,
        size_z=10,
        category="tip_rack",
        ordering=collections.OrderedDict()
    )
    tip_rack.load_state({
        "Material": {
            "uuid": prcxi_materials["10μL加长 Tip头"]["uuid"],
            "Code": "ZX-001-10+",
            "Name": "10μL加长 Tip头"
        }
    })
    return tip_rack


@pytest.fixture
def well_plate_96(prcxi_materials) -> PRCXI9300Container:
    """创建 96 孔板"""
    plate = PRCXI9300Container(
        name="well_plate_96",
        size_x=50,
        size_y=50,
        size_z=10,
        category="plate",
        ordering=collections.OrderedDict()
    )
    plate.load_state({
        "Material": {
            "uuid": prcxi_materials["96深孔板"]["uuid"],
            "Code": "ZX-019-2.2",
            "Name": "96深孔板"
        }
    })
    return plate


@pytest.fixture
def deep_well_plate(prcxi_materials) -> PRCXI9300Container:
    """创建深孔板"""
    plate = PRCXI9300Container(
        name="deep_well_plate",
        size_x=50,
        size_y=50,
        size_z=10,
        category="plate",
        ordering=collections.OrderedDict()
    )
    plate.load_state({
        "Material": {
            "uuid": prcxi_materials["96深孔板"]["uuid"],
            "Code": "ZX-019-2.2",
            "Name": "96深孔板"
        }
    })
    return plate


@pytest.fixture
def trash_container(prcxi_materials) -> PRCXI9300Trash:
    """创建垃圾桶"""
    trash = PRCXI9300Trash(name="trash", size_x=50, size_y=50, size_z=10, category="trash")
    trash.load_state({
        "Material": {
            "uuid": prcxi_materials["废弃槽"]["uuid"]
        }
    })
    return trash


@pytest.fixture
def default_layout_9300() -> DefaultLayout:
    """创建 PRCXI 9300 默认布局"""
    return DefaultLayout("PRCXI9300")


@pytest.fixture
def default_layout_9320() -> DefaultLayout:
    """创建 PRCXI 9320 默认布局"""
    return DefaultLayout("PRCXI9320")


class TestPRCXIDeckSetup:
    """测试 PRCXI 工作台设置功能"""

    def test_prcxi_9300_deck_creation(self, prcxi_9300_deck):
        """测试 PRCXI 9300 工作台创建"""
        assert prcxi_9300_deck.name == "PRCXI_Deck_9300"
        assert len(prcxi_9300_deck.sites) == 6
        assert prcxi_9300_deck._size_x == 100
        assert prcxi_9300_deck._size_y == 100
        assert prcxi_9300_deck._size_z == 100

    def test_prcxi_9320_deck_creation(self, prcxi_9320_deck):
        """测试 PRCXI 9320 工作台创建"""
        assert prcxi_9320_deck.name == "PRCXI_Deck_9320"
        assert len(prcxi_9320_deck.sites) == 16
        assert prcxi_9320_deck._size_x == 100
        assert prcxi_9320_deck._size_y == 100
        assert prcxi_9320_deck._size_z == 100

    def test_container_assignment(self, prcxi_9300_deck, tip_rack_300ul, well_plate_96, trash_container):
        """测试容器分配到工作台"""
        # 分配枪头盒
        prcxi_9300_deck.assign_child_resource(tip_rack_300ul, location=Coordinate(0, 0, 0))
        assert tip_rack_300ul in prcxi_9300_deck.children
        
        # 分配孔板
        prcxi_9300_deck.assign_child_resource(well_plate_96, location=Coordinate(0, 0, 0))
        assert well_plate_96 in prcxi_9300_deck.children
        
        # 分配垃圾桶
        prcxi_9300_deck.assign_child_resource(trash_container, location=Coordinate(0, 0, 0))
        assert trash_container in prcxi_9300_deck.children

    def test_container_material_loading(self, tip_rack_300ul, well_plate_96, prcxi_materials):
        """测试容器物料信息加载"""
        # 测试枪头盒物料信息
        tip_material = tip_rack_300ul._unilabos_state["Material"]
        assert tip_material["uuid"] == prcxi_materials["300μL Tip头"]["uuid"]
        assert tip_material["Name"] == "300μL Tip头"
        
        # 测试孔板物料信息
        plate_material = well_plate_96._unilabos_state["Material"]
        assert plate_material["uuid"] == prcxi_materials["96深孔板"]["uuid"]
        assert plate_material["Name"] == "96深孔板"


class TestPRCXISingleStepOperations:
    """测试 PRCXI 单步操作功能"""

    @pytest.mark.asyncio
    async def test_pick_up_tips_single_channel(self, prcxi_9320_handler, prcxi_9320_deck, tip_rack_10ul):
        """测试单通道拾取枪头"""
        # 将枪头盒添加到工作台
        prcxi_9320_deck.assign_child_resource(tip_rack_10ul, location=Coordinate(0, 0, 0))
        
        # 初始化处理器
        await prcxi_9320_handler.setup()
        
        # 设置枪头盒
        prcxi_9320_handler.set_tiprack([tip_rack_10ul])
        
        # 创建模拟的枪头位置
        from pylabrobot.resources import TipSpot, Tip
        tip = Tip(has_filter=False, total_tip_length=10, maximal_volume=10, fitting_depth=5)
        tip_spot = TipSpot("A1", size_x=1, size_y=1, size_z=1, make_tip=lambda: tip)
        tip_rack_10ul.assign_child_resource(tip_spot, location=Coordinate(0, 0, 0))
        
        # 直接测试后端方法
        from pylabrobot.liquid_handling import Pickup
        pickup = Pickup(resource=tip_spot, offset=None, tip=tip)
        await prcxi_9320_handler._unilabos_backend.pick_up_tips([pickup], [0])
        
        # 验证步骤已添加到待办列表
        assert len(prcxi_9320_handler._unilabos_backend.steps_todo_list) == 1
        step = prcxi_9320_handler._unilabos_backend.steps_todo_list[0]
        assert step["Function"] == "Load"

    @pytest.mark.asyncio
    async def test_pick_up_tips_multi_channel(self, prcxi_9300_handler, tip_rack_300ul):
        """测试多通道拾取枪头"""
        # 设置枪头盒
        prcxi_9300_handler.set_tiprack([tip_rack_300ul])
        
        # 拾取8个枪头
        tip_spots = tip_rack_300ul.children[:8]
        await prcxi_9300_handler.pick_up_tips(tip_spots, [0, 1, 2, 3, 4, 5, 6, 7])
        
        # 验证步骤已添加到待办列表
        assert len(prcxi_9300_handler._unilabos_backend.steps_todo_list) == 1
        step = prcxi_9300_handler._unilabos_backend.steps_todo_list[0]
        assert step["Function"] == "Load"

    @pytest.mark.asyncio
    async def test_aspirate_single_channel(self, prcxi_9320_handler, well_plate_96):
        """测试单通道吸取液体"""
        # 设置液体
        well = well_plate_96.get_item("A1")
        prcxi_9320_handler.set_liquid([well], ["water"], [50])
        
        # 吸取液体
        await prcxi_9320_handler.aspirate([well], [50], [0])
        
        # 验证步骤已添加到待办列表
        assert len(prcxi_9320_handler._unilabos_backend.steps_todo_list) == 1
        step = prcxi_9320_handler._unilabos_backend.steps_todo_list[0]
        assert step["Function"] == "Imbibing"
        assert step["DosageNum"] == 50

    @pytest.mark.asyncio
    async def test_dispense_single_channel(self, prcxi_9320_handler, well_plate_96):
        """测试单通道分配液体"""
        # 分配液体
        well = well_plate_96.get_item("A1")
        await prcxi_9320_handler.dispense([well], [25], [0])
        
        # 验证步骤已添加到待办列表
        assert len(prcxi_9320_handler._unilabos_backend.steps_todo_list) == 1
        step = prcxi_9320_handler._unilabos_backend.steps_todo_list[0]
        assert step["Function"] == "Tapping"
        assert step["DosageNum"] == 25

    @pytest.mark.asyncio
    async def test_mix_single_channel(self, prcxi_9320_handler, well_plate_96):
        """测试单通道混合液体"""
        # 混合液体
        well = well_plate_96.get_item("A1")
        await prcxi_9320_handler.mix([well], mix_time=3, mix_vol=50)
        
        # 验证步骤已添加到待办列表
        assert len(prcxi_9320_handler._unilabos_backend.steps_todo_list) == 1
        step = prcxi_9320_handler._unilabos_backend.steps_todo_list[0]
        assert step["Function"] == "Blending"
        assert step["BlendingTimes"] == 3
        assert step["DosageNum"] == 50

    @pytest.mark.asyncio
    async def test_drop_tips_to_trash(self, prcxi_9320_handler, trash_container):
        """测试丢弃枪头到垃圾桶"""
        # 丢弃枪头
        await prcxi_9320_handler.drop_tips([trash_container], [0])
        
        # 验证步骤已添加到待办列表
        assert len(prcxi_9320_handler._unilabos_backend.steps_todo_list) == 1
        step = prcxi_9320_handler._unilabos_backend.steps_todo_list[0]
        assert step["Function"] == "UnLoad"

    @pytest.mark.asyncio
    async def test_discard_tips(self, prcxi_9320_handler):
        """测试丢弃枪头"""
        # 丢弃枪头
        await prcxi_9320_handler.discard_tips([0])
        
        # 验证步骤已添加到待办列表
        assert len(prcxi_9320_handler._unilabos_backend.steps_todo_list) == 1
        step = prcxi_9320_handler._unilabos_backend.steps_todo_list[0]
        assert step["Function"] == "UnLoad"

    @pytest.mark.asyncio
    async def test_liquid_transfer_workflow(self, prcxi_9320_handler, tip_rack_10ul, well_plate_96):
        """测试完整的液体转移工作流程"""
        # 设置枪头盒和液体
        prcxi_9320_handler.set_tiprack([tip_rack_10ul])
        source_well = well_plate_96.get_item("A1")
        target_well = well_plate_96.get_item("B1")
        prcxi_9320_handler.set_liquid([source_well], ["water"], [100])
        
        # 创建协议
        await prcxi_9320_handler.create_protocol(protocol_name="Test Transfer Protocol")
        
        # 执行转移流程
        tip_spot = tip_rack_10ul.get_item("A1")
        await prcxi_9320_handler.pick_up_tips([tip_spot], [0])
        await prcxi_9320_handler.aspirate([source_well], [50], [0])
        await prcxi_9320_handler.dispense([target_well], [50], [0])
        await prcxi_9320_handler.discard_tips([0])
        
        # 验证所有步骤都已添加
        assert len(prcxi_9320_handler._unilabos_backend.steps_todo_list) == 4
        functions = [step["Function"] for step in prcxi_9320_handler._unilabos_backend.steps_todo_list]
        assert functions == ["Load", "Imbibing", "Tapping", "UnLoad"]


class TestPRCXILayoutRecommendation:
    """测试 PRCXI 板位推荐功能"""

    def test_9300_layout_creation(self, default_layout_9300):
        """测试 PRCXI 9300 布局创建"""
        layout_info = default_layout_9300.get_layout()
        assert layout_info["rows"] == 2
        assert layout_info["columns"] == 3
        assert len(layout_info["layout"]) == 6
        assert layout_info["trash_slot"] == 6
        assert "waste_liquid_slot" not in layout_info

    def test_9320_layout_creation(self, default_layout_9320):
        """测试 PRCXI 9320 布局创建"""
        layout_info = default_layout_9320.get_layout()
        assert layout_info["rows"] == 4
        assert layout_info["columns"] == 4
        assert len(layout_info["layout"]) == 16
        assert layout_info["trash_slot"] == 16
        assert layout_info["waste_liquid_slot"] == 12

    def test_layout_recommendation_9320(self, default_layout_9320, prcxi_materials):
        """测试 PRCXI 9320 板位推荐功能"""
        # 添加物料信息
        default_layout_9320.add_lab_resource(prcxi_materials)
        
        # 推荐布局
        needs = [
            ("reagent_1", "96 细胞培养皿", 3),
            ("reagent_2", "12道储液槽", 1),
            ("reagent_3", "200μL Tip头", 7),
            ("reagent_4", "10μL加长 Tip头", 1),
        ]
        
        matrix_layout, layout_list = default_layout_9320.recommend_layout(needs)
        
        # 验证返回结果
        assert "MatrixId" in matrix_layout
        assert "MatrixName" in matrix_layout
        assert "MatrixCount" in matrix_layout
        assert "WorkTablets" in matrix_layout
        assert len(layout_list) == 12  # 3+1+7+1 = 12个位置
        
        # 验证推荐的位置不包含预留位置
        reserved_positions = {12, 16}
        recommended_positions = [item["positions"] for item in layout_list]
        for pos in recommended_positions:
            assert pos not in reserved_positions

    def test_layout_recommendation_insufficient_space(self, default_layout_9320, prcxi_materials):
        """测试板位推荐空间不足的情况"""
        # 添加物料信息
        default_layout_9320.add_lab_resource(prcxi_materials)
        
        # 尝试推荐超过可用空间的布局
        needs = [
            ("reagent_1", "96 细胞培养皿", 15),  # 需要15个位置，但只有14个可用
        ]
        
        with pytest.raises(ValueError, match="需要 .* 个位置，但只有 .* 个可用位置"):
            default_layout_9320.recommend_layout(needs)

    def test_layout_recommendation_material_not_found(self, default_layout_9320, prcxi_materials):
        """测试板位推荐物料不存在的情况"""
        # 添加物料信息
        default_layout_9320.add_lab_resource(prcxi_materials)
        
        # 尝试推荐不存在的物料
        needs = [
            ("reagent_1", "不存在的物料", 1),
        ]
        
        with pytest.raises(ValueError, match="Material .* not found in lab resources"):
            default_layout_9320.recommend_layout(needs)


class TestPRCXIBackendOperations:
    """测试 PRCXI 后端操作功能"""

    def test_backend_initialization(self, prcxi_9300_handler):
        """测试后端初始化"""
        backend = prcxi_9300_handler._unilabos_backend
        assert isinstance(backend, PRCXI9300Backend)
        assert backend._num_channels == 8
        assert backend.debug is True

    def test_protocol_creation(self, prcxi_9300_handler):
        """测试协议创建"""
        backend = prcxi_9300_handler._unilabos_backend
        backend.create_protocol("Test Protocol")
        assert backend.protocol_name == "Test Protocol"
        assert len(backend.steps_todo_list) == 0

    def test_channel_validation(self):
        """测试通道验证"""
        # 测试正确的8通道配置
        valid_channels = [0, 1, 2, 3, 4, 5, 6, 7]
        result = PRCXI9300Backend.check_channels(valid_channels)
        assert result == valid_channels
        
        # 测试错误的通道配置
        invalid_channels = [0, 1, 2, 3]
        result = PRCXI9300Backend.check_channels(invalid_channels)
        assert result == [0, 1, 2, 3, 4, 5, 6, 7]

    def test_matrix_info_creation(self, prcxi_9300_handler):
        """测试矩阵信息创建"""
        backend = prcxi_9300_handler._unilabos_backend
        backend.create_protocol("Test Protocol")
        
        # 模拟运行协议时的矩阵信息创建
        run_time = 1234567890
        matrix_info = MatrixInfo(
            MatrixId=f"{int(run_time)}",
            MatrixName=f"protocol_{run_time}",
            MatrixCount=len(backend.tablets_info),
            WorkTablets=backend.tablets_info,
        )
        
        assert matrix_info["MatrixId"] == str(int(run_time))
        assert matrix_info["MatrixName"] == f"protocol_{run_time}"
        assert "WorkTablets" in matrix_info


class TestPRCXIContainerOperations:
    """测试 PRCXI 容器操作功能"""

    def test_container_serialization(self, tip_rack_300ul):
        """测试容器序列化"""
        serialized = tip_rack_300ul.serialize_state()
        assert "Material" in serialized
        assert serialized["Material"]["Name"] == "300μL Tip头"

    def test_container_deserialization(self, tip_rack_300ul):
        """测试容器反序列化"""
        # 序列化
        serialized = tip_rack_300ul.serialize_state()
        
        # 创建新容器并反序列化
        new_tip_rack = PRCXI9300Container(
            name="new_tip_rack",
            size_x=50,
            size_y=50,
            size_z=10,
            category="tip_rack",
            ordering=collections.OrderedDict()
        )
        new_tip_rack.load_state(serialized)
        
        assert new_tip_rack._unilabos_state["Material"]["Name"] == "300μL Tip头"

    def test_trash_container_creation(self, prcxi_materials):
        """测试垃圾桶容器创建"""
        trash = PRCXI9300Trash(name="trash", size_x=50, size_y=50, size_z=10, category="trash")
        trash.load_state({
            "Material": {
                "uuid": prcxi_materials["废弃槽"]["uuid"]
            }
        })
        
        assert trash.name == "trash"
        assert trash._unilabos_state["Material"]["uuid"] == prcxi_materials["废弃槽"]["uuid"]


if __name__ == "__main__":
    # 运行测试
    pytest.main([__file__, "-v"])