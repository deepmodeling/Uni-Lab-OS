import asyncio
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, Sequence, Tuple

import pytest

from unilabos.devices.liquid_handling.liquid_handler_abstract import LiquidHandlerAbstract


@dataclass(frozen=True)
class DummyContainer:
    name: str

    def __repr__(self) -> str:  # pragma: no cover
        return f"DummyContainer({self.name})"


@dataclass(frozen=True)
class DummyTipSpot:
    name: str

    def __repr__(self) -> str:  # pragma: no cover
        return f"DummyTipSpot({self.name})"


def make_tip_iter(n: int = 256) -> Iterable[List[DummyTipSpot]]:
    """Yield lists so code can safely call `tip.extend(next(self.current_tip))`."""
    for i in range(n):
        yield [DummyTipSpot(f"tip_{i}")]


class FakeLiquidHandler(LiquidHandlerAbstract):
    """不初始化真实 backend/deck；仅用来记录 transfer_liquid 内部调用序列。"""

    def __init__(self, channel_num: int = 8):
        # 不调用 super().__init__，避免真实硬件/后端依赖
        self.channel_num = channel_num
        self.support_touch_tip = True
        self.current_tip = iter(make_tip_iter())
        self.calls: List[Tuple[str, Any]] = []

    async def pick_up_tips(self, tip_spots, use_channels=None, offsets=None, **backend_kwargs):
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
            (
                "aspirate",
                {
                    "resources": list(resources),
                    "vols": list(vols),
                    "use_channels": list(use_channels) if use_channels is not None else None,
                    "flow_rates": list(flow_rates) if flow_rates is not None else None,
                    "offsets": list(offsets) if offsets is not None else None,
                    "liquid_height": list(liquid_height) if liquid_height is not None else None,
                    "blow_out_air_volume": list(blow_out_air_volume) if blow_out_air_volume is not None else None,
                },
            )
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
            (
                "dispense",
                {
                    "resources": list(resources),
                    "vols": list(vols),
                    "use_channels": list(use_channels) if use_channels is not None else None,
                    "flow_rates": list(flow_rates) if flow_rates is not None else None,
                    "offsets": list(offsets) if offsets is not None else None,
                    "liquid_height": list(liquid_height) if liquid_height is not None else None,
                    "blow_out_air_volume": list(blow_out_air_volume) if blow_out_air_volume is not None else None,
                },
            )
        )

    async def discard_tips(self, use_channels=None, *args, **kwargs):
        # 有的分支是 discard_tips(use_channels=[0])，有的分支是 discard_tips([0..7])（位置参数）
        self.calls.append(("discard_tips", {"use_channels": list(use_channels) if use_channels is not None else None}))

    async def custom_delay(self, seconds=0, msg=None):
        self.calls.append(("custom_delay", {"seconds": seconds, "msg": msg}))

    async def touch_tip(self, targets):
        # 原实现会访问 targets.get_size_x() 等；测试里只记录调用
        self.calls.append(("touch_tip", {"targets": targets}))

    async def mix(self, targets, mix_time=None, mix_vol=None, height_to_bottom=None, offsets=None, mix_rate=None, none_keys=None):
        self.calls.append(
            (
                "mix",
                {
                    "targets": targets,
                    "mix_time": mix_time,
                    "mix_vol": mix_vol,
                },
            )
        )


def run(coro):
    return asyncio.run(coro)


def test_one_to_one_single_channel_basic_calls():
    lh = FakeLiquidHandler(channel_num=1)
    lh.current_tip = iter(make_tip_iter(64))

    sources = [DummyContainer(f"S{i}") for i in range(3)]
    targets = [DummyContainer(f"T{i}") for i in range(3)]

    run(
        lh.transfer_liquid(
            sources=sources,
            targets=targets,
            tip_racks=[],
            use_channels=[0],
            asp_vols=[1, 2, 3],
            dis_vols=[4, 5, 6],
            mix_times=None,  # 应该仍能执行（不 mix）
        )
    )

    assert [c[0] for c in lh.calls].count("pick_up_tips") == 3
    assert [c[0] for c in lh.calls].count("aspirate") == 3
    assert [c[0] for c in lh.calls].count("dispense") == 3
    assert [c[0] for c in lh.calls].count("discard_tips") == 3

    # 每次 aspirate/dispense 都是单孔列表
    aspirates = [payload for name, payload in lh.calls if name == "aspirate"]
    assert aspirates[0]["resources"] == [sources[0]]
    assert aspirates[0]["vols"] == [1.0]

    dispenses = [payload for name, payload in lh.calls if name == "dispense"]
    assert dispenses[2]["resources"] == [targets[2]]
    assert dispenses[2]["vols"] == [6.0]


