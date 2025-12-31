"""
PRCXI transfer_liquid 集成测试。

这些用例会启动 UniLiquidHandler RViz 仿真 backend，需要同时满足：
1. 安装 pylabrobot 依赖；
2. 设置环境变量 UNILAB_SIM_TEST=1；
3. 具备 ROS 运行环境（rviz_backend 会创建 ROS 节点）。
"""
import asyncio
import os
from dataclasses import dataclass
from typing import List, Sequence

import pytest

from unilabos.devices.liquid_handling.liquid_handler_abstract import LiquidHandlerAbstract
from unilabos.devices.liquid_handling.prcxi.prcxi import PRCXI9300Deck, PRCXI9300Trash
from unilabos.devices.liquid_handling.prcxi.prcxi_labware import (
    PRCXI_300ul_Tips,
    PRCXI_BioER_96_wellplate,
)

pytestmark = pytest.mark.slow

try:
    from pylabrobot.resources import Coordinate, Deck, Plate, TipRack, Well
except ImportError:  # pragma: no cover - 测试环境缺少 pylabrobot 时直接跳过
    Coordinate = Deck = Plate = TipRack = Well = None  # type: ignore[assignment]
    PYLABROBOT_AVAILABLE = False
else:
    PYLABROBOT_AVAILABLE = True

SIM_ENV_VAR = "UNILAB_SIM_TEST"


@dataclass
class SimulationContext:
    handler: LiquidHandlerAbstract
    deck: Deck
    tip_rack: TipRack
    source_plate: Plate
    target_plate: Plate
    waste_plate: Plate
    channel_num: int


def run(coro):
    return asyncio.run(coro)


def _ensure_unilabos_extra(well: Well) -> None:
    if not hasattr(well, "unilabos_extra") or well.unilabos_extra is None:
        well.unilabos_extra = {}  # type: ignore[attr-defined]


def _assign_sample_uuid(well: Well, value: str) -> None:
    _ensure_unilabos_extra(well)
    well.unilabos_extra["sample_uuid"] = value  # type: ignore[attr-defined]


def _zero_coordinate() -> Coordinate:
    if hasattr(Coordinate, "zero"):
        return Coordinate.zero()
    return Coordinate(0, 0, 0)


def _zero_offsets(count: int) -> List[Coordinate]:
    return [_zero_coordinate() for _ in range(count)]


def _build_simulation_deck() -> tuple[PRCXI9300Deck, TipRack, Plate, Plate, Plate, PRCXI9300Trash]:
    deck = PRCXI9300Deck(name="PRCXI_Deck", size_x=542, size_y=374, size_z=50)
    tip_rack = PRCXI_300ul_Tips("Tips")
    source_plate = PRCXI_BioER_96_wellplate("SourcePlate")
    target_plate = PRCXI_BioER_96_wellplate("TargetPlate")
    waste_plate = PRCXI_BioER_96_wellplate("WastePlate")
    trash = PRCXI9300Trash(name="trash", size_x=100, size_y=100, size_z=50)
    deck.assign_child_resource(tip_rack, location=Coordinate(0, 0, 0))
    deck.assign_child_resource(source_plate, location=Coordinate(150, 0, 0))
    deck.assign_child_resource(target_plate, location=Coordinate(300, 0, 0))
    deck.assign_child_resource(waste_plate, location=Coordinate(450, 0, 0))
    deck.assign_child_resource(trash, location=Coordinate(150, -120, 0))
    return deck, tip_rack, source_plate, target_plate, waste_plate, trash


def _stop_backend(handler: LiquidHandlerAbstract) -> None:
    try:
        run(handler.backend.stop())
    except Exception:  # pragma: no cover - 如果 backend 已经停止
        pass
    simulate_handler = getattr(handler, "_simulate_handler", None)
    if simulate_handler is not None and getattr(simulate_handler, "backend", None) is not None:
        try:
            run(simulate_handler.backend.stop())
        except Exception:  # pragma: no cover
            pass


