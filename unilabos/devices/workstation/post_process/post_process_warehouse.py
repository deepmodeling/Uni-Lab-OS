from __future__ import annotations

from typing import List, Optional

from pylabrobot.resources import Coordinate, ResourceHolder
from pylabrobot.resources.carrier import create_homogeneous_resources

from unilabos.resources.presets.warehouse import WareHouse


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
    col_offset: int = 0,
    layout: str = "col-major",
) -> WareHouse:
    """创建后处理工作站的数字编号仓库。

    数字标签沿用该工作站原有的倒序编号；holder、物理坐标和 x/y/z
    索引由同一网格记录生成，避免移除槽位或列优先布局时错位。
    """

    if layout not in {"row-major", "col-major"}:
        raise ValueError(f"不支持的 warehouse layout: {layout!r}")
    if min(num_items_x, num_items_y, num_items_z) <= 0:
        raise ValueError("num_items_x/y/z 必须全部大于 0")

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
        locations.append(
            Coordinate(
                dx + col * item_dx,
                dy + (num_items_y - row - 1) * item_dy,
                dz + (num_items_z - layer - 1) * item_dz,
            )
        )
        reversed_row = num_items_y - 1 - row
        global_row = layer * num_items_y + reversed_row
        label = str((global_row + 1) * num_items_x + col_offset - col)
        labels.append(label)
        site_indices[label] = (col, row, layer)

    native_sites = create_homogeneous_resources(
        klass=ResourceHolder,
        locations=locations,
        resource_size_x=resource_size_x,
        resource_size_y=resource_size_y,
        resource_size_z=resource_size_z,
        name_prefix=name,
    )
    return WareHouse(
        name=name,
        size_x=dx + item_dx * num_items_x,
        size_y=dy + item_dy * num_items_y,
        size_z=dz + item_dz * num_items_z,
        num_items_x=num_items_x,
        num_items_y=num_items_y,
        num_items_z=num_items_z,
        ordering_layout=layout,
        sites=dict(zip(labels, native_sites.values())),
        site_indices=site_indices,
        category=category,
        model=model,
    )