def test_one_to_one_single_channel_before_stage_mixes_prior_to_aspirate():
    lh = FakeLiquidHandler(channel_num=1)
    lh.current_tip = iter(make_tip_iter(16))

    source = DummyContainer("S0")
    target = DummyContainer("T0")

    run(
        lh.transfer_liquid(
            sources=[source],
            targets=[target],
            tip_racks=[],
            use_channels=[0],
            asp_vols=[5],
            dis_vols=[5],
            mix_stage="before",
            mix_times=1,
            mix_vol=3,
        )
    )

    names = [name for name, _ in lh.calls]
    assert names.count("mix") == 1
    assert names.index("mix") < names.index("aspirate")


def test_one_to_one_eight_channel_groups_by_8():
    lh = FakeLiquidHandler(channel_num=8)
    lh.current_tip = iter(make_tip_iter(256))

    sources = [DummyContainer(f"S{i}") for i in range(16)]
    targets = [DummyContainer(f"T{i}") for i in range(16)]
    asp_vols = list(range(1, 17))
    dis_vols = list(range(101, 117))

    run(
        lh.transfer_liquid(
            sources=sources,
            targets=targets,
            tip_racks=[],
            use_channels=list(range(8)),
            asp_vols=asp_vols,
            dis_vols=dis_vols,
            mix_times=0,  # 触发逻辑但不 mix
        )
    )

    # 16 个任务 -> 2 组，每组 8 通道一起做
    assert [c[0] for c in lh.calls].count("pick_up_tips") == 2
    aspirates = [payload for name, payload in lh.calls if name == "aspirate"]
    dispenses = [payload for name, payload in lh.calls if name == "dispense"]
    assert len(aspirates) == 2
    assert len(dispenses) == 2

    assert aspirates[0]["resources"] == sources[0:8]
    assert aspirates[0]["vols"] == [float(v) for v in asp_vols[0:8]]
    assert dispenses[1]["resources"] == targets[8:16]
    assert dispenses[1]["vols"] == [float(v) for v in dis_vols[8:16]]


def test_one_to_one_eight_channel_requires_multiple_of_8_targets():
    lh = FakeLiquidHandler(channel_num=8)
    lh.current_tip = iter(make_tip_iter(64))

    sources = [DummyContainer(f"S{i}") for i in range(9)]
    targets = [DummyContainer(f"T{i}") for i in range(9)]

    with pytest.raises(ValueError, match="multiple of 8"):
        run(
            lh.transfer_liquid(
                sources=sources,
                targets=targets,
                tip_racks=[],
                use_channels=list(range(8)),
                asp_vols=[1] * 9,
                dis_vols=[1] * 9,
                mix_times=0,
            )
        )


def test_one_to_one_eight_channel_parameter_lists_are_chunked_per_8():
    lh = FakeLiquidHandler(channel_num=8)
    lh.current_tip = iter(make_tip_iter(512))

    sources = [DummyContainer(f"S{i}") for i in range(16)]
    targets = [DummyContainer(f"T{i}") for i in range(16)]
    asp_vols = [i + 1 for i in range(16)]
    dis_vols = [200 + i for i in range(16)]
    asp_flow_rates = [0.1 * (i + 1) for i in range(16)]
    dis_flow_rates = [0.2 * (i + 1) for i in range(16)]
    offsets = [f"offset_{i}" for i in range(16)]
    liquid_heights = [i * 0.5 for i in range(16)]
    blow_out_air_volume = [i + 0.05 for i in range(16)]

    run(
        lh.transfer_liquid(
            sources=sources,
            targets=targets,
            tip_racks=[],
            use_channels=list(range(8)),
            asp_vols=asp_vols,
            dis_vols=dis_vols,
            asp_flow_rates=asp_flow_rates,
            dis_flow_rates=dis_flow_rates,
            offsets=offsets,
            liquid_height=liquid_heights,
            blow_out_air_volume=blow_out_air_volume,
            mix_times=0,
        )
    )

    aspirates = [payload for name, payload in lh.calls if name == "aspirate"]
    dispenses = [payload for name, payload in lh.calls if name == "dispense"]
    assert len(aspirates) == len(dispenses) == 2

    for batch_idx in range(2):
        start = batch_idx * 8
        end = start + 8
        asp_call = aspirates[batch_idx]
        dis_call = dispenses[batch_idx]
        assert asp_call["resources"] == sources[start:end]
        assert asp_call["flow_rates"] == asp_flow_rates[start:end]
        assert asp_call["offsets"] == offsets[start:end]
        assert asp_call["liquid_height"] == liquid_heights[start:end]
        assert asp_call["blow_out_air_volume"] == blow_out_air_volume[start:end]
        assert dis_call["flow_rates"] == dis_flow_rates[start:end]
        assert dis_call["offsets"] == offsets[start:end]
        assert dis_call["liquid_height"] == liquid_heights[start:end]
        assert dis_call["blow_out_air_volume"] == blow_out_air_volume[start:end]


