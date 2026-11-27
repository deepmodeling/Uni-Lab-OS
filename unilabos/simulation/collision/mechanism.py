"""任意机构的运动链：关节值 → 各连杆 / 碰撞刚体的世界位姿。

不区分机构种类：机械臂、导轨、三轴龙门、移液头、门盖、离心机上料机构都是“根 + 若干关节 + 连杆”。
运动链可以来自 URDF（含 ``mimic``），也可以来自资产或对齐表登记的关节列表。只用 NumPy。
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

JOINT_TYPES = ("revolute", "continuous", "prismatic", "fixed")


def rpy_matrix(rpy) -> np.ndarray:
    """URDF 约定：R = Rz(yaw)·Ry(pitch)·Rx(roll)。"""
    r, p, y = (float(v) for v in rpy)
    cr, sr, cp, sp, cy, sy = math.cos(r), math.sin(r), math.cos(p), math.sin(p), math.cos(y), math.sin(y)
    return np.array([[cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
                     [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
                     [-sp, cp * sr, cp * cr]])


def transform(xyz=(0, 0, 0), rpy=(0, 0, 0)) -> np.ndarray:
    m = np.eye(4)
    m[:3, :3] = rpy_matrix(rpy)
    m[:3, 3] = [float(v) for v in xyz]
    return m


def axis_angle(axis, angle) -> np.ndarray:
    x, y, z = axis
    c, s = math.cos(angle), math.sin(angle)
    t = 1 - c
    return np.array([[t * x * x + c, t * x * y - s * z, t * x * z + s * y],
                     [t * x * y + s * z, t * y * y + c, t * y * z - s * x],
                     [t * x * z - s * y, t * y * z + s * x, t * z * z + c]])


def rigid(value, name="pose") -> np.ndarray:
    m = np.asarray(value, dtype=float)
    if (m.shape != (4, 4) or not np.isfinite(m).all() or not np.allclose(m[3], [0, 0, 0, 1], atol=1e-10)
            or not np.allclose(m[:3, :3].T @ m[:3, :3], np.eye(3), atol=1e-6) or np.linalg.det(m[:3, :3]) <= 0):
        raise ValueError(f"{name} 必须是米制刚体变换")
    return m


def inverse(m) -> np.ndarray:
    out = np.eye(4)
    out[:3, :3] = m[:3, :3].T
    out[:3, 3] = -m[:3, :3].T @ m[:3, 3]
    return out


@dataclass(frozen=True)
class Joint:
    name: str
    type: str
    parent: str
    child: str
    origin: np.ndarray = field(default_factory=lambda: np.eye(4))
    axis: tuple[float, float, float] = (0.0, 0.0, 1.0)
    lower: float | None = None
    upper: float | None = None
    mimic: tuple[str, float, float] | None = None   # (被跟随关节, 倍数, 偏置)

    def __post_init__(self):
        if self.type not in JOINT_TYPES:
            raise ValueError(f"关节 {self.name} 类型不支持：{self.type}")
        axis = np.asarray(self.axis, dtype=float)
        norm = float(np.linalg.norm(axis))
        if self.type != "fixed" and (not math.isfinite(norm) or norm <= 0):
            raise ValueError(f"关节 {self.name} 轴向无效")
        object.__setattr__(self, "axis", tuple(axis / norm) if norm > 0 else (0.0, 0.0, 1.0))
        object.__setattr__(self, "origin", rigid(self.origin, f"关节 {self.name} 原点"))

    def local(self, value: float) -> np.ndarray:
        if self.type == "fixed":
            return self.origin
        motion = np.eye(4)
        if self.type == "prismatic":
            motion[:3, 3] = np.asarray(self.axis) * value
        else:
            motion[:3, :3] = axis_angle(self.axis, value)
        return self.origin @ motion


class KinematicChain:
    """一棵连杆树。``link_poses(values)`` 返回根坐标下每个连杆的位姿。"""

    def __init__(self, joints: Iterable[Joint], root: str | None = None):
        self.joints = tuple(joints)
        names = [j.name for j in self.joints]
        if len(set(names)) != len(names):
            raise ValueError("关节名重复")
        children = [j.child for j in self.joints]
        if len(set(children)) != len(children):
            raise ValueError("一个连杆只能有一个父关节")
        parents = {j.parent for j in self.joints}
        roots = parents - set(children) if root is None else {root}
        if len(roots) != 1:
            raise ValueError(f"运动链必须只有一个根，得到 {sorted(roots)}")
        self.root = roots.pop()
        self.by_name = {j.name: j for j in self.joints}
        for j in self.joints:
            if j.mimic and j.mimic[0] not in self.by_name:
                raise ValueError(f"关节 {j.name} 跟随的关节 {j.mimic[0]} 不存在")
        order, frontier, by_parent = [], [self.root], {}
        for j in self.joints:
            by_parent.setdefault(j.parent, []).append(j)
        while frontier:
            link = frontier.pop()
            for j in by_parent.get(link, []):
                order.append(j)
                frontier.append(j.child)
        if len(order) != len(self.joints):
            raise ValueError("运动链有与根不连通的关节")
        self.order = tuple(order)
        self.links = (self.root,) + tuple(j.child for j in order)
        self.actuated = tuple(j.name for j in order if j.type != "fixed" and not j.mimic)

    @classmethod
    def from_urdf(cls, source) -> "KinematicChain":
        """URDF 文本或文件路径。只读运动学（joint 的 origin / axis / limit / mimic）。"""
        text = Path(source).read_text(encoding="utf-8") if isinstance(source, Path) or (
            isinstance(source, str) and not source.lstrip().startswith("<")) else source
        tree = ET.fromstring(text)
        joints = []
        for node in tree.findall("joint"):
            origin = node.find("origin")
            axis = node.find("axis")
            limit = node.find("limit")
            mimic = node.find("mimic")
            kind = node.get("type")
            if kind not in JOINT_TYPES:
                raise ValueError(f"URDF 关节 {node.get('name')} 类型不支持：{kind}")
            joints.append(Joint(
                name=node.get("name"), type=kind, parent=node.find("parent").get("link"), child=node.find("child").get("link"),
                origin=transform([float(v) for v in (origin.get("xyz", "0 0 0") if origin is not None else "0 0 0").split()],
                                 [float(v) for v in (origin.get("rpy", "0 0 0") if origin is not None else "0 0 0").split()]),
                axis=tuple(float(v) for v in (axis.get("xyz", "1 0 0") if axis is not None else "1 0 0").split()),
                lower=float(limit.get("lower")) if limit is not None and limit.get("lower") is not None and kind != "continuous" else None,
                upper=float(limit.get("upper")) if limit is not None and limit.get("upper") is not None and kind != "continuous" else None,
                mimic=(mimic.get("joint"), float(mimic.get("multiplier", 1)), float(mimic.get("offset", 0))) if mimic is not None else None,
            ))
        return cls(joints)

    @classmethod
    def from_spec(cls, joints: Iterable[Mapping], root: str | None = None) -> "KinematicChain":
        """关节列表：``{name, type, parent, child, origin: {xyz, rpy} | 4×4, axis, lower, upper, mimic}``。"""
        built = []
        for spec in joints:
            origin = spec.get("origin", np.eye(4))
            if isinstance(origin, Mapping):
                origin = transform(origin.get("xyz", (0, 0, 0)), origin.get("rpy", (0, 0, 0)))
            mimic = spec.get("mimic")
            if isinstance(mimic, Mapping):
                mimic = (mimic["joint"], float(mimic.get("multiplier", 1)), float(mimic.get("offset", 0)))
            built.append(Joint(name=spec["name"], type=spec.get("type", "revolute"), parent=spec["parent"], child=spec["child"],
                               origin=origin, axis=tuple(spec.get("axis", (0, 0, 1))), lower=spec.get("lower"),
                               upper=spec.get("upper"), mimic=tuple(mimic) if mimic else None))
        return cls(built, root)

    def values_for(self, values: Mapping[str, float], *, check_limits: bool = True) -> dict[str, float]:
        unknown = set(values) - set(self.by_name)
        if unknown:
            raise ValueError(f"未知关节：{sorted(unknown)}")
        resolved = {}
        for j in self.order:
            if j.type == "fixed":
                continue
            if j.mimic:
                source, multiplier, offset = j.mimic
                value = float(values.get(source, 0.0)) * multiplier + offset
            else:
                value = float(values.get(j.name, 0.0))
            if not math.isfinite(value):
                raise ValueError(f"关节 {j.name} 的值不是有限数")
            if check_limits and ((j.lower is not None and value < j.lower - 1e-9) or (j.upper is not None and value > j.upper + 1e-9)):
                raise ValueError(f"joint_out_of_limit：关节 {j.name}={value} 超出 [{j.lower}, {j.upper}]")
            resolved[j.name] = value
        return resolved

    def link_poses(self, values: Mapping[str, float] | None = None, *, check_limits: bool = True) -> dict[str, np.ndarray]:
        resolved = self.values_for(values or {}, check_limits=check_limits)
        poses = {self.root: np.eye(4)}
        for j in self.order:
            poses[j.child] = poses[j.parent] @ j.local(resolved.get(j.name, 0.0))
        return poses


@dataclass
class Mechanism:
    """一台设备的运动链及挂在各连杆上的碰撞刚体。

    ``bodies``：``{碰撞对象身份: (连杆名, 连杆→对象 变换)}``；``joint_map``：轨迹里的属性名 → 运动链关节名
    （缺省同名）。世界位姿 = ``world_from_root`` × 连杆位姿 × 连杆→对象。
    """

    device_id: str
    chain: KinematicChain
    world_from_root: np.ndarray
    bodies: dict[str, tuple[str, np.ndarray]]
    joint_map: dict[str, str] = field(default_factory=dict)
    check_limits: bool = True

    def __post_init__(self):
        self.world_from_root = rigid(self.world_from_root, f"{self.device_id} 根位姿")
        for identity, (link, offset) in list(self.bodies.items()):
            if link not in self.chain.links:
                raise ValueError(f"{self.device_id}：对象 {identity} 挂在不存在的连杆 {link}")
            self.bodies[identity] = (link, rigid(offset, f"{identity} 连杆偏置"))
        missing = set(self.joint_map.values()) - set(self.chain.by_name)
        if missing:
            raise ValueError(f"{self.device_id}：joint_map 指向不存在的关节 {sorted(missing)}")

    @classmethod
    def from_initial_poses(cls, device_id, chain, world_from_root, body_links: Mapping[str, str],
                           initial_world_poses: Mapping[str, object], *, zero: Mapping[str, float] | None = None,
                           joint_map=None, check_limits=True) -> "Mechanism":
        """由对象在零位（或 ``zero`` 给出的关节值）时的世界位姿推出连杆→对象偏置。

        碰撞 profile 里的 ``world_from_body_initial`` 就是导出时刻的位姿，传入导出时的关节值即可。
        """
        root = rigid(world_from_root)
        links = chain.link_poses(zero or {}, check_limits=False)
        bodies = {}
        for identity, link in body_links.items():
            world_link = root @ links[link]
            bodies[identity] = (link, inverse(world_link) @ rigid(initial_world_poses[identity], identity))
        return cls(device_id, chain, root, bodies, dict(joint_map or {}), check_limits)

    def joint_values(self, values: Mapping[str, float]) -> dict[str, float]:
        return {self.joint_map.get(name, name): float(value) for name, value in values.items()}

    def link_world(self, values: Mapping[str, float]) -> dict[str, np.ndarray]:
        links = self.chain.link_poses(self.joint_values(values), check_limits=self.check_limits)
        return {name: self.world_from_root @ pose for name, pose in links.items()}

    def poses(self, values: Mapping[str, float]) -> dict[str, np.ndarray]:
        links = self.link_world(values)
        return {identity: links[link] @ offset for identity, (link, offset) in self.bodies.items()}


@dataclass(frozen=True)
class Attachment:
    """被某台设备某连杆携带的物料或工具：位姿 = 连杆世界位姿 × ``link_from_object``。"""

    object_id: str
    device_id: str
    link: str
    link_from_object: np.ndarray

    def __post_init__(self):
        object.__setattr__(self, "link_from_object", rigid(self.link_from_object, f"{self.object_id} 携带偏置"))

    @classmethod
    def grasp(cls, object_id, mechanism: Mechanism, link: str, values: Mapping[str, float], world_from_object) -> "Attachment":
        """在抓取时刻记录相对变换：之后物料随连杆走。"""
        world_link = mechanism.link_world(values)[link]
        return cls(object_id, mechanism.device_id, link, inverse(world_link) @ rigid(world_from_object, object_id))


__all__ = ["Joint", "KinematicChain", "Mechanism", "Attachment", "transform", "rpy_matrix", "inverse", "rigid"]
