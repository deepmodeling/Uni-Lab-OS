import asyncio
import collections
import contextlib
import json
import time
from typing import Any, List, Dict, Optional, TypedDict, Union, Sequence, Iterator, Literal

from pylabrobot.liquid_handling import (
    LiquidHandlerBackend,
    Pickup,
    SingleChannelAspiration,
    Drop,
    SingleChannelDispense,
    PickupTipRack,
    DropTipRack,
    MultiHeadAspirationPlate,
)
from pylabrobot.liquid_handling.standard import (
    MultiHeadAspirationContainer,
    MultiHeadDispenseContainer,
    MultiHeadDispensePlate,
    ResourcePickup,
    ResourceMove,
    ResourceDrop,
)
from pylabrobot.resources import Tip, Deck, Plate, Well, TipRack, Resource, Container, Coordinate, TipSpot, Trash

from unilabos.devices.liquid_handling.liquid_handler_abstract import LiquidHandlerAbstract
from unilabos.devices.liquid_handling.rviz_backend import UniLiquidHandlerRvizBackend
from unilabos.devices.liquid_handling.laiyu.backend.laiyu_v_backend import UniLiquidHandlerLaiyuBackend



class TransformXYZDeck(Deck):
    """Laiyu 的专用 Deck 类，继承自 Deck。

    该类定义了 Laiyu 的工作台布局和槽位信息。
    """

    def __init__(self, name: str, size_x: float, size_y: float, size_z: float):
        super().__init__(name, size_x, size_y, size_z)
        self.name = name


class TransformXYZRvizBackend(UniLiquidHandlerRvizBackend):
    def __init__(self, name: str, channel_num: int):
        super().__init__(channel_num)
        self.name = name


class TransformXYZContainer(Plate, TipRack):
    """Laiyu 的专用 Container 类，继承自 Plate和TipRack。

    该类定义了 Laiyu 的工作台布局和槽位信息。
    """

    def __init__(
        self,
        name: str,
        size_x: float,
        size_y: float,
        size_z: float,
        category: str,
        ordering: collections.OrderedDict,
        model: Optional[str] = None,
    ):
        super().__init__(name, size_x, size_y, size_z, category=category, ordering=ordering, model=model)
        self._unilabos_state = {}

    def load_state(self, state: Dict[str, Any]) -> None:
        """从给定的状态加载工作台信息。"""
        super().load_state(state)
        self._unilabos_state = state

    def serialize_state(self) -> Dict[str, Dict[str, Any]]:
        data = super().serialize_state()
        data.update(self._unilabos_state)
        return data

