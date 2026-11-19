"""LiquidHandlerAbstract 在真实 PLR 台面 + chatterbox 后端上的接口回归（FRAMEWORK_GAPS #6、#34）。

test_transfer_liquid.py 用替身覆盖了 aspirate / mix / touch_tip，只校验调用序列；这里不覆盖任何方法，
后端记录实际下发的 PLR 操作，校验子类（莱羽、PRCXI）不再需要的绕行：

- #6：一对一转移只在 ``touch_tip=True`` 时触壁；``touch_tip()`` 能对单孔 / 多孔执行；直接用 PLR 工厂
  建的耗材（没有 ``unilabos_extra``）也能吸排。
- #34：``transfer_liquid`` 直接用传入的 ``tip_racks``；同一组枪头盒连续调用不回到 A1；单个体积按模式展开；
  ``mix`` 逐孔、按装了枪头的通道执行；``add_liquid`` 不给 ``use_channels`` 也能运行。
"""

from __future__ import annotations

import asyncio
from typing import Any, List, Tuple

import pytest
from pylabrobot.liquid_handling import LiquidHandlerChatterboxBackend
from pylabrobot.resources import Coordinate, Deck, Trash, set_tip_tracking, set_volume_tracking
from pylabrobot.resources.corning.plates import cor_96_wellplate_2mL_Vb
from pylabrobot.resources.opentrons.tip_racks import opentrons_96_tiprack_300ul

from unilabos.devices.liquid_handling.liquid_handler_abstract import LiquidHandlerAbstract
from unilabos.resources.objects.resource import EXTRA_SAMPLE_UUID
from unilabos.resources.resource_tracker import ResourceTreeSet


class RecordingBackend(LiquidHandlerChatterboxBackend):
    """记录下发到后端的每个 PLR 操作：(动作, 资源名, 体积, 通道, x 偏移)。"""

    def __init__(self, num_channels: int):
        super().__init__(num_channels)
        self.ops: List[Tuple[str, str, Any, int, float]] = []

    def _record(self, name: str, ops, use_channels) -> None:
        for op, channel in zip(ops, use_channels):
            self.ops.append((name, op.resource.name, getattr(op, "volume", None), channel, op.offset.x))

    async def pick_up_tips(self, ops, use_channels, **backend_kwargs):
        self._record("pick_up_tips", ops, use_channels)

    async def drop_tips(self, ops, use_channels, **backend_kwargs):
        self._record("drop_tips", ops, use_channels)

    async def aspirate(self, ops, use_channels, **backend_kwargs):
        self._record("aspirate", ops, use_channels)

    async def dispense(self, ops, use_channels, **backend_kwargs):
        self._record("dispense", ops, use_channels)


class FakeNode:
    """LiquidHandlerAbstract 用到的 DeviceNode 调用：只有 custom_delay 的 sleep。"""

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(0)


def _draft_deck() -> Deck:
    deck = Deck(name="deck", size_x=600, size_y=400, size_z=100)
    deck.assign_child_resource(opentrons_96_tiprack_300ul("rack"), location=Coordinate(10, 10, 0))
    deck.assign_child_resource(opentrons_96_tiprack_300ul("rack2"), location=Coordinate(10, 150, 0))
    deck.assign_child_resource(cor_96_wellplate_2mL_Vb("plate"), location=Coordinate(200, 10, 0))
    deck.assign_child_resource(Trash("trash", 50, 50, 50), location=Coordinate(400, 10, 0))
    return deck


def _authority_deck() -> Deck:
    # 运行时交给驱动的耗材是物料权威重建的（带 uuid 与 unilabos_extra）
    tree = ResourceTreeSet.from_plr_resources([_draft_deck()], known_random_uuid=True).dump()
    return ResourceTreeSet.load(tree).to_plr_resources()[0]


def _make_handler(channel_num: int = 1, deck: Deck = None) -> LiquidHandlerAbstract:
    handler = LiquidHandlerAbstract(RecordingBackend(channel_num), deck or _authority_deck(), channel_num=channel_num)
    handler.post_init(FakeNode())
    asyncio.run(handler.setup())
    return handler


