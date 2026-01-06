import asyncio
import collections
from collections import OrderedDict
import contextlib
import json
import os
import socket
import time
import uuid
from typing import Any, List, Dict, Optional, Tuple, TypedDict, Union, Sequence, Iterator, Literal
from pylabrobot.liquid_handling.standard import GripDirection

from pylabrobot.liquid_handling import (
    LiquidHandlerBackend,
    Pickup,
    SingleChannelAspiration,
    Drop,
    SingleChannelDispense,
    PickupTipRack,
    DropTipRack,
    MultiHeadAspirationPlate,
    ChatterBoxBackend,
    LiquidHandlerChatterboxBackend,
)
from pylabrobot.liquid_handling.standard import (
    MultiHeadAspirationContainer,
    MultiHeadDispenseContainer,
    MultiHeadDispensePlate,
    ResourcePickup,
    ResourceMove,
    ResourceDrop,
)
from pylabrobot.resources import ResourceHolder, ResourceStack, Tip, Deck, Plate, Well, TipRack, Resource, Container, Coordinate, TipSpot, Trash, PlateAdapter, TubeRack

from unilabos.devices.liquid_handling.liquid_handler_abstract import LiquidHandlerAbstract, SimpleReturn
from unilabos.ros.nodes.base_device_node import BaseROS2DeviceNode


class PRCXIError(RuntimeError):
    """Lilith 返回 Success=false 时抛出的业务异常"""


class Material(TypedDict):  # 和Plate同关系
    uuid: str
    Code: Optional[str]
    Name: Optional[str]
    SummaryName: Optional[str]
    PipetteHeight: Optional[int]
    materialEnum: Optional[int]


class WorkTablets(TypedDict):
    Number: int
    Code: str
    Material: Dict[str, Any]


class MatrixInfo(TypedDict):
    MatrixId: str
    MatrixName: str
    MatrixCount: int
    WorkTablets: list[WorkTablets]


class PRCXI9300Deck(Deck):
    """PRCXI 9300 的专用 Deck 类，继承自 Deck。

    该类定义了 PRCXI 9300 的工作台布局和槽位信息。
    """

    def __init__(self, name: str, size_x: float, size_y: float, size_z: float, **kwargs):
        super().__init__(name, size_x, size_y, size_z)
        self.slots = [None] * 16  # PRCXI 9300/9320 最大有 16 个槽位
        self.slot_locations = [Coordinate(0, 0, 0)] * 16

    def assign_child_at_slot(self, resource: Resource, slot: int, reassign: bool = False) -> None:
        if self.slots[slot - 1] is not None and not reassign:
            raise ValueError(f"Spot {slot} is already occupied")

        self.slots[slot - 1] = resource
        super().assign_child_resource(resource, location=self.slot_locations[slot - 1])