def test_one_to_one_eight_channel_handles_32_tasks_four_batches():
    lh = FakeLiquidHandler(channel_num=8)
    lh.current_tip = iter(make_tip_iter(1024))

    sources = [DummyContainer(f"S{i}") for i in range(32)]
    targets = [DummyContainer(f"T{i}") for i in range(32)]
    asp_vols = [i + 1 for i in range(32)]
    dis_vols = [300 + i for i in range(32)]

    run(
        lh.transfer_liquid(
            sources=sources,
            targets=targets,
            tip_racks=[],
            use_channels=list(range(8)),
            asp_vols=asp_vols,
            dis_vols=dis_vols,
            mix_times=0,
        )
    )

    pick_calls = [name for name, _ in lh.calls if name == "pick_up_tips"]
    aspirates = [payload for name, payload in lh.calls if name == "aspirate"]
    dispenses = [payload for name, payload in lh.calls if name == "dispense"]
    assert len(pick_calls) == 4
    assert len(aspirates) == len(dispenses) == 4
    assert aspirates[0]["resources"] == sources[0:8]
    assert aspirates[-1]["resources"] == sources[24:32]
    assert dispenses[0]["resources"] == targets[0:8]
    assert dispenses[-1]["resources"] == targets[24:32]


def test_one_to_many_single_channel_aspirates_total_when_asp_vol_too_small():
    lh = FakeLiquidHandler(channel_num=1)
    lh.current_tip = iter(make_tip_iter(64))

    source = DummyContainer("SRC")
    targets = [DummyContainer(f"T{i}") for i in range(3)]
    dis_vols = [10, 20, 30]  # sum=60

    run(
        lh.transfer_liquid(
            sources=[source],
            targets=targets,
            tip_racks=[],
            use_channels=[0],
            asp_vols=10,  # 小于 sum(dis_vols) -> 应吸 60
            dis_vols=dis_vols,
            mix_times=0,
        )
    )

    aspirates = [payload for name, payload in lh.calls if name == "aspirate"]
    assert len(aspirates) == 1
    assert aspirates[0]["resources"] == [source]
    assert aspirates[0]["vols"] == [60.0]
    assert aspirates[0]["use_channels"] == [0]
    dispenses = [payload for name, payload in lh.calls if name == "dispense"]
    assert [d["vols"][0] for d in dispenses] == [10.0, 20.0, 30.0]


def test_one_to_many_eight_channel_basic():
    lh = FakeLiquidHandler(channel_num=8)
    lh.current_tip = iter(make_tip_iter(128))

    source = DummyContainer("SRC")
    targets = [DummyContainer(f"T{i}") for i in range(8)]
    dis_vols = [i + 1 for i in range(8)]

    run(
        lh.transfer_liquid(
            sources=[source],
            targets=targets,
            tip_racks=[],
            use_channels=list(range(8)),
            asp_vols=999,  # one-to-many 8ch 会按 dis_vols 吸（每通道各自）
            dis_vols=dis_vols,
            mix_times=0,
        )
    )

    aspirates = [payload for name, payload in lh.calls if name == "aspirate"]
    assert aspirates[0]["resources"] == [source] * 8
    assert aspirates[0]["vols"] == [float(v) for v in dis_vols]
    dispenses = [payload for name, payload in lh.calls if name == "dispense"]
    assert dispenses[0]["resources"] == targets
    assert dispenses[0]["vols"] == [float(v) for v in dis_vols]


