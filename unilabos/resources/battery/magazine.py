from typing import Any, Dict, List, Optional, OrderedDict, Sequence, Tuple, Union, Callable
import math

from pylabrobot.resources.coordinate import Coordinate
from pylabrobot.resources import Resource, ResourceStack, ItemizedResource
from pylabrobot.resources.carrier import create_homogeneous_resources
from pylabrobot.resources.volume_tracker import VolumeTracker

from unilabos.utils.log import logger
from unilabos.resources.battery.electrode_sheet import (
    PositiveCan, PositiveElectrode,
    NegativeCan, NegativeElectrode,
    SpringWasher, FlatWasher,
    AluminumFoil,
    Battery
)


def _as_count(value: Any) -> int:
    """余量是件数，向下取整。

    PLC 那边的计数器是 FLOAT32，上料录入的初值常带小数尾数（如正极壳的 .089294），
    递减只动整数位，于是读数一直拖着这条尾巴。件数不能有小数，且不可多报——
    132.5 片只能取出 132 片，故一律向下取整而非四舍五入。
    """
    return math.floor(float(value))


class Magazine(ResourceStack):
    """子弹夹洞位类"""

    def __init__(
        self,
        name: str,
        direction: str = 'z',
        resources: Optional[List[Resource]] = None,
        max_sheets: int = 100,
        **kwargs
    ):
        """初始化子弹夹洞位

        Args:
            name: 洞位名称
            direction: 堆叠方向
            resources: 资源列表
            max_sheets: 最大极片数量
        """
        super().__init__(
            name=name,
            direction=direction,
            resources=resources,
        )
        self.max_sheets = max_sheets

    @property
    def size_x(self) -> float:
        return self.get_size_x()

    @property
    def size_y(self) -> float:
        return self.get_size_y()

    @property
    def size_z(self) -> float:
        return self.get_size_z()

    def serialize(self) -> dict:
        data = super().serialize()
        # 物料余量由寄存器接管，不再持久化极片子节点，
        # 防止旧数据写回数据库后下次启动时再次引发重复 UUID。
        data["children"] = []
        data.update({
            "size_x": self.size_x or 10.0,
            "size_y": self.size_y or 10.0,
            "size_z": self.size_z or 10.0,
            "max_sheets": self.max_sheets,
        })
        return data

    @classmethod
    def deserialize(cls, data: dict, allow_marshal: bool = False):
        """反序列化时丢弃极片子节点（ElectrodeSheet 等）。

        物料余量已由寄存器接管，不再在资源树中追踪每个极片实体。
        清空 children 可防止数据库中的旧极片记录被重新加载，避免重复 UUID 报错。
        """
        data = dict(data)
        data["children"] = []
        return super().deserialize(data, allow_marshal=allow_marshal)


