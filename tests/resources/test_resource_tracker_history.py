"""P9 — ``_augment_states_with_liquid_history`` 单元测试（OS→Cloud sync 链路 Phase C）。

详见 ``product_designs/protocol_convert/09-liquid-history-unknown-debug.md`` §6.3 / §8 T4。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

import pytest

from unilabos.resources.resource_tracker import _augment_states_with_liquid_history


# ---------------------------------------------------------------------------
# Fixtures：纯 dataclass 模拟 PLR 资源树（避免引入 PLR 真实实例化）
# ---------------------------------------------------------------------------


@dataclass
class FakeTracker:
    liquid_history: Any = field(default_factory=list)


@dataclass
class FakeResource:
    name: str
    tracker: Any = None
    children: List["FakeResource"] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAugmentStatesWithLiquidHistory:
    def test_single_well_history_attached(self) -> None:
        well = FakeResource("well_A1", tracker=FakeTracker(liquid_history=[
            {"name": "Plasma", "volume": 100, "action": "set"}
        ]))
        states: Dict[str, Any] = {"well_A1": {"liquids": [], "pending_liquids": []}}

        _augment_states_with_liquid_history(well, states)

        assert "liquid_history" in states["well_A1"]
        assert states["well_A1"]["liquid_history"] == [
            {"name": "Plasma", "volume": 100, "action": "set"}
        ]

    def test_recursive_walk_attaches_to_all_wells(self) -> None:
        """resource 树有多层时，每个有 tracker 的节点都会被并入 states。"""
        wells = [
            FakeResource(f"well_{i}", tracker=FakeTracker(liquid_history=[
                {"name": f"L_{i}", "volume": i * 10, "action": "set"}
            ]))
            for i in range(3)
        ]
        plate = FakeResource("plate", children=wells)
        deck = FakeResource("deck", children=[plate])
        states: Dict[str, Any] = {
            "deck": {"liquids": []},
            "plate": {"liquids": []},
            "well_0": {"liquids": []},
            "well_1": {"liquids": []},
            "well_2": {"liquids": []},
        }

        _augment_states_with_liquid_history(deck, states)

        assert states["well_0"]["liquid_history"] == [{"name": "L_0", "volume": 0, "action": "set"}]
        assert states["well_1"]["liquid_history"] == [{"name": "L_1", "volume": 10, "action": "set"}]
        assert states["well_2"]["liquid_history"] == [{"name": "L_2", "volume": 20, "action": "set"}]

    def test_no_tracker_node_skipped(self) -> None:
        """没有 tracker 的节点（如 deck 自身）跳过，state dict 不被污染。"""
        deck = FakeResource("deck")  # tracker=None
        states: Dict[str, Any] = {"deck": {"some_field": 1}}

        _augment_states_with_liquid_history(deck, states)

        assert "liquid_history" not in states["deck"]

    def test_existing_liquid_history_in_state_not_overwritten(self) -> None:
        """state 已经有 liquid_history 字段（例如 PLR 升级未来支持了）→ 不覆盖。"""
        well = FakeResource("well_A1", tracker=FakeTracker(liquid_history=[
            {"name": "Plasma", "volume": 100, "action": "set"}
        ]))
        states: Dict[str, Any] = {"well_A1": {"liquid_history": ["preexisting"]}}

        _augment_states_with_liquid_history(well, states)

        assert states["well_A1"]["liquid_history"] == ["preexisting"]

    def test_history_is_shallow_copied(self) -> None:
        """augment 后的 history 应是独立 list（避免运行时 mutate 污染 dump 结果）。"""
        original_history = [{"name": "X", "volume": 1, "action": "set"}]
        well = FakeResource("well_A1", tracker=FakeTracker(liquid_history=original_history))
        states: Dict[str, Any] = {"well_A1": {}}

        _augment_states_with_liquid_history(well, states)

        # mutate runtime history 不应反映到 augmented state
        original_history.append({"name": "Y", "volume": 2, "action": "set"})
        assert len(states["well_A1"]["liquid_history"]) == 1

    def test_node_not_in_states_silently_skipped(self) -> None:
        """resource 树中的节点 name 不在 ``states`` 字典里 → 静默跳过。"""
        well = FakeResource("well_orphan", tracker=FakeTracker(liquid_history=[
            {"name": "X", "volume": 1, "action": "set"}
        ]))
        states: Dict[str, Any] = {"well_A1": {}}

        _augment_states_with_liquid_history(well, states)

        # 不应该新增 well_orphan 键，也不应污染 well_A1
        assert "well_orphan" not in states
        assert "liquid_history" not in states["well_A1"]

    def test_non_list_liquid_history_skipped(self) -> None:
        """tracker.liquid_history 非 list 时（异常情况）→ 跳过，不写入 state。"""
        well = FakeResource("well_A1", tracker=FakeTracker(liquid_history="broken"))
        states: Dict[str, Any] = {"well_A1": {}}

        _augment_states_with_liquid_history(well, states)

        assert "liquid_history" not in states["well_A1"]

    def test_empty_history_still_written(self) -> None:
        """tracker.liquid_history = [] 是合法状态 → 应写入空 list（表示"未有任何液体操作"）。"""
        well = FakeResource("well_A1", tracker=FakeTracker(liquid_history=[]))
        states: Dict[str, Any] = {"well_A1": {}}

        _augment_states_with_liquid_history(well, states)

        assert states["well_A1"]["liquid_history"] == []
