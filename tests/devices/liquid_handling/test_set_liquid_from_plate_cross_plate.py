"""P2 v2 跨板能力验证 —— device 层 ``set_liquid_from_plate`` 单测。

对应 ``product_designs/protocol_convert/02-cross-slot-merge.md`` §9.1 / §9.5 step 6.3。

本测试聚焦于 **`_set_liquid_grouped_by_plate`** 已天然支持跨板 wells 的能力（v2 设计
的核心依据）：

- 输入 ``wells`` 列表来自多个 plate（每板各一/多个 well）时，``set_liquid`` 应按 plate
  分桶串行调用，每板一次（plate-bucket 顺序按 first-occurrence）。
- 同板内多孔归到同一桶。
- 返回 ``volumes`` 按 **输入 index 顺序**回拼，与 wells 一致 —— 这是 v2 Stage 3
  merged ``set_liquid_from_plate.output_wells`` 的顺序权威来源。
- ``Well.set_liquids`` 在 ``set_liquid`` 链内被逐孔调用，与 PLR 实现的预期接口一致。

为了避免引入完整 PLR 资源树，测试用 duck-typed ``DummyWell`` / ``DummyPlate`` +
``ResourceTreeSet`` 的 monkeypatch（dump 直接返回输入列表）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Tuple

import pytest


# ----------------------------------------------------------------------
# 跨环境兼容：与现有 ``tests/devices/liquid_handling/test_transfer_liquid.py`` 一致，
# 本测试通过 import ``unilabos.devices.liquid_handling.liquid_handler_abstract``
# 拉起 pylabrobot 链；某些本地开发机的 pylabrobot 版本与代码库要求不一致，
# 会在 import 阶段抛 ``ImportError``。这里用 ``importorskip`` 优雅跳过，让
# CI（统一 pylabrobot 版本）跑全；纯逻辑测试（Stage 2 / Stage 3）不受影响。
# ----------------------------------------------------------------------
LiquidHandlerAbstract = pytest.importorskip(
    "unilabos.devices.liquid_handling.liquid_handler_abstract",
    reason="pylabrobot 链未完整可用，跳过 device 单测；CI 上请保证 pylabrobot ≥ 项目要求版本",
    exc_type=ImportError,
).LiquidHandlerAbstract


# ==================== Duck-typed PLR-like 资源 ====================


@dataclass
class DummyPlate:
    name: str

    def __repr__(self) -> str:  # pragma: no cover
        return f"DummyPlate({self.name})"


@dataclass
class DummyWell:
    name: str
    parent: DummyPlate
    max_volume: float = 1000.0
    liquid_history: List[Tuple[str, float]] = field(default_factory=list)

    def set_liquids(self, items):
        """模拟 PLR ``Well.set_liquids([(name, vol), ...])`` 接口。"""
        for name, vol in items:
            self.liquid_history.append((str(name), float(vol)))

    def __repr__(self) -> str:  # pragma: no cover
        return f"DummyWell({self.parent.name}/{self.name})"


# ==================== fixture：装一台 FakeLiquidHandler ====================


@pytest.fixture
def patched_resource_tree(monkeypatch):
    """patch ``ResourceTreeSet.from_plr_resources`` 使其接受 duck-typed wells/plates。

    返回的对象只要带 ``.dump()`` 即可（``_set_liquid_grouped_by_plate`` 仅消费该方法）。
    """
    from unilabos.devices.liquid_handling import liquid_handler_abstract as lha

    class _FakeTree:
        def __init__(self, items):
            self._items = items

        def dump(self):
            return [
                {"name": getattr(x, "name", None), "type": type(x).__name__}
                for x in self._items
            ]

    def _fake_from_plr_resources(items, known_newly_created=False):  # noqa: ARG001
        return _FakeTree(list(items))

    monkeypatch.setattr(
        lha.ResourceTreeSet,
        "from_plr_resources",
        staticmethod(_fake_from_plr_resources),
    )
    return lha


@pytest.fixture
def handler(patched_resource_tree):
    """构造一台最小 LiquidHandlerAbstract 实例，绕过真实 backend / deck。"""

    class _FakeHandler(LiquidHandlerAbstract):
        def __init__(self):
            # 不调用 super().__init__，避免真实硬件/后端依赖
            self.channel_num = 8
            self.support_touch_tip = True

    return _FakeHandler()


def _wells_grid(plate_name: str, well_names: List[str]) -> List[DummyWell]:
    plate = DummyPlate(name=plate_name)
    return [DummyWell(name=w, parent=plate) for w in well_names]


# ==================== 用例 ====================


def test_grouped_by_plate_single_plate_set_liquid_inline(handler):
    """单 plate 多孔：set_liquids 按 wells 顺序逐项调用，volumes 回拼一致。"""
    wells = _wells_grid("plate_slot2", ["A1", "A2", "A3"])
    ret = handler._set_liquid_grouped_by_plate(
        wells=wells,
        liquid_names=["reagent_X"] * 3,
        volumes=[10.0, 20.0, 30.0],
    )

    # 每个 well 的 liquid_history 各 1 条
    for w, expected_vol in zip(wells, [10.0, 20.0, 30.0]):
        assert w.liquid_history == [("reagent_X", expected_vol)]

    # 返回 volumes 顺序与输入一致
    assert ret.volumes == [10.0, 20.0, 30.0]


def test_grouped_by_plate_cross_plate_buckets_by_parent(handler):
    """跨板 wells 列表 → 按 first-occurrence plate 顺序分桶，每板单独 set_liquid。

    51b9a5 简化（每板 1 孔）：4 plate × 1 well = 4 set_liquids 调用。
    """
    p2 = _wells_grid("plate_slot2", ["A1"])
    p3 = _wells_grid("plate_slot3", ["A1"])
    p5 = _wells_grid("plate_slot5", ["A1"])
    p6 = _wells_grid("plate_slot6", ["A1"])
    wells = p2 + p3 + p5 + p6

    ret = handler._set_liquid_grouped_by_plate(
        wells=wells,
        liquid_names=["l1"] * 4,
        volumes=[8.3] * 4,
    )

    # 每个 well 都被 set_liquids 设过
    for w in wells:
        assert w.liquid_history == [("l1", 8.3)], f"well {w.parent.name}/{w.name} 未正确设液"

    # volumes 顺序与输入对齐
    assert ret.volumes == [8.3, 8.3, 8.3, 8.3]

    # plate dump 应含 4 个 plate（按 first-occurrence）
    plate_dump = ret.plate
    plate_names = [p["name"] for p in plate_dump]
    assert plate_names == ["plate_slot2", "plate_slot3", "plate_slot5", "plate_slot6"]


def test_grouped_by_plate_interleaved_cross_plate_preserves_input_order(handler):
    """交错跨板：wells=[p2.A1, p3.A1, p2.A2, p5.A1] → volumes 顺序按输入回拼。

    内部仍按 plate 分桶执行 set_liquid（per-plate 串行），但返回顺序遵循输入 index。
    """
    p2 = DummyPlate(name="plate_slot2")
    p3 = DummyPlate(name="plate_slot3")
    p5 = DummyPlate(name="plate_slot5")
    w_p2_a1 = DummyWell(name="A1", parent=p2)
    w_p2_a2 = DummyWell(name="A2", parent=p2)
    w_p3_a1 = DummyWell(name="A1", parent=p3)
    w_p5_a1 = DummyWell(name="A1", parent=p5)

    wells = [w_p2_a1, w_p3_a1, w_p2_a2, w_p5_a1]
    ret = handler._set_liquid_grouped_by_plate(
        wells=wells,
        liquid_names=["l1"] * 4,
        volumes=[10.0, 20.0, 30.0, 40.0],
    )

    # 每个 well 都被设液
    assert w_p2_a1.liquid_history == [("l1", 10.0)]
    assert w_p3_a1.liquid_history == [("l1", 20.0)]
    assert w_p2_a2.liquid_history == [("l1", 30.0)]
    assert w_p5_a1.liquid_history == [("l1", 40.0)]

    # 返回 volumes 严格按输入 index 顺序回拼
    assert ret.volumes == [10.0, 20.0, 30.0, 40.0]

    # plate dump：按 first-occurrence（plate_slot2 第 1 次出现于 idx=0，plate_slot3 idx=1，plate_slot5 idx=3）
    plate_names = [p["name"] for p in ret.plate]
    assert plate_names == ["plate_slot2", "plate_slot3", "plate_slot5"]


def test_grouped_by_plate_volumes_clamped_to_max_volume(handler):
    """``set_liquid`` 会按 ``max_volume`` 做 clamp，防止初始化液量超容器容量。"""
    plate = DummyPlate(name="plate_slot2")
    well = DummyWell(name="A1", parent=plate, max_volume=200.0)

    ret = handler._set_liquid_grouped_by_plate(
        wells=[well],
        liquid_names=["overflow"],
        volumes=[500.0],          # 超过 max_volume=200
    )

    assert well.liquid_history == [("overflow", 200.0)]
    assert ret.volumes == [200.0]


def test_grouped_by_plate_empty_names_short_circuit(handler):
    """``liquid_names`` 与 ``volumes`` 均为空：早返回，wells 列表回显但不设液。"""
    wells = _wells_grid("plate_slot2", ["A1", "A2"])
    ret = handler._set_liquid_grouped_by_plate(
        wells=wells,
        liquid_names=[],
        volumes=[],
    )
    # 不调用 set_liquids
    assert all(w.liquid_history == [] for w in wells)
    assert ret.volumes == []
    # wells dump 仍返回输入列表
    assert [w["name"] for w in ret.wells] == ["A1", "A2"]


def test_grouped_by_plate_length_mismatch_raises(handler):
    """wells / liquid_names / volumes 长度不一致应直接 raise（防御性校验）。"""
    wells = _wells_grid("plate_slot2", ["A1", "A2"])
    with pytest.raises(ValueError, match=r"必须等长"):
        handler._set_liquid_grouped_by_plate(
            wells=wells,
            liquid_names=["r"] * 2,
            volumes=[10.0],          # 长度 1，不匹配
        )