class MagazineHolder(ItemizedResource):
    """子弹夹类 - 有多个洞位，每个洞位放多个极片"""

    def __init__(
        self,
        name: str,
        size_x: float,
        size_y: float,
        size_z: float,
        ordered_items: Optional[Dict[str, Magazine]] = None,
        ordering: Optional[OrderedDict[str, str]] = None,
        hole_diameter: float = 14.0,
        hole_depth: float = 10.0,
        max_sheets_per_hole: int = 100,
        cross_section_type: str = "circle",
        category: str = "magazine_holder",
        model: Optional[str] = None,
    ):
        """初始化子弹夹

        Args:
            name: 子弹夹名称
            size_x: 长度 (mm)
            size_y: 宽度 (mm)
            size_z: 高度 (mm)
            hole_diameter: 洞直径 (mm)
            hole_depth: 洞深度 (mm)
            max_sheets_per_hole: 每个洞位最大极片数量
            category: 类别
            model: 型号
        """

        super().__init__(
            name=name,
            size_x=size_x,
            size_y=size_y,
            size_z=size_z,
            ordered_items=ordered_items,
            ordering=ordering,
            category=category,
            model=model,
        )

        # 保存洞位的直径和深度
        self.hole_diameter = hole_diameter
        self.hole_depth = hole_depth
        self.max_sheets_per_hole = max_sheets_per_hole
        self.cross_section_type = cross_section_type

        # 余量展示：极片子节点已不再持久化，改由寄存器读数写进 tracker，
        # 与配液站载架用同一套 liquids 三元组格式，前端渲染逻辑可复用。
        self.tracker = VolumeTracker(thing=f"{name}_stock_tracker", max_volume=1e9)
        self._stock_entries: List[Tuple[str, int, str]] = []

    def serialize(self) -> dict:
        return {
            **super().serialize(),
            "hole_diameter": self.hole_diameter,
            "hole_depth": self.hole_depth,
            "max_sheets_per_hole": self.max_sheets_per_hole,
            "cross_section_type": self.cross_section_type,
        }

    def set_stock(self, entries: Sequence[Tuple[str, float, str]]) -> Dict[str, int]:
        """整体替换弹夹余量，entries 为 (物料名, 数量, 单位) 三元组，数量按件数向下取整。

        先清历史再写，避免周期刷新把 liquid_history 越堆越长。

        Returns:
            {物料名: 件数}，仅含零或负的项。依华 PLC 的余量是逐颗递减的计数器，
            上料时若没录初值就会一路减穿零，此时的负值意味着「自上次复位以来已
            消耗这么多」，须交调用方告警提醒补设初值。
        """
        self._stock_entries = [(str(n), _as_count(q), str(u)) for n, q, u in entries]

        # tracker 承载不了零与负数，只把正数灌进去以保持容量语义可用；
        # 负值另由 serialize_state 直接送出，不能让它在前端消失。
        self.tracker.liquid_history.clear()
        self.tracker._unknown_counter = 0
        self.tracker.liquids = [item for item in self._stock_entries if item[1] > 0]

        extra = getattr(self, "unilabos_extra", None)
        if not isinstance(extra, dict):
            extra = {}
            self.unilabos_extra = extra
        extra["stock_remaining"] = {n: q for n, q, _ in self._stock_entries}

        return {n: q for n, q, _ in self._stock_entries if q <= 0}

    def serialize_state(self) -> Dict[str, Any]:
        data = self.tracker.serialize()
        if self._stock_entries:
            # 用原始读数覆盖 tracker 给的 liquids：计数器减穿零后的负值同样要显示，
            # 那正是「上料没设初值」的唯一现场信号，被过滤掉就只剩一片空白。
            data["liquids"] = [list(item) for item in self._stock_entries]
        return data

    def load_state(self, state: Dict[str, Any]) -> None:
        """余量特性上线前入库的 state 为 {}，VolumeTracker 对缺键无副作用。"""
        if not isinstance(state, dict):
            return
        try:
            self.tracker.load_state(state)
        except Exception as e:
            # 云端回来的 state 可能是旧版本或已损坏，而余量下一轮采集就会整体重写，
            # 不值得为它中断整棵资源树的加载
            logger.warning(f"弹夹 {self.name} 余量 state 解析失败，按空处理: {e}")
        restored: List[Tuple[str, int, str]] = []
        for item in state.get("liquids") or []:
            if isinstance(item, (list, tuple)) and len(item) >= 3:
                try:
                    restored.append((str(item[0]), _as_count(item[1]), str(item[2])))
                except (TypeError, ValueError):
                    continue
        self._stock_entries = restored


def magazine_factory(
    name: str,
    size_x: float,
    size_y: float,
    size_z: float,
    locations: List[Coordinate],
    klasses: Optional[List[Callable[[str], str]]] = None,
    hole_diameter: float = 14.0,
    hole_depth: float = 10.0,
    max_sheets_per_hole: int = 100,
    category: str = "magazine_holder",
    model: Optional[str] = None,
) -> 'MagazineHolder':
    """工厂函数：创建子弹夹
    
    Args:
        name: 子弹夹名称
        size_x: 长度 (mm)
        size_y: 宽度 (mm)
        size_z: 高度 (mm)
        locations: 洞位坐标列表
        klasses: 每个洞位中极片的类列表
        hole_diameter: 洞直径 (mm)
        hole_depth: 洞深度 (mm)
        max_sheets_per_hole: 每个洞位最大极片数量
        category: 类别
        model: 型号
    """
    for loc in locations:
        loc.x -= hole_diameter / 2
        loc.y -= hole_diameter / 2

    # 创建洞位
    _sites = create_homogeneous_resources(
        klass=Magazine,
        locations=locations,
        resource_size_x=hole_diameter,
        resource_size_y=hole_diameter,
        name_prefix=name,
        max_sheets=max_sheets_per_hole,
    )
    
    # 生成编号键
    keys = [f"A{i+1}" for i in range(len(locations))]
    sites = dict(zip(keys, _sites.values()))
    
    holder = MagazineHolder(
        name=name,
        size_x=size_x,
        size_y=size_y,
        size_z=size_z,
        ordered_items=sites,
        hole_diameter=hole_diameter,
        hole_depth=hole_depth,
        max_sheets_per_hole=max_sheets_per_hole,
        category=category,
        model=model,
    )

    if klasses is not None:
        for i, klass in enumerate(klasses):
            hole_key = keys[i]
            hole = holder.children[i]
            for j in reversed(range(max_sheets_per_hole)):
                item_name = f"{hole_key}_sheet{j+1}"
                item = klass(name=item_name)
                hole.assign_child_resource(item)
    return holder