@pytest.fixture(autouse=True)
def _plr_tracking():
    set_volume_tracking(enabled=True)
    set_tip_tracking(enabled=False)
    yield
    set_volume_tracking(enabled=False)
    set_tip_tracking(enabled=False)


@pytest.fixture()
def lh() -> LiquidHandlerAbstract:
    return _make_handler()


def _ops(handler, name: str = None) -> List[Tuple[str, str, Any, int, float]]:
    return [op for op in handler.backend.ops if name is None or op[0] == name]


def _names(handler, name: str) -> List[str]:
    return [op[1] for op in _ops(handler, name)]


def _well(handler, name: str):
    return handler.deck.get_resource("plate").get_well(name)


def _volume(handler, name: str) -> float:
    return _well(handler, name).tracker.get_used_volume()


def _fill(handler, **volumes: float) -> None:
    for name, volume in volumes.items():
        _well(handler, name).set_liquids([("water", volume)])


# ---------------------------------------------------------------- #6 触壁


def test_one_to_one_transfer_touches_tips_only_when_asked(lh):
    """一对一转移每次分液后都调 touch_tip()（无视参数），touch_tip() 再以 use_channels=None 吸液崩溃。"""

    _fill(lh, A1=500.0, A2=500.0)
    lh.set_tiprack([lh.deck.get_resource("rack")])

    asyncio.run(
        lh.transfer_liquid(
            [_well(lh, "A1"), _well(lh, "A2")],
            [_well(lh, "B1"), _well(lh, "B2")],
            [],
            asp_vols=[100.0, 50.0],
            dis_vols=[100.0, 50.0],
        )
    )

    assert [op[:3] for op in _ops(lh)] == [
        ("pick_up_tips", "rack_A1", None),
        ("aspirate", "plate_well_A1", 100.0),
        ("dispense", "plate_well_B1", 100.0),
        ("drop_tips", "trash", None),
        ("pick_up_tips", "rack_B1", None),
        ("aspirate", "plate_well_A2", 50.0),
        ("dispense", "plate_well_B2", 50.0),
        ("drop_tips", "trash", None),
    ]
    assert (_volume(lh, "B1"), _volume(lh, "B2")) == (100.0, 50.0)


def test_one_to_one_transfer_touches_both_walls_when_asked(lh):
    _fill(lh, A1=500.0)
    lh.set_tiprack([lh.deck.get_resource("rack")])

    asyncio.run(
        lh.transfer_liquid([_well(lh, "A1")], [_well(lh, "B1")], [], asp_vols=[80.0], dis_vols=[80.0], touch_tip=True)
    )

    half = _well(lh, "B1").get_size_x() / 2
    assert _ops(lh)[2:5] == [
        ("dispense", "plate_well_B1", 80.0, 0, 0.0),
        ("aspirate", "plate_well_B1", 0, 0, -half),
        ("aspirate", "plate_well_B1", 0, 0, half),
    ]
    assert _ops(lh)[-1][:2] == ("drop_tips", "trash")
    assert _volume(lh, "B1") == 80.0


def test_touch_tip_accepts_one_well_or_several_and_uses_the_tipped_channel():
    lh = _make_handler(channel_num=2)
    rack = lh.deck.get_resource("rack")

    async def run():
        await lh.pick_up_tips([rack.get_item("A1")], use_channels=[1])
        await lh.touch_tip(_well(lh, "C1"))
        await lh.touch_tip([_well(lh, "C2"), _well(lh, "C3")])

    asyncio.run(run())
    touches = _ops(lh, "aspirate")
    assert [(op[1], op[3]) for op in touches] == [
        ("plate_well_C1", 1),
        ("plate_well_C1", 1),
        ("plate_well_C2", 1),
        ("plate_well_C2", 1),
        ("plate_well_C3", 1),
        ("plate_well_C3", 1),
    ]
    assert all(op[2] == 0 for op in touches)


