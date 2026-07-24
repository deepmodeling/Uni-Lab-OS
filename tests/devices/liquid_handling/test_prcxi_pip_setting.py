"""PRCXI ``pip_setting`` 轴路由测试。

覆盖：
  - ``normalize_pip_setting`` / ``select_axis`` / ``axis_channel_list`` /
    ``axis_from_channels`` 纯 helper（PLR-free，无论 pylabrobot 是否安装都跑）。
  - ``PRCXI9300Backend._axis_from_channels`` / ``_effective_num_channels``
    （legacy ``[0]/[1]`` vs. pip_setting 范围映射 + 体积校验）。
  - ``transfer_liquid`` pip_setting 路由：通道优先选轴 + 按轴并行度扁平化 +
    新通道编号（左[0..7]/右[8..15]）；以及 ``pip_setting=None`` 时 legacy 路径不变。

helper 测试不依赖 PLR；backend / E2E 测试依赖 PRCXI 完整 import 链，环境无 PLR 时
``skipif`` 跳过（与 ``test_prcxi_flatten_multi_channel.py`` 的环境兼容策略一致）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple
from unittest.mock import patch

import pytest

# pip_setting helper 位于 PLR-free 模块，无论 pylabrobot 是否安装都能 import。
from unilabos.devices.liquid_handling.prcxi.flatten_utils import (
    normalize_pip_setting,
    select_axis,
    axis_channel_list,
    axis_from_channels,
)

try:
    from pylabrobot.resources import Coordinate, TipRack

    from unilabos.devices.liquid_handling.liquid_handler_abstract import (
        LiquidHandlerAbstract,
        LiquidHandlerMiddleware,
    )
    from unilabos.devices.liquid_handling.prcxi.prcxi import (
        PRCXI9300Handler,
        PRCXI9300Backend,
    )
    from unilabos.devices.liquid_handling.prcxi.prcxi_labware import PRCXI_300ul_Tips

    _PLR_AVAILABLE = True
    _PLR_IMPORT_ERROR: Optional[Exception] = None
except Exception as exc:  # pragma: no cover - 环境相关
    Coordinate = None  # type: ignore[assignment, misc]
    TipRack = None  # type: ignore[assignment, misc]
    LiquidHandlerAbstract = None  # type: ignore[assignment, misc]
    LiquidHandlerMiddleware = None  # type: ignore[assignment, misc]
    PRCXI9300Handler = None  # type: ignore[assignment, misc]
    PRCXI9300Backend = None  # type: ignore[assignment, misc]
    PRCXI_300ul_Tips = None  # type: ignore[assignment, misc]
    _PLR_AVAILABLE = False
    _PLR_IMPORT_ERROR = exc


_skip_if_no_plr = pytest.mark.skipif(
    not _PLR_AVAILABLE,
    reason=f"pylabrobot not importable in this env: {_PLR_IMPORT_ERROR!r}",
)

# 典型双轴配置：左 100µL/8 通道（小量程多通道头），右 1000µL/1 通道（大量程单通道头）。
PIP = {"left": {"vol": 100, "channels": 8}, "right": {"vol": 1000, "channels": 1}}


# ---------------------------------------------------------------------------
# normalize_pip_setting
# ---------------------------------------------------------------------------


class TestNormalizePipSetting:
    def test_none_passthrough(self) -> None:
        assert normalize_pip_setting(None) is None

    def test_valid_normalized(self) -> None:
        out = normalize_pip_setting(PIP)
        assert out == {"left": {"vol": 100.0, "channels": 8}, "right": {"vol": 1000.0, "channels": 1}}

    def test_single_axis_allowed(self) -> None:
        out = normalize_pip_setting({"left": {"vol": 100, "channels": 8}})
        assert out == {"left": {"vol": 100.0, "channels": 8}}

    def test_non_dict_raises(self) -> None:
        with pytest.raises(ValueError):
            normalize_pip_setting([1, 2, 3])

    def test_missing_field_raises(self) -> None:
        with pytest.raises(ValueError):
            normalize_pip_setting({"left": {"vol": 100}})

    def test_non_positive_raises(self) -> None:
        with pytest.raises(ValueError):
            normalize_pip_setting({"left": {"vol": 0, "channels": 8}})

    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError):
            normalize_pip_setting({})


# ---------------------------------------------------------------------------
# select_axis（通道优先、再看体积；体积是硬约束）
# ---------------------------------------------------------------------------


class TestSelectAxis:
    def test_multi_channel_small_vol_left(self) -> None:
        """8 通道 + 50µL（≤左轴量程）→ 左轴（真并行，无需扁平化）。"""
        assert select_axis(PIP, 8, 50.0) == "left"

    def test_single_small_vol_prefers_smallest_axis(self) -> None:
        """单通道 + 50µL → 左轴（精度优先：能容纳的最小量程轴）。"""
        assert select_axis(PIP, 1, 50.0) == "left"

    def test_single_large_vol_right(self) -> None:
        """单通道 + 500µL（>左轴量程）→ 右轴。"""
        assert select_axis(PIP, 1, 500.0) == "right"

    def test_multi_channel_large_vol_falls_back_to_right(self) -> None:
        """8 通道 + 150µL（超左轴量程）→ 回退右轴（单通道，调用方据此扁平化）。"""
        assert select_axis(PIP, 8, 150.0) == "right"

    def test_volume_exceeds_all_axes_raises(self) -> None:
        """体积超过所有轴量程 → 抛错。"""
        with pytest.raises(ValueError):
            select_axis(PIP, 1, 1500.0)

    def test_boundary_vol_equals_axis_cap_left(self) -> None:
        """边界：vol == 左轴量程上限仍归左轴。"""
        assert select_axis(PIP, 1, 100.0) == "left"


# ---------------------------------------------------------------------------
# axis_channel_list（通道编号：左[0..n-1]，右[8..8+n-1]）
# ---------------------------------------------------------------------------


class TestAxisChannelList:
    def test_left_single(self) -> None:
        assert axis_channel_list("left", 1) == [0]

    def test_left_eight(self) -> None:
        assert axis_channel_list("left", 8) == [0, 1, 2, 3, 4, 5, 6, 7]

    def test_right_single(self) -> None:
        assert axis_channel_list("right", 1) == [8]

    def test_right_eight(self) -> None:
        assert axis_channel_list("right", 8) == [8, 9, 10, 11, 12, 13, 14, 15]

    def test_non_positive_raises(self) -> None:
        with pytest.raises(ValueError):
            axis_channel_list("left", 0)


# ---------------------------------------------------------------------------
# axis_from_channels（范围反推：左 0-7，右 8-15）
# ---------------------------------------------------------------------------


class TestAxisFromChannels:
    def test_left_eight(self) -> None:
        assert axis_from_channels([0, 1, 2, 3, 4, 5, 6, 7]) == "Left"

    def test_left_single(self) -> None:
        assert axis_from_channels([0]) == "Left"

    def test_right_single(self) -> None:
        assert axis_from_channels([8]) == "Right"

    def test_right_eight(self) -> None:
        assert axis_from_channels([8, 9, 10, 11, 12, 13, 14, 15]) == "Right"

    def test_cross_axis_raises(self) -> None:
        with pytest.raises(ValueError):
            axis_from_channels([7, 8])

    def test_out_of_range_raises(self) -> None:
        with pytest.raises(ValueError):
            axis_from_channels([16])

    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError):
            axis_from_channels([])


# ---------------------------------------------------------------------------
# Backend：_axis_from_channels / _effective_num_channels
# ---------------------------------------------------------------------------


def _make_backend(pip_setting: Any = None, num_channels: int = 8, active_axis: Any = None) -> Any:
    """构造 ``PRCXI9300Backend`` 实例但跳过 ``__init__``（不建 api / 不连网）。"""
    inst: Any = PRCXI9300Backend.__new__(PRCXI9300Backend)
    inst.pip_setting = pip_setting
    inst._num_channels = num_channels
    inst._active_axis = active_axis
    return inst


@_skip_if_no_plr
class TestBackendAxisMapping:
    """pip_setting 模式下 backend 凭 ``_active_axis`` 判轴（不再解码通道下标）。"""

    def test_legacy_left_right(self) -> None:
        be = _make_backend(pip_setting=None)
        assert be._axis_from_channels([0]) == "Left"
        assert be._axis_from_channels([1]) == "Right"

    def test_legacy_invalid_raises(self) -> None:
        be = _make_backend(pip_setting=None)
        with pytest.raises(ValueError):
            be._axis_from_channels([2])

    def test_pip_setting_uses_active_axis(self) -> None:
        be = _make_backend(pip_setting=normalize_pip_setting(PIP), active_axis="Right")
        # use_channels 已是 0-based，不参与判轴；以 _active_axis 为准
        assert be._axis_from_channels([0]) == "Right"
        be._active_axis = "Left"
        assert be._axis_from_channels([0, 1, 2, 3, 4, 5, 6, 7]) == "Left"

    def test_pip_setting_active_axis_default_left(self) -> None:
        be = _make_backend(pip_setting=normalize_pip_setting(PIP), active_axis=None)
        assert be._axis_from_channels([0]) == "Left"  # 默认 Left

    def test_pip_setting_volume_validation_passes(self) -> None:
        be = _make_backend(pip_setting=normalize_pip_setting(PIP), active_axis="Right")
        # 右轴 1000µL，500µL 在量程内
        assert be._axis_from_channels([0], volume=500.0) == "Right"

    def test_pip_setting_volume_over_range_raises(self) -> None:
        be = _make_backend(pip_setting=normalize_pip_setting(PIP), active_axis="Left")
        # 左轴 100µL，150µL 超量程
        with pytest.raises(ValueError):
            be._axis_from_channels([0], volume=150.0)

    def test_effective_num_channels_legacy(self) -> None:
        be = _make_backend(pip_setting=None, num_channels=8)
        assert be._effective_num_channels([0]) == 8  # legacy: 全局 num_channels

    def test_effective_num_channels_left_axis(self) -> None:
        be = _make_backend(pip_setting=normalize_pip_setting(PIP), active_axis="Left")
        assert be._effective_num_channels([0, 1, 2, 3, 4, 5, 6, 7]) == 8

    def test_effective_num_channels_right_axis(self) -> None:
        be = _make_backend(pip_setting=normalize_pip_setting(PIP), active_axis="Right")
        # 右轴 channels=1 → 单通道（use_channels 已翻译为 0-based [0]）
        assert be._effective_num_channels([0]) == 1


# ---------------------------------------------------------------------------
# E2E：transfer_liquid pip_setting 路由（mock super().transfer_liquid 捕获入参）
# ---------------------------------------------------------------------------


@dataclass
class _DummyWell:
    name: str
    parent: Optional[Any] = None

    def get_size_x(self) -> float:
        return 9.0

    def get_size_z(self) -> float:
        return 10.0


@dataclass
class _DummyPlate:
    name: str
    children: List[Any] = field(default_factory=lambda: [_DummyWell("w0")])
    parent: Optional[Any] = None


@dataclass
class _FakeApiClient:
    sent: List[Any] = field(default_factory=list)

    def update_pipetting_position(self, matrix_id: str, positions: List[Any]) -> None:
        self.sent.append((matrix_id, positions))


@dataclass
class _FakeBackend:
    matrix_id: str = "matrix-1"
    api_client: _FakeApiClient = field(default_factory=_FakeApiClient)
    _active_axis: Optional[str] = None


def _make_tip_rack() -> Any:
    """真实 PRCXI 300µL tip rack（newer PLR 下不能再用 ``TipRack`` 子类 + 直接赋值的 stub）。"""
    return PRCXI_300ul_Tips("tr")


def _make_fake_prcxi(*, pip_setting: Any) -> Any:
    inst: Any = PRCXI9300Handler.__new__(PRCXI9300Handler)
    inst._first_transfer_done = True
    inst.step_mode = False
    inst.has_true_8channel = False
    inst.pip_setting = normalize_pip_setting(pip_setting)
    inst._tip_reuse_by_liquid_name = True
    inst.tip_height = 0
    inst._unilabos_backend = _FakeBackend()
    inst.x_increase = -0.003636
    inst.right_2_left = Coordinate(22, -1, 12)

    async def _identity_resolve(resources: Any) -> Any:
        return list(resources) if not isinstance(resources, list) else resources

    inst._resolve_to_plr_resources = _identity_resolve  # type: ignore[assignment]
    inst._attach_resources_to_deck_if_needed = lambda *a, **kw: None  # type: ignore[assignment]
    inst._get_slot_number = lambda *a, **kw: 1  # type: ignore[assignment]
    inst.plr_pos_to_prcxi = lambda well: Coordinate(0.0, 0.0, 0.0)  # type: ignore[assignment]
    # change_slots 位置计算依赖真实 PLR 资源内部；本测试聚焦轴路由（use_channels/flatten），
    # 故 stub 掉位置计算，避免触碰 PLR Resource 内部（与硬件/PLR 版本无关）。
    inst._recover_height = lambda *a, **kw: 0.0  # type: ignore[assignment]
    inst._support_free_prcxi_z = lambda *a, **kw: 0.0  # type: ignore[assignment]
    return inst


class _SuperCallCapture:
    def __init__(self) -> None:
        self.calls: List[Tuple[Tuple[Any, ...], dict]] = []

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, dict(kwargs)))
        return "OK"

    @property
    def last_kwargs(self) -> dict:
        assert self.calls, "super().transfer_liquid was not called"
        return self.calls[-1][1]


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


@_skip_if_no_plr
class TestPrcxiPipSettingRouting:
    def test_multi_channel_small_vol_routes_left_no_flatten(self) -> None:
        """8 通道 + 50µL → 左轴并行，不扁平化，super 收到 use_channels=[0..7]，长度不变。"""
        prcxi = _make_fake_prcxi(pip_setting=PIP)
        sources = [_DummyWell(f"S{i}", parent=_DummyPlate("p_src")) for i in range(24)]
        targets = [_DummyWell(f"T{i}", parent=_DummyPlate("p_tgt")) for i in range(24)]
        cap = _SuperCallCapture()
        with patch.object(LiquidHandlerAbstract, "transfer_liquid", cap):
            _run(
                prcxi.transfer_liquid(
                    sources=sources,
                    targets=targets,
                    tip_racks=[_make_tip_rack()],
                    use_channels=list(range(8)),
                    asp_vols=[50.0] * 24,
                    dis_vols=[50.0] * 24,
                )
            )
        kw = cap.last_kwargs
        assert kw["use_channels"] == [0, 1, 2, 3, 4, 5, 6, 7]
        assert len(kw["asp_vols"]) == 24  # 未扁平化（asp_vols 为 kwarg）

    def test_single_channel_large_vol_routes_right(self) -> None:
        """单通道 + 500µL → 右轴，super 收到 use_channels=[8]。"""
        prcxi = _make_fake_prcxi(pip_setting=PIP)
        sources = [_DummyWell("S0", parent=_DummyPlate("p_src"))]
        targets = [_DummyWell("T0", parent=_DummyPlate("p_tgt"))]
        cap = _SuperCallCapture()
        with patch.object(LiquidHandlerAbstract, "transfer_liquid", cap):
            _run(
                prcxi.transfer_liquid(
                    sources=sources,
                    targets=targets,
                    tip_racks=[_make_tip_rack()],
                    use_channels=[0],
                    asp_vols=[500.0],
                    dis_vols=[500.0],
                )
            )
        assert cap.last_kwargs["use_channels"] == [8]

    def test_single_channel_small_vol_routes_left(self) -> None:
        """单通道 + 50µL → 左轴，super 收到 use_channels=[0]。"""
        prcxi = _make_fake_prcxi(pip_setting=PIP)
        sources = [_DummyWell("S0", parent=_DummyPlate("p_src"))]
        targets = [_DummyWell("T0", parent=_DummyPlate("p_tgt"))]
        cap = _SuperCallCapture()
        with patch.object(LiquidHandlerAbstract, "transfer_liquid", cap):
            _run(
                prcxi.transfer_liquid(
                    sources=sources,
                    targets=targets,
                    tip_racks=[_make_tip_rack()],
                    use_channels=[0],
                    asp_vols=[50.0],
                    dis_vols=[50.0],
                )
            )
        assert cap.last_kwargs["use_channels"] == [0]

    def test_multi_channel_large_vol_falls_back_right_and_flattens(self) -> None:
        """8 通道 + 150µL（超左轴）→ 回退右轴(单通道) + 扁平化 8→1，super 收到 use_channels=[8]。"""
        prcxi = _make_fake_prcxi(pip_setting=PIP)
        sources = [_DummyWell(f"S{i}", parent=_DummyPlate("p_src")) for i in range(24)]
        targets = [_DummyWell(f"T{i}", parent=_DummyPlate("p_tgt")) for i in range(24)]
        cap = _SuperCallCapture()
        with patch.object(LiquidHandlerAbstract, "transfer_liquid", cap):
            _run(
                prcxi.transfer_liquid(
                    sources=sources,
                    targets=targets,
                    tip_racks=[_make_tip_rack()],
                    use_channels=list(range(8)),
                    asp_vols=[150.0] * 24,
                    dis_vols=[150.0] * 24,
                )
            )
        kw = cap.last_kwargs
        assert kw["use_channels"] == [8]  # 右轴单通道
        # 扁平化后逐孔体积全量保留（24 长度，asp_vols 为 kwarg）
        assert len(kw["asp_vols"]) == 24


# ---------------------------------------------------------------------------
# 设备层 op 边界翻译：[8..15] -> 0-based + backend._active_axis（不透传给 PLR）
# ---------------------------------------------------------------------------


@_skip_if_no_plr
class TestRouteAxisAndChannels:
    def test_right_eight_to_zero_based(self) -> None:
        prcxi = _make_fake_prcxi(pip_setting=PIP)
        out = prcxi._route_axis_and_channels([8, 9, 10, 11, 12, 13, 14, 15])
        assert out == [0, 1, 2, 3, 4, 5, 6, 7]
        assert prcxi._unilabos_backend._active_axis == "Right"

    def test_right_single_to_zero(self) -> None:
        prcxi = _make_fake_prcxi(pip_setting=PIP)
        out = prcxi._route_axis_and_channels([8])
        assert out == [0]
        assert prcxi._unilabos_backend._active_axis == "Right"

    def test_left_eight_unchanged(self) -> None:
        prcxi = _make_fake_prcxi(pip_setting=PIP)
        out = prcxi._route_axis_and_channels([0, 1, 2, 3, 4, 5, 6, 7])
        assert out == [0, 1, 2, 3, 4, 5, 6, 7]
        assert prcxi._unilabos_backend._active_axis == "Left"

    def test_left_single_unchanged(self) -> None:
        prcxi = _make_fake_prcxi(pip_setting=PIP)
        out = prcxi._route_axis_and_channels([0])
        assert out == [0]
        assert prcxi._unilabos_backend._active_axis == "Left"

    def test_no_pip_setting_passthrough(self) -> None:
        prcxi = _make_fake_prcxi(pip_setting=PIP)
        prcxi.pip_setting = None  # 模拟未配置
        prcxi._unilabos_backend._active_axis = None
        out = prcxi._route_axis_and_channels([8])
        assert out == [8]  # 原样（legacy）
        assert prcxi._unilabos_backend._active_axis is None

    def test_empty_passthrough(self) -> None:
        prcxi = _make_fake_prcxi(pip_setting=PIP)
        assert prcxi._route_axis_and_channels(None) is None
        assert prcxi._route_axis_and_channels([]) == []


# ---------------------------------------------------------------------------
# 回归：discard_tips → (PLR 回调) drop_tips 不再二次路由覆写 _active_axis
# ---------------------------------------------------------------------------


@_skip_if_no_plr
class TestDropTipsNoReroute:
    def test_drop_tips_keeps_active_axis(self) -> None:
        """drop_tips（PLR discard_tips 回调，通道已是 0-based）不再路由，_active_axis 保持。"""
        prcxi = _make_fake_prcxi(pip_setting=PIP)
        be = prcxi._unilabos_backend
        be._active_axis = "Right"  # 模拟上游 discard_tips 已置 Right

        route_calls = {"n": 0}
        orig_route = prcxi._route_axis_and_channels

        def _spy_route(uc: Any) -> Any:
            route_calls["n"] += 1
            return orig_route(uc)

        prcxi._route_axis_and_channels = _spy_route  # type: ignore[assignment]

        super_calls = {"n": 0}

        async def _super_drop(self_: Any, *a: Any, **k: Any) -> Any:
            super_calls["n"] += 1

        with patch.object(LiquidHandlerMiddleware, "drop_tips", _super_drop):
            _run(prcxi.drop_tips([], use_channels=[0, 1, 2, 3, 4, 5, 6, 7]))

        assert route_calls["n"] == 0, "drop_tips 不应再调用 _route_axis_and_channels"
        assert super_calls["n"] == 1
        assert be._active_axis == "Right", "drop_tips 不应把 _active_axis 覆写成 Left"

    def test_discard_tips_still_sets_right(self) -> None:
        """discard_tips（顶层入口，收到 [8..15]）仍正确置 _active_axis=Right。"""
        prcxi = _make_fake_prcxi(pip_setting=PIP)
        be = prcxi._unilabos_backend
        be._active_axis = None

        captured = {"use_channels": None}

        async def _super_discard(self_: Any, use_channels: Any = None, *a: Any, **k: Any) -> Any:
            captured["use_channels"] = use_channels

        with patch.object(LiquidHandlerMiddleware, "discard_tips", _super_discard):
            _run(prcxi.discard_tips(use_channels=[8, 9, 10, 11, 12, 13, 14, 15]))

        assert be._active_axis == "Right"
        # discard_tips 应把 [8..15] 翻译成 0-based 传给 super（PLR）
        assert captured["use_channels"] == [0, 1, 2, 3, 4, 5, 6, 7]


# ---------------------------------------------------------------------------
# transfer_liquid 出错自动清理残留 tip（无需重启 edge）
# ---------------------------------------------------------------------------


@_skip_if_no_plr
class TestTransferCleanupOnError:
    """``_cleanup_after_failed_transfer`` 行为 + ``transfer_liquid`` 失败时触发清理。"""

    def test_cleanup_discards_and_clears_when_tip_mounted(self) -> None:
        """有残留 tip → discard_tips（丢 trash）+ clear_head_state 都被调用。"""
        prcxi = _make_fake_prcxi(pip_setting=PIP)
        calls = {"discard": 0, "clear": 0}
        prcxi.get_mounted_tips = lambda: [object(), None]  # type: ignore[assignment]

        async def _discard(*a: Any, **k: Any) -> Any:
            calls["discard"] += 1

        prcxi.discard_tips = _discard  # type: ignore[assignment]
        prcxi.clear_head_state = lambda: calls.__setitem__("clear", calls["clear"] + 1)  # type: ignore[assignment]

        _run(prcxi._cleanup_after_failed_transfer())

        assert calls["discard"] == 1
        assert calls["clear"] == 1

    def test_cleanup_skips_discard_when_no_tip(self) -> None:
        """无残留 tip → 不调用 discard_tips，但仍 clear_head_state 兜底。"""
        prcxi = _make_fake_prcxi(pip_setting=PIP)
        calls = {"discard": 0, "clear": 0}
        prcxi.get_mounted_tips = lambda: [None, None]  # type: ignore[assignment]

        async def _discard(*a: Any, **k: Any) -> Any:
            calls["discard"] += 1

        prcxi.discard_tips = _discard  # type: ignore[assignment]
        prcxi.clear_head_state = lambda: calls.__setitem__("clear", calls["clear"] + 1)  # type: ignore[assignment]

        _run(prcxi._cleanup_after_failed_transfer())

        assert calls["discard"] == 0
        assert calls["clear"] == 1

    def test_cleanup_swallows_discard_error_and_still_clears(self) -> None:
        """物理丢弃报错（如机器实际没夹 tip）被吞掉，clear_head_state 仍执行。"""
        prcxi = _make_fake_prcxi(pip_setting=PIP)
        calls = {"clear": 0}
        prcxi.get_mounted_tips = lambda: [object()]  # type: ignore[assignment]

        async def _discard(*a: Any, **k: Any) -> Any:
            raise RuntimeError("device has no physical tip")

        prcxi.discard_tips = _discard  # type: ignore[assignment]
        prcxi.clear_head_state = lambda: calls.__setitem__("clear", calls["clear"] + 1)  # type: ignore[assignment]

        # 不应抛出
        _run(prcxi._cleanup_after_failed_transfer())

        assert calls["clear"] == 1

    def test_cleanup_swallows_get_mounted_tips_error(self) -> None:
        """get_mounted_tips 自身报错 → 视为无 tip，仍 clear_head_state，不抛出。"""
        prcxi = _make_fake_prcxi(pip_setting=PIP)
        calls = {"discard": 0, "clear": 0}

        def _raise_mounted() -> Any:
            raise RuntimeError("head not ready")

        prcxi.get_mounted_tips = _raise_mounted  # type: ignore[assignment]

        async def _discard(*a: Any, **k: Any) -> Any:
            calls["discard"] += 1

        prcxi.discard_tips = _discard  # type: ignore[assignment]
        prcxi.clear_head_state = lambda: calls.__setitem__("clear", calls["clear"] + 1)  # type: ignore[assignment]

        _run(prcxi._cleanup_after_failed_transfer())

        assert calls["discard"] == 0
        assert calls["clear"] == 1

    def test_transfer_error_triggers_cleanup_and_reraises(self) -> None:
        """transfer_liquid 中途异常 → _cleanup_after_failed_transfer 被调用一次且原异常透传。"""
        prcxi = _make_fake_prcxi(pip_setting=PIP)
        spy = {"n": 0}

        async def _cleanup() -> None:
            spy["n"] += 1

        prcxi._cleanup_after_failed_transfer = _cleanup  # type: ignore[assignment]

        class _Boom(Exception):
            pass

        async def _raise_super(self_: Any, *a: Any, **k: Any) -> Any:
            raise _Boom("fail mid transfer")

        sources = [_DummyWell("S0", parent=_DummyPlate("p_src"))]
        targets = [_DummyWell("T0", parent=_DummyPlate("p_tgt"))]
        with patch.object(LiquidHandlerAbstract, "transfer_liquid", _raise_super):
            with pytest.raises(_Boom):
                _run(
                    prcxi.transfer_liquid(
                        sources=sources,
                        targets=targets,
                        tip_racks=[_make_tip_rack()],
                        use_channels=[0],
                        asp_vols=[50.0],
                        dis_vols=[50.0],
                    )
                )

        assert spy["n"] == 1