def MagazineHolder_6_Cathode(
    name: str,
    size_x: float = 80.0,
    size_y: float = 80.0,
    size_z: float = 40.0,
    hole_diameter: float = 14.0,
    hole_depth: float = 10.0,
    hole_spacing: float = 20.0,
    max_sheets_per_hole: int = 100,
) -> MagazineHolder:
    """创建6孔子弹夹 - 六边形排布"""
    center_x = size_x / 2
    center_y = size_y / 2

    locations = []

    # 周围6个孔，按六边形排布
    for i in range(6):
        angle = i * 60 * math.pi / 180  # 每60度一个孔
        x = center_x + hole_spacing * math.cos(angle)
        y = center_y + hole_spacing * math.sin(angle)
        locations.append(Coordinate(x, y, size_z - hole_depth))

    return magazine_factory(
        name=name,
        size_x=size_x,
        size_y=size_y,
        size_z=size_z,
        locations=locations,
        klasses=None,
        hole_diameter=hole_diameter,
        hole_depth=hole_depth,
        max_sheets_per_hole=max_sheets_per_hole,
        category="magazine_holder",
        model="MagazineHolder_6_Cathode",
    )


def MagazineHolder_6_Anode(
    name: str,
    size_x: float = 80.0,
    size_y: float = 80.0,
    size_z: float = 40.0,
    hole_diameter: float = 14.0,
    hole_depth: float = 10.0,
    hole_spacing: float = 20.0,
    max_sheets_per_hole: int = 100,
) -> MagazineHolder:
    """创建6孔子弹夹 - 六边形排布"""
    center_x = size_x / 2
    center_y = size_y / 2

    locations = []

    # 周围6个孔，按六边形排布
    for i in range(6):
        angle = i * 60 * math.pi / 180  # 每60度一个孔
        x = center_x + hole_spacing * math.cos(angle)
        y = center_y + hole_spacing * math.sin(angle)
        locations.append(Coordinate(x, y, size_z - hole_depth))

    return magazine_factory(
        name=name,
        size_x=size_x,
        size_y=size_y,
        size_z=size_z,
        locations=locations,
        klasses=None,
        hole_diameter=hole_diameter,
        hole_depth=hole_depth,
        max_sheets_per_hole=max_sheets_per_hole,
        category="magazine_holder",
        model="MagazineHolder_6_Anode",
    )


def MagazineHolder_6_Battery(
    name: str,
    size_x: float = 80.0,
    size_y: float = 80.0,
    size_z: float = 40.0,
    hole_diameter: float = 14.0,
    hole_depth: float = 10.0,
    hole_spacing: float = 20.0,
    max_sheets_per_hole: int = 100,
) -> MagazineHolder:
    """创建6孔子弹夹 - 六边形排布"""
    center_x = size_x / 2
    center_y = size_y / 2

    locations = []

    # 周围6个孔，按六边形排布
    for i in range(6):
        angle = i * 60 * math.pi / 180  # 每60度一个孔
        x = center_x + hole_spacing * math.cos(angle)
        y = center_y + hole_spacing * math.sin(angle)
        locations.append(Coordinate(x, y, size_z - hole_depth))

    return magazine_factory(
        name=name,
        size_x=size_x,
        size_y=size_y,
        size_z=size_z,
        locations=locations,
        klasses=None, # 初始化时，不放入装好的电池
        hole_diameter=hole_diameter,
        hole_depth=hole_depth,
        max_sheets_per_hole=max_sheets_per_hole,
        category="magazine_holder",
        model="MagazineHolder_6_Battery",
    )


def MagazineHolder_4_Cathode(
    name: str,
) -> MagazineHolder:
    """创建4孔子弹夹 - 正方形四角排布"""
    size_x: float = 80.0
    size_y: float = 80.0
    size_z: float = 10.0
    hole_diameter: float = 14.0
    hole_depth: float = 10.0
    hole_spacing: float = 25.0
    max_sheets_per_hole: int = 100

    # 计算4个洞位的坐标（正方形四角排布）
    center_x = size_x / 2
    center_y = size_y / 2
    offset = hole_spacing / 2
    
    locations = [
        Coordinate(center_x - offset, center_y - offset, size_z - hole_depth),  # 左下
        Coordinate(center_x + offset, center_y - offset, size_z - hole_depth),  # 右下
        Coordinate(center_x - offset, center_y + offset, size_z - hole_depth),  # 左上
        Coordinate(center_x + offset, center_y + offset, size_z - hole_depth),  # 右上
    ]
    
    return magazine_factory(
        name=name,
        size_x=size_x,
        size_y=size_y,
        size_z=size_z,
        locations=locations,
        klasses=None,
        hole_diameter=hole_diameter,
        hole_depth=hole_depth,
        max_sheets_per_hole=max_sheets_per_hole,
        category="magazine_holder",
        model="MagazineHolder_4_Cathode",
    )