def test_add_liquid_does_not_touch_tips_unless_asked(lh):
    """add_liquid 原来每次分液后无条件触壁（同样崩溃）；现在与 transfer_liquid 一样由 touch_tip 参数决定。"""

    _fill(lh, A1=500.0)
    lh.set_tiprack([lh.deck.get_resource("rack")])

    asyncio.run(lh.add_liquid([30.0], [30.0], [_well(lh, "A1")], [_well(lh, "D1")], use_channels=[0]))
    assert [op[0] for op in _ops(lh)] == ["pick_up_tips", "aspirate", "dispense", "drop_tips"]

    asyncio.run(lh.add_liquid([30.0], [30.0], [_well(lh, "A1")], [_well(lh, "D2")], use_channels=[0], touch_tip=True))
    assert [op[0] for op in _ops(lh)][4:] == ["pick_up_tips", "aspirate", "dispense", "aspirate", "aspirate", "drop_tips"]
    assert (_volume(lh, "D1"), _volume(lh, "D2")) == (30.0, 30.0)


def test_touching_a_well_does_not_change_the_sample_in_the_tip(lh):
    """触壁是 0 uL 吸液：不能把触碰的孔的样品记成枪头里的样品，否则随后分液的目标拿错样品 uuid。"""

    _fill(lh, A1=500.0, E1=100.0)
    _well(lh, "A1").unilabos_extra[EXTRA_SAMPLE_UUID] = "sample-A1"
    _well(lh, "E1").unilabos_extra[EXTRA_SAMPLE_UUID] = "sample-E1"

    async def run():
        await lh.pick_up_tips([lh.deck.get_resource("rack").get_item("A1")])
        await lh.aspirate([_well(lh, "A1")], [40.0])
        await lh.touch_tip(_well(lh, "E1"))
        await lh.dispense([_well(lh, "E2")], [40.0])

    asyncio.run(run())
    assert _well(lh, "E2").unilabos_extra[EXTRA_SAMPLE_UUID] == "sample-A1"
    assert _well(lh, "E1").unilabos_extra[EXTRA_SAMPLE_UUID] == "sample-E1"


def test_aspirate_and_dispense_work_on_labware_built_from_plr_factories():
    """aspirate()/dispense() 读写 resource.unilabos_extra，只有经 ResourceTreeSet 重建的耗材才有。"""

    lh = _make_handler(deck=_draft_deck())
    source, target = _well(lh, "A1"), _well(lh, "A2")
    source.set_liquids([("water", 200.0)])
    source.unilabos_extra = {EXTRA_SAMPLE_UUID: "sample-1"}
    assert not hasattr(target, "unilabos_extra")

    async def run():
        await lh.pick_up_tips([lh.deck.get_resource("rack").get_item("A1")])
        aspirated = await lh.aspirate([source], [60.0])
        dispensed = await lh.dispense([target], [60.0])
        return aspirated, dispensed

    aspirated, dispensed = asyncio.run(run())
    assert aspirated["samples"] == [{"name": "plate_well_A1", EXTRA_SAMPLE_UUID: "sample-1"}]
    assert dispensed["samples"] == [{"name": "plate_well_A2", EXTRA_SAMPLE_UUID: "sample-1"}]
    assert target.unilabos_extra[EXTRA_SAMPLE_UUID] == "sample-1"
    assert target.tracker.get_used_volume() == 60.0


# ---------------------------------------------------------------- #34 枪头盒、体积、混匀、通道


def test_transfer_liquid_takes_tips_from_the_given_racks_without_set_tiprack(lh):
    _fill(lh, A1=500.0)

    asyncio.run(
        lh.transfer_liquid(
            [_well(lh, "A1")], [_well(lh, "B1")], [lh.deck.get_resource("rack")], asp_vols=[10.0], dis_vols=[10.0]
        )
    )

    assert _names(lh, "pick_up_tips") == ["rack_A1"]
    assert _volume(lh, "B1") == 10.0


