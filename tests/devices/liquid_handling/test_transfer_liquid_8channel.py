"""LiquidHandlerAbstract 8 通道 transfer_liquid 分支测试。

覆盖：
  - ``transfer_liquid`` 在 ``use_channels==[0..7]`` 时按列（每 8 孔一组）调用
    ``_transfer_base_method``，每列 pick_up/drop=True、入参为对应列的 8 元素切片。
  - ``_transfer_base_method`` 多通道路径取 ``n_ch`` 个 tip 工位（每通道一个）。

测试用 ``__new__`` 绕过 ``__init__``，并 patch 掉下层 PLR 调用，避免构造真实
PLR 资源（newer PLR 下 stub 资源构造已不可用）。依赖 PRCXI/PLR import 链，
环境无 PLR 时整体 skip。
"""

from __future__ import annotations

import asyncio
from typing import Any, List, Optional, Tuple
from unittest.mock import patch

import pytest

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


_skip_if_no_plr = pytest.mark.skipif(
    not _PLR_AVAILABLE,
    reason=f"pylabrobot not importable in this env: {_PLR_IMPORT_ERROR!r}",
)


class _Well:
    """轻量 well 占位（_transfer_base_method 被 mock，不触碰真实属性）。"""

    def __init__(self, name: str) -> None:
        self.name = name


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# transfer_liquid 末尾用 ResourceTreeSet.from_plr_resources 序列化返回值，会触碰真实
# PLR 资源属性；本测试聚焦分发逻辑（_transfer_base_method 调用），故 patch 掉序列化。
def _patch_serialize() -> Any:
    return patch(
        "unilabos.devices.liquid_handling.liquid_handler_abstract.ResourceTreeSet"
    )


@_skip_if_no_plr
class TestTransferLiquid8Channel:
    def _make_handler(self) -> Any:
        inst: Any = LiquidHandlerAbstract.__new__(LiquidHandlerAbstract)
        inst.channel_num = 8

        async def _identity_resolve(resources: Any) -> Any:
            return list(resources) if not isinstance(resources, list) else resources

        inst._resolve_to_plr_resources = _identity_resolve  # type: ignore[assignment]
        inst.set_tiprack = lambda tip_racks: None  # type: ignore[assignment]
        return inst

    def test_8channel_dispatches_per_column(self) -> None:
        """16 sources/targets（2 列）→ _transfer_base_method 调用 2 次，列切片正确。"""
        handler = self._make_handler()
        sources = [_Well(f"S{i}") for i in range(16)]
        targets = [_Well(f"T{i}") for i in range(16)]
        asp_vols = [float(i) for i in range(16)]
        dis_vols = [float(100 + i) for i in range(16)]

        calls: List[Tuple[Tuple[Any, ...], dict]] = []

        async def _capture(*args: Any, **kwargs: Any) -> Any:
            calls.append((args, dict(kwargs)))

        handler._transfer_base_method = _capture  # type: ignore[assignment]

        with _patch_serialize():
            _run(
                handler.transfer_liquid(
                    sources=sources,
                    targets=targets,
                    tip_racks=[object()],
                    use_channels=list(range(8)),
                    asp_vols=asp_vols,
                    dis_vols=dis_vols,
                )
            )

        assert len(calls) == 2, f"应每 8 孔一列调用一次，实际 {len(calls)}"

        for col, (_, kw) in enumerate(calls):
            base = col * 8
            assert kw["pick_up"] is True
            assert kw["drop"] is True
            assert kw["use_channels"] == list(range(8))
            assert len(kw["sources"]) == 8
            assert len(kw["targets"]) == 8
            assert kw["sources"] == sources[base:base + 8]
            assert kw["targets"] == targets[base:base + 8]
            assert kw["asp_vols"] == asp_vols[base:base + 8]
            assert kw["dis_vols"] == dis_vols[base:base + 8]

    def test_8channel_optional_params_sliced_per_column(self) -> None:
        """可选 per-well 参数（offsets/flow_rates）按列切成 8 元素。"""
        handler = self._make_handler()
        sources = [_Well(f"S{i}") for i in range(16)]
        targets = [_Well(f"T{i}") for i in range(16)]
        asp_flow = [float(i) for i in range(16)]

        calls: List[dict] = []

        async def _capture(*args: Any, **kwargs: Any) -> Any:
            calls.append(dict(kwargs))

        handler._transfer_base_method = _capture  # type: ignore[assignment]

        with _patch_serialize():
            _run(
                handler.transfer_liquid(
                    sources=sources,
                    targets=targets,
                    tip_racks=[object()],
                    use_channels=list(range(8)),
                    asp_vols=[10.0] * 16,
                    dis_vols=[10.0] * 16,
                    asp_flow_rates=asp_flow,
                )
            )

        assert len(calls) == 2
        assert calls[0]["asp_flow_rates"] == asp_flow[0:8]
        assert calls[1]["asp_flow_rates"] == asp_flow[8:16]

    def test_8channel_distribute_targets_equal_M(self) -> None:
        """distribute：sources=8（单列）、targets=9（M 个锚）、vols=72（8×M）。

        复现 00222e-dd/Drug Dosing 真实形态：旧实现因 targets=9 不被 8 整除而 ValueError。
        修复（见 01-multi-channel-flatten.md §6.1）后应放行并按组分发：M=9 次
        _transfer_base_method，sources 整列 tile 复用、targets 每锚复制 8、vols 取 8 切片。
        """
        handler = self._make_handler()
        sources = [_Well(f"S{i}") for i in range(8)]  # 单列 8 孔
        targets = [_Well(f"T{i}") for i in range(9)]  # 9 个锚 = M
        asp_vols = [float(i) for i in range(72)]      # 8×M
        dis_vols = [float(100 + i) for i in range(72)]

        calls: List[dict] = []

        async def _capture(*args: Any, **kwargs: Any) -> Any:
            calls.append(dict(kwargs))

        handler._transfer_base_method = _capture  # type: ignore[assignment]

        with _patch_serialize():
            _run(
                handler.transfer_liquid(
                    sources=sources,
                    targets=targets,
                    tip_racks=[object()],
                    use_channels=list(range(8)),
                    asp_vols=asp_vols,
                    dis_vols=dis_vols,
                )
            )

        assert len(calls) == 9, f"M=9 应分发 9 次，实际 {len(calls)}"
        # 第 0 组：sources 整列复用、targets 每锚复制 8、vols 取 [0:8]
        assert calls[0]["sources"] == sources[0:8]
        assert calls[0]["targets"] == [targets[0]] * 8
        assert calls[0]["asp_vols"] == asp_vols[0:8]
        assert calls[0]["dis_vols"] == dis_vols[0:8]
        # 第 8 组（最后一组）
        assert calls[8]["sources"] == sources[0:8]
        assert calls[8]["targets"] == [targets[8]] * 8
        assert calls[8]["dis_vols"] == dis_vols[64:72]

    def test_8channel_requires_vols_divisible_by_8(self) -> None:
        """vols 非 8 倍数（=9）应报错；targets=9 不再因此被拒。"""
        handler = self._make_handler()
        sources = [_Well(f"S{i}") for i in range(9)]
        targets = [_Well(f"T{i}") for i in range(9)]

        async def _capture(*args: Any, **kwargs: Any) -> Any:
            return None

        handler._transfer_base_method = _capture  # type: ignore[assignment]

        with _patch_serialize(), pytest.raises(ValueError, match="divisible by 8"):
            _run(
                handler.transfer_liquid(
                    sources=sources,
                    targets=targets,
                    tip_racks=[object()],
                    use_channels=list(range(8)),
                    asp_vols=[1.0] * 9,
                    dis_vols=[1.0] * 9,
                )
            )


