"""PRCXI 96 孔移液、悬停传参与 titration 单测。"""

from __future__ import annotations

import asyncio
from typing import Any, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

try:
    from unilabos.devices.liquid_handling.liquid_handler_abstract import (
        LiquidHandlerAbstract,
        TransferLiquidReturn,
        _build_dispense_hover_kwargs,
    )

    _ABSTRACT_AVAILABLE = True
    _ABSTRACT_IMPORT_ERROR: Optional[Exception] = None
except Exception as exc:  # pragma: no cover
    LiquidHandlerAbstract = None  # type: ignore[assignment, misc]
    TransferLiquidReturn = None  # type: ignore[assignment, misc]
    _build_dispense_hover_kwargs = None  # type: ignore[assignment, misc]
    _ABSTRACT_AVAILABLE = False
    _ABSTRACT_IMPORT_ERROR = exc

try:
    from pylabrobot.resources import Plate

    from unilabos.devices.liquid_handling.prcxi.prcxi import (
        PRCXI9300Backend,
        PRCXI9300Handler,
    )

    _PRCXI_AVAILABLE = True
    _PRCXI_IMPORT_ERROR: Optional[Exception] = None
except Exception as exc:  # pragma: no cover
    Plate = None  # type: ignore[assignment, misc]
    PRCXI9300Backend = None  # type: ignore[assignment, misc]
    PRCXI9300Handler = None  # type: ignore[assignment, misc]
    _PRCXI_AVAILABLE = False
    _PRCXI_IMPORT_ERROR = exc

_skip_if_no_abstract = pytest.mark.skipif(
    not _ABSTRACT_AVAILABLE,
    reason=f"liquid_handler_abstract not importable: {_ABSTRACT_IMPORT_ERROR!r}",
)
_skip_if_no_prcxi = pytest.mark.skipif(
    not _PRCXI_AVAILABLE,
    reason=f"PRCXI handler/backend not importable: {_PRCXI_IMPORT_ERROR!r}",
)


def _run(coro):
    return asyncio.run(coro)


@_skip_if_no_abstract
class TestBuildDispenseHoverKwargs:
    def test_assembles_all_fields(self) -> None:
        out = _build_dispense_hover_kwargs(
            dispensing_method=["DiveToBottom"],
            hover_below_liquid_level=[5],
            z_start_point_offset_height=[2],
            post_discharge_pause_time_ms=[100],
            dis_flow_rates=[120.0],
        )
        assert out == {
            "dispensing_method": "DiveToBottom",
            "hover_below_liquid_level": 5,
            "z_start_point_offset_height": 2,
            "post_discharge_pause_time_ms": 100,
            "dispense_speed": 120,
        }

    def test_skips_empty_values(self) -> None:
        assert _build_dispense_hover_kwargs() == {}

    def test_liquid_height_fallback_for_hover(self) -> None:
        out = _build_dispense_hover_kwargs(liquid_height=[3.7])
        assert out["hover_below_liquid_level"] == 3


if _ABSTRACT_AVAILABLE:

    class _Fake96Handler(LiquidHandlerAbstract):
        """记录 96 头 API 调用序列。"""

        def __init__(self) -> None:
            self.calls: List[str] = []

        async def pick_up_tips96(self, tip_rack, offset=None, **kwargs):
            self.calls.append("pick_up_tips96")

        async def drop_tips96(self, tip_rack, offset=None, **kwargs):
            self.calls.append("drop_tips96")

        async def aspirate96(self, resource, volume, offset=None, **kwargs):
            self.calls.append("aspirate96")

        async def dispense96(self, resource, volume, offset=None, **kwargs):
            self.calls.append("dispense96")

        async def mix96(self, plate, mix_time, **kwargs):
            self.calls.append("mix96")


@_skip_if_no_abstract
class TestTransferLiquidDefaultPath:
    def test_default_does_not_call_96well_helper(self) -> None:
        handler = _Fake96Handler()
        plate = MagicMock(spec=Plate)
        plate.parent = MagicMock()
        tip_rack = MagicMock()
        tip_rack.parent = MagicMock()

        with patch.object(
            LiquidHandlerAbstract,
            "_transfer_liquid_96well",
            new_callable=AsyncMock,
        ) as mock_96:
            with patch.object(
                LiquidHandlerAbstract,
                "_transfer_liquid",
                new_callable=AsyncMock,
                return_value=TransferLiquidReturn(sources=[], targets=[]),
            ):
                _run(
                    handler.transfer_liquid(
                        [plate],
                        [plate],
                        [tip_rack],
                        asp_vols=10.0,
                        dis_vols=10.0,
                    )
                )
            mock_96.assert_not_called()