@pytest.fixture(params=[1, 8])
def prcxi_simulation(request) -> SimulationContext:
    if not PYLABROBOT_AVAILABLE:
        pytest.skip("pylabrobot is required for PRCXI simulation tests.")
    if os.environ.get(SIM_ENV_VAR) != "1":
        pytest.skip(f"Set {SIM_ENV_VAR}=1 to run PRCXI simulation tests.")

    channel_num = request.param
    deck, tip_rack, source_plate, target_plate, waste_plate, _trash = _build_simulation_deck()
    backend_cfg = {
        "type": "unilabos.devices.liquid_handling.rviz_backend.UniLiquidHandlerRvizBackend",
        "channel_num": channel_num,
        "total_height": 310,
        "lh_device_id": f"pytest_prcxi_{channel_num}",
    }
    handler = LiquidHandlerAbstract(
        backend=backend_cfg,
        deck=deck,
        simulator=True,
        channel_num=channel_num,
        total_height=310,
    )
    run(handler.setup())
    handler.set_tiprack([tip_rack])
    handler.support_touch_tip = False

    context = SimulationContext(
        handler=handler,
        deck=deck,
        tip_rack=tip_rack,
        source_plate=source_plate,
        target_plate=target_plate,
        waste_plate=waste_plate,
        channel_num=channel_num,
    )

    yield context

    _stop_backend(handler)


def _pick_wells(plate: Plate, start: int, count: int) -> List[Well]:
    wells = plate.children[start : start + count]
    for well in wells:
        _ensure_unilabos_extra(well)
    return wells


def _assert_samples_match(sources: Sequence[Well], targets: Sequence[Well]) -> None:
    for src, tgt in zip(sources, targets):
        src_uuid = getattr(src, "unilabos_extra", {}).get("sample_uuid")
        tgt_uuid = getattr(tgt, "unilabos_extra", {}).get("sample_uuid")
        assert tgt_uuid == src_uuid


def test_transfer_liquid_single_channel_one_to_one(prcxi_simulation: SimulationContext):
    if prcxi_simulation.channel_num != 1:
        pytest.skip("仅在单通道配置下运行")

    handler = prcxi_simulation.handler
    for well in prcxi_simulation.source_plate.children + prcxi_simulation.target_plate.children:
        _ensure_unilabos_extra(well)
    sources = prcxi_simulation.source_plate[0:3]
    targets = prcxi_simulation.target_plate["A4:A6"]
    for idx, src in enumerate(sources):
        _assign_sample_uuid(src, f"single_{idx}")
    offsets = _zero_offsets(max(len(sources), len(targets)))

    result = run(
        handler.transfer_liquid(
            sources=sources,
            targets=targets,
            tip_racks=[prcxi_simulation.tip_rack],
            use_channels=[0],
            asp_vols=[5.0, 6.0, 7.0],
            dis_vols=[10.0, 11.0, 12.0],
            offsets=offsets,
            mix_times=None,
        )
    )

    # assert result == """"""

    _assert_samples_match(sources, targets)


def test_transfer_liquid_single_channel_one_to_many(prcxi_simulation: SimulationContext):
    if prcxi_simulation.channel_num != 1:
        pytest.skip("仅在单通道配置下运行")

    handler = prcxi_simulation.handler
    for well in prcxi_simulation.source_plate.children + prcxi_simulation.target_plate.children:
        _ensure_unilabos_extra(well)
    source = prcxi_simulation.source_plate.children[0] 
    targets = prcxi_simulation.target_plate["A1:E1"]
    _assign_sample_uuid(source, "one_to_many_source")
    offsets = _zero_offsets(max(len(targets), 1))

    run(
        handler.transfer_liquid(
            sources=[source],
            targets=targets,
            tip_racks=[prcxi_simulation.tip_rack],
            use_channels=[0],
            asp_vols=10.0,
            dis_vols=[2.0, 2.0, 2.0, 2.0, 2.0],
            offsets=offsets,
            mix_times=0,
        )
    )

    for target in targets:
        assert getattr(target, "unilabos_extra", {}).get("sample_uuid") == "one_to_many_source"