def test_consecutive_transfers_with_the_same_racks_do_not_restart_at_a1(lh):
    """PLR 枪头跟踪关闭时取走的位置 has_tip() 仍为 True：每次调用都重设迭代器会回到 A1 重复取枪头。"""

    _fill(lh, A1=500.0)
    rack = lh.deck.get_resource("rack")
    for target in ("B1", "B2", "B3"):
        asyncio.run(lh.transfer_liquid([_well(lh, "A1")], [_well(lh, target)], [rack], asp_vols=10.0, dis_vols=10.0))

    assert _names(lh, "pick_up_tips") == ["rack_A1", "rack_B1", "rack_C1"]


def test_other_racks_switch_the_tip_supply(lh):
    _fill(lh, A1=500.0)
    rack, rack2 = lh.deck.get_resource("rack"), lh.deck.get_resource("rack2")
    for racks in ([rack], [rack2], [rack2]):
        asyncio.run(lh.transfer_liquid([_well(lh, "A1")], [_well(lh, "B1")], racks, asp_vols=10.0, dis_vols=10.0))

    assert _names(lh, "pick_up_tips") == ["rack_A1", "rack2_A1", "rack2_B1"]


def test_set_tiprack_skips_spots_already_emptied_when_tip_tracking_is_on(lh):
    rack = lh.deck.get_resource("rack")
    set_tip_tracking(enabled=True)
    try:
        lh.set_tiprack([rack])
        asyncio.run(lh.pick_up_tips(next(lh.current_tip)))
        asyncio.run(lh.discard_tips())
        lh.set_tiprack([rack])
        asyncio.run(lh.pick_up_tips(next(lh.current_tip)))
    finally:
        set_tip_tracking(enabled=False)

    assert _names(lh, "pick_up_tips") == ["rack_A1", "rack_B1"]


def test_missing_tip_racks_fail_clearly(lh):
    with pytest.raises(RuntimeError, match="set_tiprack"):
        asyncio.run(lh.transfer_liquid([_well(lh, "A1")], [_well(lh, "B1")], [], asp_vols=10.0, dis_vols=10.0))
    with pytest.raises(RuntimeError, match="set_tiprack"):
        asyncio.run(lh.add_liquid(10.0, 10.0, [_well(lh, "A1")], [_well(lh, "B1")]))


def test_running_out_of_tips_fails_clearly(lh):
    _fill(lh, A1=500.0)
    lh.set_tiprack([lh.deck.get_resource("rack")])
    lh.current_tip = iter([[lh.deck.get_resource("rack").get_item("A1")]])

    asyncio.run(lh.transfer_liquid([_well(lh, "A1")], [_well(lh, "B1")], [], asp_vols=10.0, dis_vols=10.0))
    with pytest.raises(RuntimeError, match="枪头已用完"):
        asyncio.run(lh.transfer_liquid([_well(lh, "A1")], [_well(lh, "B2")], [], asp_vols=10.0, dis_vols=10.0))


@pytest.mark.parametrize("volume", [25.0, [25.0]], ids=["scalar", "one-element-list"])
def test_one_to_one_expands_a_single_volume(lh, volume):
    _fill(lh, A1=500.0, A2=500.0)

    asyncio.run(
        lh.transfer_liquid(
            [_well(lh, "A1"), _well(lh, "A2")],
            [_well(lh, "C1"), _well(lh, "C2")],
            [lh.deck.get_resource("rack")],
            asp_vols=volume,
            dis_vols=volume,
        )
    )

    assert (_volume(lh, "C1"), _volume(lh, "C2")) == (25.0, 25.0)
    assert (_volume(lh, "A1"), _volume(lh, "A2")) == (475.0, 475.0)


def test_one_to_many_and_many_to_one_expand_a_single_volume(lh):
    _fill(lh, A1=500.0, A2=500.0, A3=500.0)
    rack = lh.deck.get_resource("rack")

    # 一对多：一次吸液，每个目标分一份
    asyncio.run(lh.transfer_liquid([_well(lh, "A1")], [_well(lh, "F1"), _well(lh, "F2")], [rack], asp_vols=40.0, dis_vols=20.0))
    # 多对一：每个源吸一份
    asyncio.run(lh.transfer_liquid([_well(lh, "A2"), _well(lh, "A3")], [_well(lh, "G1")], [rack], asp_vols=15.0, dis_vols=30.0))

    assert (_volume(lh, "F1"), _volume(lh, "F2"), _volume(lh, "G1")) == (20.0, 20.0, 30.0)
    assert (_volume(lh, "A2"), _volume(lh, "A3")) == (485.0, 485.0)