def test_many_to_one_single_channel_standard_dispense_equals_asp_by_default():
    lh = FakeLiquidHandler(channel_num=1)
    lh.current_tip = iter(make_tip_iter(128))

    sources = [DummyContainer(f"S{i}") for i in range(3)]
    target = DummyContainer("T")
    asp_vols = [5, 6, 7]

    run(
        lh.transfer_liquid(
            sources=sources,
            targets=[target],
            tip_racks=[],
            use_channels=[0],
            asp_vols=asp_vols,
            dis_vols=1,  # many-to-one 允许标量；非比例模式下实际每次分液=对应 asp_vol
            mix_times=0,
        )
    )

    dispenses = [payload for name, payload in lh.calls if name == "dispense"]
    assert [d["vols"][0] for d in dispenses] == [float(v) for v in asp_vols]
    assert all(d["resources"] == [target] for d in dispenses)


def test_many_to_one_single_channel_before_stage_mixes_target_once():
    lh = FakeLiquidHandler(channel_num=1)
    lh.current_tip = iter(make_tip_iter(128))

    sources = [DummyContainer("S0"), DummyContainer("S1")]
    target = DummyContainer("T")

    run(
        lh.transfer_liquid(
            sources=sources,
            targets=[target],
            tip_racks=[],
            use_channels=[0],
            asp_vols=[5, 6],
            dis_vols=1,
            mix_stage="before",
            mix_times=2,
            mix_vol=4,
        )
    )

    names = [name for name, _ in lh.calls]
    assert names[0] == "mix"
    assert names.count("mix") == 1


def test_many_to_one_single_channel_proportional_mixing_uses_dis_vols_per_source():
    lh = FakeLiquidHandler(channel_num=1)
    lh.current_tip = iter(make_tip_iter(128))

    sources = [DummyContainer(f"S{i}") for i in range(3)]
    target = DummyContainer("T")
    asp_vols = [5, 6, 7]
    dis_vols = [1, 2, 3]

    run(
        lh.transfer_liquid(
            sources=sources,
            targets=[target],
            tip_racks=[],
            use_channels=[0],
            asp_vols=asp_vols,
            dis_vols=dis_vols,  # 比例模式
            mix_times=0,
        )
    )

    dispenses = [payload for name, payload in lh.calls if name == "dispense"]
    assert [d["vols"][0] for d in dispenses] == [float(v) for v in dis_vols]


def test_many_to_one_eight_channel_basic():
    lh = FakeLiquidHandler(channel_num=8)
    lh.current_tip = iter(make_tip_iter(256))

    sources = [DummyContainer(f"S{i}") for i in range(8)]
    target = DummyContainer("T")
    asp_vols = [10 + i for i in range(8)]

    run(
        lh.transfer_liquid(
            sources=sources,
            targets=[target],
            tip_racks=[],
            use_channels=list(range(8)),
            asp_vols=asp_vols,
            dis_vols=999,  # 非比例模式下每通道分液=对应 asp_vol
            mix_times=0,
        )
    )

    aspirates = [payload for name, payload in lh.calls if name == "aspirate"]
    dispenses = [payload for name, payload in lh.calls if name == "dispense"]
    assert aspirates[0]["resources"] == sources
    assert aspirates[0]["vols"] == [float(v) for v in asp_vols]
    assert dispenses[0]["resources"] == [target] * 8
    assert dispenses[0]["vols"] == [float(v) for v in asp_vols]


def test_transfer_liquid_mode_detection_unsupported_shape_raises():
    lh = FakeLiquidHandler(channel_num=8)
    lh.current_tip = iter(make_tip_iter(64))

    sources = [DummyContainer("S0"), DummyContainer("S1")]
    targets = [DummyContainer("T0"), DummyContainer("T1"), DummyContainer("T2")]

    with pytest.raises(ValueError, match="Unsupported transfer mode"):
        run(
            lh.transfer_liquid(
                sources=sources,
                targets=targets,
                tip_racks=[],
                use_channels=[0],
                asp_vols=[1, 1],
                dis_vols=[1, 1, 1],
                mix_times=0,
            )
        )