def test_transfer_liquid_single_channel_many_to_one(prcxi_simulation: SimulationContext):
    if prcxi_simulation.channel_num != 1:
        pytest.skip("仅在单通道配置下运行")

    handler = prcxi_simulation.handler
    for well in prcxi_simulation.source_plate.children + prcxi_simulation.target_plate.children:
        _ensure_unilabos_extra(well)
    sources = prcxi_simulation.source_plate[0:3]
    target = prcxi_simulation.target_plate.children[4]
    for idx, src in enumerate(sources):
        _assign_sample_uuid(src, f"many_to_one_{idx}")
    offsets = _zero_offsets(max(len(sources), len([target])))

    run(
        handler.transfer_liquid(
            sources=sources,
            targets=[target],
            tip_racks=[prcxi_simulation.tip_rack],
            use_channels=[0],
            asp_vols=[8.0, 9.0, 10.0],
            dis_vols=1,
            offsets=offsets,
            mix_stage="after",
            mix_times=1,
            mix_vol=5,
        )
    )

    assert getattr(target, "unilabos_extra", {}).get("sample_uuid") == "many_to_one_2"


def test_transfer_liquid_eight_channel_batches(prcxi_simulation: SimulationContext):
    if prcxi_simulation.channel_num != 8:
        pytest.skip("仅在八通道配置下运行")

    handler = prcxi_simulation.handler
    for well in prcxi_simulation.source_plate.children + prcxi_simulation.target_plate.children:
        _ensure_unilabos_extra(well)
    sources = prcxi_simulation.source_plate[0:8]
    targets = prcxi_simulation.target_plate[16:24]
    for idx, src in enumerate(sources):
        _assign_sample_uuid(src, f"batch_{idx}")
    offsets = _zero_offsets(len(targets))

    use_channels = list(range(8))
    asp_vols = [float(i + 1) * 2 for i in range(8)]
    dis_vols = [float(i + 10) for i in range(8)]

    run(
        handler.transfer_liquid(
            sources=sources,
            targets=targets,
            tip_racks=[prcxi_simulation.tip_rack],
            use_channels=use_channels,
            asp_vols=asp_vols,
            dis_vols=dis_vols,
            offsets=offsets,
            mix_times=0,
        )
    )

    _assert_samples_match(sources, targets)


@pytest.mark.parametrize("mix_stage", ["before", "after", "both"])
def test_transfer_liquid_mix_stages(prcxi_simulation: SimulationContext, mix_stage: str):
    if prcxi_simulation.channel_num != 1:
        pytest.skip("仅在单通道配置下运行")

    handler = prcxi_simulation.handler
    for well in prcxi_simulation.source_plate.children + prcxi_simulation.target_plate.children:
        _ensure_unilabos_extra(well)
    target = prcxi_simulation.target_plate[70]
    sources = prcxi_simulation.source_plate[80:82]
    for idx, src in enumerate(sources):
        _assign_sample_uuid(src, f"mix_stage_{mix_stage}_{idx}")

    run(
        handler.transfer_liquid(
            sources=sources,
            targets=[target],
            tip_racks=[prcxi_simulation.tip_rack],
            use_channels=[0],
            asp_vols=[4.0, 5.0],
            dis_vols=1,
            offsets=_zero_offsets(len(sources)),
            mix_stage=mix_stage,
            mix_times=2,
            mix_vol=3,
        )
    )

    # mix_stage 前后都应该保留最新源的 sample_uuid
    assert getattr(target, "unilabos_extra", {}).get("sample_uuid") == f"mix_stage_{mix_stage}_1"
    if prcxi_simulation.channel_num != 8:
        pytest.skip("仅在八通道配置下运行")

    handler = prcxi_simulation.handler
    sources = prcxi_simulation.source_plate[0:8]
    targets = prcxi_simulation.target_plate[16:24]
    for idx, src in enumerate(sources):
        _assign_sample_uuid(src, f"batch_{idx}")
    offsets = _zero_offsets(len(targets))

    use_channels = list(range(8))
    asp_vols = [float(i + 1) * 2 for i in range(8)]
    dis_vols = [float(i + 10) for i in range(8)]

    run(
        handler.transfer_liquid(
            sources=sources,
            targets=targets,
            tip_racks=[prcxi_simulation.tip_rack],
            use_channels=use_channels,
            asp_vols=asp_vols,
            dis_vols=dis_vols,
            offsets=offsets,
            mix_stage="after",
            mix_times=2,
            mix_vol=3,
        )
    )

    _assert_samples_match(sources, targets)
