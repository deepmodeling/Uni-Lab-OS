from typing import Dict, Optional, List, Union
from pylabrobot.resources import Coordinate
from pylabrobot.resources.carrier import ResourceHolder, create_homogeneous_resources
import uuid
from unilabos.resources.itemized_carrier import ItemizedCarrier, ResourcePLR


LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def warehouse_factory(
    name: str,
    num_items_x: int = 1,
    num_items_y: int = 4,
    num_items_z: int = 4,
    dx: float = 350.0,
    dy: float = 160.0,
    dz: float = 120.0,
    item_dx: float = 10.0,
    item_dy: float = 10.0,
    item_dz: float = 10.0,
    resource_size_x: float = 340.0,
    resource_size_y: float = 150.0,
    resource_size_z: float = 25.0,
    removed_positions: Optional[List[int]] = None,
    empty: bool = False,
    category: str = "warehouse",
    model: Optional[str] = None,
    col_offset: int = 0,  # 列起始偏移量，用于生成A05-D08等命名
    row_offset: int = 0,  # 行起始偏移量，用于生成F01-J03等命名
    layout: str = "col-major",  # 新增：排序方式，"col-major"=列优先，"row-major"=行优先
    name_by_layout_code: bool = False,
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
                    # 行优先：row=0(A行) 应该显示在上方，需要较小的 y 值
                    y = dy + row * item_dy
                elif layout == "vertical-col-major":
                    # 竖向warehouse: row=0 对应顶部（y小），row=n-1 对应底部（y大）
                    # 但标签 01 应该在底部，所以使用反向映射
                    y = dy + (num_items_y - row - 1) * item_dy
                else:
                    # 列优先：保持原逻辑（row=0 对应较大的 y）
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

    for site in _sites.values():
        if not hasattr(site, "unilabos_uuid") or not site.unilabos_uuid:
            # 建议使用基于名字的确定性 UUID (uuid5)，或者直接用 uuid4
            site.unilabos_uuid = str(uuid.uuid4())

    len_x, len_y = (num_items_x, num_items_y) if num_items_z == 1 else (num_items_y, num_items_z) if num_items_x == 1 else (num_items_x, num_items_z)

    # 根据 layout 参数生成不同的排序方式
    # 注意：物理位置的 y 坐标是倒序的 (row=0 时 y 最大，对应前端显示的顶部)
    if layout == "row-major":
        # 行优先顺序: A01,A02,A03,A04, B01,B02,B03,B04
        # locations[0] 对应 row=0, y最大（前端顶部）→ 应该是 A01
        keys = [f"{LETTERS[j + row_offset]}{i + 1 + col_offset:02d}" for j in range(len_y) for i in range(len_x)]
    else:
        # 列优先顺序: A01,B01,C01,D01, A02,B02,C02,D02
        keys = [f"{LETTERS[j + row_offset]}{i + 1 + col_offset:02d}" for i in range(len_x) for j in range(len_y)]

    if name_by_layout_code:
        def _layout_code_from_key(key: str) -> str:
            if "-" in key:
                return key
            row_letter = key[:1]
            col_str = key[1:]
            try:
                row = LETTERS.index(row_letter) + 1
            except ValueError:
                row = 1
            try:
                col = int(col_str)
            except ValueError:
                col = 1
            if num_items_y == 1:
                return f"{name}-{col}"
            return f"{name}-{row}-{col}"

        layout_keys = [_layout_code_from_key(key) for key in keys]
        for key, site in zip(layout_keys, _sites.values()):
            site.name = key
        keys = layout_keys

    sites = {i: site for i, site in zip(keys, _sites.values())}

    return WareHouse(
        name=name,
        size_x=dx + item_dx * num_items_x,
        size_y=dy + item_dy * num_items_y,
        size_z=dz + item_dz * num_items_z,
        num_items_x = num_items_x,
        num_items_y = num_items_y,
        num_items_z = num_items_z,
        ordering_layout=layout,  # 传递排序方式到 ordering_layout
        # ordered_items=ordered_items,
        # ordering=ordering,
        sites=sites,
        category=category,
        model=model,
        name_by_layout_code=name_by_layout_code,
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
        name_by_layout_code: bool = False,
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
        self.name_by_layout_code = name_by_layout_code

    def _layout_code_from_key(self, key: str) -> str:
        if "-" in key:
            return key
        row_letter = key[:1]
        col_str = key[1:]
        try:
            row = LETTERS.index(row_letter) + 1
        except ValueError:
            row = 1
        try:
            col = int(col_str)
        except ValueError:
            col = 1
        if self.num_items_y == 1:
            return f"{self.name}-{col}"
        return f"{self.name}-{row}-{col}"

    def serialize(self) -> dict:
        """序列化时保存 ordering_layout 属性"""
        data = super().serialize()
        data['ordering_layout'] = self.ordering_layout
        if self.name_by_layout_code:
            for site in data.get("sites", []):
                site["label"] = self._layout_code_from_key(site.get("label", ""))
        return data

    def get_site_by_layer_position(self, row: int, col: int, layer: int) -> ResourceHolder:
        if not (0 <= layer < self.num_items_z and 0 <= row < self.num_items_y and 0 <= col < self.num_items_x):
            raise ValueError(f"无效的位置: layer={layer}, row={row}, col={col}")
        
        row_str = LETTERS[row]
        col_str = f"{col + 1:02d}"
        target_key = f"{row_str}{col_str}"
        if self.name_by_layout_code:
            target_key = self._layout_code_from_key(target_key)

        return self.get_item(target_key)
    
    def add_rack_to_position(self, row: int, col: int, layer: int, rack) -> None:
        site = self.get_site_by_layer_position(row, col, layer)
        site.assign_child_resource(rack)

    def get_rack_at_position(self, row: int, col: int, layer: int):
        site = self.get_site_by_layer_position(row, col, layer)
        return site.resource