def test_mix_runs_per_target_on_the_tipped_channel(lh):
    """mix 原来把整个目标列表当成一个资源，且吸排不带通道。"""

    _fill(lh, H1=100.0, H2=100.0)

    async def run():
        await lh.pick_up_tips([lh.deck.get_resource("rack").get_item("A1")])
        await lh.mix([_well(lh, "H1"), _well(lh, "H2")], mix_time=2, mix_vol=30)

    asyncio.run(run())
    assert [(op[0], op[1], op[2], op[3]) for op in _ops(lh)[1:]] == [
        ("aspirate", "plate_well_H1", 30, 0),
        ("dispense", "plate_well_H1", 30, 0),
        ("aspirate", "plate_well_H1", 30, 0),
        ("dispense", "plate_well_H1", 30, 0),
        ("aspirate", "plate_well_H2", 30, 0),
        ("dispense", "plate_well_H2", 30, 0),
        ("aspirate", "plate_well_H2", 30, 0),
        ("dispense", "plate_well_H2", 30, 0),
    ]
    assert (_volume(lh, "H1"), _volume(lh, "H2")) == (100.0, 100.0)


def test_mix_after_dispense_in_a_transfer(lh):
    _fill(lh, A1=500.0)

    asyncio.run(
        lh.transfer_liquid(
            [_well(lh, "A1")],
            [_well(lh, "G2")],
            [lh.deck.get_resource("rack")],
            asp_vols=[100.0],
            dis_vols=[100.0],
            mix_stage="after",
            mix_times=[2],
            mix_vol=30,
        )
    )

    assert [op[:2] for op in _ops(lh)[1:7]] == [
        ("aspirate", "plate_well_A1"),
        ("dispense", "plate_well_G2"),
        ("aspirate", "plate_well_G2"),
        ("dispense", "plate_well_G2"),
        ("aspirate", "plate_well_G2"),
        ("dispense", "plate_well_G2"),
    ]
    assert _volume(lh, "G2") == 100.0


def test_mix_uses_all_tipped_channels_at_once():
    lh = _make_handler(channel_num=8)
    rack = lh.deck.get_resource("rack")
    wells = [_well(lh, f"{row}5") for row in "ABCDEFGH"]
    for well in wells:
        well.set_liquids([("water", 100.0)])

    async def run():
        await lh.pick_up_tips(rack["A1:H1"], use_channels=list(range(8)))
        await lh.mix(wells, mix_time=1, mix_vol=20)

    asyncio.run(run())
    aspirates = _ops(lh, "aspirate")
    assert [(op[1], op[3]) for op in aspirates] == [(well.name, channel) for channel, well in enumerate(wells)]


def test_add_liquid_without_use_channels_and_with_scalar_volumes(lh):
    _fill(lh, A1=500.0)
    lh.set_tiprack([lh.deck.get_resource("rack")])

    asyncio.run(lh.add_liquid(30.0, 30.0, [_well(lh, "A1")], [_well(lh, "H5"), _well(lh, "H6")]))

    assert (_volume(lh, "H5"), _volume(lh, "H6")) == (30.0, 30.0)
    assert _names(lh, "aspirate") == ["plate_well_A1", "plate_well_A1"]
    assert _names(lh, "pick_up_tips") == ["rack_A1", "rack_B1"]


def test_remove_liquid_without_use_channels_and_with_a_scalar_volume(lh):
    """remove_liquid 与 add_liquid 同样在 use_channels=None 时 len(None) 失败。"""

    _fill(lh, A1=100.0, A2=100.0)
    lh.set_tiprack([lh.deck.get_resource("rack")])

    asyncio.run(lh.remove_liquid(20.0, [_well(lh, "A1"), _well(lh, "A2")], _well(lh, "H12")))

    assert (_volume(lh, "A1"), _volume(lh, "A2"), _volume(lh, "H12")) == (80.0, 80.0, 40.0)
