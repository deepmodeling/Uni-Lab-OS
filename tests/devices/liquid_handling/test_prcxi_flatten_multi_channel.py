"""P1 v5 — PRCXI 8 通道 → 1 通道扁平化测试。

测试覆盖（详见 ``product_designs/protocol_convert/01-multi-channel-flatten.md`` §11.6）：

    - F1–F8：扁平化基础场景（plate / reservoir / small-vols heuristic / 长度异常 /
      ``has_true_8channel`` 旁路 / flow_rates / offsets / blow_out 同步展开）。
    - F9–F11：扁平化期间 ``_tip_reuse_by_liquid_name`` 临时关 + ``try/finally`` 恢复 +
      identity-keep 在扁平化路径仍能复用 1 个 tip（reservoir 集成场景）。

F6 / F8 是 helper 静态方法的纯单元测试（不依赖 PLR，从 PLR-free 模块
``prcxi.flatten_utils`` import）；F1–F5, F7, F9–F11 是 PRCXI 子类入口的端到端
（mock ``super().transfer_liquid`` 捕获入参，依赖 PRCXI 完整 import 链）。
若本地 PLR 版本不匹配，端到端测试 ``skipif`` 跳过但 helper 测试照常运行（与
``test_tip_reuse_by_liquid_name.py`` 的环境兼容策略一致）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence, Tuple
from unittest.mock import patch

import pytest

# P1 v5 扁平化 helper 位于 PLR-free 模块，无论 pylabrobot 是否安装都能 import。
from unilabos.devices.liquid_handling.prcxi.flatten_utils import (
    flatten_multi_channel_kwargs,
)

# E2E 测试依赖 PRCXI 完整 import 链（顶部 import 整个 PLR 模块）。
# 沿用 P10 v2 的 try/except 模式，环境无 PLR 时端到端段 skip 但不报模块级 error。
try:
    from pylabrobot.resources import Coordinate, TipRack

    from unilabos.devices.liquid_handling.liquid_handler_abstract import (
        LiquidHandlerAbstract,
    )
    from unilabos.devices.liquid_handling.prcxi.prcxi import PRCXI9300Handler

    _PLR_AVAILABLE = True
    _PLR_IMPORT_ERROR: Optional[Exception] = None
except Exception as exc:  # pragma: no cover - 环境相关
    Coordinate = None  # type: ignore[assignment, misc]
    TipRack = None  # type: ignore[assignment, misc]
    LiquidHandlerAbstract = None  # type: ignore[assignment, misc]
    PRCXI9300Handler = None  # type: ignore[assignment, misc]
    _PLR_AVAILABLE = False
    _PLR_IMPORT_ERROR = exc


_skip_if_no_plr = pytest.mark.skipif(
    not _PLR_AVAILABLE,
    reason=f"pylabrobot not importable in this env: {_PLR_IMPORT_ERROR!r}",
)


# ---------------------------------------------------------------------------
# Dummy resources（不依赖真实 PLR Plate / Well / TipRack 构造链）
# ---------------------------------------------------------------------------


@dataclass
class DummyTipSpot:
    name: str = "spot"

    def get_size_z(self) -> float:
        return 40.0

    def get_size_x(self) -> float:
        return 9.0


@dataclass
class DummyTipRack:
    """模拟 TipRack：仅提供 ``children`` 让 ``tip_rack.children[0].get_size_z()`` 不炸。

    通过 ``__class__`` 直接挂为 ``TipRack`` 子类绕开 ``isinstance(tip_racks[0], TipRack)`` 校验。
    """

    name: str = "tip_rack"
    children: List[DummyTipSpot] = field(default_factory=lambda: [DummyTipSpot()])
    model: str = ""

    def __post_init__(self) -> None:
        # PRCXI ``isinstance(tip_racks[0], TipRack)`` 需要真实继承关系；
        # 注释掉的 trick：直接换 class 让 isinstance 返回 True。
        pass


@dataclass
class DummyPlate:
    """模拟 Plate（``source.parent``）。"""

    name: str
    children: List[Any] = field(default_factory=list)


@dataclass
class DummyWell:
    name: str
    parent: Optional[DummyPlate] = None


def make_tip_rack() -> Any:
    """构造一个 isinstance(TipRack) 为 True 的轻量 stub。"""

    class _LiteTipRack(TipRack):  # type: ignore[misc, valid-type]
        def __init__(self, name: str = "tr") -> None:
            self.name = name
            self.children = [DummyTipSpot()]
            self.model = ""

    return _LiteTipRack()


def make_10ul_tip_rack() -> Any:
    """构造一个 isinstance(TipRack) 为 True 且会被 ``_tip_rack_is_10ul_range`` 判 True 的 stub。"""

    class _Lite10ulTipRack(TipRack):  # type: ignore[misc, valid-type]
        def __init__(self, name: str = "tr_10ul") -> None:
            self.name = name
            self.children = [DummyTipSpot()]
            self.model = "PRCXI_10ul_Tips"

    return _Lite10ulTipRack()


# ---------------------------------------------------------------------------
# FakePRCXI：跳过 __init__ 真实初始化，仅设置 transfer_liquid 路径用到的 attr。
# ---------------------------------------------------------------------------


@dataclass
class _FakeApiClient:
    sent: List[Any] = field(default_factory=list)

    def update_pipetting_position(self, matrix_id: str, positions: List[Any]) -> None:
        self.sent.append((matrix_id, positions))


@dataclass
class _FakeBackend:
    matrix_id: str = "matrix-1"
    api_client: _FakeApiClient = field(default_factory=_FakeApiClient)


def _make_fake_prcxi(
    *,
    tip_reuse_by_liquid_name: bool = True,
    has_true_8channel: bool = False,
) -> Any:
    """构造一个 ``PRCXI9300Handler`` 实例，但跳过 ``__init__``。

    仅设置 ``transfer_liquid`` 入口路径（resolve / attach / change_slots / super 调用）
    会访问到的 attr。``super().transfer_liquid`` 在测试中通过 ``patch.object`` 拦截。
    """
    inst: Any = PRCXI9300Handler.__new__(PRCXI9300Handler)
    inst._first_transfer_done = True  # 跳过 _match_and_create_matrix
    inst.step_mode = False
    inst.has_true_8channel = has_true_8channel
    inst._tip_reuse_by_liquid_name = tip_reuse_by_liquid_name
    inst.tip_height = 0
    inst._unilabos_backend = _FakeBackend()
    inst.x_increase = -0.003636

    async def _identity_resolve(resources: Any) -> Any:
        return list(resources) if not isinstance(resources, list) else resources

    inst._resolve_to_plr_resources = _identity_resolve  # type: ignore[assignment]
    inst._attach_resources_to_deck_if_needed = lambda *a, **kw: None  # type: ignore[assignment]
    inst._get_slot_number = lambda *a, **kw: 1  # type: ignore[assignment]
    inst.plr_pos_to_prcxi = lambda well: Coordinate(0.0, 0.0, 0.0)  # type: ignore[assignment]
    return inst


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Helper 单元测试（F6 / F8）
# ---------------------------------------------------------------------------


class TestFlattenHelper:
    """F6 / F8：``flatten_multi_channel_kwargs`` 静态方法行为（PLR-free，单跑）。"""

    def test_f6_length_mismatch_raises_value_error(self) -> None:
        """F6：``sources`` 长度 21（非 8/3/1）应 raise ValueError 并提示字段名。"""
        sources = [DummyWell(f"S{i}") for i in range(21)]
        targets = [DummyWell(f"T{i}") for i in range(24)]
        with pytest.raises(ValueError, match="sources"):
            flatten_multi_channel_kwargs(
                sources=sources,
                targets=targets,
                asp_vols=[100.0] * 24,
                dis_vols=[100.0] * 24,
            )

    def test_f6_asp_dis_length_inconsistent_raises_value_error(self) -> None:
        """F6 补充：``asp_vols`` / ``dis_vols`` 长度不一致也应 raise。"""
        with pytest.raises(ValueError, match="asp_vols"):
            flatten_multi_channel_kwargs(
                sources=[DummyWell(f"S{i}") for i in range(24)],
                targets=[DummyWell(f"T{i}") for i in range(24)],
                asp_vols=[100.0] * 24,
                dis_vols=[100.0] * 16,
            )

    def test_f6_n_total_not_multiple_of_8_raises(self) -> None:
        """F6 补充：``asp_vols`` 长度非 8 倍数应 raise。"""
        with pytest.raises(ValueError, match="8 的正倍数"):
            flatten_multi_channel_kwargs(
                sources=[DummyWell(f"S{i}") for i in range(10)],
                targets=[DummyWell(f"T{i}") for i in range(10)],
                asp_vols=[100.0] * 10,
                dis_vols=[100.0] * 10,
            )

    def test_f6_empty_asp_vols_raises(self) -> None:
        """F6 补充：空 ``asp_vols`` 也应 raise（n_total=0 是退化输入）。"""
        with pytest.raises(ValueError, match="8 的正倍数"):
            flatten_multi_channel_kwargs(
                sources=[],
                targets=[],
                asp_vols=[],
                dis_vols=[],
            )

    def test_f6b_empty_optional_per_well_equals_none(self) -> None:
        """F6b：可选 per-well 参数空 list 等价于 None（"未填值"语义）。

        Caller 不传 ``offsets`` / ``asp_flow_rates`` / ``delays`` 等参数时上游下发
        ``[]`` 是常见行为；必须当 "走默认 / 不限制" 处理，不能触发 rule 5 报错。
        2026-05-28 用户实测：transfer 流程下发 ``offsets=[]`` 触发 ValueError
        ("参数 offsets 长度 0 不匹配 8 通道扁平化要求") 阻塞 protocol 执行。
        """
        sources = [DummyWell(f"S{i}") for i in range(8)]
        targets = [DummyWell(f"T{i}") for i in range(8)]
        out = flatten_multi_channel_kwargs(
            sources=sources,
            targets=targets,
            asp_vols=[100.0] * 8,
            dis_vols=[100.0] * 8,
            offsets=[],
            asp_flow_rates=(),
            dis_flow_rates=[],
            delays=[],
            blow_out_air_volume=[],
            blow_out_air_volume_before=[],
            liquid_height=[],
            pre_aspirate_from_target=[],
        )
        for key in (
            "offsets",
            "asp_flow_rates",
            "dis_flow_rates",
            "delays",
            "blow_out_air_volume",
            "blow_out_air_volume_before",
            "liquid_height",
            "pre_aspirate_from_target",
        ):
            assert out[key] is None, f"{key} 期望被归一为 None，实际 {out[key]!r}"

    def test_f8_plate_passthrough_no_expand(self) -> None:
        """F8：plate 整列 24 wells 直接透传，长度不变。"""
        sources = [DummyWell(f"S{i}") for i in range(24)]
        targets = [DummyWell(f"T{i}") for i in range(24)]
        out = flatten_multi_channel_kwargs(
            sources=sources,
            targets=targets,
            asp_vols=[100.0] * 24,
            dis_vols=[120.0] * 24,
        )
        assert out["sources"] == sources
        assert out["targets"] == targets
        assert out["asp_vols"] == [100.0] * 24
        assert out["dis_vols"] == [120.0] * 24

    def test_f8_length_m_sources_raises(self) -> None:
        """F8（policy 2026-05-28）：sources 长度 ``= M`` 在 8 通道模式下**非法**。

        8 通道模式的最小操作单元是 "1 op = 8 通道并行"，length=M 的 sources
        隐含 "每 op 只移液 M 个"，与硬件物理事实冲突。多 reservoir 必须改用
        ``n == 1`` 广播或 ``n == N`` 显式逐 op。
        """
        reservoir_wells = [DummyWell(f"R{i}") for i in range(3)]
        with pytest.raises(ValueError, match="不匹配 8 通道扁平化要求"):
            flatten_multi_channel_kwargs(
                sources=reservoir_wells,         # 长度 3 = M，应报错
                targets=reservoir_wells,
                asp_vols=[8.3] * 24,             # n_total=24, M=3
                dis_vols=[8.3] * 24,
            )

    def test_f8_flow_rates_liquid_height_sync_expand(self) -> None:
        """F8：``flow_rates`` / ``liquid_height`` 等同步展开（n_total / 8 / 1 / None / 标量）。

        2026-05-28 policy：移除 ``n == M`` 分支后，per-channel 参数合法长度仅
        n_total / 8 / 1（per-channel-tile / 广播）。
        """
        sources = [DummyWell(f"R{i}") for i in range(8)]      # 长度 8 → rule 2 tile M 次
        targets = [DummyWell(f"T{i}") for i in range(8)]
        out = flatten_multi_channel_kwargs(
            sources=sources,
            targets=targets,
            asp_vols=[8.3] * 24,                              # n_total=24, M=3
            dis_vols=[8.3] * 24,
            asp_flow_rates=[1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0],  # 长度 8 → per-channel tile
            dis_flow_rates=[0.5] * 24,                         # 已经 24 → 透传
            liquid_height=[0.0],                                # 长度 1 → 广播 24
            blow_out_air_volume=None,                           # None 透传
            blow_out_air_volume_before=5.0,                     # 标量透传
            delays=42,                                          # 标量透传
        )
        # 长度 8 → tile M=3 次：列 1/2/3 各拷贝一份 [1..8]
        assert out["asp_flow_rates"] == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0] * 3
        assert out["sources"] == sources * 3
        assert out["targets"] == targets * 3
        assert out["dis_flow_rates"] == [0.5] * 24
        assert out["liquid_height"] == [0.0] * 24
        assert out["blow_out_air_volume"] is None
        assert out["blow_out_air_volume_before"] == 5.0
        assert out["delays"] == 42

    def test_f8_scalar_passthrough(self) -> None:
        """F8 补充：非 list/tuple 的标量参数原样透传（不被错误展开）。"""

        class _FakeCoord:
            def __init__(self, x: float, y: float, z: float) -> None:
                self.x, self.y, self.z = x, y, z

            def __eq__(self, other: object) -> bool:
                return (
                    isinstance(other, _FakeCoord)
                    and (self.x, self.y, self.z) == (other.x, other.y, other.z)
                )

        fake = _FakeCoord(1.0, 2.0, 3.0)
        out = flatten_multi_channel_kwargs(
            sources=[DummyWell("R0")],
            targets=[DummyWell("R0")],
            asp_vols=[10.0] * 8,
            dis_vols=[10.0] * 8,
            blow_out_air_volume=fake,                 # 任意非 list/tuple 对象 → 透传
            pre_aspirate_from_target=0.5,             # float 透传
        )
        assert out["blow_out_air_volume"] is fake
        assert out["pre_aspirate_from_target"] == 0.5


# ---------------------------------------------------------------------------
# §13 新增：length-8 → tile M 次（A~H channel column 复用 M 个目标列）
# 详见 product_designs/protocol_convert/01-multi-channel-flatten.md §0 / §13
# ---------------------------------------------------------------------------


class TestFlattenLength8TileRule:
    """F12 / F12b / F13 / F14 / F15：length-8 → tile M 次规则（§13 / §0.2 rule 2）。

    governing rule（§0.2 policy 2026-05-28）顺序：
      1. n == n_total → passthrough
      2. n == 8       → tile M 次（A~H channel column 唯一语义）
      3. n == 1       → broadcast
      4. else         → raise ValueError

    注：``n == m_cols`` (repeat-each by 8) **已删除**——8 通道模式的最小操作单元
    是 "1 op = 8 通道并行"，length=M 隐含 "每 op 只移液 M 个" 与硬件冲突。
    """

    def test_f12_length8_sources_tile_m4_columns(self) -> None:
        """F12：length-8 sources（A..H 一整列）+ M=4 → tile 4 次（75cfa6 step 5 主路径）。"""
        column_a_to_h = [DummyWell(name) for name in ["A1", "B1", "C1", "D1", "E1", "F1", "G1", "H1"]]
        # samples_8 在 75cfa6 中 column-major 排列：A1..H1, A2..H2, A3..H3, A4..H4
        targets_32 = [
            DummyWell(f"{row}{col}") for col in (1, 2, 3, 4) for row in "ABCDEFGH"
        ]
        out = flatten_multi_channel_kwargs(
            sources=column_a_to_h,
            targets=targets_32,
            asp_vols=[2.0] * 32,    # n_total=32, m_cols=4
            dis_vols=[2.0] * 32,
        )
        # 关键断言：tile（非 repeat-each）—— 每 8 个一组**循环**为 [A1..H1]
        assert len(out["sources"]) == 32
        assert out["sources"][0:8] == column_a_to_h
        assert out["sources"][8:16] == column_a_to_h     # tile：A1..H1 再次复用
        assert out["sources"][16:24] == column_a_to_h    # tile：第 3 次
        assert out["sources"][24:32] == column_a_to_h    # tile：第 4 次
        # 反断言：**不是** repeat-each —— 否则 [0:8] 应全为 column_a_to_h[0] 而非 A1..H1
        assert out["sources"][0:8] != [column_a_to_h[0]] * 8
        # targets 长度 == n_total → passthrough
        assert out["targets"] == targets_32

    def test_f12_iteration_pairing_is_a_to_h_per_column(self) -> None:
        """F12 补充：iteration `i` 的 source / target 配对验证 A→H 列复用语义。"""
        column_a_to_h = [DummyWell(name) for name in ["A1", "B1", "C1", "D1", "E1", "F1", "G1", "H1"]]
        targets_32 = [
            DummyWell(f"{row}{col}") for col in (1, 2, 3, 4) for row in "ABCDEFGH"
        ]
        out = flatten_multi_channel_kwargs(
            sources=column_a_to_h,
            targets=targets_32,
            asp_vols=[2.0] * 32,
            dis_vols=[2.0] * 32,
        )
        # op i 的物理含义：channel = i % 8（A..H 循环）, move/col = i // 8
        for i in range(32):
            channel = i % 8
            assert out["sources"][i] is column_a_to_h[channel], (
                f"op {i}: channel={channel} 应抽 source[{channel}]={column_a_to_h[channel].name}，"
                f"实际 {out['sources'][i].name}"
            )

    def test_f12b_per_channel_flow_rates_tile(self) -> None:
        """F12b：length-8 per-channel asp_flow_rates 同步 tile（§0.2 第 3 个不变量）。"""
        column_a_to_h = [DummyWell(name) for name in ["A1", "B1", "C1", "D1", "E1", "F1", "G1", "H1"]]
        targets_32 = [DummyWell(f"T{i}") for i in range(32)]
        per_channel_rates = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]   # 8 通道各自 flow rate
        out = flatten_multi_channel_kwargs(
            sources=column_a_to_h,
            targets=targets_32,
            asp_vols=[2.0] * 32,
            dis_vols=[2.0] * 32,
            asp_flow_rates=per_channel_rates,                          # length 8 → 同步 tile
            dis_flow_rates=per_channel_rates,
        )
        expected = per_channel_rates * 4
        assert out["asp_flow_rates"] == expected
        assert out["dis_flow_rates"] == expected
        # 物理语义验证：每个 channel 在所有 M 列都保持自己的速率
        for i in range(32):
            channel = i % 8
            assert out["asp_flow_rates"][i] == per_channel_rates[channel]

    def test_f13_length8_n_total8_m1_passthrough_wins(self) -> None:
        """F13：length-8 + n_total=8（M=1，单列）→ rule 1 passthrough 优先于 rule 2 tile。"""
        column_a_to_h = [DummyWell(name) for name in ["A1", "B1", "C1", "D1", "E1", "F1", "G1", "H1"]]
        out = flatten_multi_channel_kwargs(
            sources=column_a_to_h,
            targets=column_a_to_h,
            asp_vols=[100.0] * 8,   # n_total=8, m_cols=1
            dis_vols=[100.0] * 8,
        )
        # rule 1 命中：passthrough（不进 tile 分支，列表对象同 identity）
        assert out["sources"] == column_a_to_h
        # 验证 identity（rule 1 走 list(value) 拷贝，元素 is 同对象）
        for i in range(8):
            assert out["sources"][i] is column_a_to_h[i]

    def test_f14_length8_m_cols8_collision_tile_wins(self) -> None:
        """F14：length-8 + m_cols=8（碰撞带）→ rule 2 tile 命中（length=M=8 与 rule 2 同长度，rule 2 唯一可行）。"""
        column_a_to_h = [DummyWell(name) for name in ["A1", "B1", "C1", "D1", "E1", "F1", "G1", "H1"]]
        targets_64 = [DummyWell(f"T{i}") for i in range(64)]
        out = flatten_multi_channel_kwargs(
            sources=column_a_to_h,
            targets=targets_64,
            asp_vols=[8.3] * 64,    # n_total=64, m_cols=8（碰撞带）
            dis_vols=[8.3] * 64,
        )
        # 关键断言：tile（**非** repeat-each）—— 每 8 个一组循环为 A..H 而非 [A1]*8 + [B1]*8 + ...
        assert len(out["sources"]) == 64
        for col in range(8):
            assert out["sources"][col * 8:(col + 1) * 8] == column_a_to_h, (
                f"col {col}: tile 应得 A..H 全列，"
                f"实际 {[w.name for w in out['sources'][col * 8:(col + 1) * 8]]}"
            )
        # 反断言：**不是** repeat-each —— [0:8] 不应全为 A1
        assert out["sources"][0:8] != [column_a_to_h[0]] * 8
        # 反断言：i 位置的 channel 应是 i % 8（tile），而非 i // 8（repeat-each）
        for i in range(64):
            assert out["sources"][i] is column_a_to_h[i % 8]

    def test_f15_length_neither_8_nor_known_raises(self) -> None:
        """F15：length 7（非 8 / n_total / 1）仍 raise，message 含期望集合。

        2026-05-28 policy：移除 ``n == m_cols`` 分支后，合法长度集仅 n_total / 8 / 1。
        错误信息不再列 m_cols。
        """
        sources_7 = [DummyWell(f"S{i}") for i in range(7)]
        targets_32 = [DummyWell(f"T{i}") for i in range(32)]
        with pytest.raises(ValueError) as exc_info:
            flatten_multi_channel_kwargs(
                sources=sources_7,
                targets=targets_32,
                asp_vols=[100.0] * 32,   # n_total=32, M=4
                dis_vols=[100.0] * 32,
            )
        msg = str(exc_info.value)
        assert "sources" in msg
        assert "7" in msg
        # 期望集合应明示 8 / n_total / 1
        assert "8" in msg
        assert "32" in msg
        assert "1" in msg
        # 反断言：错误信息不再列 m_cols (4) —— 它是反例不是合法长度
        assert " 4 " not in msg, f"错误信息不应再列 m_cols=4，实际: {msg}"

    def test_length_m_neither_8_nor_n_total_raises(self) -> None:
        """policy 2026-05-28：M=3 + length=3 在 8 通道模式下**报错**。

        移除 rule 3（n == M repeat-each by 8）后，length=M 既不是 n_total，
        也不是 8 / 1，落到 rule 4 必 raise ValueError。多 reservoir 必须
        改用 ``n == 1`` 广播或 ``n == N`` 显式逐 op。
        """
        reservoirs_3 = [DummyWell(f"R{i}") for i in range(3)]
        with pytest.raises(ValueError, match="不匹配 8 通道扁平化要求"):
            flatten_multi_channel_kwargs(
                sources=reservoirs_3,
                targets=reservoirs_3,
                asp_vols=[8.3] * 24,    # n_total=24, M=3，sources 长度 3 = M 非法
                dis_vols=[8.3] * 24,
            )


# ---------------------------------------------------------------------------
# E2E：transfer_liquid 入口（mock super().transfer_liquid 捕获入参）
# ---------------------------------------------------------------------------


class _SuperCallCapture:
    """patch ``LiquidHandlerAbstract.transfer_liquid`` 用：捕获最后一次入参。"""

    def __init__(self, *, side_effect: Optional[Exception] = None) -> None:
        self.calls: List[Tuple[Tuple[Any, ...], dict]] = []
        self.side_effect = side_effect

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, dict(kwargs)))
        if self.side_effect is not None:
            raise self.side_effect
        return "OK"

    @property
    def last_kwargs(self) -> dict:
        assert self.calls, "super().transfer_liquid was not called"
        return self.calls[-1][1]


def _patch_super_transfer_liquid(capture: _SuperCallCapture) -> Any:
    return patch.object(LiquidHandlerAbstract, "transfer_liquid", capture)


@_skip_if_no_plr
class TestPrcxiFlattenE2E:
    """F1–F5, F7：扁平化入口端到端验证（不实跑抽象循环，只验证 super 收到的 kwargs）。"""

    def test_f1_plate_24_wells_flatten_passthrough(self) -> None:
        """F1：plate 24 wells（M=3）+ asp_vols=[100]*24 → super 收到 24 长度 + use_channels=None / [0]。"""
        prcxi = _make_fake_prcxi()
        sources = [DummyWell(f"S{i}", parent=DummyPlate("p_src")) for i in range(24)]
        targets = [DummyWell(f"T{i}", parent=DummyPlate("p_tgt")) for i in range(24)]
        tip_rack = make_tip_rack()

        cap = _SuperCallCapture()
        with _patch_super_transfer_liquid(cap):
            run(
                prcxi.transfer_liquid(
                    sources=sources,
                    targets=targets,
                    tip_racks=[tip_rack],
                    use_channels=list(range(8)),
                    asp_vols=[100.0] * 24,
                    dis_vols=[100.0] * 24,
                )
            )

        kw = cap.last_kwargs
        assert len(kw["asp_vols"]) == 24
        assert len(kw["dis_vols"]) == 24
        assert len(kw["sources"]) == 24
        assert len(kw["targets"]) == 24
        # 100uL + 300uL tip rack → small-vols 不触发，use_channels=None（落到默认 [0]）
        assert kw["use_channels"] is None

    def test_f2_reservoir_3_wells_flatten_expand(self) -> None:
        """F2：reservoir M=3 + asp_vols=[8.3]*24 + 10uL tip rack → small-vols → use_channels=[1]。"""
        prcxi = _make_fake_prcxi()
        reservoir = [DummyWell(f"R{i}", parent=DummyPlate("rv")) for i in range(3)]
        tip_rack = make_10ul_tip_rack()

        cap = _SuperCallCapture()
        with _patch_super_transfer_liquid(cap):
            run(
                prcxi.transfer_liquid(
                    sources=reservoir,
                    targets=reservoir,
                    tip_racks=[tip_rack],
                    use_channels=list(range(8)),
                    asp_vols=[8.3] * 24,
                    dis_vols=[8.3] * 24,
                )
            )

        kw = cap.last_kwargs
        assert len(kw["sources"]) == 24
        # 同一 reservoir well 应连续出现 8 次
        assert kw["sources"][0:8] == [reservoir[0]] * 8
        assert kw["sources"][8:16] == [reservoir[1]] * 8
        # small-vols + 10uL tip → use_channels=[1]
        assert kw["use_channels"] == [1]

    def test_f3_small_vols_trigger_right_head(self) -> None:
        """F3：F2 场景（小体积 + 10uL tip rack）→ use_channels=[1]（右头小量程）。"""
        # F2 已覆盖该点；此处复用 F2 的断言形式，独立保留覆盖标识。
        self.test_f2_reservoir_3_wells_flatten_expand()

    def test_f4_small_vols_not_trigger_with_large_tip(self) -> None:
        """F4：F1 场景（300uL tip rack + 100uL volumes）→ small-vols 不触发，use_channels=None。"""
        # F1 已覆盖该点。
        self.test_f1_plate_24_wells_flatten_passthrough()

    def test_f5_single_channel_no_flatten(self) -> None:
        """F5：``use_channels=[0]`` + 单孔 → 不走扁平化分支；``_tip_reuse_by_liquid_name`` 不被改。"""
        prcxi = _make_fake_prcxi(tip_reuse_by_liquid_name=True)
        sources = [DummyWell("S0", parent=DummyPlate("p_src"))]
        targets = [DummyWell("T0", parent=DummyPlate("p_tgt"))]
        tip_rack = make_tip_rack()

        cap = _SuperCallCapture()
        with _patch_super_transfer_liquid(cap):
            run(
                prcxi.transfer_liquid(
                    sources=sources,
                    targets=targets,
                    tip_racks=[tip_rack],
                    use_channels=[0],
                    asp_vols=[100.0],
                    dis_vols=[100.0],
                )
            )

        kw = cap.last_kwargs
        # 入参未被扁平化展开
        assert kw["sources"] == sources
        assert kw["asp_vols"] == [100.0]
        assert kw["use_channels"] == [0]
        # F5 关键断言：tip-reuse switch 没有被改回
        assert prcxi._tip_reuse_by_liquid_name is True

    def test_f7_has_true_8channel_skips_flatten(self) -> None:
        """F7：``has_true_8channel=True`` → 不扁平化，super 收到原始 use_channels=[0..7]。"""
        prcxi = _make_fake_prcxi(has_true_8channel=True)
        sources = [DummyWell(f"S{i}", parent=DummyPlate("p")) for i in range(24)]
        tip_rack = make_tip_rack()

        cap = _SuperCallCapture()
        with _patch_super_transfer_liquid(cap):
            run(
                prcxi.transfer_liquid(
                    sources=sources,
                    targets=sources,
                    tip_racks=[tip_rack],
                    use_channels=list(range(8)),
                    asp_vols=[100.0] * 24,
                    dis_vols=[100.0] * 24,
                )
            )

        kw = cap.last_kwargs
        assert kw["use_channels"] == list(range(8))  # 原样透传，未被扁平化覆写为 None

    def test_empty_tip_racks_raises_value_error(self) -> None:
        """空 tip_racks 输入应在 PRCXI 入口处报 ValueError（而不是 IndexError）。"""
        prcxi = _make_fake_prcxi()
        sources = [DummyWell("S0", parent=DummyPlate("p_src"))]
        targets = [DummyWell("T0", parent=DummyPlate("p_tgt"))]

        with pytest.raises(ValueError, match="at least one tip rack"):
            run(
                prcxi.transfer_liquid(
                    sources=sources,
                    targets=targets,
                    tip_racks=[],
                    use_channels=[0],
                    asp_vols=[10.0],
                    dis_vols=[10.0],
                )
            )


@_skip_if_no_plr
class TestPrcxiFlattenTipReuseToggle:
    """F9 / F10：扁平化路径下 ``_tip_reuse_by_liquid_name`` 临时关 + try/finally 恢复。"""

    def test_f9_liquids_keep_disabled_during_super_call(self) -> None:
        """F9：扁平化路径下 super 被调用时刻 ``_tip_reuse_by_liquid_name == False``；返回后恢复 True。"""
        prcxi = _make_fake_prcxi(tip_reuse_by_liquid_name=True)
        sources = [DummyWell(f"S{i}", parent=DummyPlate("p")) for i in range(24)]
        tip_rack = make_tip_rack()

        captured_at_super_time: List[bool] = []

        async def _spy_super(*args: Any, **kwargs: Any) -> Any:
            # super 被调用时刻读取一次开关
            captured_at_super_time.append(prcxi._tip_reuse_by_liquid_name)
            return "OK"

        with patch.object(LiquidHandlerAbstract, "transfer_liquid", _spy_super):
            run(
                prcxi.transfer_liquid(
                    sources=sources,
                    targets=sources,
                    tip_racks=[tip_rack],
                    use_channels=list(range(8)),
                    asp_vols=[100.0] * 24,
                    dis_vols=[100.0] * 24,
                )
            )

        assert captured_at_super_time == [False], "扁平化路径下 super 调用时刻 liquids-keep 应被临时关闭"
        # 返回后恢复 True
        assert prcxi._tip_reuse_by_liquid_name is True

    def test_f10_finally_restores_on_super_exception(self) -> None:
        """F10：super 抛 RuntimeError，finally 仍能恢复 ``_tip_reuse_by_liquid_name``。"""
        prcxi = _make_fake_prcxi(tip_reuse_by_liquid_name=True)
        sources = [DummyWell(f"S{i}", parent=DummyPlate("p")) for i in range(24)]
        tip_rack = make_tip_rack()

        cap = _SuperCallCapture(side_effect=RuntimeError("backend boom"))
        with _patch_super_transfer_liquid(cap):
            with pytest.raises(RuntimeError, match="backend boom"):
                run(
                    prcxi.transfer_liquid(
                        sources=sources,
                        targets=sources,
                        tip_racks=[tip_rack],
                        use_channels=list(range(8)),
                        asp_vols=[100.0] * 24,
                        dis_vols=[100.0] * 24,
                    )
                )

        # finally 必须恢复
        assert prcxi._tip_reuse_by_liquid_name is True

    def test_f9b_user_default_false_stays_false(self) -> None:
        """F9 补充：用户原始 config = False，扁平化路径下 super 拿到 False，返回后仍 False（不被错误置回 True）。"""
        prcxi = _make_fake_prcxi(tip_reuse_by_liquid_name=False)
        sources = [DummyWell(f"S{i}", parent=DummyPlate("p")) for i in range(24)]
        tip_rack = make_tip_rack()

        captured: List[bool] = []

        async def _spy_super(*args: Any, **kwargs: Any) -> Any:
            captured.append(prcxi._tip_reuse_by_liquid_name)
            return "OK"

        with patch.object(LiquidHandlerAbstract, "transfer_liquid", _spy_super):
            run(
                prcxi.transfer_liquid(
                    sources=sources,
                    targets=sources,
                    tip_racks=[tip_rack],
                    use_channels=list(range(8)),
                    asp_vols=[100.0] * 24,
                    dis_vols=[100.0] * 24,
                )
            )

        assert captured == [False]
        assert prcxi._tip_reuse_by_liquid_name is False


# ---------------------------------------------------------------------------
# F11：reservoir 集成场景（不 mock super，让抽象单通道循环实际跑）
# ---------------------------------------------------------------------------


@_skip_if_no_plr
class TestPrcxiFlattenReservoirIdentityKeep:
    """F11：reservoir 场景下扁平化 + identity-keep 仍能复用 1 个 tip。

    集成型测试：不 mock ``super().transfer_liquid``；而是把 ``aspirate`` / ``dispense`` /
    ``pick_up_tips`` / ``discard_tips`` 直接 stub 到 ``LiquidHandlerAbstract``（PRCXI
    继承链），让 v2 单通道循环实际执行 24 次 aspirate，验证 identity-keep 触发后只
    pick_up_tips 一次。
    """

    def test_f11_reservoir_identity_keep_uses_single_tip(self) -> None:
        prcxi = _make_fake_prcxi(tip_reuse_by_liquid_name=True)
        # 8 个 reservoir well（M=8）+ 8 个 plate target，每孔 8.3uL → 8×8=64 次 op
        reservoir = [DummyWell(f"R{i}", parent=DummyPlate("rv")) for i in range(1)]
        targets = [DummyWell(f"T{i}", parent=DummyPlate("plate")) for i in range(1)]
        tip_rack = make_10ul_tip_rack()

        calls: List[str] = []

        async def _pick(self_, tip_spots, use_channels=None, offsets=None, **kw):
            calls.append("pick_up_tips")

        async def _asp(self_, resources, vols, **kw):
            calls.append("aspirate")

        async def _disp(self_, resources, vols, **kw):
            calls.append("dispense")

        async def _drop(self_, use_channels=None, *args, **kwargs):
            calls.append("discard_tips")

        # set_tiprack 在 super loop 里会跑，stub 掉
        def _set_tiprack(self_, tip_racks):
            return None

        # 单次 reservoir → 单次 target；asp_vols=[8.3]*8（M=1, 8 通道）
        from unittest.mock import patch as _patch

        with _patch.object(LiquidHandlerAbstract, "pick_up_tips", _pick), \
             _patch.object(LiquidHandlerAbstract, "aspirate", _asp), \
             _patch.object(LiquidHandlerAbstract, "dispense", _disp), \
             _patch.object(LiquidHandlerAbstract, "discard_tips", _drop), \
             _patch.object(LiquidHandlerAbstract, "set_tiprack", _set_tiprack):
            # 不 mock super().transfer_liquid → 实跑抽象循环
            run(
                prcxi.transfer_liquid(
                    sources=reservoir,  # M=1, 单 reservoir
                    targets=targets,
                    tip_racks=[tip_rack],
                    use_channels=list(range(8)),
                    asp_vols=[8.3] * 8,
                    dis_vols=[8.3] * 8,
                )
            )

        # 期待：扁平化后 sources / targets 都展开为 len 8（M=1 → 8 次顺序）；
        # 但 F11 关键断言是 identity-keep 命中：reservoir 是同一 well 重复 8 次 →
        # 只 pick_up_tips 1 次 + discard_tips 1 次 + aspirate 8 次 + dispense 8 次。
        # 注意：扁平化路径下 _tip_reuse_by_liquid_name 被关闭，但 identity-keep
        # （source `is` 判等）不依赖该开关，仍然生效。
        assert calls.count("aspirate") == 8
        assert calls.count("dispense") == 8
        # 关键：identity-keep 命中 → tip 只 pick / drop 各 1 次
        assert calls.count("pick_up_tips") == 1, (
            f"reservoir identity-keep 应只 pick 1 次 tip，实际 {calls.count('pick_up_tips')}；calls={calls}"
        )
        assert calls.count("discard_tips") == 1, (
            f"reservoir identity-keep 应只 drop 1 次 tip，实际 {calls.count('discard_tips')}；calls={calls}"
        )
