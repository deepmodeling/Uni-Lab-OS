from __future__ import annotations

from typing import Dict, List, Mapping, Optional, Sequence, Union

from pylabrobot.resources import Coordinate
from pylabrobot.resources.carrier import ResourceHolder, create_homogeneous_resources

from unilabos.resources.presets.itemized_carrier import ItemizedCarrier, ResourcePLR


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
    category: str = "warehouse",
    model: Optional[str] = None,
    col_offset: int = 0,  # 列起始偏移量，用于生成A05-D08等命名
    row_offset: int = 0,  # 行起始偏移量，用于生成F01-J03等命名
    layout: str = "col-major",  # 新增：排序方式，"col-major"=列优先，"row-major"=行优先
):
    if layout not in {"row-major", "col-major", "vertical-col-major"}:
        raise ValueError(f"不支持的 warehouse layout: {layout!r}")
    if min(num_items_x, num_items_y, num_items_z) <= 0:
        raise ValueError("num_items_x/y/z 必须全部大于 0")

    # 位置、标签、逻辑索引由同一组记录生成，避免过去分别 zip 后错位。
    grid_indices = []
    for layer in range(num_items_z):
        if layout == "row-major":
            grid_indices.extend(
                (col, row, layer)
                for row in range(num_items_y)
                for col in range(num_items_x)
            )
        else:
            grid_indices.extend(
                (col, row, layer)
                for col in range(num_items_x)
                for row in range(num_items_y)
            )

    removed = set(removed_positions or [])
    grid_indices = [
        indices for ordinal, indices in enumerate(grid_indices) if ordinal not in removed
    ]
    locations = []
    labels = []
    site_indices = {}
    for col, row, layer in grid_indices:
        x = dx + col * item_dx
        y = (
            dy + row * item_dy
            if layout == "row-major"
            else dy + (num_items_y - row - 1) * item_dy
        )
        z = dz + (num_items_z - layer - 1) * item_dz
        locations.append(Coordinate(x, y, z))

        if num_items_z == 1:
            label = f"{LETTERS[row + row_offset]}{col + 1 + col_offset:02d}"
        elif num_items_x == 1:
            # z-y 平面：字母表示 y，数字表示 z。
            label = f"{LETTERS[row + row_offset]}{layer + 1 + col_offset:02d}"
        elif num_items_y == 1:
            # x-z 平面：字母表示 z，数字表示 x。
            label = f"{LETTERS[layer + row_offset]}{col + 1 + col_offset:02d}"
        else:
            # 完整三维网格不能只靠 A01 唯一标识，显式带上层号。
            label = (
                f"Z{layer + 1:02d}-"
                f"{LETTERS[row + row_offset]}{col + 1 + col_offset:02d}"
            )
        labels.append(label)
        site_indices[label] = (col, row, layer)

    _sites = create_homogeneous_resources(
        klass=ResourceHolder,
        locations=locations,
        resource_size_x=resource_size_x,
        resource_size_y=resource_size_y,
        resource_size_z=resource_size_z,
        name_prefix=name,
    )
    sites = dict(zip(labels, _sites.values()))

    return WareHouse(
        name=name,
        size_x=dx + item_dx * num_items_x,
        size_y=dy + item_dy * num_items_y,
        size_z=dz + item_dz * num_items_z,
        num_items_x=num_items_x,
        num_items_y=num_items_y,
        num_items_z=num_items_z,
        ordering_layout=layout,  # 传递排序方式到 ordering_layout
        site_indices=site_indices,
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
        site_indices: Optional[Mapping[Union[int, str], Sequence[int]]] = None,
        **kwargs,
    ):
        super().__init__(
            name=name,
            size_x=size_x,
            size_y=size_y,
            size_z=size_z,
            num_items_x=num_items_x,
            num_items_y=num_items_y,
            num_items_z=num_items_z,
            layout=layout,
            sites=sites,
            category=category,
            model=model,
            site_indices=site_indices,
            **kwargs,
        )

        # 保存排序方式，供graphio.py的坐标映射使用
        # 使用独立属性避免与父类的layout冲突
        self.ordering_layout = ordering_layout

    def serialize(self) -> dict:
        """在统一 Site 序列化结果上补充 warehouse 专属排序方式。"""
        data = super().serialize()
        data["ordering_layout"] = self.ordering_layout
        return data

    def get_site_by_layer_position(
        self, row: int, col: int, layer: int
    ) -> ResourceHolder:
        if not (
            0 <= layer < self.num_items_z
            and 0 <= row < self.num_items_y
            and 0 <= col < self.num_items_x
        ):
            raise ValueError(
                "无效的位置: layer={}, row={}, col={}".format(layer, row, col)
            )

        requested = (col, row, layer)
        for ordinal, label in enumerate(self._ordering):
            if self._site_indices.get(str(label)) == requested:
                return self.sites[ordinal]
        raise ValueError(
            "位置 layer={}, row={}, col={} 已被移除".format(layer, row, col)
        )

    def add_rack_to_position(self, row: int, col: int, layer: int, rack) -> None:
        site = self.get_site_by_layer_position(row, col, layer)
        site.assign_child_resource(rack)

    def get_rack_at_position(self, row: int, col: int, layer: int):
        site = self.get_site_by_layer_position(row, col, layer)
        return site.resource