@_skip_if_no_abstract
class TestTransferLiquid96WellPath:
    def test_96_path_calls_pickup_asp_disp_drop(self) -> None:
        handler = _Fake96Handler()
        plate = MagicMock(spec=Plate)
        plate.parent = MagicMock()
        tip_rack = MagicMock()
        tip_rack.parent = MagicMock()

        _run(
            handler._transfer_liquid_96well(
                [plate],
                [plate],
                [tip_rack],
                asp_vols=50.0,
                dis_vols=50.0,
            )
        )
        assert handler.calls == [
            "pick_up_tips96",
            "aspirate96",
            "dispense96",
            "drop_tips96",
        ]


@_skip_if_no_prcxi
class TestBackendHoverFields:
    def test_tapping_hover_step_fields(self) -> None:
        backend = PRCXI9300Backend.__new__(PRCXI9300Backend)
        opts = {
            "dispensing_method": "Offset",
            "hover_below_liquid_level": 4,
            "z_start_point_offset_height": 1,
            "post_discharge_pause_time_ms": 50,
            "dispense_speed": 80,
        }
        fields = backend._tapping_hover_step_fields(opts)
        assert fields["dispensing_method_v04"] == "Offset"
        assert fields["hover_below_liquid_level"] == 4
        assert fields["z_start_point_offset_height"] == 1
        assert fields["post_discharge_pause_time_ms"] == 50
        assert fields["dosage_speed"] == 80


@_skip_if_no_prcxi
class TestHandlerDispense96HoverCtx:
    def test_dispense96_stashes_hover_options(self) -> None:
        handler = PRCXI9300Handler.__new__(PRCXI9300Handler)
        backend = PRCXI9300Backend.__new__(PRCXI9300Backend)
        backend.api_client = MagicMock(is_v04=False)
        handler._unilabos_backend = backend

        with patch.object(
            LiquidHandlerAbstract,
            "dispense96",
            new_callable=AsyncMock,
        ):
            _run(
                handler.dispense96(
                    MagicMock(),
                    10.0,
                    dispensing_method="DiveToBottom",
                    hover_below_liquid_level=3,
                    z_start_point_offset_height=2,
                    post_discharge_pause_time_ms=100,
                )
            )

        assert backend._ctx_dispense_options == {
            "dispensing_method": "DiveToBottom",
            "hover_below_liquid_level": 3,
            "z_start_point_offset_height": 2,
            "post_discharge_pause_time_ms": 100,
        }


@_skip_if_no_abstract
class TestTitrationLiquid:
    def test_repeat_count_three_runs_three_cycles(self) -> None:
        handler = _Fake96Handler()

        async def _asp96(*args, **kwargs):
            handler.calls.append("aspirate96")

        async def _disp96(*args, **kwargs):
            handler.calls.append("dispense96")

        handler.aspirate96 = _asp96  # type: ignore[method-assign]
        handler.dispense96 = _disp96  # type: ignore[method-assign]

        plate = MagicMock(spec=Plate)
        plate.parent = MagicMock()
        tip_rack = MagicMock()
        tip_rack.parent = MagicMock()

        _run(
            handler._titration_liquid_96well(
                [plate],
                [plate],
                [tip_rack],
                asp_vols=5.0,
                dis_vols=5.0,
                repeat_count=3,
            )
        )
        assert handler.calls.count("aspirate96") == 3
        assert handler.calls.count("dispense96") == 3
        assert handler.calls[0] == "pick_up_tips96"
        assert handler.calls[-1] == "drop_tips96"


@_skip_if_no_prcxi
class TestV03BlowOutBeforeIgnored:
    def test_aspirate96_v03_ignores_reverse_imbibing(self, capsys) -> None:
        handler = PRCXI9300Handler.__new__(PRCXI9300Handler)
        backend = PRCXI9300Backend.__new__(PRCXI9300Backend)
        backend.api_client = MagicMock(is_v04=False)
        handler._unilabos_backend = backend

        with patch.object(
            LiquidHandlerAbstract,
            "aspirate96",
            new_callable=AsyncMock,
        ):
            _run(
                handler.aspirate96(
                    MagicMock(),
                    10.0,
                    blow_out_air_volume_before=2.5,
                )
            )

        assert backend._ctx_reverse_imbibing is None
        captured = capsys.readouterr()
        assert "v03" in captured.out
