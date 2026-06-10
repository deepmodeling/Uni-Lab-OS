from typing import Dict, Optional, List, Union
from pylabrobot.resources import Coordinate
from pylabrobot.resources.carrier import ResourceHolder, create_homogeneous_resources

from unilabos.resources.itemized_carrier import ItemizedCarrier, ResourcePLR


LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def warehouse_factory(
    name: str,
    num_items_x: int = 1,
    num_items_y: int = 4,
    num_items_z: int = 4,
    dx: float = 137.0,
    dy: float = 96.0,
    dz: float = 120.0,
    item_dx: float = 10.0,
    item_dy: float = 10.0,
    item_dz: float = 10.0,
    resource_size_x: float = 127.0,
    resource_size_y: float = 86.0,
    resource_size_z: float = 25.0,
    removed_positions: Optional[List[int]] = None,
    empty: bool = False,
    category: str = "warehouse",
    model: Optional[str] = None,
    col_offset: int = 0,  # 列起始偏移量，用于生成5-8等命名
    layout: str = "col-major",  # 新增：排序方式，"col-major"=列优先，"row-major"=行优先
):
    # 创建位置坐标
    locations = []

    for layer in range(num_items_z):  # 层
        for row in range(num_items_y):  # 行
            for col in range(num_items_x):  # 列
                # 计算位置
                x = dx + col * item_dx

                # 根据 layout 决定 y 坐标计算
                if layout == "row-major":
                    # 行优先：row=0(第1行) 应该显示在上方，y 值最小
                    y = dy + row * item_dy
                else:
                    # 列优先：保持原逻辑
                    y = dy + (num_items_y - row - 1) * item_dy

                z = dz + (num_items_z - layer - 1) * item_dz
                locations.append(Coordinate(x, y, z))

    if removed_positions:
        locations = [loc for i, loc in enumerate(locations) if i not in removed_positions]

    _sites = create_homogeneous_resources(
        klass=ResourceHolder,
        locations=locations,
        resource_size_x=resource_size_x,
        resource_size_y=resource_size_y,
        resource_size_z=resource_size_z,
        name_prefix=name,
    )

    len_x, len_y = (num_items_x, num_items_y) if num_items_z == 1 else (
        num_items_y, num_items_z) if num_items_x == 1 else (num_items_x, num_items_z)

    # 🔑 修改：使用数字命名，最上面是4321，最下面是12,11,10,9
    # 命名顺序必须与坐标生成顺序一致：层 → 行 → 列
    keys = []
    for layer in range(num_items_z):  # 遍历每一层
        for row in range(num_items_y):  # 遍历每一行
            for col in range(num_items_x):  # 遍历每一列
                # 倒序计算全局行号：row=0 应该对应 global_row=0（第1行：4321）
                # row=1 应该对应 global_row=1（第2行：8765）
                # row=2 应该对应 global_row=2（第3行：12,11,10,9）
                # 但前端显示时 row=2 在最上面，所以需要反转
                # row=0→reversed_row=2, row=1→reversed_row=1, row=2→reversed_row=0
                reversed_row = (num_items_y - 1 - row)
                global_row = layer * num_items_y + reversed_row

                # 每行的最大数字 = (global_row + 1) * num_items_x + col_offset
                base_num = (global_row + 1) * num_items_x + col_offset

                # 从右到左递减：4,3,2,1
                key = str(base_num - col)
                keys.append(key)

    sites = {i: site for i, site in zip(keys, _sites.values())}

    return WareHouse(
        name=name,
        size_x=dx + item_dx * num_items_x,
        size_y=dy + item_dy * num_items_y,
        size_z=dz + item_dz * num_items_z,
        num_items_x=num_items_x,
        num_items_y=num_items_y,
        num_items_z=num_items_z,
        ordering_layout=layout,  # 传递排序方式到 ordering_layout
        sites=sites,
        category=category,
        model=model,
    )


class WareHouse(ItemizedCarrier):
    """堆栈载体类 - 可容纳16个板位的载体（4层x4行x1列）"""

    def __init__(
        self,
        name: str,
        size_x: float,
        size_y: float,
        size_z: float,
        num_items_x: int,
        num_items_y: int,
        num_items_z: int,
        layout: str = "x-y",
        sites: Optional[Dict[Union[int, str], Optional[ResourcePLR]]] = None,
        category: str = "warehouse",
        model: Optional[str] = None,
        ordering_layout: str = "col-major",
        **kwargs
    ):
        super().__init__(
            name=name,
            size_x=size_x,
            size_y=size_y,
            size_z=size_z,
            # ordered_items=ordered_items,
            # ordering=ordering,
            num_items_x=num_items_x,
            num_items_y=num_items_y,
            num_items_z=num_items_z,
            layout=layout,
            sites=sites,
            category=category,
            model=model,
        )

        # 保存排序方式，供graphio.py的坐标映射使用
        # 使用独立属性避免与父类的layout冲突
        self.ordering_layout = ordering_layout

    def serialize(self) -> dict:
        """序列化时保存 ordering_layout 属性"""
        data = super().serialize()
        data['ordering_layout'] = self.ordering_layout
        return data

    def get_site_by_layer_position(self, row: int, col: int, layer: int) -> ResourceHolder:
        if not (0 <= layer < 4 and 0 <= row < 4 and 0 <= col < 1):
            raise ValueError("无效的位置: layer={}, row={}, col={}".format(layer, row, col))

        site_index = layer * 4 + row * 1 + col
        return self.sites[site_index]

    def add_rack_to_position(self, row: int, col: int, layer: int, rack) -> None:
        site = self.get_site_by_layer_position(row, col, layer)
        site.assign_child_resource(rack)

    def get_rack_at_position(self, row: int, col: int, layer: int):
        site = self.get_site_by_layer_position(row, col, layer)
        return site.resource