class PRCXI9300Container(Plate):
    """PRCXI 9300 的专用 Container 类，继承自 Plate，用于槽位定位和未知模块。

    该类定义了 PRCXI 9300 的工作台布局和槽位信息。
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
        **kwargs,
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
class PRCXI9300Plate(Plate):
    """ 
    专用孔板类：
    1. 继承自 PLR 原生 Plate，保留所有物理特性。
    2. 增加 material_info 参数，用于在初始化时直接绑定 Unilab UUID。
    """
    def __init__(self, name: str, size_x: float, size_y: float, size_z: float, 
                 category: str = "plate", 
                 ordered_items: collections.OrderedDict = None, 
                 ordering: Optional[collections.OrderedDict] = None,
                 model: Optional[str] = None, 
                 material_info: Optional[Dict[str, Any]] = None, 
                 **kwargs):
        # 如果 ordered_items 不为 None，直接使用
        if ordered_items is not None:
            items = ordered_items
        elif ordering is not None:
            # 检查 ordering 中的值是否是字符串（从 JSON 反序列化时的情况）
            # 如果是字符串，说明这是位置名称，需要让 Plate 自己创建 Well 对象
            # 我们只传递位置信息（键），不传递值，使用 ordering 参数
            if ordering and isinstance(next(iter(ordering.values()), None), str):
                # ordering 的值是字符串，只使用键（位置信息）创建新的 OrderedDict
                # 传递 ordering 参数而不是 ordered_items，让 Plate 自己创建 Well 对象
                items = None
                # 使用 ordering 参数，只包含位置信息（键）
                ordering_param = collections.OrderedDict((k, None) for k in ordering.keys())
            else:
                # ordering 的值已经是对象，可以直接使用
                items = ordering
                ordering_param = None
        else:
            items = None
            ordering_param = None
        
        # 根据情况传递不同的参数
        if items is not None:
            super().__init__(name, size_x, size_y, size_z, 
                             ordered_items=items,
                             category=category,
                             model=model, **kwargs)
        elif ordering_param is not None:
            # 传递 ordering 参数，让 Plate 自己创建 Well 对象
            super().__init__(name, size_x, size_y, size_z, 
                             ordering=ordering_param,
                             category=category,
                             model=model, **kwargs)
        else:
            super().__init__(name, size_x, size_y, size_z, 
                             category=category,
                             model=model, **kwargs)
        
        self._unilabos_state = {}
        if material_info:
            self._unilabos_state["Material"] = material_info
        

    def load_state(self, state: Dict[str, Any]) -> None:
        super().load_state(state)
        self._unilabos_state = state


    def serialize_state(self) -> Dict[str, Dict[str, Any]]:
        try:
            data = super().serialize_state()
        except AttributeError:
            data = {}
        if hasattr(self, '_unilabos_state') and self._unilabos_state:
            safe_state = {}
            for k, v in self._unilabos_state.items():
                # 如果是 Material 字典，深入检查
                if k == "Material" and isinstance(v, dict):
                    safe_material = {}
                    for mk, mv in v.items():
                        # 只保留基本数据类型 (字符串, 数字, 布尔值, 列表, 字典)
                        if isinstance(mv, (str, int, float, bool, list, dict, type(None))):
                            safe_material[mk] = mv
                        else:
                            # 打印日志提醒（可选）
                            # print(f"Warning: Removing non-serializable key {mk} from {self.name}")
                            pass 
                    safe_state[k] = safe_material
                # 其他顶层属性也进行类型检查
                elif isinstance(v, (str, int, float, bool, list, dict, type(None))):
                    safe_state[k] = v
            
            data.update(safe_state)
        return data            # 其他顶层属性也进行类型检查
class PRCXI9300TipRack(TipRack):
    """ 专用吸头盒类 """
    def __init__(self, name: str, size_x: float, size_y: float, size_z: float, 
                 category: str = "tip_rack", 
                 ordered_items: collections.OrderedDict = None, 
                 ordering: Optional[collections.OrderedDict] = None,
                 model: Optional[str] = None,
                 material_info: Optional[Dict[str, Any]] = None,
                 **kwargs):
        # 如果 ordered_items 不为 None，直接使用
        if ordered_items is not None:
            items = ordered_items
        elif ordering is not None:
            # 检查 ordering 中的值是否是字符串（从 JSON 反序列化时的情况）
            # 如果是字符串，说明这是位置名称，需要让 TipRack 自己创建 Tip 对象
            # 我们只传递位置信息（键），不传递值，使用 ordering 参数
            if ordering and isinstance(next(iter(ordering.values()), None), str):
                # ordering 的值是字符串，只使用键（位置信息）创建新的 OrderedDict
                # 传递 ordering 参数而不是 ordered_items，让 TipRack 自己创建 Tip 对象
                items = None
                # 使用 ordering 参数，只包含位置信息（键）
                ordering_param = collections.OrderedDict((k, None) for k in ordering.keys())
            else:
                # ordering 的值已经是对象，可以直接使用
                items = ordering
                ordering_param = None
        else:
            items = None
            ordering_param = None
        
        # 根据情况传递不同的参数
        if items is not None:
            super().__init__(name, size_x, size_y, size_z, 
                             ordered_items=items,
                             category=category, 
                             model=model, **kwargs)
        elif ordering_param is not None:
            # 传递 ordering 参数，让 TipRack 自己创建 Tip 对象
            super().__init__(name, size_x, size_y, size_z, 
                             ordering=ordering_param,
                             category=category, 
                             model=model, **kwargs)
        else:
            super().__init__(name, size_x, size_y, size_z, 
                             category=category, 
                             model=model, **kwargs)
        self._unilabos_state = {}
        if material_info:
            self._unilabos_state["Material"] = material_info
            
    def load_state(self, state: Dict[str, Any]) -> None:
        super().load_state(state)
        self._unilabos_state = state

    def serialize_state(self) -> Dict[str, Dict[str, Any]]:
        try:
            data = super().serialize_state()
        except AttributeError:
            data = {}
        if hasattr(self, '_unilabos_state') and self._unilabos_state:
            safe_state = {}
            for k, v in self._unilabos_state.items():
                # 如果是 Material 字典，深入检查
                if k == "Material" and isinstance(v, dict):
                    safe_material = {}
                    for mk, mv in v.items():
                        # 只保留基本数据类型 (字符串, 数字, 布尔值, 列表, 字典)
                        if isinstance(mv, (str, int, float, bool, list, dict, type(None))):
                            safe_material[mk] = mv
                        else:
                            # 打印日志提醒（可选）
                            # print(f"Warning: Removing non-serializable key {mk} from {self.name}")
                            pass 
                    safe_state[k] = safe_material
                # 其他顶层属性也进行类型检查
                elif isinstance(v, (str, int, float, bool, list, dict, type(None))):
                    safe_state[k] = v
            
            data.update(safe_state)
        return data
    
class PRCXI9300Trash(Trash):
    """PRCXI 9300 的专用 Trash 类，继承自 Trash。

    该类定义了 PRCXI 9300 的工作台布局和槽位信息。
    """

    def __init__(self, name: str, size_x: float, size_y: float, size_z: float, 
                 category: str = "trash", 
                 material_info: Optional[Dict[str, Any]] = None,
                 **kwargs):
        
        if name != "trash":
            print(f"Warning: PRCXI9300Trash usually expects name='trash' for backend logic, but got '{name}'.")
        super().__init__(name, size_x, size_y, size_z, **kwargs)
        self._unilabos_state = {}
        # 初始化时注入 UUID
        if material_info:
            self._unilabos_state["Material"] = material_info

    def load_state(self, state: Dict[str, Any]) -> None:
        """从给定的状态加载工作台信息。"""
        # super().load_state(state)
        self._unilabos_state = state

    def serialize_state(self) -> Dict[str, Dict[str, Any]]:
        try:
            data = super().serialize_state()
        except AttributeError:
            data = {}
        if hasattr(self, '_unilabos_state') and self._unilabos_state:
            safe_state = {}
            for k, v in self._unilabos_state.items():
                # 如果是 Material 字典，深入检查
                if k == "Material" and isinstance(v, dict):
                    safe_material = {}
                    for mk, mv in v.items():
                        # 只保留基本数据类型 (字符串, 数字, 布尔值, 列表, 字典)
                        if isinstance(mv, (str, int, float, bool, list, dict, type(None))):
                            safe_material[mk] = mv
                        else:
                            # 打印日志提醒（可选）
                            # print(f"Warning: Removing non-serializable key {mk} from {self.name}")
                            pass 
                    safe_state[k] = safe_material
                # 其他顶层属性也进行类型检查
                elif isinstance(v, (str, int, float, bool, list, dict, type(None))):
                    safe_state[k] = v
            
            data.update(safe_state)
        return data

class PRCXI9300TubeRack(TubeRack):
    """
    专用管架类：用于 EP 管架、试管架等。
    继承自 PLR 的 TubeRack，并支持注入 material_info (UUID)。
    """
    def __init__(self, name: str, size_x: float, size_y: float, size_z: float, 
                 category: str = "tube_rack", 
                 items: Optional[Dict[str, Any]] = None,
                 ordered_items: Optional[OrderedDict] = None,
                 ordering: Optional[OrderedDict] = None,
                 model: Optional[str] = None,
                 material_info: Optional[Dict[str, Any]] = None, 
                 **kwargs):
        
        # 如果 ordered_items 不为 None，直接使用
        if ordered_items is not None:
            items_to_pass = ordered_items
            ordering_param = None
        elif ordering is not None:
            # 检查 ordering 中的值是否是字符串（从 JSON 反序列化时的情况）
            # 如果是字符串，说明这是位置名称，需要让 TubeRack 自己创建 Tube 对象
            # 我们只传递位置信息（键），不传递值，使用 ordering 参数
            if ordering and isinstance(next(iter(ordering.values()), None), str):
                # ordering 的值是字符串，只使用键（位置信息）创建新的 OrderedDict
                # 传递 ordering 参数而不是 ordered_items，让 TubeRack 自己创建 Tube 对象
                items_to_pass = None
                # 使用 ordering 参数，只包含位置信息（键）
                ordering_param = collections.OrderedDict((k, None) for k in ordering.keys())
            else:
                # ordering 的值已经是对象，可以直接使用
                items_to_pass = ordering
                ordering_param = None
        elif items is not None:
            # 兼容旧的 items 参数
            items_to_pass = items
            ordering_param = None
        else:
            items_to_pass = None
            ordering_param = None
        
        # 根据情况传递不同的参数
        if items_to_pass is not None:
            super().__init__(name, size_x, size_y, size_z, 
                             ordered_items=items_to_pass, 
                             model=model, 
                             **kwargs)
        elif ordering_param is not None:
            # 传递 ordering 参数，让 TubeRack 自己创建 Tube 对象
            super().__init__(name, size_x, size_y, size_z, 
                             ordering=ordering_param,
                             model=model, 
                             **kwargs)
        else:
            super().__init__(name, size_x, size_y, size_z, 
                             model=model, 
                             **kwargs)
        
        self._unilabos_state = {}
        if material_info:
            self._unilabos_state["Material"] = material_info

    def serialize_state(self) -> Dict[str, Dict[str, Any]]:
        try:
            data = super().serialize_state()
        except AttributeError:
            data = {}
        if hasattr(self, '_unilabos_state') and self._unilabos_state:
            safe_state = {}
            for k, v in self._unilabos_state.items():
                # 如果是 Material 字典，深入检查
                if k == "Material" and isinstance(v, dict):
                    safe_material = {}
                    for mk, mv in v.items():
                        # 只保留基本数据类型 (字符串, 数字, 布尔值, 列表, 字典)
                        if isinstance(mv, (str, int, float, bool, list, dict, type(None))):
                            safe_material[mk] = mv
                        else:
                            # 打印日志提醒（可选）
                            # print(f"Warning: Removing non-serializable key {mk} from {self.name}")
                            pass 
                    safe_state[k] = safe_material
                # 其他顶层属性也进行类型检查
                elif isinstance(v, (str, int, float, bool, list, dict, type(None))):
                    safe_state[k] = v
            
            data.update(safe_state)
        return data

class PRCXI9300PlateAdapter(PlateAdapter):
    """
    专用板式适配器类：用于承载 Plate 的底座（如 PCR 适配器、磁吸架等）。
    支持注入 material_info (UUID)。
    """
    def __init__(self, name: str, size_x: float, size_y: float, size_z: float, 
                 category: str = "plate_adapter", 
                 model: Optional[str] = None,
                 material_info: Optional[Dict[str, Any]] = None, 
                 # 参数给予默认值 (标准96孔板尺寸)
                 adapter_hole_size_x: float = 127.76, 
                 adapter_hole_size_y: float = 85.48, 
                 adapter_hole_size_z: float = 10.0, # 假设凹槽深度或板子放置高度
                 dx: Optional[float] = None,
                 dy: Optional[float] = None,
                 dz: float = 0.0, # 默认Z轴偏移
                 **kwargs):
        
        # 自动居中计算：如果未指定 dx/dy，则根据适配器尺寸和孔尺寸计算居中位置
        if dx is None:
            dx = (size_x - adapter_hole_size_x) / 2
        if dy is None:
            dy = (size_y - adapter_hole_size_y) / 2

        super().__init__(
            name=name, 
            size_x=size_x, 
            size_y=size_y, 
            size_z=size_z, 
            dx=dx,
            dy=dy,
            dz=dz,
            adapter_hole_size_x=adapter_hole_size_x,
            adapter_hole_size_y=adapter_hole_size_y,
            adapter_hole_size_z=adapter_hole_size_z,
            model=model, 
            **kwargs
        )
        
        self._unilabos_state = {}
        if material_info:
            self._unilabos_state["Material"] = material_info

    def serialize_state(self) -> Dict[str, Dict[str, Any]]:
        try:
            data = super().serialize_state()
        except AttributeError:
            data = {}
        if hasattr(self, '_unilabos_state') and self._unilabos_state:
            safe_state = {}
            for k, v in self._unilabos_state.items():
                # 如果是 Material 字典，深入检查
                if k == "Material" and isinstance(v, dict):
                    safe_material = {}
                    for mk, mv in v.items():
                        # 只保留基本数据类型 (字符串, 数字, 布尔值, 列表, 字典)
                        if isinstance(mv, (str, int, float, bool, list, dict, type(None))):
                            safe_material[mk] = mv
                        else:
                            # 打印日志提醒（可选）
                            # print(f"Warning: Removing non-serializable key {mk} from {self.name}")
                            pass 
                    safe_state[k] = safe_material
                # 其他顶层属性也进行类型检查
                elif isinstance(v, (str, int, float, bool, list, dict, type(None))):
                    safe_state[k] = v
            
            data.update(safe_state)
        return data

class PRCXI9300Handler(LiquidHandlerAbstract):
    support_touch_tip = False

    @property
    def reset_ok(self) -> bool:
        """检查设备是否已重置成功。"""
        if self._unilabos_backend.debug:
            return True
        return self._unilabos_backend.is_reset_ok

    def __init__(
        self,
        deck: Deck,
        host: str,
        port: int,
        timeout: float,
        channel_num=8,
        axis="Left",
        setup=True,
        debug=False,
        simulator=False,
        step_mode=False,
        matrix_id="",
        is_9320=False,
    ):
        tablets_info = []
        count = 0
        for child in deck.children:
            if child.children:
                if "Material" in child.children[0]._unilabos_state:
                    number = int(child.name.replace("T", ""))
                    tablets_info.append(
                        WorkTablets(Number=number, Code=f"T{number}", Material=child.children[0]._unilabos_state["Material"])
                    )
        if is_9320:
            print("当前设备是9320")
        # 始终初始化 step_mode 属性
        self.step_mode = False
        if step_mode:
            if is_9320:
                self.step_mode = step_mode
            else:
                print("9300设备不支持 单点动作模式")
        self._unilabos_backend = PRCXI9300Backend(
            tablets_info, host, port, timeout, channel_num, axis, setup, debug, matrix_id, is_9320
        )
        super().__init__(backend=self._unilabos_backend, deck=deck, simulator=simulator, channel_num=channel_num)

    def post_init(self, ros_node: BaseROS2DeviceNode):
        super().post_init(ros_node)
        self._unilabos_backend.post_init(ros_node)

    def set_liquid(self, wells: list[Well], liquid_names: list[str], volumes: list[float]) -> SimpleReturn:
        return super().set_liquid(wells, liquid_names, volumes)

    def set_group(self, group_name: str, wells: List[Well], volumes: List[float]):
        return super().set_group(group_name, wells, volumes)

    async def transfer_group(self, source_group_name: str, target_group_name: str, unit_volume: float):
        return await super().transfer_group(source_group_name, target_group_name, unit_volume)

    async def create_protocol(
        self,
        protocol_name: str = "",
        protocol_description: str = "",
        protocol_version: str = "",
        protocol_author: str = "",
        protocol_date: str = "",
        protocol_type: str = "",
        none_keys: List[str] = [],
    ):
        self._unilabos_backend.create_protocol(protocol_name)

    async def run_protocol(self):
        return self._unilabos_backend.run_protocol()

    async def remove_liquid(
        self,
        vols: List[float],
        sources: Sequence[Container],
        waste_liquid: Optional[Container] = None,
        *,
        use_channels: Optional[List[int]] = None,
        flow_rates: Optional[List[Optional[float]]] = None,
        offsets: Optional[List[Coordinate]] = None,
        liquid_height: Optional[List[Optional[float]]] = None,
        blow_out_air_volume: Optional[List[Optional[float]]] = None,
        spread: Optional[Literal["wide", "tight", "custom"]] = "wide",
        delays: Optional[List[int]] = None,
        is_96_well: Optional[bool] = False,
        top: Optional[List[float]] = None,
        none_keys: List[str] = [],
    ):
        return await super().remove_liquid(
            vols,
            sources,
            waste_liquid,
            use_channels=use_channels,
            flow_rates=flow_rates,
            offsets=offsets,
            liquid_height=liquid_height,
            blow_out_air_volume=blow_out_air_volume,
            spread=spread,
            delays=delays,
            is_96_well=is_96_well,
            top=top,
            none_keys=none_keys,
        )

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
            asp_vols,
            dis_vols,
            reagent_sources,
            targets,
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
            sources,
            targets,
            tip_racks,
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

    async def custom_delay(self, seconds=0, msg=None):
        return await super().custom_delay(seconds, msg)

    async def touch_tip(self, targets: Sequence[Container]):
        return await super().touch_tip(targets)

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
        return await self._unilabos_backend.mix(
            targets, mix_time, mix_vol, height_to_bottom, offsets, mix_rate, none_keys
        )

    def iter_tips(self, tip_racks: Sequence[TipRack]) -> Iterator[Resource]:
        return super().iter_tips(tip_racks)

    async def pick_up_tips(
        self,
        tip_spots: List[TipSpot],
        use_channels: Optional[List[int]] = None,
        offsets: Optional[List[Coordinate]] = None,
        **backend_kwargs,
    ):
        if self.step_mode:
            await self.create_protocol(f"单点动作{time.time()}")
            await super().pick_up_tips(tip_spots, use_channels, offsets, **backend_kwargs)
            await self.run_protocol()
        return await super().pick_up_tips(tip_spots, use_channels, offsets, **backend_kwargs)

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
            resources,
            vols,
            use_channels,
            flow_rates,
            offsets,
            liquid_height,
            blow_out_air_volume,
            spread,
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
        return await super().drop_tips(tip_spots, use_channels, offsets, allow_nonzero_volume, **backend_kwargs)

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
            resources,
            vols,
            use_channels,
            flow_rates,
            offsets,
            liquid_height,
            blow_out_air_volume,
            spread,
            **backend_kwargs,
        )

    async def discard_tips(
        self,
        use_channels: Optional[List[int]] = None,
        allow_nonzero_volume: bool = True,
        offsets: Optional[List[Coordinate]] = None,
        **backend_kwargs,
    ):
        return await super().discard_tips(use_channels, allow_nonzero_volume, offsets, **backend_kwargs)

    def set_tiprack(self, tip_racks: Sequence[TipRack]):
        super().set_tiprack(tip_racks)

    async def move_to(self, well: Well, dis_to_top: float = 0, channel: int = 0):
        return await super().move_to(well, dis_to_top, channel)

    async def shaker_action(self, time: int, module_no: int, amplitude: int, is_wait: bool):
        return await self._unilabos_backend.shaker_action(time, module_no, amplitude, is_wait)

    async def heater_action(self, temperature: float, time: int):
        return await self._unilabos_backend.heater_action(temperature, time)  
    async def move_plate(
        self,
        plate: Plate,
        to: Resource,
        intermediate_locations: Optional[List[Coordinate]] = None,
        pickup_offset: Coordinate = Coordinate.zero(),
        destination_offset: Coordinate = Coordinate.zero(),
        drop_direction: GripDirection = GripDirection.FRONT,
        pickup_direction: GripDirection = GripDirection.FRONT,
        pickup_distance_from_top: float = 13.2 - 3.33,
        **backend_kwargs,
    ):

        return await super().move_plate(
            plate,
            to,
            intermediate_locations,
            pickup_offset,
            destination_offset,
            drop_direction,
            pickup_direction,
            pickup_distance_from_top,
            target_plate_number = to,
            **backend_kwargs,
        )

class PRCXI9300Backend(LiquidHandlerBackend):
    """PRCXI 9300 的后端实现，继承自 LiquidHandlerBackend。

    该类提供了与 PRCXI 9300 设备进行通信的基本方法，包括方案管理、自动化控制、运行状态查询等。
    """

    _num_channels = 8  # 默认通道数为 8
    _is_reset_ok = False
    _ros_node: BaseROS2DeviceNode

    @property
    def is_reset_ok(self) -> bool:
        self._is_reset_ok = self.api_client.get_reset_status()
        return self._is_reset_ok

    matrix_info: MatrixInfo
    protocol_name: str
    steps_todo_list = []

    def __init__(
        self,
        tablets_info: list[WorkTablets],
        host: str = "127.0.0.1",
        port: int = 9999,
        timeout: float = 10.0,
        channel_num: int = 8,
        axis: str = "Left",
        setup=True,
        debug=False,
        matrix_id="",
        is_9320=False,
    ) -> None:
        super().__init__()
        self.tablets_info = tablets_info
        self.matrix_id = matrix_id
        self.api_client = PRCXI9300Api(host, port, timeout, axis, debug, is_9320)
        self.host, self.port, self.timeout = host, port, timeout
        self._num_channels = channel_num
        self._execute_setup = setup
        self.debug = debug
        self.axis = "Left"

    async def shaker_action(self, time: int, module_no: int, amplitude: int, is_wait: bool):
        step = self.api_client.shaker_action(
            time=time,
            module_no=module_no,
            amplitude=amplitude,
            is_wait=is_wait,
        )
        self.steps_todo_list.append(step)
        return step


    async def pick_up_resource(self, pickup: ResourcePickup, **backend_kwargs):
        
        resource=pickup.resource
        offset=pickup.offset
        pickup_distance_from_top=pickup.pickup_distance_from_top
        direction=pickup.direction

        plate_number = int(resource.parent.name.replace("T", ""))
        is_whole_plate = True
        balance_height = 0
        step = self.api_client.clamp_jaw_pick_up(plate_number, is_whole_plate, balance_height)
        
        self.steps_todo_list.append(step)
        return step

    async def drop_resource(self, drop: ResourceDrop, **backend_kwargs):


        plate_number = None
        target_plate_number = backend_kwargs.get("target_plate_number", None)
        if target_plate_number is not None:
            plate_number = int(target_plate_number.name.replace("T", ""))


        is_whole_plate = True
        balance_height = 0
        if plate_number is None:
            raise ValueError("target_plate_number is required when dropping a resource")
        step = self.api_client.clamp_jaw_drop(plate_number, is_whole_plate, balance_height)
        self.steps_todo_list.append(step)
        return step


    async def heater_action(self, temperature: float, time: int):
        print(f"\n\nHeater action: temperature={temperature}, time={time}\n\n")
        # return await self.api_client.heater_action(temperature, time)

    def post_init(self, ros_node: BaseROS2DeviceNode):
        self._ros_node = ros_node

    def create_protocol(self, protocol_name):
        self.protocol_name = protocol_name
        self.steps_todo_list = []

    def run_protocol(self):
        assert self.is_reset_ok, "PRCXI9300Backend is not reset successfully. Please call setup() first."
        run_time = time.time()
        self.matrix_info = MatrixInfo(
            MatrixId=f"{int(run_time)}",
            MatrixName=f"protocol_{run_time}",
            MatrixCount=len(self.tablets_info),
            WorkTablets=self.tablets_info,
        )
        # print(json.dumps(self.matrix_info, indent=2))
        if not len(self.matrix_id):
            res = self.api_client.add_WorkTablet_Matrix(self.matrix_info)
            assert res["Success"], f"Failed to create matrix: {res.get('Message', 'Unknown error')}"
            print(f"PRCXI9300Backend created matrix with ID: {self.matrix_info['MatrixId']}, result: {res}")
            solution_id = self.api_client.add_solution(
                f"protocol_{run_time}", self.matrix_info["MatrixId"], self.steps_todo_list
            )
        else:
            print(f"PRCXI9300Backend using predefined worktable {self.matrix_id}, skipping matrix creation.")
            solution_id = self.api_client.add_solution(f"protocol_{run_time}", self.matrix_id, self.steps_todo_list)
        print(f"PRCXI9300Backend created solution with ID: {solution_id}")
        self.api_client.load_solution(solution_id)
        print(json.dumps(self.steps_todo_list, indent=2))
        if not self.api_client.start():
            return False
        if not self.api_client.wait_for_finish():
            return False
        return True

    @classmethod
    def check_channels(cls, use_channels: List[int]) -> List[int]:
        """检查通道是否符合要求，PRCXI9300Backend 只支持所有 8 个通道。"""
        if use_channels != [0, 1, 2, 3, 4, 5, 6, 7]:
            print("PRCXI9300Backend only supports all 8 channels, using default [0, 1, 2, 3, 4, 5, 6, 7].")
            return [0, 1, 2, 3, 4, 5, 6, 7]
        return use_channels

    async def setup(self):
        await super().setup()
        try:
            if self._execute_setup:
                # 先获取错误代码
                error_code = self.api_client.get_error_code()
                if error_code:
                    print(f"PRCXI9300 error code detected: {error_code}")
                
                # 清除错误代码
                self.api_client.clear_error_code()
                print("PRCXI9300 error code cleared.")
                self.api_client.call("IAutomation", "Stop")
                # 执行重置
                print("Starting PRCXI9300 reset...")
                self.api_client.call("IAutomation", "Reset")
                
                # 检查重置状态并等待完成
                while not self.is_reset_ok:
                    print("Waiting for PRCXI9300 to reset...")
                    if hasattr(self, '_ros_node') and self._ros_node is not None:
                        await self._ros_node.sleep(1)
                    else:
                        await asyncio.sleep(1)
                print("PRCXI9300 reset successfully.")
        except ConnectionRefusedError as e:
            raise RuntimeError(
                f"Failed to connect to PRCXI9300 API at {self.host}:{self.port}. "
                "Please ensure the PRCXI9300 service is running."
            ) from e

    async def stop(self):
        self.api_client.call("IAutomation", "Stop")

    async def pick_up_tips(self, ops: List[Pickup], use_channels: List[int] = None):
        """Pick up tips from the specified resource."""
        # INSERT_YOUR_CODE
        # Ensure use_channels is converted to a list of ints if it's an array
        if hasattr(use_channels, 'tolist'):
            _use_channels = use_channels.tolist()
        else:
            _use_channels = list(use_channels) if use_channels is not None else None
        if _use_channels == [0]:
            axis = "Left"
        elif _use_channels == [1]:
            axis = "Right"
        else:
            raise ValueError("Invalid use channels: " + str(_use_channels))
        plate_indexes = []
        for op in ops:
            plate = op.resource.parent
            deck = plate.parent.parent
            plate_index = deck.children.index(plate.parent)
            # print(f"Plate index: {plate_index}, Plate name: {plate.name}")
            # print(f"Number of children in deck: {len(deck.children)}")

            plate_indexes.append(plate_index)

        if len(set(plate_indexes)) != 1:
            raise ValueError("All pickups must be from the same plate. Found different plates: " + str(plate_indexes))

        tip_columns = []
        for op in ops:
            tipspot = op.resource
            tipspot_index = tipspot.parent.children.index(tipspot)
            tip_columns.append(tipspot_index // 8)
        if len(set(tip_columns)) != 1:
            raise ValueError(
                "All pickups must be from the same tip column. Found different columns: " + str(tip_columns)
            )
        PlateNo = plate_indexes[0] + 1
        hole_col = tip_columns[0] + 1
        hole_row = 1
        if self._num_channels == 1:
            hole_row = tipspot_index % 8 + 1

        step = self.api_client.Load(
            axis=axis,
            dosage=0,
            plate_no=PlateNo,
            is_whole_plate=False,
            hole_row=hole_row,
            hole_col=hole_col,
            blending_times=0,
            balance_height=0,
            plate_or_hole=f"H{hole_col}-8,T{PlateNo}",
            hole_numbers=f"{(hole_col - 1) * 8 + hole_row}" if self._num_channels == 1 else "1,2,3,4,5",
        )
        self.steps_todo_list.append(step)

    async def drop_tips(self, ops: List[Drop], use_channels: List[int] = None):
        """Pick up tips from the specified resource."""
        if hasattr(use_channels, 'tolist'):
            _use_channels = use_channels.tolist()
        else:
            _use_channels = list(use_channels) if use_channels is not None else None
        if _use_channels == [0]:
            axis = "Left"
        elif _use_channels == [1]:
            axis = "Right"
        else:
            raise ValueError("Invalid use channels: " + str(_use_channels))
        # 检查trash #
        if ops[0].resource.name == "trash":

            PlateNo = ops[0].resource.parent.parent.children.index(ops[0].resource.parent) + 1

            step = self.api_client.UnLoad(
                axis=axis,
                dosage=0,
                plate_no=PlateNo,
                is_whole_plate=False,
                hole_row=1,
                hole_col=3,
                blending_times=0,
                balance_height=0,
                plate_or_hole=f"H{1}-8,T{PlateNo}",
                hole_numbers="1,2,3,4,5,6,7,8",
            )
            self.steps_todo_list.append(step)
            return
        # print(ops[0].resource.parent.children.index(ops[0].resource))

        plate_indexes = []
        for op in ops:
            plate = op.resource.parent
            deck = plate.parent.parent
            plate_index = deck.children.index(plate.parent)
            plate_indexes.append(plate_index)
        if len(set(plate_indexes)) != 1:
            raise ValueError(
                "All drop_tips must be from the same plate. Found different plates: " + str(plate_indexes)
            )

        tip_columns = []
        for op in ops:
            tipspot = op.resource
            tipspot_index = tipspot.parent.children.index(tipspot)
            tip_columns.append(tipspot_index // 8)
        if len(set(tip_columns)) != 1:
            raise ValueError(
                "All drop_tips must be from the same tip column. Found different columns: " + str(tip_columns)
            )

        PlateNo = plate_indexes[0] + 1
        hole_col = tip_columns[0] + 1

        if self.channel_num == 1:
            hole_row = tipspot_index % 8 + 1

        step = self.api_client.UnLoad(
            axis=axis,
            dosage=0,
            plate_no=PlateNo,
            is_whole_plate=False,
            hole_row=hole_row,
            hole_col=hole_col,
            blending_times=0,
            balance_height=0,
            plate_or_hole=f"H{hole_col}-8,T{PlateNo}",
            hole_numbers="1,2,3,4,5,6,7,8",
        )
        self.steps_todo_list.append(step)

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
        """Mix liquid in the specified resources."""
        
        plate_indexes = []
        for op in targets:
            deck = op.parent.parent.parent
            plate = op.parent
            plate_index = deck.children.index(plate.parent)
            plate_indexes.append(plate_index)

        if len(set(plate_indexes)) != 1:
            raise ValueError("All pickups must be from the same plate. Found different plates: " + str(plate_indexes))

        tip_columns = []
        for op in targets:
            tipspot_index = op.parent.children.index(op)
            tip_columns.append(tipspot_index // 8)

        if len(set(tip_columns)) != 1:
            raise ValueError(
                "All pickups must be from the same tip column. Found different columns: " + str(tip_columns)
            )

        PlateNo = plate_indexes[0] + 1
        hole_col = tip_columns[0] + 1
        hole_row = 1
        if self.num_channels == 1:
            hole_row = tipspot_index % 8 + 1

        assert mix_time > 0
        step = self.api_client.Blending(
            dosage=mix_vol,
            plate_no=PlateNo,
            is_whole_plate=False,
            hole_row=hole_row,
            hole_col=hole_col,
            blending_times=mix_time,
            balance_height=0,
            plate_or_hole=f"H{hole_col}-8,T{PlateNo}",
            hole_numbers="1,2,3,4,5,6,7,8",
        )
        self.steps_todo_list.append(step)

    async def aspirate(self, ops: List[SingleChannelAspiration], use_channels: List[int] = None):
        """Aspirate liquid from the specified resources."""
        if hasattr(use_channels, 'tolist'):
            _use_channels = use_channels.tolist()
        else:
            _use_channels = list(use_channels) if use_channels is not None else None
        if _use_channels == [0]:
            axis = "Left"
        elif _use_channels == [1]:
            axis = "Right"
        else:
            raise ValueError("Invalid use channels: " + str(_use_channels))
        plate_indexes = []
        for op in ops:
            plate = op.resource.parent
            deck = plate.parent.parent
            plate_index = deck.children.index(plate.parent)
            plate_indexes.append(plate_index)

        if len(set(plate_indexes)) != 1:
            raise ValueError("All pickups must be from the same plate. Found different plates: " + str(plate_indexes))

        tip_columns = []
        for op in ops:
            tipspot = op.resource
            tipspot_index = tipspot.parent.children.index(tipspot)
            tip_columns.append(tipspot_index // 8)

        if len(set(tip_columns)) != 1:
            raise ValueError(
                "All pickups must be from the same tip column. Found different columns: " + str(tip_columns)
            )

        volumes = [op.volume for op in ops]
        if len(set(volumes)) != 1:
            raise ValueError("All aspirate volumes must be the same. Found different volumes: " + str(volumes))

        PlateNo = plate_indexes[0] + 1
        hole_col = tip_columns[0] + 1
        hole_row = 1
        if self.num_channels == 1:
            hole_row = tipspot_index % 8 + 1

        step = self.api_client.Imbibing(
            axis=axis,
            dosage=int(volumes[0]),
            plate_no=PlateNo,
            is_whole_plate=False,
            hole_row=hole_row,
            hole_col=hole_col,
            blending_times=0,
            balance_height=0,
            plate_or_hole=f"H{hole_col}-8,T{PlateNo}",
            hole_numbers="1,2,3,4,5,6,7,8",
        )
        self.steps_todo_list.append(step)

    async def dispense(self, ops: List[SingleChannelDispense], use_channels: List[int] = None):
        """Dispense liquid into the specified resources."""
        if hasattr(use_channels, 'tolist'):
            _use_channels = use_channels.tolist()
        else:
            _use_channels = list(use_channels) if use_channels is not None else None
        if _use_channels == [0]:
            axis = "Left"
        elif _use_channels == [1]:
            axis = "Right"
        else:
            raise ValueError("Invalid use channels: " + str(_use_channels))
        plate_indexes = []
        for op in ops:
            plate = op.resource.parent
            deck = plate.parent.parent
            plate_index = deck.children.index(plate.parent)
            plate_indexes.append(plate_index)

        if len(set(plate_indexes)) != 1:
            raise ValueError("All dispense must be from the same plate. Found different plates: " + str(plate_indexes))

        tip_columns = []
        for op in ops:
            tipspot = op.resource
            tipspot_index = tipspot.parent.children.index(tipspot)
            tip_columns.append(tipspot_index // 8)

        if len(set(tip_columns)) != 1:
            raise ValueError(
                "All dispense must be from the same tip column. Found different columns: " + str(tip_columns)
            )

        volumes = [op.volume for op in ops]
        if len(set(volumes)) != 1:
            raise ValueError("All dispense volumes must be the same. Found different volumes: " + str(volumes))

        PlateNo = plate_indexes[0] + 1
        hole_col = tip_columns[0] + 1

        hole_row = 1
        if self.num_channels == 1:
            hole_row = tipspot_index % 8 + 1

        step = self.api_client.Tapping(
            axis=axis,
            dosage=int(volumes[0]),
            plate_no=PlateNo,
            is_whole_plate=False,
            hole_row=hole_row,
            hole_col=hole_col,
            blending_times=0,
            balance_height=0,
            plate_or_hole=f"H{hole_col}-8,T{PlateNo}",
            hole_numbers="1,2,3,4,5,6,7,8",
        )
        self.steps_todo_list.append(step)

    async def pick_up_tips96(self, pickup: PickupTipRack):
        raise NotImplementedError("The PRCXI backend does not support the 96 head.")

    async def drop_tips96(self, drop: DropTipRack):
        raise NotImplementedError("The PRCXI backend does not support the 96 head.")

    async def aspirate96(self, aspiration: Union[MultiHeadAspirationPlate, MultiHeadAspirationContainer]):
        raise NotImplementedError("The Opentrons backend does not support the 96 head.")

    async def dispense96(self, dispense: Union[MultiHeadDispensePlate, MultiHeadDispenseContainer]):
        raise NotImplementedError("The Opentrons backend does not support the 96 head.")

    async def move_picked_up_resource(self, move: ResourceMove):
        pass

    def can_pick_up_tip(self, channel_idx: int, tip: Tip) -> bool:
        return True  # PRCXI9300Backend does not have tip compatibility issues

    def serialize(self) -> dict:
        raise NotImplementedError()

    @property
    def num_channels(self) -> int:
        return self._num_channels


class PRCXI9300Api:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 9999,
        timeout: float = 10.0,
        axis="Left",
        debug: bool = False,
        is_9320: bool = False,
    ) -> None:
        self.host, self.port, self.timeout = host, port, timeout
        self.debug = debug
        self.axis = axis
        self.is_9320 = is_9320

    @staticmethod
    def _len_prefix(n: int) -> bytes:
        return bytes.fromhex(format(n, "016x"))

    def _raw_request(self, payload: str) -> str:
        if self.debug:
            # 调试/仿真模式下直接返回可解析的模拟 JSON，避免后续 json.loads 报错
            try:
                req = json.loads(payload)
                method = req.get("MethodName")
            except Exception:
                method = None

            data: Any = True
            if method in {"AddSolution"}:
                data = str(uuid.uuid4())
            elif method in {"AddWorkTabletMatrix", "AddWorkTabletMatrix2"}:
                data = {"Success": True, "Message": "debug mock"}
            elif method in {"GetErrorCode"}:
                data = ""
            elif method in {"RemoveErrorCodet", "Reset", "Start", "LoadSolution", "Pause", "Resume", "Stop"}:
                data = True
            elif method in {"GetStepStateList", "GetStepStatus", "GetStepState"}:
                data = []
            elif method in {"GetLocation"}:
                data = {"X": 0, "Y": 0, "Z": 0}
            elif method in {"GetResetStatus"}:
                data = False

            return json.dumps({"Success": True, "Msg": "debug mock", "Data": data})
        with contextlib.closing(socket.socket()) as sock:
            sock.settimeout(self.timeout)
            sock.connect((self.host, self.port))
            data = payload.encode()
            sock.sendall(self._len_prefix(len(data)) + data)

            chunks, first = [], True
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                if first:
                    chunk, first = chunk[8:], False
                chunks.append(chunk)
            return b"".join(chunks).decode()

    # ---------------------------------------------------- 方案相关（ISolution）
    def list_solutions(self) -> List[Dict[str, Any]]:
        """GetSolutionList"""
        return self.call("ISolution", "GetSolutionList")

    def load_solution(self, solution_id: str) -> bool:
        """LoadSolution"""
        return self.call("ISolution", "LoadSolution", [solution_id])

    def add_solution(self, name: str, matrix_id: str, steps: List[Dict[str, Any]]) -> str:
        """AddSolution → 返回新方案 GUID"""
        return self.call("ISolution", "AddSolution", [name, matrix_id, steps])

    # ---------------------------------------------------- 自动化控制（IAutomation）
    def start(self) -> bool:
        return self.call("IAutomation", "Start")

    def wait_for_finish(self) -> bool:
        success = False
        start = False
        while not success:
            status = self.step_state_list()
            if len(status) == 1:
                start = True
            if status is None:
                break
            if len(status) == 0:
                break
            if status[-1]["State"] == 2 and start:
                success = True
            elif status[-1]["State"] > 2:
                break
            elif status[-1]["State"] == 0:
                start = True
            else:
                time.sleep(1)
        return success


    def call(self, service: str, method: str, params: Optional[list] = None) -> Any:
        payload = json.dumps(
            {"ServiceName": service, "MethodName": method, "Paramters": params or []}, separators=(",", ":")
        )
        resp = json.loads(self._raw_request(payload))
        if not resp.get("Success", False):
            raise PRCXIError(resp.get("Msg", "Unknown error"))
        data = resp.get("Data")
        try:
            return json.loads(data)
        except (TypeError, json.JSONDecodeError):
            return data

    def pause(self) -> bool:
        """Pause"""
        return self.call("IAutomation", "Pause")

    def resume(self) -> bool:
        """Resume"""
        return self.call("IAutomation", "Resume")

    def get_error_code(self) -> Optional[str]:
        """GetErrorCode"""
        return self.call("IAutomation", "GetErrorCode")

    def get_reset_status(self) -> bool:
        """GetErrorCode"""
        if self.debug:
            return True
        res = self.call("IAutomation", "GetResetStatus")
        return not res

    def clear_error_code(self) -> bool:
        """RemoveErrorCodet"""
        return self.call("IAutomation", "RemoveErrorCodet")

    # ---------------------------------------------------- 运行状态（IMachineState）
    def step_state_list(self) -> List[Dict[str, Any]]:
        """GetStepStateList"""
        return self.call("IMachineState", "GetStepStateList")

    def step_status(self, seq_num: int) -> Dict[str, Any]:
        """GetStepStatus"""
        return self.call("IMachineState", "GetStepStatus", [seq_num])

    def step_state(self, seq_num: int) -> Dict[str, Any]:
        """GetStepState"""
        return self.call("IMachineState", "GetStepState", [seq_num])

    def axis_location(self, axis_num: int = 1) -> Dict[str, Any]:
        """GetLocation"""
        return self.call("IMachineState", "GetLocation", [axis_num])

    # ---------------------------------------------------- 版位矩阵（IMatrix）
    def get_all_materials(self) -> Dict[str, Any]:
        """GetStepState"""
        return self.call("IMatrix", "GetAllMaterial", [])

    def list_matrices(self) -> List[Dict[str, Any]]:
        """GetWorkTabletMatrices"""
        return self.call("IMatrix", "GetWorkTabletMatrices")

    def matrix_by_id(self, matrix_id: str) -> Dict[str, Any]:
        """GetWorkTabletMatrixById"""
        return self.call("IMatrix", "GetWorkTabletMatrixById", [matrix_id])

    def add_WorkTablet_Matrix(self, matrix: MatrixInfo):
        return self.call("IMatrix", "AddWorkTabletMatrix2" if self.is_9320 else "AddWorkTabletMatrix", [matrix])

    def Load(
        self,
        dosage: int,
        plate_no: int,
        is_whole_plate: bool,
        hole_row: int,
        hole_col: int,
        blending_times: int,
        balance_height: int,
        plate_or_hole: str,
        hole_numbers: str,
        assist_fun1: str = "",
        assist_fun2: str = "",
        assist_fun3: str = "",
        assist_fun4: str = "",
        assist_fun5: str = "",
        liquid_method: str = "NormalDispense",
        axis: str = "Left",
    ) -> Dict[str, Any]:
        return {
            "StepAxis": axis,
            "Function": "Load",
            "DosageNum": dosage,
            "PlateNo": plate_no,
            "IsWholePlate": is_whole_plate,
            "HoleRow": hole_row,
            "HoleCol": hole_col,
            "BlendingTimes": blending_times,
            "BalanceHeight": balance_height,
            "PlateOrHoleNum": plate_or_hole,
            "AssistFun1": assist_fun1,
            "AssistFun2": assist_fun2,
            "AssistFun3": assist_fun3,
            "AssistFun4": assist_fun4,
            "AssistFun5": assist_fun5,
            "HoleNumbers": hole_numbers,
            "LiquidDispensingMethod": liquid_method,
        }

    def Imbibing(
        self,
        dosage: int,
        plate_no: int,
        is_whole_plate: bool,
        hole_row: int,
        hole_col: int,
        blending_times: int,
        balance_height: int,
        plate_or_hole: str,
        hole_numbers: str,
        assist_fun1: str = "",
        assist_fun2: str = "",
        assist_fun3: str = "",
        assist_fun4: str = "",
        assist_fun5: str = "",
        liquid_method: str = "NormalDispense",
        axis: str = "Left",
        ) -> Dict[str, Any]:
        return {
            "StepAxis": axis,
            "Function": "Imbibing",
            "DosageNum": dosage,
            "PlateNo": plate_no,
            "IsWholePlate": is_whole_plate,
            "HoleRow": hole_row,
            "HoleCol": hole_col,
            "BlendingTimes": blending_times,
            "BalanceHeight": balance_height,
            "PlateOrHoleNum": plate_or_hole,
            "AssistFun1": assist_fun1,
            "AssistFun2": assist_fun2,
            "AssistFun3": assist_fun3,
            "AssistFun4": assist_fun4,
            "AssistFun5": assist_fun5,
            "HoleNumbers": hole_numbers,
            "LiquidDispensingMethod": liquid_method,
        }

    def Tapping(
        self,
        dosage: int,
        plate_no: int,
        is_whole_plate: bool,
        hole_row: int,
        hole_col: int,
        blending_times: int,
        balance_height: int,
        plate_or_hole: str,
        hole_numbers: str,
        assist_fun1: str = "",
        assist_fun2: str = "",
        assist_fun3: str = "",
        assist_fun4: str = "",
        assist_fun5: str = "",
        liquid_method: str = "NormalDispense",
        axis: str = "Left",
    ) -> Dict[str, Any]:
        return {
            "StepAxis": axis,
            "Function": "Tapping",
            "DosageNum": dosage,
            "PlateNo": plate_no,
            "IsWholePlate": is_whole_plate,
            "HoleRow": hole_row,
            "HoleCol": hole_col,
            "BlendingTimes": blending_times,
            "BalanceHeight": balance_height,
            "PlateOrHoleNum": plate_or_hole,
            "AssistFun1": assist_fun1,
            "AssistFun2": assist_fun2,
            "AssistFun3": assist_fun3,
            "AssistFun4": assist_fun4,
            "AssistFun5": assist_fun5,
            "HoleNumbers": hole_numbers,
            "LiquidDispensingMethod": liquid_method,
        }

    def Blending(
        self,
        dosage: int,
        plate_no: int,
        is_whole_plate: bool,
        hole_row: int,
        hole_col: int,
        blending_times: int,
        balance_height: int,
        plate_or_hole: str,
        hole_numbers: str,
        assist_fun1: str = "",
        assist_fun2: str = "",
        assist_fun3: str = "",
        assist_fun4: str = "",
        assist_fun5: str = "",
        liquid_method: str = "NormalDispense",
        axis: str = "Left",
        ) -> Dict[str, Any]:
        return {
            "StepAxis": axis,
            "Function": "Blending",
            "DosageNum": dosage,
            "PlateNo": plate_no,
            "IsWholePlate": is_whole_plate,
            "HoleRow": hole_row,
            "HoleCol": hole_col,
            "BlendingTimes": blending_times,
            "BalanceHeight": balance_height,
            "PlateOrHoleNum": plate_or_hole,
            "AssistFun1": assist_fun1,
            "AssistFun2": assist_fun2,
            "AssistFun3": assist_fun3,
            "AssistFun4": assist_fun4,
            "AssistFun5": assist_fun5,
            "HoleNumbers": hole_numbers,
            "LiquidDispensingMethod": liquid_method,
        }

    def UnLoad(
        self,
        dosage: int,
        plate_no: int,
        is_whole_plate: bool,
        hole_row: int,
        hole_col: int,
        blending_times: int,
        balance_height: int,
        plate_or_hole: str,
        hole_numbers: str,
        assist_fun1: str = "",
        assist_fun2: str = "",
        assist_fun3: str = "",
        assist_fun4: str = "",
        assist_fun5: str = "",
        liquid_method: str = "NormalDispense",
        axis: str = "Left",
    ) -> Dict[str, Any]:
        return {
            "StepAxis": axis,
            "Function": "UnLoad",
            "DosageNum": dosage,
            "PlateNo": plate_no,
            "IsWholePlate": is_whole_plate,
            "HoleRow": hole_row,
            "HoleCol": hole_col,
            "BlendingTimes": blending_times,
            "BalanceHeight": balance_height,
            "PlateOrHoleNum": plate_or_hole,
            "AssistFun1": assist_fun1,
            "AssistFun2": assist_fun2,
            "AssistFun3": assist_fun3,
            "AssistFun4": assist_fun4,
            "AssistFun5": assist_fun5,
            "HoleNumbers": hole_numbers,
            "LiquidDispensingMethod": liquid_method,
        }

    def clamp_jaw_pick_up(self,
        plate_no: int,
        is_whole_plate: bool,
        balance_height: int,

    ) -> Dict[str, Any]:
        return {
            "StepAxis": "ClampingJaw",
            "Function": "DefectiveLift",
            "PlateNo": plate_no,
            "IsWholePlate": is_whole_plate,
            "HoleRow": 1,
            "HoleCol": 1,
            "BalanceHeight": balance_height,
            "PlateOrHoleNum": f"T{plate_no}"
        }

    def clamp_jaw_drop(
        self,
        plate_no: int,
        is_whole_plate: bool,
        balance_height: int,

    ) -> Dict[str, Any]:
        return {
            "StepAxis": "ClampingJaw",
            "Function": "PutDown",
            "PlateNo": plate_no,
            "IsWholePlate": is_whole_plate,
            "HoleRow": 1,
            "HoleCol": 1,
            "BalanceHeight": balance_height,
            "PlateOrHoleNum": f"T{plate_no}"
        }

    def shaker_action(self, time: int, module_no: int, amplitude: int, is_wait: bool):
        return {
            "StepAxis": "Left",
            "Function": "Shaking",
            "AssistFun1": time,
            "AssistFun2": module_no,
            "AssistFun3": amplitude,
            "AssistFun4": is_wait,
        }

class DefaultLayout:

    def __init__(self, product_name: str = "PRCXI9300"):
        self.labresource = {}
        if product_name not in ["PRCXI9300", "PRCXI9320"]:
            raise ValueError(
                f"Unsupported product_name: {product_name}. Only 'PRCXI9300' and 'PRCXI9320' are supported."
            )

        if product_name == "PRCXI9300":
            self.rows = 2
            self.columns = 3
            self.layout = [1, 2, 3, 4, 5, 6]
            self.trash_slot = 3
            self.waste_liquid_slot = 6

        elif product_name == "PRCXI9320":
            self.rows = 4
            self.columns = 4
            self.layout = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16]
            self.trash_slot = 16
            self.waste_liquid_slot = 12
            self.default_layout = {
                "MatrixId": f"{time.time()}",
                "MatrixName": f"{time.time()}",
                "MatrixCount": 16,
                "WorkTablets": [
                    {
                        "Number": 1,
                        "Code": "T1",
                        "Material": {"uuid": "57b1e4711e9e4a32b529f3132fc5931f", "materialEnum": 0},
                    },
                    {
                        "Number": 2,
                        "Code": "T2",
                        "Material": {"uuid": "57b1e4711e9e4a32b529f3132fc5931f", "materialEnum": 0},
                    },
                    {
                        "Number": 3,
                        "Code": "T3",
                        "Material": {"uuid": "57b1e4711e9e4a32b529f3132fc5931f", "materialEnum": 0},
                    },
                    {
                        "Number": 4,
                        "Code": "T4",
                        "Material": {"uuid": "57b1e4711e9e4a32b529f3132fc5931f", "materialEnum": 0},
                    },
                    {
                        "Number": 5,
                        "Code": "T5",
                        "Material": {"uuid": "57b1e4711e9e4a32b529f3132fc5931f", "materialEnum": 0},
                    },
                    {
                        "Number": 6,
                        "Code": "T6",
                        "Material": {"uuid": "57b1e4711e9e4a32b529f3132fc5931f", "materialEnum": 0},
                    },
                    {
                        "Number": 7,
                        "Code": "T7",
                        "Material": {"uuid": "57b1e4711e9e4a32b529f3132fc5931f", "materialEnum": 0},
                    },
                    {
                        "Number": 8,
                        "Code": "T8",
                        "Material": {"uuid": "57b1e4711e9e4a32b529f3132fc5931f", "materialEnum": 0},
                    },
                    {
                        "Number": 9,
                        "Code": "T9",
                        "Material": {"uuid": "57b1e4711e9e4a32b529f3132fc5931f", "materialEnum": 0},
                    },
                    {
                        "Number": 10,
                        "Code": "T10",
                        "Material": {"uuid": "57b1e4711e9e4a32b529f3132fc5931f", "materialEnum": 0},
                    },
                    {
                        "Number": 11,
                        "Code": "T11",
                        "Material": {"uuid": "57b1e4711e9e4a32b529f3132fc5931f", "materialEnum": 0},
                    },
                    {
                        "Number": 12,
                        "Code": "T12",
                        "Material": {"uuid": "730067cf07ae43849ddf4034299030e9", "materialEnum": 0},
                    },  # 这个设置成废液槽，用储液槽表示
                    {
                        "Number": 13,
                        "Code": "T13",
                        "Material": {"uuid": "57b1e4711e9e4a32b529f3132fc5931f", "materialEnum": 0},
                    },
                    {
                        "Number": 14,
                        "Code": "T14",
                        "Material": {"uuid": "57b1e4711e9e4a32b529f3132fc5931f", "materialEnum": 0},
                    },
                    {
                        "Number": 15,
                        "Code": "T15",
                        "Material": {"uuid": "57b1e4711e9e4a32b529f3132fc5931f", "materialEnum": 0},
                    },
                    {
                        "Number": 16,
                        "Code": "T16",
                        "Material": {"uuid": "730067cf07ae43849ddf4034299030e9", "materialEnum": 0},
                    },  # 这个设置成垃圾桶，用储液槽表示
                ],
            }

    def get_layout(self) -> Dict[str, Any]:
        return {
            "rows": self.rows,
            "columns": self.columns,
            "layout": self.layout,
            "trash_slot": self.trash_slot,
            "waste_liquid_slot": self.waste_liquid_slot,
        }

    def get_trash_slot(self) -> int:
        return self.trash_slot

    def get_waste_liquid_slot(self) -> int:
        return self.waste_liquid_slot

    def add_lab_resource(self, material_info):
        self.labresource = material_info

    def recommend_layout(self, needs: List[Tuple[str, str, int]]) -> Dict[str, Any]:
        layout_list = []
        for reagent_name, material_name, count in needs:

            if material_name not in self.labresource:
                raise ValueError(f"Material {reagent_name} not found in lab resources.")

            # 预留位置12和16不动
        reserved_positions = {12, 16}
        available_positions = [i for i in range(1, 17) if i not in reserved_positions]

        # 计算总需求
        total_needed = sum(count for _, _, count in needs)
        if total_needed > len(available_positions):
            raise ValueError(
                f"需要 {total_needed} 个位置，但只有 {len(available_positions)} 个可用位置（排除位置12和16）"
            )

            # 依次分配位置
        current_pos = 0
        for reagent_name, material_name, count in needs:

            material_uuid = self.labresource[material_name]["uuid"]
            material_enum = self.labresource[material_name]["materialEnum"]

            for _ in range(count):
                if current_pos >= len(available_positions):
                    raise ValueError("位置不足，无法分配更多物料")

                position = available_positions[current_pos]
                # 找到对应的tablet并更新
                for tablet in self.default_layout["WorkTablets"]:
                    if tablet["Number"] == position:
                        tablet["Material"]["uuid"] = material_uuid
                        tablet["Material"]["materialEnum"] = material_enum
                        layout_list.append(
                            dict(reagent_name=reagent_name, material_name=material_name, positions=position)
                        )
                        break
                current_pos += 1
        return self.default_layout, layout_list


if __name__ == "__main__":
    # Example usage
    # 1. 用导出的json，给每个T1 T2板子设定相应的物料，如果是孔板和枪头盒，要对应区分
    # 2. backend需要支持num channel为1的情况
    # 3. 设计一个单点动作流程，可以跑
    # 4.

    # deck = PRCXI9300Deck(name="PRCXI_Deck_9300", size_x=100, size_y=100, size_z=100)

    # from pylabrobot.resources.opentrons.tip_racks import opentrons_96_tiprack_300ul,opentrons_96_tiprack_10ul
    # from pylabrobot.resources.opentrons.plates import corning_96_wellplate_360ul_flat, nest_96_wellplate_2ml_deep

    # def get_well_container(name: str) -> PRCXI9300Container:
    #     well_containers = corning_96_wellplate_360ul_flat(name).serialize()
    #     plate = PRCXI9300Container(name=name, size_x=50, size_y=50, size_z=10, category="plate",
    #                        ordering=well_containers["ordering"])
    #     plate_serialized = plate.serialize()
    #     plate_serialized["parent_name"] = deck.name
    #     well_containers.update({k: v for k, v in plate_serialized.items() if k not in ["children"]})
    #     new_plate: PRCXI9300Container = PRCXI9300Container.deserialize(well_containers)
    #     return new_plate

    # def get_tip_rack(name: str) -> PRCXI9300Container:
    #     tip_racks = opentrons_96_tiprack_300ul("name").serialize()
    #     tip_rack = PRCXI9300Container(name=name, size_x=50, size_y=50, size_z=10, category="tip_rack",
    #                        ordering=tip_racks["ordering"])
    #     tip_rack_serialized = tip_rack.serialize()
    #     tip_rack_serialized["parent_name"] = deck.name
    #     tip_racks.update({k: v for k, v in tip_rack_serialized.items() if k not in ["children"]})
    #     new_tip_rack: PRCXI9300Container = PRCXI9300Container.deserialize(tip_racks)
    #     return new_tip_rack

    # plate1 = get_tip_rack("RackT1")
    # plate1.load_state({
    #     "Material": {
    #         "uuid": "076250742950465b9d6ea29a225dfb00",
    #         "Code": "ZX-001-300",
    #         "Name": "300μL Tip头"
    #     }
    # })

    # plate2 = get_well_container("PlateT2")
    # plate2.load_state({
    #     "Material": {
    #         "uuid": "57b1e4711e9e4a32b529f3132fc5931f",
    #         "Code": "ZX-019-2.2",
    #         "Name": "96深孔板"
    #     }
    # })

    # plate3 = PRCXI9300Trash("trash", size_x=50, size_y=100, size_z=10, category="trash")
    # plate3.load_state({
    #     "Material": {
    #         "uuid": "730067cf07ae43849ddf4034299030e9"
    #     }
    # })

    # plate4 = get_well_container("PlateT4")
    # plate4.load_state({
    #     "Material": {
    #         "uuid": "57b1e4711e9e4a32b529f3132fc5931f",
    #         "Code": "ZX-019-2.2",
    #         "Name": "96深孔板"
    #     }
    # })

    # plate5 = get_well_container("PlateT5")
    # plate5.load_state({
    #     "Material": {
    #         "uuid": "57b1e4711e9e4a32b529f3132fc5931f",
    #         "Code": "ZX-019-2.2",
    #         "Name": "96深孔板"
    #     }
    # })
    # plate6 = get_well_container("PlateT6")

    # plate6.load_state({
    #     "Material": {
    #         "uuid": "57b1e4711e9e4a32b529f3132fc5931f",
    #         "Code": "ZX-019-2.2",
    #         "Name": "96深孔板"
    #     }
    # })

    # deck.assign_child_resource(plate1, location=Coordinate(0, 0, 0))
    # deck.assign_child_resource(plate2, location=Coordinate(0, 0, 0))
    # deck.assign_child_resource(plate3, location=Coordinate(0, 0, 0))
    # deck.assign_child_resource(plate4, location=Coordinate(0, 0, 0))
    # deck.assign_child_resource(plate5, location=Coordinate(0, 0, 0))
    # deck.assign_child_resource(plate6, location=Coordinate(0, 0, 0))

    # # # plate_2_liquids = [[('water', 500)]]*96

    # # # plate2.set_well_liquids(plate_2_liquids)

    # handler = PRCXI9300Handler(deck=deck, host="10.181.214.132", port=9999,
    #                            timeout=10.0, setup=False, debug=False,
    #                            simulator=True,
    #                            matrix_id="71593",
    #                            channel_num=8, axis="Left")  # Initialize the handler with the deck and host settings

    # plate_2_liquids = handler.set_group("water", plate2.children[:8], [200]*8)

    # plate5_liquids = handler.set_group("master_mix", plate5.children[:8], [100]*8)

    # handler.set_tiprack([plate1])
    # asyncio.run(handler.setup())  # Initialize the handler and setup the connection
    # from pylabrobot.resources import set_volume_tracking
    # from pylabrobot.resources import set_tip_tracking
    # set_volume_tracking(enabled=True)
    # from unilabos.resources.graphio import *
    # # A = tree_to_list([resource_plr_to_ulab(deck)])
    # # with open("deck_9300_new.json", "w", encoding="utf-8") as f:
    # #     json.dump(A, f, indent=4, ensure_ascii=False)
    # asyncio.run(handler.create_protocol(protocol_name="Test Protocol"))  # Initialize the backend and setup the connection
    # asyncio.run(handler.transfer_group("water", "master_mix", 100))  # Reset tip tracking

    # asyncio.run(handler.pick_up_tips(plate1.children[:8],[0,1,2,3,4,5,6,7]))
    # print(plate1.children[:8])
    # asyncio.run(handler.aspirate(plate2.children[:8],[50]*8, [0,1,2,3,4,5,6,7]))
    # print(plate2.children[:8])
    # asyncio.run(handler.dispense(plate5.children[:8],[50]*8,[0,1,2,3,4,5,6,7]))
    # print(plate5.children[:8])

    # #asyncio.run(handler.drop_tips(tip_rack.children[8:16],[0,1,2,3,4,5,6,7]))
    # asyncio.run(handler.discard_tips([0,1,2,3,4,5,6,7]))

    # asyncio.run(handler.mix(well_containers.children[:8
    # ], mix_time=3, mix_vol=50, height_to_bottom=0.5, offsets=Coordinate(0, 0, 0), mix_rate=100))
    # #print(json.dumps(handler._unilabos_backend.steps_todo_list, indent=2))  # Print matrix info
    # asyncio.run(handler.add_liquid(
    #     asp_vols=[100]*16,
    #     dis_vols=[100]*16,
    #     reagent_sources=plate2.children[:16],
    #     targets=plate5.children[:16],
    #     use_channels=[0, 1, 2, 3, 4, 5, 6, 7],
    #     flow_rates=[None] * 32,
    #     offsets=[Coordinate(0, 0, 0)] * 32,
    #     liquid_height=[None] * 16,
    #     blow_out_air_volume=[None] * 16,
    #     delays=None,
    #     mix_time=3,
    #     mix_vol=50,
    #     spread="wide",
    # ))
    # asyncio.run(handler.run_protocol())  # Run the protocol
    # asyncio.run(handler.remove_liquid(
    #     vols=[100]*16,
    #     sources=plate2.children[-16:],
    #     waste_liquid=plate5.children[:16], # 这个有些奇怪，但是好像也只能这么写
    #     use_channels=[0, 1, 2, 3, 4, 5, 6, 7],
    #     flow_rates=[None] * 32,
    #     offsets=[Coordinate(0, 0, 0)] * 32,
    #     liquid_height=[None] * 32,
    #     blow_out_air_volume=[None] * 32,
    #     spread="wide",
    # ))

    # acid = [20]*8+[40]*8+[60]*8+[80]*8+[100]*8+[120]*8+[140]*8+[160]*8+[180]*8+[200]*8+[220]*8+[240]*8
    # alkaline = acid[::-1]  # Reverse the acid list for alkaline
    # asyncio.run(handler.transfer_liquid(
    #     asp_vols=acid,
    #     dis_vols=acid,
    #     tip_racks=[plate1],
    #     sources=plate2.children[:],
    #     targets=plate5.children[:],
    #     use_channels=[0, 1, 2, 3, 4, 5, 6, 7],
    #     offsets=[Coordinate(0, 0, 0)] * 32,
    #     asp_flow_rates=[None] * 16,
    #     dis_flow_rates=[None] * 16,
    #     liquid_height=[None] * 32,
    #     blow_out_air_volume=[None] * 32,
    #     mix_times=3,
    #     mix_vol=50,
    #     spread="wide",
    # ))
    # asyncio.run(handler.run_protocol())  # Run the protocol
    # # input("Running protocol...")
    # # input("Press Enter to continue...")  # Wait for user input before proceeding
    # # print("PRCXI9300Handler initialized with deck and host settings.")

    ### 9320 ###

    deck = PRCXI9300Deck(name="PRCXI_Deck", size_x=100, size_y=100, size_z=100)

    from pylabrobot.resources.opentrons.tip_racks import tipone_96_tiprack_200ul, opentrons_96_tiprack_10ul
    from pylabrobot.resources.opentrons.plates import corning_96_wellplate_360ul_flat, nest_96_wellplate_2ml_deep

    def get_well_container(name: str) -> PRCXI9300Plate:
        well_containers = corning_96_wellplate_360ul_flat(name).serialize()
        plate = PRCXI9300Plate(
            name=name, size_x=50, size_y=50, size_z=10, category="plate", ordered_items=well_containers["ordering"]
        )
        plate_serialized = plate.serialize()
        plate_serialized["parent_name"] = deck.name
        well_containers.update({k: v for k, v in plate_serialized.items() if k not in ["children"]})
        new_plate: PRCXI9300Plate = PRCXI9300Plate.deserialize(well_containers)
        return new_plate

    def get_tip_rack(name: str, child_prefix: str = "tip") -> PRCXI9300TipRack:
        tip_racks = opentrons_96_tiprack_10ul(name).serialize()
        tip_rack = PRCXI9300TipRack(
            name=name,
            size_x=50,
            size_y=50,
            size_z=10,
            category="tip_rack",
            ordered_items=collections.OrderedDict({k: f"{child_prefix}_{k}" for k, v in tip_racks["ordering"].items()}),
        )
        tip_rack_serialized = tip_rack.serialize()
        tip_rack_serialized["parent_name"] = deck.name
        tip_racks.update({k: v for k, v in tip_rack_serialized.items() if k not in ["children"]})
        new_tip_rack: PRCXI9300TipRack = PRCXI9300TipRack.deserialize(tip_racks)
        return new_tip_rack

    plate1 = get_tip_rack("RackT1")
    plate1.load_state(
        {"Material": {"uuid": "068b3815e36b4a72a59bae017011b29f", "Code": "ZX-001-10+", "Name": "10μL加长 Tip头"}}
    )
    plate2 = get_well_container("PlateT2")
    plate2.load_state(
        {"Material": {"uuid": "b05b3b2aafd94ec38ea0cd3215ecea8f", "Code": "ZX-78-096", "Name": "细菌培养皿"}}
    )
    plate3 = get_well_container("PlateT3")
    plate3.load_state(
        {
            "Material": {
                "uuid": "04211a2dc93547fe9bf6121eac533650",
            }
        }
    )
    plate4 = get_well_container("PlateT4")
    plate4.load_state(
        {"Material": {"uuid": "b05b3b2aafd94ec38ea0cd3215ecea8f", "Code": "ZX-78-096", "Name": "细菌培养皿"}}
    )

    plate5 = get_tip_rack("RackT5")
    plate5.load_state(
        {
            "Material": {
                "uuid": "076250742950465b9d6ea29a225dfb00",
                "Code": "ZX-001-300",
                "SupplyType": 1,
                "Name": "300μL Tip头",
            }
        }
    )
    plate6 = get_well_container("PlateT6")
    plate6.load_state(
        {
            "Material": {
                "uuid": "e146697c395e4eabb3d6b74f0dd6aaf7",
                "Code": "1",
                "SupplyType": 1,
                "Name": "ep适配器",
                "SummaryName": "ep适配器",
            }
        }
    )
    plate7 = PRCXI9300Plate(
        name="plateT7", size_x=50, size_y=50, size_z=10, category="plate", ordered_items=collections.OrderedDict()
    )
    plate7.load_state({"Material": {"uuid": "04211a2dc93547fe9bf6121eac533650"}})
    plate8 = get_tip_rack("PlateT8")
    plate8.load_state({"Material": {"uuid": "04211a2dc93547fe9bf6121eac533650"}})
    plate9 = get_well_container("PlateT9")
    plate9.load_state(
        {
            "Material": {
                "uuid": "4a043a07c65a4f9bb97745e1f129b165",
                "Code": "ZX-58-0001",
                "SupplyType": 2,
                "Name": "全裙边 PCR适配器",
                "SummaryName": "全裙边 PCR适配器",
            }
        }
    )
    plate10 = get_well_container("PlateT10")
    plate10.load_state(
        {
            "Material": {
                "uuid": "4a043a07c65a4f9bb97745e1f129b165",
                "Code": "ZX-58-0001",
                "SupplyType": 2,
                "Name": "全裙边 PCR适配器",
                "SummaryName": "全裙边 PCR适配器",
            }
        }
    )
    plate11 = get_well_container("PlateT11")
    plate11.load_state(
        {
            "Material": {
                "uuid": "04211a2dc93547fe9bf6121eac533650",
            }
        }
    )
    plate12 = get_well_container("PlateT12")
    plate12.load_state({"Material": {"uuid": "04211a2dc93547fe9bf6121eac533650"}})
    plate13 = get_well_container("PlateT13")
    plate13.load_state(
        {
            "Material": {
                "uuid": "4a043a07c65a4f9bb97745e1f129b165",
                "Code": "ZX-58-0001",
                "SupplyType": 2,
                "Name": "全裙边 PCR适配器",
                "SummaryName": "全裙边 PCR适配器",
            }
        }
    ),
    plate14 = get_well_container("PlateT14")
    plate14.load_state(
        {
            "Material": {
                "uuid": "4a043a07c65a4f9bb97745e1f129b165",
                "Code": "ZX-58-0001",
                "SupplyType": 2,
                "Name": "全裙边 PCR适配器",
                "SummaryName": "全裙边 PCR适配器",
            }
        }
    ),
    plate15 = get_well_container("PlateT15")
    plate15.load_state({"Material": {"uuid": "04211a2dc93547fe9bf6121eac533650"}})

    trash = PRCXI9300Trash(name="trash", size_x=50, size_y=50, size_z=10, category="trash")
    trash.load_state({"Material": {"uuid": "730067cf07ae43849ddf4034299030e9"}})

    # container_for_nothing = PRCXI9300Container(name="container_for_nothing", size_x=50, size_y=50, size_z=10, category="plate", ordering=collections.OrderedDict())

    deck.assign_child_resource(plate1, location=Coordinate(0, 0, 0))
    deck.assign_child_resource(plate2, location=Coordinate(0, 0, 0))
    deck.assign_child_resource(
        PRCXI9300Plate(
            name="container_for_nothin3",
            size_x=50,
            size_y=50,
            size_z=10,
            category="plate",
            ordered_items=collections.OrderedDict(),
        ),
        location=Coordinate(0, 0, 0),
    )
    deck.assign_child_resource(plate4, location=Coordinate(0, 0, 0))
    deck.assign_child_resource(plate5, location=Coordinate(0, 0, 0))
    deck.assign_child_resource(plate6, location=Coordinate(0, 0, 0))
    deck.assign_child_resource(
        PRCXI9300Plate(
            name="container_for_nothing7",
            size_x=50,
            size_y=50,
            size_z=10,
            category="plate",
            ordered_items=collections.OrderedDict(),
        ),
        location=Coordinate(0, 0, 0),
    )
    deck.assign_child_resource(
        PRCXI9300Plate(
            name="container_for_nothing8",
            size_x=50,
            size_y=50,
            size_z=10,
            category="plate",
            ordered_items=collections.OrderedDict(),
        ),
        location=Coordinate(0, 0, 0),
    )
    deck.assign_child_resource(plate9, location=Coordinate(0, 0, 0))
    deck.assign_child_resource(plate10, location=Coordinate(0, 0, 0))
    deck.assign_child_resource(
        PRCXI9300Plate(
            name="container_for_nothing11",
            size_x=50,
            size_y=50,
            size_z=10,
            category="plate",
            ordered_items=collections.OrderedDict(),
        ),
        location=Coordinate(0, 0, 0),
    )
    deck.assign_child_resource(
        PRCXI9300Plate(
            name="container_for_nothing12",
            size_x=50,
            size_y=50,
            size_z=10,
            category="plate",
            ordered_items=collections.OrderedDict(),
        ),
        location=Coordinate(0, 0, 0),
    )
    deck.assign_child_resource(plate13, location=Coordinate(0, 0, 0))
    deck.assign_child_resource(plate14, location=Coordinate(0, 0, 0))
    deck.assign_child_resource(plate15, location=Coordinate(0, 0, 0))
    deck.assign_child_resource(trash, location=Coordinate(0, 0, 0))

    from unilabos.resources.graphio import tree_to_list, resource_plr_to_ulab

    A = tree_to_list([resource_plr_to_ulab(deck)])
    with open("deck.json", "w", encoding="utf-8") as f:
        A.insert(0, {
            "id": "PRCXI",
            "name": "PRCXI",
            "parent": None,
            "type": "device",
            "class": "liquid_handler.prcxi",
            "position": {
                "x": 0,
                "y": 0,
                "z": 0
            },
            "config": {
                "deck": {
                    "_resource_child_name": "PRCXI_Deck",
                    "_resource_type": "unilabos.devices.liquid_handling.prcxi.prcxi:PRCXI9300Deck"
                },
                "host": "192.168.0.121",
                "port": 9999,
                "timeout": 10.0,
                "axis": "Right",
                "channel_num": 1,
                "setup": False,
                "debug": True,
                "simulator": True,
                "matrix_id": "5de524d0-3f95-406c-86dd-f83626ebc7cb",
                "is_9320": True
            },
            "data": {},
            "children": [
                "PRCXI_Deck"
            ]
        })
        A[1]["parent"] = "PRCXI"
        json.dump({
            "nodes": A,
            "links": []
        }, f, indent=4, ensure_ascii=False)

    handler = PRCXI9300Handler(
        deck=deck,
        host="192.168.1.201",
        port=9999,
        timeout=10.0,
        setup=True,
        debug=False,
        matrix_id="5de524d0-3f95-406c-86dd-f83626ebc7cb",
        channel_num=1,
        axis="Right",
        simulator=False,
        is_9320=True,
    )
    backend: PRCXI9300Backend = handler.backend
    from pylabrobot.resources import set_volume_tracking

    set_volume_tracking(enabled=True)
    # res = backend.api_client.get_all_materials()
    asyncio.run(handler.setup())  # Initialize the handler and setup the connection
    handler.set_tiprack([plate1, plate5])  # Set the tip rack for the handler
    handler.set_liquid([plate9.get_well("H12")], ["water"], [5])
    asyncio.run(handler.create_protocol(protocol_name="Test Protocol"))
    asyncio.run(handler.pick_up_tips([plate5.get_item("C5")], [0]))
    asyncio.run(handler.aspirate([plate9.get_item("H12")], [5], [0]))

    for well in plate13.get_all_items():
        # well_pos = well.name.split("_")[1]       # 走一行
        # if well_pos.startswith("A"):
        if well.name.startswith("PlateT13"):  # 走整个Plate
            asyncio.run(handler.dispense([well], [0.01], [0]))

    # asyncio.run(handler.dispense([plate10.get_item("H12")], [1], [0]))
    # asyncio.run(handler.dispense([plate13.get_item("A1")], [1], [0]))
    # asyncio.run(handler.dispense([plate14.get_item("C5")], [1], [0]))
    asyncio.run(handler.mix([plate10.get_item("H12")], mix_time=3, mix_vol=5))
    asyncio.run(handler.discard_tips([0]))
    asyncio.run(handler.run_protocol())
    time.sleep(5)
    os._exit(0)


    prcxi_api = PRCXI9300Api(host="192.168.0.121", port=9999)
    prcxi_api.list_matrices()
    prcxi_api.get_all_materials()

    # 第一种情景：一个孔往多个孔加液
    # plate_2_liquids = handler.set_group("water", [plate2.children[0]], [300])
    # plate5_liquids = handler.set_group("master_mix", plate5.children[:23], [100]*23)
    # 第二个情景：多个孔往多个孔加液(但是个数得对应)
    plate_2_liquids = handler.set_group("water", plate2.children[:23], [300] * 23)
    plate5_liquids = handler.set_group("master_mix", plate5.children[:23], [100] * 23)

    # plate11.set_well_liquids([("Water", 100) if (i % 8 == 0 and i // 8 < 6) else (None, 100) for i in range(96)])  # Set liquids for every 8 wells in plate8

    # plate11.set_well_liquids([("Water", 100) if (i % 8 == 0 and i // 8 < 6) else (None, 100) for i in range(96)])  # Set liquids for every 8 wells in plate8

    #     A = tree_to_list([resource_plr_to_ulab(deck)])
    #     # with open("deck.json", "w", encoding="utf-8") as f:
    #     #     json.dump(A, f, indent=4, ensure_ascii=False)

    #     print(plate11.get_well(0).tracker.get_used_volume())
    # Initialize the backend and setup the connection
    asyncio.run(handler.transfer_group("water", "master_mix", 10))  # Reset tip tracking

    # asyncio.run(handler.pick_up_tips([plate8.children[8]],[0]))
    # print(plate8.children[8])
    # asyncio.run(handler.run_protocol())
    # asyncio.run(handler.aspirate([plate11.children[0]],[10], [0]))
    # print(plate11.children[0])
    # # asyncio.run(handler.run_protocol())
    # asyncio.run(handler.dispense([plate1.children[0]],[10],[0]))
    # print(plate1.children[0])
    # asyncio.run(handler.run_protocol())
    # asyncio.run(handler.mix([plate1.children[0]], mix_time=3, mix_vol=5, height_to_bottom=0.5, offsets=Coordinate(0, 0, 0), mix_rate=100))
    # print(plate1.children[0])
    # asyncio.run(handler.discard_tips([0]))

    #     asyncio.run(handler.add_liquid(
    #     asp_vols=[10]*7,
    #     dis_vols=[10]*7,
    #     reagent_sources=plate11.children[:7],
    #     targets=plate1.children[2:9],
    #     use_channels=[0],
    #     flow_rates=[None] * 7,
    #     offsets=[Coordinate(0, 0, 0)] * 7,
    #     liquid_height=[None] * 7,
    #     blow_out_air_volume=[None] * 2,
    #     delays=None,
    #     mix_time=3,
    #     mix_vol=5,
    #     spread="custom",
    # ))

    # asyncio.run(handler.run_protocol())  # Run the protocol

    # # #     asyncio.run(handler.transfer_liquid(
    # # #     asp_vols=[10]*2,
    # # #     dis_vols=[10]*2,
    # # #     sources=plate11.children[:2],
    # # #     targets=plate11.children[-2:],
    # # #     use_channels=[0],
    # # #     offsets=[Coordinate(0, 0, 0)] * 4,
    # # #     liquid_height=[None] * 2,
    # # #     blow_out_air_volume=[None] * 2,
    # # #     delays=None,
    # # #     mix_times=3,
    # # #     mix_vol=5,
    # # #     spread="wide",
    # # #     tip_racks=[plate8]
    # # # ))

    # # #     asyncio.run(handler.remove_liquid(
    # # #     vols=[10]*2,
    # # #     sources=plate11.children[:2],
    # # #     waste_liquid=plate11.children[43],
    # # #     use_channels=[0],
    # # #     offsets=[Coordinate(0, 0, 0)] * 4,
    # # #     liquid_height=[None] * 2,
    # # #     blow_out_air_volume=[None] * 2,
    # # #     delays=None,
    # # #     spread="wide"
    # # # ))
    # #     asyncio.run(handler.run_protocol())

    # #     # asyncio.run(handler.discard_tips())
    # #     # asyncio.run(handler.mix(well_containers.children[:8
    # #     # ], mix_time=3, mix_vol=50, height_to_bottom=0.5, offsets=Coordinate(0, 0, 0), mix_rate=100))
    # #     #print(json.dumps(handler._unilabos_backend.steps_todo_list, indent=2))  # Print matrix info

    # #     # asyncio.run(handler.remove_liquid(
    # #     #     vols=[100]*16,
    # #     #     sources=well_containers.children[-16:],
    # #     #     waste_liquid=well_containers.children[:16], # 这个有些奇怪，但是好像也只能这么写
    # #     #     use_channels=[0, 1, 2, 3, 4, 5, 6, 7],
    # #     #     flow_rates=[None] * 32,
    # #     #     offsets=[Coordinate(0, 0, 0)] * 32,
    # #     #     liquid_height=[None] * 32,
    # #     #     blow_out_air_volume=[None] * 32,
    # #     #     spread="wide",
    # #     # ))
    # #     # asyncio.run(handler.transfer_liquid(
    # #     #     asp_vols=[100]*16,
    # #     #     dis_vols=[100]*16,
    # #     #     tip_racks=[tip_rack],
    # #     #     sources=well_containers.children[-16:],
    # #     #     targets=well_containers.children[:16],
    # #     #     use_channels=[0, 1, 2, 3, 4, 5, 6, 7],
    # #     #     offsets=[Coordinate(0, 0, 0)] * 32,
    # #     #     asp_flow_rates=[None] * 16,
    # #     #     dis_flow_rates=[None] * 16,
    # #     #     liquid_height=[None] * 32,
    # #     #     blow_out_air_volume=[None] * 32,
    # #     #     mix_times=3,
    # #     #     mix_vol=50,
    # #     #     spread="wide",
    # #     # ))
    #       # print(json.dumps(handler._unilabos_backend.steps_todo_list, indent=2))  # Print matrix info
    # #     # input("pick_up_tips add step")
    # asyncio.run(handler.run_protocol())  # Run the protocol
    # #     # input("Running protocol...")
    # #     # input("Press Enter to continue...")  # Wait for user input before proceeding
    # #     # print("PRCXI9300Handler initialized with deck and host settings.")

    # 一些推荐版位组合的测试样例：

    # 一些推荐版位组合的测试样例：

    with open("prcxi_material.json", "r") as f:
        material_info = json.load(f)

    layout = DefaultLayout("PRCXI9320")
    layout.add_lab_resource(material_info)
    MatrixLayout_1, dict_1 = layout.recommend_layout(
        [
            ("reagent_1", "96 细胞培养皿", 3),
            ("reagent_2", "12道储液槽", 1),
            ("reagent_3", "200μL Tip头", 7),
            ("reagent_4", "10μL加长 Tip头", 1),
        ]
    )
    print(dict_1)
    MatrixLayout_2, dict_2 = layout.recommend_layout(
        [
            ("reagent_1", "96深孔板", 4),
            ("reagent_2", "12道储液槽", 1),
            ("reagent_3", "200μL Tip头", 1),
            ("reagent_4", "10μL加长 Tip头", 1),
        ]
    )

# with open("prcxi_material.json", "r") as f:
#     material_info = json.load(f)

# layout = DefaultLayout("PRCXI9320")
# layout.add_lab_resource(material_info)
# MatrixLayout_1, dict_1 = layout.recommend_layout([
#     ("reagent_1", "96 细胞培养皿", 3),
#     ("reagent_2", "12道储液槽", 1),
#     ("reagent_3", "200μL Tip头", 7),
#     ("reagent_4", "10μL加长 Tip头", 1),
# ])
# print(dict_1)
# MatrixLayout_2, dict_2 = layout.recommend_layout([
#     ("reagent_1", "96深孔板", 4),
#     ("reagent_2", "12道储液槽", 1),
#     ("reagent_3", "200μL Tip头", 1),
#     ("reagent_4", "10μL加长 Tip头", 1),
# ])
