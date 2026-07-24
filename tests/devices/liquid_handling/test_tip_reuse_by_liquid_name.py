"""P10 v2 — Tip 复用 ``tracker.liquids`` 等价规则单元测试。

测试覆盖（详见 ``product_designs/protocol_convert/10-tip-reuse-by-liquid-history.md`` §5）：

    - Helper：``is_known_liquid_name`` / ``same_liquid_via_liquids`` /
      ``same_liquid_via_liquids_pair`` / ``capture_tip_liquid_name``（4 helper
      位于 ``liquid_history.py``，PLR-free 模块）。
    - 单通道 transfer_liquid 主循环：identity-keep / liquids-keep / 配置开关 /
      未知 name 保守换 tip / aspirate 顶层归零时序。
    - 8 通道分支：段锚孔 liquids-keep。
    - 跨节点边界：两个独立 transfer_liquid 调用状态隔离。

helper 测试独立于 PLR，可在 ``pylabrobot`` 缺失环境下单独运行；端到端
``transfer_liquid`` 主循环测试需要 PLR 环境（沿用 ``test_transfer_liquid.py`` 的
``FakeLiquidHandler`` 模式：跳过 ``super().__init__``，仅 stub 4 类方法记录调用）。
若 PLR import 失败则自动 skip 端到端测试，保留 helper 测试结果。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Iterable, List, Optional, Sequence, Tuple

import pytest

# P10 v2 helper 位于 PLR-free 模块，无论 pylabrobot 是否安装都能 import。
from unilabos.devices.liquid_handling.liquid_history import (
    capture_tip_liquid_name,
    is_known_liquid_name,
    same_liquid_via_liquids,
    same_liquid_via_liquids_pair,
)

# 端到端测试依赖 PLR 完整环境；若 import 失败（例如本地 PLR 版本不匹配），
# 整段端到端测试自动 skip，但 helper 测试照常执行。
try:
    from unilabos.devices.liquid_handling.liquid_handler_abstract import (
        LiquidHandlerAbstract,
    )

    _PLR_AVAILABLE = True
    _PLR_IMPORT_ERROR: Optional[Exception] = None
except Exception as exc:  # pragma: no cover - 环境相关
    LiquidHandlerAbstract = None  # type: ignore[assignment, misc]
    _PLR_AVAILABLE = False
    _PLR_IMPORT_ERROR = exc


# ---------------------------------------------------------------------------
# Fixtures：DummyTracker / DummyWell / DummyTipSpot / FakeLiquidHandler
# ---------------------------------------------------------------------------


@dataclass
class DummyTracker:
    """模拟 PLR ``VolumeTracker``：仅暴露 P10 v2 关心的 ``liquids`` 字段。"""

    liquids: List[Tuple[Any, float]] = field(default_factory=list)
    max_volume: float = 200.0
    is_disabled: bool = False


@dataclass
class DummyWell:
    """模拟 PLR ``Well``：仅暴露 ``tracker``。"""

    name: str = "well"
    tracker: DummyTracker = field(default_factory=DummyTracker)

    def __repr__(self) -> str:  # pragma: no cover
        return f"DummyWell({self.name})"


def make_well(name: str, liquid_name: Optional[str] = None, vol: float = 100.0) -> DummyWell:
    """构造一个 well；若指定 ``liquid_name`` 则写入 ``tracker.liquids`` 顶层。"""
    well = DummyWell(name=name, tracker=DummyTracker())
    if liquid_name is not None:
        well.tracker.liquids = [(liquid_name, vol)]
    return well


@dataclass(frozen=True)
class DummyTipSpot:
    name: str


def make_tip_iter(n: int = 256) -> Iterable[List[DummyTipSpot]]:
    for i in range(n):
        yield [DummyTipSpot(f"tip_{i}")]


# E2E 测试用的 base：PLR 可用时是 ``LiquidHandlerAbstract``，否则 fallback 到
# ``object`` 让模块仍能 import；带 ``LiquidHandlerAbstract`` 的 e2e 测试用
# ``skipif`` 跳过。
_FakeBase = LiquidHandlerAbstract if _PLR_AVAILABLE else object


class FakeLiquidHandler(_FakeBase):  # type: ignore[misc, valid-type]
    """不初始化真实 backend/deck；仅记录 transfer_liquid 内部 4 类调用序列。

    P10 v2 测试关心 ``pick_up_tips`` / ``discard_tips`` 的触发次数 + 顺序，
    以推断 tip 是否被复用（一次 pick_up_tips 多次 aspirate/dispense → 复用）。
    """

    def __init__(self, channel_num: int = 1, tip_reuse_by_liquid_name: bool = True):
        # 不调用 super().__init__，避免硬件 / ROS / PLR Deck 初始化。
        self.channel_num = channel_num
        self.support_touch_tip = True
        self.current_tip = iter(make_tip_iter(2048))
        self.calls: List[Tuple[str, Any]] = []
        self._tip_reuse_by_liquid_name: bool = tip_reuse_by_liquid_name

    def set_tiprack(self, tip_racks):
        if not tip_racks:
            return
        # 跳过真实 set_tiprack（依赖 PLR Deck）
        return

    async def pick_up_tips(self, tip_spots, use_channels=None, offsets=None, **kw):
        self.calls.append(("pick_up_tips", {"tips": list(tip_spots), "use_channels": use_channels}))

    async def aspirate(
        self,
        resources: Sequence[Any],
        vols: List[float],
        use_channels: Optional[List[int]] = None,
        flow_rates: Optional[List[Optional[float]]] = None,
        offsets: Any = None,
        liquid_height: Any = None,
        blow_out_air_volume: Any = None,
        spread: str = "wide",
        **backend_kwargs,
    ):
        self.calls.append(
            ("aspirate", {"resources": list(resources), "vols": list(vols)})
        )

    async def dispense(
        self,
        resources: Sequence[Any],
        vols: List[float],
        use_channels: Optional[List[int]] = None,
        flow_rates: Optional[List[Optional[float]]] = None,
        offsets: Any = None,
        liquid_height: Any = None,
        blow_out_air_volume: Any = None,
        spread: str = "wide",
        **backend_kwargs,
    ):
        self.calls.append(
            ("dispense", {"resources": list(resources), "vols": list(vols)})
        )

    async def discard_tips(self, use_channels=None, *args, **kwargs):
        self.calls.append(("discard_tips", {"use_channels": use_channels}))


class AspiratePopFakeLiquidHandler(FakeLiquidHandler):
    """T11 专用：aspirate 时模拟 PLR "顶层归零时 pop ``tracker.liquids`` 顶层" 的行为。

    用于验证 P10 v2 的关键时序约束：tip name 必须在 aspirate **之前**预读，
    否则 aspirate 后再读 ``tracker.liquids[-1]`` 会拿不到液体身份。
    """

    async def aspirate(self, resources, vols, **kwargs):
        await super().aspirate(resources, vols, **kwargs)
        # 模拟 PLR 顶层归零时 pop：对每个 source well，若 liquids 非空则 pop 顶层
        for r in resources:
            tracker = getattr(r, "tracker", None)
            if tracker is not None and tracker.liquids:
                tracker.liquids.pop()


def run(coro):
    return asyncio.run(coro)


def call_names(lh: FakeLiquidHandler) -> List[str]:
    return [c[0] for c in lh.calls]


# ---------------------------------------------------------------------------
# Helper 单元测试
# ---------------------------------------------------------------------------


class TestIsKnownLiquidName:
    def test_empty_string_is_unknown(self) -> None:
        assert is_known_liquid_name("") is False

    def test_none_is_unknown(self) -> None:
        assert is_known_liquid_name(None) is False

    def test_literal_unknown_is_unknown(self) -> None:
        assert is_known_liquid_name("unknown") is False
        assert is_known_liquid_name("UNKNOWN") is False
        assert is_known_liquid_name("  Unknown  ") is False

    def test_literal_none_string_is_unknown(self) -> None:
        assert is_known_liquid_name("none") is False
        assert is_known_liquid_name("None") is False

    def test_real_liquid_name_is_known(self) -> None:
        assert is_known_liquid_name("PBS") is True
        assert is_known_liquid_name("Tris HCl") is True
        assert is_known_liquid_name("Liquid_3") is True


class TestSameLiquidViaLiquids:
    def test_well_and_tip_same_name_match(self) -> None:
        well = make_well("A1", "PBS")
        assert same_liquid_via_liquids(well, "PBS") is True

    def test_well_and_tip_different_names_no_match(self) -> None:
        well = make_well("A1", "PBS")
        assert same_liquid_via_liquids(well, "Tris HCl") is False

    def test_tip_unknown_returns_false(self) -> None:
        well = make_well("A1", "PBS")
        assert same_liquid_via_liquids(well, None) is False
        assert same_liquid_via_liquids(well, "") is False
        assert same_liquid_via_liquids(well, "unknown") is False

    def test_well_empty_liquids_returns_false(self) -> None:
        well = make_well("A1", liquid_name=None)  # 不写 liquids
        assert same_liquid_via_liquids(well, "PBS") is False

    def test_well_unknown_literal_returns_false(self) -> None:
        well = make_well("A1", "unknown")
        assert same_liquid_via_liquids(well, "unknown") is False


class TestSameLiquidViaLiquidsPair:
    def test_two_wells_same_name_match(self) -> None:
        a = make_well("A1", "PBS")
        b = make_well("B1", "PBS")
        assert same_liquid_via_liquids_pair(a, b) is True

    def test_two_wells_different_names_no_match(self) -> None:
        a = make_well("A1", "PBS")
        b = make_well("B1", "Tris HCl")
        assert same_liquid_via_liquids_pair(a, b) is False

    def test_either_well_empty_returns_false(self) -> None:
        a = make_well("A1", "PBS")
        b = make_well("B1", liquid_name=None)
        assert same_liquid_via_liquids_pair(a, b) is False
        assert same_liquid_via_liquids_pair(b, a) is False


class TestCaptureTipLiquidName:
    def test_known_name_returned(self) -> None:
        well = make_well("A1", "PBS")
        assert capture_tip_liquid_name(well) == "PBS"

    def test_empty_well_returns_none(self) -> None:
        well = make_well("A1", liquid_name=None)
        assert capture_tip_liquid_name(well) is None

    def test_unknown_literal_returns_none(self) -> None:
        well = make_well("A1", "unknown")
        assert capture_tip_liquid_name(well) is None


# ---------------------------------------------------------------------------
# T1–T12 端到端测试（单通道 transfer_liquid 主循环）
#
# 需要 PLR 完整环境（``pylabrobot.liquid_handling.LiquidHandlerBackend`` 等）。
# 若 PLR import 失败则整段 skip，helper 测试照常运行。
# ---------------------------------------------------------------------------

_skip_if_no_plr = pytest.mark.skipif(
    not _PLR_AVAILABLE,
    reason=f"pylabrobot import failed: {_PLR_IMPORT_ERROR}",
)


@_skip_if_no_plr
class TestSingleChannelTipReuse:
    """覆盖 §5 矩阵 T1 / T2 / T3 / T4 / T5 / T6 / T8 / T10 / T11。"""

    def test_T1_identity_hit_reuses_tip(self) -> None:
        """T1：连续 2 轮同 source/target → identity-keep 命中，复用 tip。"""
        lh = FakeLiquidHandler(channel_num=1)
        src = make_well("S0", "PBS")
        tgt = make_well("T0")
        run(
            lh.transfer_liquid(
                sources=[src, src],
                targets=[tgt, tgt],
                tip_racks=[],
                use_channels=[0],
                asp_vols=[1, 1],
                dis_vols=[1, 1],
            )
        )
        # 2 次 transfer，但 identity-keep → 仅 1 次 pick_up_tips / 1 次 discard_tips
        assert call_names(lh).count("pick_up_tips") == 1
        assert call_names(lh).count("discard_tips") == 1
        assert call_names(lh).count("aspirate") == 2
        assert call_names(lh).count("dispense") == 2

    def test_T2_liquids_hit_across_plates(self) -> None:
        """T2：9 个独立 source well（不同 PLR Well 对象）都装 PBS → identity 全 fail，liquids-keep 全命中。"""
        lh = FakeLiquidHandler(channel_num=1)
        sources = [make_well(f"S{i}", "PBS") for i in range(9)]
        targets = [make_well(f"T{i}") for i in range(9)]
        run(
            lh.transfer_liquid(
                sources=sources,
                targets=targets,
                tip_racks=[],
                use_channels=[0],
                asp_vols=[1] * 9,
                dis_vols=[1] * 9,
            )
        )
        # 9 个 source 物理上同液 → 整段共用 1 个 tip
        assert call_names(lh).count("pick_up_tips") == 1
        assert call_names(lh).count("discard_tips") == 1
        assert call_names(lh).count("aspirate") == 9
        assert call_names(lh).count("dispense") == 9

    def test_T3_liquids_hit_same_plate_different_wells(self) -> None:
        """T3：同 plate 上 A1-H1 都装 PBS（8 个不同 Well 对象）→ identity 全 fail，liquids-keep 命中。"""
        lh = FakeLiquidHandler(channel_num=1)
        sources = [make_well(f"A{i}", "PBS") for i in range(1, 9)]
        targets = [make_well(f"T{i}") for i in range(8)]
        run(
            lh.transfer_liquid(
                sources=sources,
                targets=targets,
                tip_racks=[],
                use_channels=[0],
                asp_vols=[1] * 8,
                dis_vols=[1] * 8,
            )
        )
        assert call_names(lh).count("pick_up_tips") == 1
        assert call_names(lh).count("discard_tips") == 1

    def test_T4_liquids_not_match_forces_tip_change(self) -> None:
        """T4：A1=PBS，B1=Tris HCl → liquids 名不等，强制换 tip。"""
        lh = FakeLiquidHandler(channel_num=1)
        sources = [make_well("A1", "PBS"), make_well("B1", "Tris HCl")]
        targets = [make_well("T0"), make_well("T1")]
        run(
            lh.transfer_liquid(
                sources=sources,
                targets=targets,
                tip_racks=[],
                use_channels=[0],
                asp_vols=[1, 1],
                dis_vols=[1, 1],
            )
        )
        # 2 次完全独立的 transfer：2 次 pick_up / 2 次 discard
        assert call_names(lh).count("pick_up_tips") == 2
        assert call_names(lh).count("discard_tips") == 2

    def test_T5_empty_liquids_forces_tip_change(self) -> None:
        """T5：source 从未调过 set_liquids（liquids 空）→ 视为未知，强制换 tip。"""
        lh = FakeLiquidHandler(channel_num=1)
        sources = [make_well("A1"), make_well("B1")]  # 没装液体名
        targets = [make_well("T0"), make_well("T1")]
        run(
            lh.transfer_liquid(
                sources=sources,
                targets=targets,
                tip_racks=[],
                use_channels=[0],
                asp_vols=[1, 1],
                dis_vols=[1, 1],
            )
        )
        assert call_names(lh).count("pick_up_tips") == 2
        assert call_names(lh).count("discard_tips") == 2

    def test_T6_switch_off_disables_liquids_keep(self) -> None:
        """T6：tip_reuse_by_liquid_name=False，T2 场景退化为 identity-only，强制换 tip。"""
        lh = FakeLiquidHandler(channel_num=1, tip_reuse_by_liquid_name=False)
        sources = [make_well(f"S{i}", "PBS") for i in range(9)]
        targets = [make_well(f"T{i}") for i in range(9)]
        run(
            lh.transfer_liquid(
                sources=sources,
                targets=targets,
                tip_racks=[],
                use_channels=[0],
                asp_vols=[1] * 9,
                dis_vols=[1] * 9,
            )
        )
        # 关闭开关后 → 退化为 identity-only，9 次独立换 tip
        assert call_names(lh).count("pick_up_tips") == 9
        assert call_names(lh).count("discard_tips") == 9

    def test_T8_mix_style_same_source_reuses_via_identity(self) -> None:
        """T8：单 source 反复 aspirate/dispense → identity-keep 命中（mix-style）。"""
        lh = FakeLiquidHandler(channel_num=1)
        src = make_well("S0", "Methanol")
        tgt = make_well("T0")
        run(
            lh.transfer_liquid(
                sources=[src, src, src],
                targets=[tgt, tgt, tgt],
                tip_racks=[],
                use_channels=[0],
                asp_vols=[1, 1, 1],
                dis_vols=[1, 1, 1],
            )
        )
        assert call_names(lh).count("pick_up_tips") == 1
        assert call_names(lh).count("discard_tips") == 1

    def test_T10_unknown_literal_treated_as_unknown(self) -> None:
        """T10：``tracker.liquids = [("unknown", v)]``（兼容旧数据）→ 视为未知，强制换 tip。"""
        lh = FakeLiquidHandler(channel_num=1)
        sources = [make_well("A1", "unknown"), make_well("B1", "unknown")]
        targets = [make_well("T0"), make_well("T1")]
        run(
            lh.transfer_liquid(
                sources=sources,
                targets=targets,
                tip_racks=[],
                use_channels=[0],
                asp_vols=[1, 1],
                dis_vols=[1, 1],
            )
        )
        assert call_names(lh).count("pick_up_tips") == 2
        assert call_names(lh).count("discard_tips") == 2

    def test_T11_aspirate_pop_timing_pre_read(self) -> None:
        """T11：aspirate 顶层归零 → PLR pop ``tracker.liquids`` 顶层；
        验证 P10 v2 ``pending_tip_name`` 必须在 aspirate **之前**预读才能命中下一轮。
        """
        lh = AspiratePopFakeLiquidHandler(channel_num=1)
        sources = [make_well(f"S{i}", "PBS") for i in range(3)]
        targets = [make_well(f"T{i}") for i in range(3)]
        run(
            lh.transfer_liquid(
                sources=sources,
                targets=targets,
                tip_racks=[],
                use_channels=[0],
                asp_vols=[1] * 3,
                dis_vols=[1] * 3,
            )
        )
        # 即使 aspirate 后 source.tracker.liquids 被 pop，pending_tip_name 已捕获 "PBS"
        # → 下一轮 source 仍是 PBS（aspirate 还没发生），liquids-keep 命中
        # → 整段 1 次 pick_up_tips
        assert call_names(lh).count("pick_up_tips") == 1
        assert call_names(lh).count("discard_tips") == 1


# ---------------------------------------------------------------------------
# T7：跨节点边界（两个独立 transfer_liquid 调用，状态隔离）
# ---------------------------------------------------------------------------


@_skip_if_no_plr
class TestCrossNodeBoundary:
    """T7：两个 transfer_liquid 节点之间不复用 tip（每次调用初始化 current_tip_liquid_name=None）。"""

    def test_T7_two_calls_dont_share_tip_state(self) -> None:
        lh = FakeLiquidHandler(channel_num=1)
        src_a = make_well("A_src", "PBS")
        tgt_a = make_well("A_tgt")
        src_b = make_well("B_src", "PBS")  # 同名液，但不同 well
        tgt_b = make_well("B_tgt")

        run(
            lh.transfer_liquid(
                sources=[src_a],
                targets=[tgt_a],
                tip_racks=[],
                use_channels=[0],
                asp_vols=[1],
                dis_vols=[1],
            )
        )
        run(
            lh.transfer_liquid(
                sources=[src_b],
                targets=[tgt_b],
                tip_racks=[],
                use_channels=[0],
                asp_vols=[1],
                dis_vols=[1],
            )
        )
        # 两次调用各自独立换 tip → 2 次 pick_up_tips / 2 次 discard_tips
        assert call_names(lh).count("pick_up_tips") == 2
        assert call_names(lh).count("discard_tips") == 2


# ---------------------------------------------------------------------------
# T9：8 通道段锚孔 liquids-keep
# ---------------------------------------------------------------------------


@_skip_if_no_plr
class TestEightChannelSegmentTipReuse:
    """T9：8 通道分段，连续两段 src_slice[0] 同名 → 段间不换 tip。"""

    def test_T9_two_segments_same_anchor_liquid(self) -> None:
        lh = FakeLiquidHandler(channel_num=8)
        # 16 个 source wells，分 2 段；段 1 锚孔 = sources[0]，段 2 锚孔 = sources[8]
        sources = [make_well(f"S{i}", "PBS") for i in range(16)]
        targets = [make_well(f"T{i}") for i in range(16)]
        run(
            lh.transfer_liquid(
                sources=sources,
                targets=targets,
                tip_racks=[],
                use_channels=list(range(8)),
                asp_vols=[1] * 16,
                dis_vols=[1] * 16,
                mix_times=0,
            )
        )
        # 2 段都同液 → liquids-keep 命中 → 仅 1 次 pick_up_tips
        assert call_names(lh).count("pick_up_tips") == 1
        assert call_names(lh).count("discard_tips") == 1

    def test_T9b_two_segments_different_anchor_liquid_forces_tip_change(self) -> None:
        """T9b：段 1 锚孔 = PBS，段 2 锚孔 = Tris → 段间强制换 tip。"""
        lh = FakeLiquidHandler(channel_num=8)
        seg1 = [make_well(f"S{i}", "PBS") for i in range(8)]
        seg2 = [make_well(f"S{i + 8}", "Tris HCl") for i in range(8)]
        sources = seg1 + seg2
        targets = [make_well(f"T{i}") for i in range(16)]
        run(
            lh.transfer_liquid(
                sources=sources,
                targets=targets,
                tip_racks=[],
                use_channels=list(range(8)),
                asp_vols=[1] * 16,
                dis_vols=[1] * 16,
                mix_times=0,
            )
        )
        # 2 段不同液 → 2 次独立换 tip
        assert call_names(lh).count("pick_up_tips") == 2
        assert call_names(lh).count("discard_tips") == 2


# ---------------------------------------------------------------------------
# 配置开关默认值 / 实例字段读取
# ---------------------------------------------------------------------------


@_skip_if_no_plr
class TestConfigDefault:
    def test_default_switch_is_on(self) -> None:
        """默认 ``_tip_reuse_by_liquid_name`` 应为 True（测试 fixture 显式 default 一致）。"""
        lh = FakeLiquidHandler()
        assert lh._tip_reuse_by_liquid_name is True

    def test_switch_off_takes_effect(self) -> None:
        lh = FakeLiquidHandler(tip_reuse_by_liquid_name=False)
        assert lh._tip_reuse_by_liquid_name is False