@_skip_if_no_plr
class TestTransferBaseMethodMultiChannelTips:
    def test_multi_channel_picks_n_ch_tips(self) -> None:
        """多通道 pick_up 取 n_ch=8 个 tip 工位（每通道一个）。"""
        inst: Any = LiquidHandlerAbstract.__new__(LiquidHandlerAbstract)

        tip_calls = {"count": 0}

        def _next_tip() -> Any:
            tip_calls["count"] += 1
            return _Well(f"tip{tip_calls['count']}")

        picked: List[Any] = []

        async def _pick(tip: Any, use_channels: Any = None, *a: Any, **k: Any) -> Any:
            picked.append(list(tip))

        async def _noop(*a: Any, **k: Any) -> Any:
            return None

        inst._get_next_tip = _next_tip  # type: ignore[assignment]
        inst.pick_up_tips = _pick  # type: ignore[assignment]
        inst.aspirate = _noop  # type: ignore[assignment]
        inst.dispense = _noop  # type: ignore[assignment]
        inst.touch_tip = _noop  # type: ignore[assignment]
        inst.discard_tips = _noop  # type: ignore[assignment]

        sources = [_Well(f"S{i}") for i in range(8)]
        targets = [_Well(f"T{i}") for i in range(8)]

        _run(
            inst._transfer_base_method(
                sources=sources,
                targets=targets,
                tip_racks=[object()],
                use_channels=list(range(8)),
                asp_vols=[10.0] * 8,
                dis_vols=[10.0] * 8,
                pick_up=True,
                drop=True,
            )
        )

        assert tip_calls["count"] == 8, f"8 通道应取 8 个 tip，实际 {tip_calls['count']}"
        assert len(picked) == 1 and len(picked[0]) == 8

    def test_single_channel_picks_one_tip(self) -> None:
        """单通道 pick_up 仍只取 1 个 tip（无回归）。"""
        inst: Any = LiquidHandlerAbstract.__new__(LiquidHandlerAbstract)

        tip_calls = {"count": 0}

        def _next_tip() -> Any:
            tip_calls["count"] += 1
            return _Well(f"tip{tip_calls['count']}")

        async def _noop(*a: Any, **k: Any) -> Any:
            return None

        inst._get_next_tip = _next_tip  # type: ignore[assignment]
        inst.pick_up_tips = _noop  # type: ignore[assignment]
        inst.aspirate = _noop  # type: ignore[assignment]
        inst.dispense = _noop  # type: ignore[assignment]
        inst.touch_tip = _noop  # type: ignore[assignment]
        inst.discard_tips = _noop  # type: ignore[assignment]

        _run(
            inst._transfer_base_method(
                sources=[_Well("S0")],
                targets=[_Well("T0")],
                tip_racks=[object()],
                use_channels=[0],
                asp_vols=[10.0],
                dis_vols=[10.0],
                pick_up=True,
                drop=True,
            )
        )

        assert tip_calls["count"] == 1