class TransformXYZHandler(LiquidHandlerAbstract):
    support_touch_tip = False

    def __init__(self, deck: Deck, host: str = "127.0.0.1", port: int = 9999, timeout: float = 10.0, channel_num=1, simulator=True,
                 serial_port: str = "/dev/ttyUSB0", baudrate: int = 115200, pipette_address: int = 4,
                 total_height: float = 310, **backend_kwargs):
        # Handle case where deck is passed as a dict (from serialization)
        if isinstance(deck, dict):
            # Try to create a TransformXYZDeck from the dict
            if 'name' in deck and 'size_x' in deck and 'size_y' in deck and 'size_z' in deck:
                deck = TransformXYZDeck(
                    name=deck['name'],
                    size_x=deck.get('size_x', 100),
                    size_y=deck.get('size_y', 100),
                    size_z=deck.get('size_z', 100)
                )
            else:
                # Fallback: create a basic deck
                deck = TransformXYZDeck(name='deck', size_x=100, size_y=100, size_z=100)

        if simulator:
            self._unilabos_backend = TransformXYZRvizBackend(name="laiyu", channel_num=channel_num)
        else:
            self._unilabos_backend = UniLiquidHandlerLaiyuBackend(
                num_channels=channel_num,
                total_height=total_height,
                port=serial_port,
                baudrate=baudrate,
                pipette_address=pipette_address,
            )
        super().__init__(backend=self._unilabos_backend, deck=deck, simulator=simulator, channel_num=channel_num)

    def post_init(self, ros_node):
        super().post_init(ros_node)
        if hasattr(self._unilabos_backend, 'post_init'):
            self._unilabos_backend.post_init(ros_node)

    async def add_liquid(
        self,
        asp_vols: Union[List[float], float],
        dis_vols: Union[List[float], float],
        reagent_sources: Sequence[Container],
        targets: Sequence[Container],
        *,
        use_channels: Optional[List[int]] = None,
        flow_rates: Optional[List[Optional[float]]] = None,
        offsets: Optional[List[Coordinate]] = None,
        liquid_height: Optional[List[Optional[float]]] = None,
        blow_out_air_volume: Optional[List[Optional[float]]] = None,
        spread: Optional[Literal["wide", "tight", "custom"]] = "wide",
        is_96_well: bool = False,
        delays: Optional[List[int]] = None,
        mix_time: Optional[int] = None,
        mix_vol: Optional[int] = None,
        mix_rate: Optional[int] = None,
        mix_liquid_height: Optional[float] = None,
        none_keys: List[str] = [],
    ):
        return await super().add_liquid(
            asp_vols=asp_vols,
            dis_vols=dis_vols,
            reagent_sources=reagent_sources,
            targets=targets,
            use_channels=use_channels,
            flow_rates=flow_rates,
            offsets=offsets,
            liquid_height=liquid_height,
            blow_out_air_volume=blow_out_air_volume,
            spread=spread,
            is_96_well=is_96_well,
            delays=delays,
            mix_time=mix_time,
            mix_vol=mix_vol,
            mix_rate=mix_rate,
            mix_liquid_height=mix_liquid_height,
            none_keys=none_keys,
        )

    async def aspirate(
        self,
        resources: Sequence[Container],
        vols: List[float],
        use_channels: Optional[List[int]] = None,
        flow_rates: Optional[List[Optional[float]]] = None,
        offsets: Optional[List[Coordinate]] = None,
        liquid_height: Optional[List[Optional[float]]] = None,
        blow_out_air_volume: Optional[List[Optional[float]]] = None,
        spread: Literal["wide", "tight", "custom"] = "wide",
        **backend_kwargs,
    ):
        return await super().aspirate(
            resources=resources,
            vols=vols,
            use_channels=use_channels,
            flow_rates=flow_rates,
            offsets=offsets,
            liquid_height=liquid_height,
            blow_out_air_volume=blow_out_air_volume,
            spread=spread,
            **backend_kwargs,
        )

    async def dispense(
        self,
        resources: Sequence[Container],
        vols: List[float],
        use_channels: Optional[List[int]] = None,
        flow_rates: Optional[List[Optional[float]]] = None,
        offsets: Optional[List[Coordinate]] = None,
        liquid_height: Optional[List[Optional[float]]] = None,
        blow_out_air_volume: Optional[List[Optional[float]]] = None,
        spread: Literal["wide", "tight", "custom"] = "wide",
        **backend_kwargs,
    ):
        return await super().dispense(
            resources=resources,
            vols=vols,
            use_channels=use_channels,
            flow_rates=flow_rates,
            offsets=offsets,
            liquid_height=liquid_height,
            blow_out_air_volume=blow_out_air_volume,
            spread=spread,
            **backend_kwargs,
        )

    async def drop_tips(
        self,
        tip_spots: Sequence[Union[TipSpot, Trash]],
        use_channels: Optional[List[int]] = None,
        offsets: Optional[List[Coordinate]] = None,
        allow_nonzero_volume: bool = False,
        **backend_kwargs,
    ):
        return await super().drop_tips(
            tip_spots=tip_spots,
            use_channels=use_channels,
            offsets=offsets,
            allow_nonzero_volume=allow_nonzero_volume,
            **backend_kwargs,
        )

    async def mix(
        self,
        targets: Sequence[Container],
        mix_time: int = None,
        mix_vol: Optional[int] = None,
        height_to_bottom: Optional[float] = None,
        offsets: Optional[Coordinate] = None,
        mix_rate: Optional[float] = None,
        none_keys: List[str] = [],
    ):
        return await super().mix(
            targets=targets,
            mix_time=mix_time,
            mix_vol=mix_vol,
            height_to_bottom=height_to_bottom,
            offsets=offsets,
            mix_rate=mix_rate,
            none_keys=none_keys,
        )

    async def pick_up_tips(
        self,
        tip_spots: List[TipSpot],
        use_channels: Optional[List[int]] = None,
        offsets: Optional[List[Coordinate]] = None,
        **backend_kwargs,
    ):
        return await super().pick_up_tips(
            tip_spots=tip_spots,
            use_channels=use_channels,
            offsets=offsets,
            **backend_kwargs,
        )

    async def transfer_liquid(
        self,
        sources: Sequence[Container],
        targets: Sequence[Container],
        tip_racks: Sequence[TipRack],
        *,
        use_channels: Optional[List[int]] = None,
        asp_vols: Union[List[float], float],
        dis_vols: Union[List[float], float],
        asp_flow_rates: Optional[List[Optional[float]]] = None,
        dis_flow_rates: Optional[List[Optional[float]]] = None,
        offsets: Optional[List[Coordinate]] = None,
        touch_tip: bool = False,
        liquid_height: Optional[List[Optional[float]]] = None,
        blow_out_air_volume: Optional[List[Optional[float]]] = None,
        spread: Literal["wide", "tight", "custom"] = "wide",
        is_96_well: bool = False,
        mix_stage: Optional[Literal["none", "before", "after", "both"]] = "none",
        mix_times: Optional[List[int]] = None,
        mix_vol: Optional[int] = None,
        mix_rate: Optional[int] = None,
        mix_liquid_height: Optional[float] = None,
        delays: Optional[List[int]] = None,
        none_keys: List[str] = [],
    ):
        return await super().transfer_liquid(
            sources=sources,
            targets=targets,
            tip_racks=tip_racks,
            use_channels=use_channels,
            asp_vols=asp_vols,
            dis_vols=dis_vols,
            asp_flow_rates=asp_flow_rates,
            dis_flow_rates=dis_flow_rates,
            offsets=offsets,
            touch_tip=touch_tip,
            liquid_height=liquid_height,
            blow_out_air_volume=blow_out_air_volume,
            spread=spread,
            is_96_well=is_96_well,
            mix_stage=mix_stage,
            mix_times=mix_times,
            mix_vol=mix_vol,
            mix_rate=mix_rate,
            mix_liquid_height=mix_liquid_height,
            delays=delays,
            none_keys=none_keys,
        )
