"""碰撞几何与刚体的最小数据结构（与机构种类、规划器无关）。

内核只按属性读取对象（``object_id``、``world_pose``、``geometry``、``geometry_poses``；几何读
``kind``、``vertices``、``triangles``、``mesh_scale``、``size_m``、``radius_m``、``length_m``），
所以机器人领域库自己的同名合同对象也能直接交给内核，不必转换。这里给出 OS 自用的实现。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

IDENTITY = tuple(tuple(float(i == j) for j in range(4)) for i in range(4))


@dataclass(frozen=True)
class PackedRows:
    """打包的 N×3 行：``code='d'`` 为 little-endian float64 顶点，``code='q'`` 为 int64 三角索引。"""

    data: bytes
    code: str

    def __post_init__(self) -> None:
        if self.code not in ("d", "q") or len(self.data) % 24:
            raise ValueError("打包行必须是 N×3 的 float64 或 int64")

    def __len__(self) -> int:
        return len(self.data) // 24


@dataclass(frozen=True)
class Geometry:
    """一个碰撞图元：内存三角网格、盒体或圆柱（圆柱轴为局部 z）。"""

    kind: str
    vertices: Any = None
    triangles: Any = None
    mesh_scale: tuple[float, float, float] = (1.0, 1.0, 1.0)
    size_m: tuple[float, float, float] | None = None
    radius_m: float | None = None
    length_m: float | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("mesh", "box", "cylinder"):
            raise ValueError("碰撞图元只支持 mesh / box / cylinder")
        if self.kind == "box" and (self.size_m is None or len(self.size_m) != 3 or min(self.size_m) <= 0):
            raise ValueError("盒体需要三个正尺寸")
        if self.kind == "cylinder" and not (self.radius_m and self.radius_m > 0 and self.length_m and self.length_m > 0):
            raise ValueError("圆柱需要正半径和正长度")


@dataclass(frozen=True)
class Body:
    """一个碰撞对象：身份、世界位姿、若干图元及各自相对对象的位姿。"""

    object_id: str
    world_pose: tuple[tuple[float, ...], ...]
    geometry: tuple[Geometry, ...]
    role: str = "world"
    geometry_poses: tuple[tuple[tuple[float, ...], ...], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.object_id, str) or not self.object_id:
            raise ValueError("碰撞对象需要非空身份")
        if not self.geometry:
            raise ValueError(f"碰撞对象 {self.object_id} 至少需要一个图元")
        if self.geometry_poses and len(self.geometry_poses) != len(self.geometry):
            raise ValueError("geometry_poses 与图元数量不一致")
        object.__setattr__(self, "world_pose", matrix_tuple(self.world_pose))
        object.__setattr__(self, "geometry", tuple(self.geometry))
        object.__setattr__(self, "geometry_poses", tuple(matrix_tuple(p) for p in self.geometry_poses))


def matrix_tuple(matrix) -> tuple[tuple[float, ...], ...]:
    return tuple(tuple(float(value) for value in row) for row in matrix)


__all__ = ["IDENTITY", "PackedRows", "Geometry", "Body", "matrix_tuple"]
