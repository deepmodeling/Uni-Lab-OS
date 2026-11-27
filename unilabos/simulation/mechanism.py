"""资产行为类的宿主与播放器：在已装配 USD 上按关节定义执行机构动作；只做运动学。

资产包里的行为类是**零导入的普通 Python 类**，不依赖 OS、不碰 USD。公开方法就是机构动作，
第一个参数是 OS 传入的只读关节视图（查限位和当前位置），返回普通数据::

    class Freezer:
        def open_door(self, joints):
            # 开门
            return {"move": {"drawer_joint": joints.upper("drawer_joint")}, "duration": 4.0,
                    "state": {"door": "open"}}

返回一个步骤（字典）或按顺序执行的步骤列表；每步可含 ``move``（关节 → SI 目标）、``duration``（秒）、
``state``（完成时写入的语义状态）、``ease``（默认 True）。没有几何运动的动作返回只有 ``duration`` 的步骤。
另有两种相对运动，都只用于转动关节：

- ``spin``：``{关节: 角速度}`` 或 ``{关节: [起始角速度, 结束角速度]}``（rad/s），这一段内角速度线性变化，
  不做平滑。只用于没有声明限位的连续转动关节（转子、转盘），终值 = 起始值 + 平均角速度 × 时长。
- ``advance``：``{关节: 增量}``（rad），相对当前位置转过这么多。声明了限位的关节，只要声明范围覆盖整圈
  （上限 − 下限 ≥ 2π），播放时按整圈折回到声明范围内：几何姿态与不折回完全相同，限位不放宽（卷轴、收卷盘）。
  范围不足一整圈时，越界照常拒绝。

:class:`Mechanism` 是 OS 侧的宿主：读关节定义、校验限位、摆位、读回。播放器（:func:`plan_action` /
:class:`Playback`）由仿真执行、孪生和 ``python -m unilabos.simulation try`` 共用。行程未声明或目标超出
资产声明范围时拒绝，不裁剪、不缩放。结果是几何运动学证据，不是物理或真机证据。
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import math
import operator
from dataclasses import dataclass, field
from typing import Any, Mapping


class MechanismError(ValueError):
    """机构动作不满足资产声明；``code`` 稳定，供调用方区分未确认行程与越限。"""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class Joint:
    """资产里的一个关节；限位为 SI（平移 m、转动 rad），未声明有限行程时为 None。"""

    name: str
    path: str
    kind: str
    lower: float | None
    upper: float | None


@dataclass(frozen=True)
class Segment:
    """一段运动：到达 ``targets``；``duration`` 为 None 时由动作预计时长分摊。"""

    targets: Mapping[str, float]
    duration: float | None = None
    ease: bool = True
    # 连续转动关节：(起始, 结束) 角速度，rad/s；这一段内线性变化
    spin: Mapping[str, tuple[float, float]] = field(default_factory=dict)
    # 转动关节的相对增量（rad）；声明范围覆盖整圈时按整圈折回
    advance: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Motion:
    """动作方法的返回值：按顺序执行的运动段，以及完成时写入的语义状态。"""

    segments: tuple[Segment, ...] = ()
    state: Mapping[str, Any] = field(default_factory=dict)

    def __add__(self, other: "Motion") -> "Motion":
        if not isinstance(other, Motion):
            return NotImplemented
        return Motion(self.segments + other.segments, {**self.state, **other.state})

    def then(self, **state: Any) -> "Motion":
        """动作完成时把这些语义状态写入机构（例如 door="open"）。"""
        return Motion(self.segments, {**self.state, **state})


def smoothstep(x: float) -> float:
    """0→1 的平滑插值，起止速度为零。"""
    x = min(1.0, max(0.0, x))
    return x * x * (3.0 - 2.0 * x)


class JointView:
    """传给资产行为方法的只读视图：只能查关节限位和当前位置（SI），不能改任何东西。"""

    def __init__(self, mechanism: "Mechanism"):
        self._mechanism = mechanism

    @property
    def names(self) -> list[str]:
        return sorted(self._mechanism.joints)

    def kind(self, name: str) -> str:
        return self._mechanism.joint(name).kind

    def lower(self, name: str) -> float:
        return self._mechanism.lower(name)

    def upper(self, name: str) -> float:
        return self._mechanism.upper(name)

    def position(self, name: str) -> float:
        return self._mechanism.position(name)


class SiteView:
    """传给资产行为方法的只读库位视图：资产自己的库位（USD 里 ``Sites`` 下的 Xform）和参考点（``Frames`` 下的 Xform，
    例如台面基准、工具点）在实例坐标系里的位置。

    名字是实例根下的相对路径去掉 ``Sites`` / ``Frames`` 一级，例如 ``Mechanism/carousel/Racks/column_01/level_1``；
    也可以只写能唯一确定的结尾部分（``column_01/level_1``、``interaction_site``）。库位挂在哪个连杆下
    就跟着哪个连杆动，这里给的是当前关节位置下的值。
    """

    def __init__(self, mechanism: "Mechanism"):
        self._mechanism = mechanism

    @property
    def names(self) -> list[str]:
        return sorted(self._mechanism.sites)

    def has(self, name: str) -> bool:
        try:
            self._mechanism.site(name)
        except MechanismError:
            return False
        return True

    def position(self, name: str) -> tuple[float, float, float]:
        """库位原点在实例坐标系里的位置（米）。"""
        return self._mechanism.site_position(name)

    def along(self, joint: str, name: str) -> float:
        """库位沿某个关节轴方向的坐标（米）：两个库位的差就是让平移关节对齐它们要走的距离。"""
        origin, axis, _, _ = self._mechanism.joint_frame(joint)
        position = self._mechanism.site_position(name)
        return sum((position[i] - origin[i]) * axis[i] for i in range(3))

    def from_frame(self, frame: str, point) -> tuple[float, float, float]:
        """把以某个库位为原点的坐标（例如台面坐标系里的点）换到实例坐标系。"""
        return self._mechanism.point_from_site(frame, point)

    def reach(self, tool: str, point, *, clearance: float = 0.0) -> dict[str, float]:
        """让工具点（挂在活动连杆上的库位）到达实例坐标系里的 ``point`` 所需的关节目标。

        只适用于直角坐标机构：工具点到机座之间全是平移关节，且各轴互相垂直；每个轴走目标点与工具点
        在该轴方向上的差。``clearance`` 沿 +Z 抬高目标点（米）。返回值可直接作为步骤的 ``move``。
        """
        return self._mechanism.reach(tool, (point[0], point[1], point[2] + clearance))

    def angle_about(self, joint: str, name: str) -> float:
        """库位绕某个关节轴的方位角（弧度）：关节正转 q，随它转的库位的方位角就增加 q。"""
        origin, axis, first, second = self._mechanism.joint_frame(joint)
        position = self._mechanism.site_position(name)
        offset = [position[i] - origin[i] for i in range(3)]
        return math.atan2(sum(offset[i] * second[i] for i in range(3)), sum(offset[i] * first[i] for i in range(3)))


class MaterialView:
    """传给资产行为方法的只读物料视图：动作里各物料参数的通用几何属性，由 OS 提供。

    资产只按属性取数（例如 ``materials.get("plate", "size_z")``），不按物料名字判断；
    物料没有声明需要的属性时拒绝，不用默认值。
    """

    def __init__(self, materials: Mapping[str, Mapping[str, Any]] | None, resolver: Any = None):
        self._materials = {key: dict(value) for key, value in (materials or {}).items()}
        self._resolver = resolver

    def point(self, name: str, anchor: str = "top_center") -> tuple[float, float, float]:
        """资源（板、孔、吸头位…）上一个锚点在台面坐标系里的位置（米）；由 OS 沿资源树算出。"""
        if self._resolver is None:
            raise MechanismError("resource_tree_unavailable", "没有提供资源树，算不出 " + name)
        return self._resolver.point(name, anchor)

    def has(self, parameter: str, attribute: str) -> bool:
        return attribute in self._materials.get(parameter, {})

    def get(self, parameter: str, attribute: str) -> Any:
        if not self.has(parameter, attribute) and self._resolver is not None:
            return self._resolver.attribute(parameter, attribute)
        if not self.has(parameter, attribute):
            raise MechanismError("material_geometry_undeclared", f"物料 {parameter} 没有声明 {attribute}")
        return self._materials[parameter][attribute]


class Mechanism:
    """一个资产实例的机构宿主；``instance_root`` 下的 UsdPhysics 关节即它的全部自由度。

    关节可用实例根下的相对路径（``MotionJoints/rotate_joint``）或唯一的 prim 名（``rotate_joint``）
    引用。位置从关节定义的零位起算，与资产加载时的静止画面无关。``behavior`` 是资产包里的行为对象
    （普通 Python 对象）；不给时只能直接摆位。
    """

    def __init__(self, stage, instance_root: str, behavior: Any = None, resources: Any = None):
        from pxr import Usd, UsdGeom, UsdPhysics

        self.behavior = behavior
        self.resources = resources  # 可选：资源树几何查询（mechanism_resources.ResourcePoints）

        if abs(UsdGeom.GetStageMetersPerUnit(stage) - 1) > 1e-12:
            raise MechanismError("stage_units_unsupported", "机构运动要求以米为单位的 USD stage")
        self.stage, self.root = stage, instance_root.rstrip("/")
        root = stage.GetPrimAtPath(self.root)
        if not root:
            raise MechanismError("instance_root_missing", self.root)
        joints: dict[str, Joint] = {}
        leaves: dict[str, list[str]] = {}
        for prim in Usd.PrimRange(root):
            if prim.IsA(UsdPhysics.PrismaticJoint):
                kind, factor = "prismatic", 1.0
            elif prim.IsA(UsdPhysics.RevoluteJoint):
                kind, factor = "revolute", math.pi / 180.0
            else:
                continue
            limits = []
            for side in ("lower", "upper"):
                value = prim.GetAttribute(f"physics:{side}Limit").Get()
                limits.append(float(value) * factor if value is not None and math.isfinite(value) else None)
            name = str(prim.GetPath())[len(self.root) + 1:]
            joints[name] = Joint(name, str(prim.GetPath()), kind, limits[0], limits[1])
            leaves.setdefault(prim.GetName(), []).append(name)
        if not joints:
            raise MechanismError("no_joints", "实例里没有可动关节：" + self.root)
        self._joints = joints
        sites: dict[str, str] = {}
        for prim in Usd.PrimRange(root):
            # ``Sites`` 下是放物料的库位；``Frames`` 下是机构自己的参考点（台面基准、工具点），不当库位用。
            if prim.GetParent().GetName() in ("Sites", "Frames") and prim.IsA(UsdGeom.Xformable):
                relative = str(prim.GetPath())[len(self.root) + 1:]
                sites["/".join(part for part in relative.split("/") if part not in ("Sites", "Frames"))] = str(prim.GetPath())
        self._sites = sites
        self._alias = {leaf: names[0] for leaf, names in leaves.items() if len(names) == 1}
        self._positions = {name: 0.0 for name in joints}
        self._driven = False
        self._stale = False   # 已记下关节值、还没摆到 stage 上（逐帧推演只记值，读 stage 前再摆）
        self.state: dict[str, Any] = {}

    # ------------------------------------------------------------------ 查询
    @property
    def joints(self) -> dict[str, Joint]:
        return dict(self._joints)

    def joint(self, name: str) -> Joint:
        key = name if name in self._joints else self._alias.get(name)
        if key is None:
            raise MechanismError("joint_unknown", f"{name}（可用：{sorted(self._joints)}）")
        return self._joints[key]

    def lower(self, name: str) -> float:
        return self._bound(name, "lower")

    def upper(self, name: str) -> float:
        return self._bound(name, "upper")

    def _bound(self, name: str, side: str) -> float:
        value = getattr(self.joint(name), side)
        if value is None:
            raise MechanismError("joint_range_undeclared", f"{name} 没有声明有限的{side}行程")
        return value

    def position(self, name: str) -> float:
        return self._positions[self.joint(name).name]

    @property
    def positions(self) -> dict[str, float]:
        return dict(self._positions)

    # ------------------------------------------------------------------ 库位与关节坐标系（只读）
    @property
    def sites(self) -> dict[str, str]:
        return dict(self._sites)

    def site(self, name: str) -> str:
        """库位的规范名；可以给完整相对路径，或能唯一确定的结尾部分。"""
        if name in self._sites:
            return name
        matches = [key for key in self._sites if key.endswith("/" + name)]
        if len(matches) != 1:
            raise MechanismError("site_unknown" if not matches else "site_ambiguous",
                                 f"{name}（匹配到 {len(matches)} 个库位）")
        return matches[0]

    def _in_instance(self, world_point):
        self._sync()
        from pxr import UsdGeom

        root = UsdGeom.XformCache().GetLocalToWorldTransform(self.stage.GetPrimAtPath(self.root))
        return root.GetInverse().Transform(world_point)

    def site_position(self, name: str) -> tuple[float, float, float]:
        self._sync()
        from pxr import UsdGeom

        prim = self.stage.GetPrimAtPath(self._sites[self.site(name)])
        world = UsdGeom.XformCache().GetLocalToWorldTransform(prim).ExtractTranslation()
        point = self._in_instance(world)
        return (float(point[0]), float(point[1]), float(point[2]))

    def point_from_site(self, frame: str, point) -> tuple[float, float, float]:
        self._sync()
        from pxr import Gf, UsdGeom

        prim = self.stage.GetPrimAtPath(self._sites[self.site(frame)])
        world = UsdGeom.XformCache().GetLocalToWorldTransform(prim).Transform(Gf.Vec3d(*map(float, point)))
        local = self._in_instance(world)
        return (float(local[0]), float(local[1]), float(local[2]))

    def reach(self, tool: str, point) -> dict[str, float]:
        """直角坐标机构的求解：工具点所在连杆到机座之间的平移关节各走各的轴向差。"""
        self._sync()
        site_path = self._sites[self.site(tool)]
        by_child = {}
        for joint in self._joints.values():
            targets = self.stage.GetPrimAtPath(joint.path).GetRelationship("physics:body1").GetTargets()
            if len(targets) == 1:
                by_child[str(targets[0])] = joint
        body = next((path for path in sorted(by_child, key=len, reverse=True)
                     if site_path == path or site_path.startswith(path + "/")), None)
        if body is None:
            raise MechanismError("tool_not_on_moving_link", tool)
        chain = []
        while body in by_child:
            joint = by_child[body]
            if joint.kind != "prismatic":
                raise MechanismError("reach_needs_cartesian_chain", f"{joint.name} 不是平移关节；非直角机构要用逆解")
            # 没有声明有限行程的平移关节不参与求解（例如通道间距调节），保持原位。
            if joint.lower is not None and joint.upper is not None:
                chain.append(joint)
            parents = self.stage.GetPrimAtPath(joint.path).GetRelationship("physics:body0").GetTargets()
            body = str(parents[0]) if parents else None
        axes = [self.joint_frame(joint.name)[1] for joint in chain]
        for i in range(len(axes)):
            for j in range(i + 1, len(axes)):
                if abs(sum(axes[i][k] * axes[j][k] for k in range(3))) > 1e-6:
                    raise MechanismError("reach_needs_cartesian_chain", "各平移轴必须互相垂直")
        current = self.site_position(tool)
        delta = [float(point[k]) - current[k] for k in range(3)]
        covered = [sum(delta[k] * axis[k] for k in range(3)) for axis in axes]
        residual = [delta[k] - sum(covered[i] * axes[i][k] for i in range(len(axes))) for k in range(3)]
        if max(abs(value) for value in residual) > 1e-6:
            raise MechanismError("reach_direction_uncovered", f"{tool} 的平移轴到不了这个方向：差 {residual}")
        return {joint.name: self._positions[joint.name] + covered[index] for index, joint in enumerate(chain)}

    def _relative(self, step: Mapping[str, Any], targets: Mapping[str, float]):
        """校验 ``spin`` / ``advance``：只用于转动关节，同一关节不能在一步里既 move 又相对转动。"""
        spin, advance = {}, {}
        for key, out in (("spin", spin), ("advance", advance)):
            raw = step.get(key) or {}
            if not isinstance(raw, Mapping):
                raise MechanismError("action_result_unsupported", f"{key} 必须是 {{关节: 值}}")
            for name, value in raw.items():
                joint = self.joint(name)
                if joint.kind != "revolute":
                    raise MechanismError(f"{key}_needs_revolute_joint", f"{name} 不是转动关节")
                if joint.name in targets or joint.name in spin or joint.name in advance:
                    raise MechanismError("target_duplicate", name)
                if key == "spin":
                    if joint.lower is not None or joint.upper is not None:
                        raise MechanismError("spin_needs_continuous_joint",
                                             f"{name} 声明了限位，匀速转动只用于连续转动关节；有限转动用 move 或 advance")
                    pair = list(value) if isinstance(value, (list, tuple)) else [value, value]
                    if len(pair) != 2 or any(isinstance(v, bool) or not isinstance(v, (int, float))
                                             or not math.isfinite(v) for v in pair):
                        raise MechanismError("target_invalid", f"{name} 的 spin={value!r}")
                    out[joint.name] = (float(pair[0]), float(pair[1]))
                else:
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                        raise MechanismError("target_invalid", f"{name} 的 advance={value!r}")
                    out[joint.name] = float(value)
        return spin, advance

    def wrapped(self, name: str, value: float) -> float:
        """把转动关节的值按整圈折回到声明范围内；范围不足整圈、折不回去时拒绝。"""
        joint = self.joint(name)
        if joint.lower is None or joint.upper is None:
            return value
        turn = 2 * math.pi
        if joint.upper - joint.lower < turn - 1e-9:
            if joint.lower - 1e-7 <= value <= joint.upper + 1e-7:
                return value
            raise MechanismError("target_out_of_range",
                                 f"{name}={value} 超出资产声明范围 [{joint.lower}, {joint.upper}]，范围不足一整圈，不能折回")
        while value > joint.upper + 1e-9:
            value -= turn
        while value < joint.lower - 1e-9:
            value += turn
        return value

    def joint_frame(self, name: str):
        """关节坐标系在实例坐标系里的原点、轴方向和两个垂直方向（随父连杆，不随本关节动）。"""
        self._sync()
        from pxr import Gf, UsdGeom

        prim = self.stage.GetPrimAtPath(self.joint(name).path)
        parents = prim.GetRelationship("physics:body0").GetTargets()
        cache = UsdGeom.XformCache()
        parent = cache.GetLocalToWorldTransform(self.stage.GetPrimAtPath(parents[0])) if parents else Gf.Matrix4d(1)
        rotation = prim.GetAttribute("physics:localRot0").Get()
        local = Gf.Matrix4d(1).SetRotate(Gf.Rotation(Gf.Quatd(rotation.GetReal(), Gf.Vec3d(rotation.GetImaginary())))) \
            * Gf.Matrix4d(1).SetTranslate(Gf.Vec3d(prim.GetAttribute("physics:localPos0").Get()))
        frame = local * parent * cache.GetLocalToWorldTransform(self.stage.GetPrimAtPath(self.root)).GetInverse()
        index = "XYZ".index(prim.GetAttribute("physics:axis").Get())
        basis = [Gf.Vec3d(1, 0, 0), Gf.Vec3d(0, 1, 0), Gf.Vec3d(0, 0, 1)]
        directions = [frame.TransformDir(basis[(index + k) % 3]).GetNormalized() for k in range(3)]
        origin = frame.ExtractTranslation()
        return tuple(tuple(float(v[i]) for i in range(3)) for v in (origin, *directions))

    # ------------------------------------------------------------------ 规划（不改 stage）
    def move(self, targets: Mapping[str, float], *, duration: float | None = None, ease: bool = True) -> Motion:
        """到达关节目标（SI）；立刻按资产限位校验，越限或行程未声明则拒绝。"""
        return Motion((Segment(self._checked(targets), self._duration(duration), ease),))

    def hold(self, duration: float) -> Motion:
        """保持当前位置一段时间。"""
        return Motion((Segment({}, self._duration(duration)),))

    @staticmethod
    def _duration(duration: float | None) -> float | None:
        if duration is None:
            return None
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) \
                or duration < 0:
            raise MechanismError("duration_invalid", repr(duration))
        return float(duration)

    def _checked(self, targets: Mapping[str, float]) -> dict[str, float]:
        result = {}
        for name, value in targets.items():
            joint = self.joint(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise MechanismError("target_invalid", f"{name}={value!r}")
            if joint.kind == "prismatic" and (joint.lower is None or joint.upper is None):
                # 平移关节没有声明有限行程：任何非零目标都没有依据。连续转动关节（转子、转盘）不受此限。
                raise MechanismError("joint_range_undeclared", f"{name} 没有声明有限行程，不能给出运动目标")
            for bound, wrong in ((joint.lower, lambda b: value < b - 1e-7), (joint.upper, lambda b: value > b + 1e-7)):
                if bound is not None and wrong(bound):
                    raise MechanismError(
                        "target_out_of_range",
                        f"{name}={value} 超出资产声明范围 [{joint.lower}, {joint.upper}]，不裁剪、不缩放")
            if joint.name in result:
                raise MechanismError("target_duplicate", name)
            result[joint.name] = float(value)
        return result

    # ------------------------------------------------------------------ 摆位与读回
    def hold_positions(self, values: Mapping[str, float]) -> dict[str, float]:
        """只记下关节值（经过同样的行程校验），不摆 stage；任何读 stage 的查询之前会先补摆。

        逐帧推演用它：大资产每次全量摆位要几十到几百毫秒，而观测流只需要关节值。返回记下的值。"""
        merged = {**self._positions, **self._checked(values)}
        self._positions, self._stale = merged, True
        return dict(merged)

    def _sync(self) -> None:
        if self._stale:
            self.set_positions({})

    def set_positions(self, values: Mapping[str, float]) -> dict[str, float]:
        """立刻摆到给定关节值；每次按依赖顺序重摆全部关节，返回从 stage 读回的值。"""
        from .joint_projection import project_joint_positions

        merged = {**self._positions, **self._checked(values)}
        project_joint_positions(self.stage, {self._joints[n].path: q for n, q in merged.items()}, limit_tolerance=1e-7)
        self._positions, self._driven, self._stale = merged, True, False
        return self.read()

    def read(self) -> dict[str, float]:
        """从已装配的刚体位姿反算关节值；还没摆过位时返回零位（资产静止画面不一定在零位）。"""
        from .joint_projection import read_joint_positions

        self._sync()
        if not self._driven:
            return dict(self._positions)
        by_path = read_joint_positions(self.stage, [j.path for j in self._joints.values()])
        result = {}
        for name, joint in self._joints.items():
            value = by_path[joint.path]
            if joint.kind == "revolute":
                # 刚体位姿只能确定转角模 2π；圈数取与已下发位置最接近的那一个。
                value += 2 * math.pi * round((self._positions[name] - value) / (2 * math.pi))
            result[name] = value
        return result

    def reset(self) -> dict[str, float]:
        return self.set_positions({name: 0.0 for name in self._joints})

    def snapshot(self) -> dict[str, Any]:
        return {"positions_si": self.read(), "state": dict(self.state),
                "qualification": "kinematic_geometry_only", "hardware_calibrated": False}


# ---------------------------------------------------------------------- 动作 → 计划
_BINARY = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
           ast.Pow: operator.pow, ast.Mod: operator.mod, ast.FloorDiv: operator.floordiv}
_FUNCTIONS = {"abs": abs, "min": min, "max": max, "ceil": math.ceil, "floor": math.floor, "sqrt": math.sqrt}
_CONSTANTS = {"pi": math.pi}


def evaluate_expression(expression: str, parameters: Mapping[str, Any]) -> float:
    """计算一个数值表达式：只允许数字、动作入参、四则运算和少数函数。"""

    def value(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in parameters:
                item = parameters[node.id]
                if isinstance(item, bool) or not isinstance(item, (int, float)):
                    raise MechanismError("expression_parameter_not_numeric", node.id)
                return item
            if node.id in _CONSTANTS:
                return _CONSTANTS[node.id]
            raise MechanismError("expression_name_unknown", node.id)
        if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
            return _BINARY[type(node.op)](value(node.left), value(node.right))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            operand = value(node.operand)
            return -operand if isinstance(node.op, ast.USub) else operand
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCTIONS \
                and not node.keywords:
            return _FUNCTIONS[node.func.id](*[value(a) for a in node.args])
        raise MechanismError("expression_unsupported", expression)

    try:
        result = float(value(ast.parse(expression, mode="eval").body))
    except (SyntaxError, ZeroDivisionError, OverflowError, TypeError) as error:
        raise MechanismError("expression_invalid", f"{expression}: {error}") from error
    if not math.isfinite(result):
        raise MechanismError("expression_invalid", f"{expression} = {result}")
    return result


def evaluate_duration(expression: str, parameters: Mapping[str, Any]) -> float:
    """计算 ``estimate_duration_express``；结果必须是非负秒数。"""
    result = evaluate_expression(expression, parameters)
    if result < 0:
        raise MechanismError("duration_expression_invalid", f"{expression} = {result}")
    return result


def behavior_actions(behavior: Any) -> dict[str, Any]:
    """行为对象的机构动作：{名字: 绑定方法}。公开方法（不以下划线开头）即动作。"""
    found = {}
    for name, member in inspect.getmembers(type(behavior), inspect.isfunction):
        if not name.startswith("_"):
            found[name] = getattr(behavior, name)
    return found


def mechanism_actions(mechanism: Mechanism) -> dict[str, Any]:
    if mechanism.behavior is None:
        raise MechanismError("behavior_missing", "这个机构没有行为对象")
    return behavior_actions(mechanism.behavior)


INJECTED_VIEWS = ("sites", "materials")


def action_parameters(method) -> list[str]:
    """机构动作的入参名：不含第一个参数（关节视图），也不含按名字注入的 ``sites`` / ``materials`` 视图。"""
    return [name for name in list(inspect.signature(method).parameters)[1:] if name not in INJECTED_VIEWS]


@dataclass(frozen=True)
class Plan:
    """一次动作的完整计划：各段时长已定，整条运动已按资产限位校验。"""

    action: str
    segments: tuple[Segment, ...]
    duration: float
    state: Mapping[str, Any]
    # 显示提示：(开始时刻, 源库位, 目标库位)。物料看起来从哪个库位挂到哪个库位；不改库存。
    carries: tuple[tuple[float, str, str], ...] = ()


_STEP_KEYS = {"move", "duration", "state", "ease", "carry", "spin", "advance"}


def plan_action(mechanism: Mechanism, action: str, parameters: Mapping[str, Any] | None = None, *,
                default_duration: float | None = None,
                materials: Mapping[str, Mapping[str, Any]] | None = None) -> Plan:
    """调用行为对象的动作方法得到步骤，按资产限位校验整条运动。

    步骤没写 ``duration`` 时用 ``default_duration``（例如驱动动作的预计时长）均分；两者都没有则拒绝。
    """
    actions = mechanism_actions(mechanism)
    if action not in actions:
        raise MechanismError("action_unknown", f"{action}（可用：{sorted(actions)}）")
    injected = {}
    declared = inspect.signature(actions[action]).parameters
    if "sites" in declared:
        injected["sites"] = SiteView(mechanism)
    if "materials" in declared:
        injected["materials"] = MaterialView(materials, mechanism.resources)
    returned = actions[action](JointView(mechanism), **{**dict(parameters or {}), **injected})
    if inspect.isawaitable(returned):
        returned = asyncio.run(_await(returned))
    steps = [] if returned is None else [returned] if isinstance(returned, Mapping) else list(returned) \
        if isinstance(returned, (list, tuple)) else None
    if steps is None or any(not isinstance(step, Mapping) or set(step) - _STEP_KEYS for step in steps):
        raise MechanismError("action_result_unsupported",
                             f"{action} 必须返回步骤字典或其列表，键只能是 {sorted(_STEP_KEYS)}")
    segments, state, carry_steps = [], {}, []
    for step in steps:
        motion = mechanism.move(step.get("move") or {}, duration=step.get("duration"), ease=step.get("ease", True))
        spin, advance = mechanism._relative(step, motion.segments[0].targets)
        if spin or advance:
            base = motion.segments[0]
            motion = Motion((Segment(base.targets, base.duration, base.ease, spin, advance),))
        carry = step.get("carry")
        if carry is not None:
            if not isinstance(carry, Mapping) or set(carry) != {"from", "to"}:
                raise MechanismError("action_result_unsupported", f"{action} 的 carry 必须是 {{from, to}} 两个库位名")
            # 两端是资产库位，或 ``resource:<资源名>``（资源树里的位置，例如吸头位）。
            ends = [end if isinstance(end, str) and end.startswith("resource:") else mechanism.site(end)
                    for end in (carry["from"], carry["to"])]
            carry_steps.append((len(segments), ends[0], ends[1]))
        segments.extend(motion.segments)
        if not isinstance(step.get("state") or {}, Mapping):
            raise MechanismError("action_result_unsupported", f"{action} 的 state 必须是字典")
        state.update(step.get("state") or {})
    open_segments = [i for i, s in enumerate(segments) if s.duration is None]
    if open_segments:
        if default_duration is None:
            raise MechanismError("duration_undeclared", f"{action} 有没写 duration 的步骤")
        explicit = sum(s.duration for s in segments if s.duration is not None)
        share = max(0.0, float(default_duration) - explicit) / len(open_segments)
        for index in open_segments:
            segments[index] = Segment(segments[index].targets, share, segments[index].ease,
                                      segments[index].spin, segments[index].advance)
    # 相对转动按顺序折算一遍：范围不足整圈的关节越界在计划阶段就拒绝
    values = dict(mechanism.positions)
    for segment in segments:
        values.update(segment.targets)
        for name, delta in segment.advance.items():
            values[name] = mechanism.wrapped(name, values[name] + delta)
    starts, clock = [], 0.0
    for segment in segments:
        starts.append(clock)
        clock += segment.duration
    carries = tuple((starts[index], source, target) for index, source, target in carry_steps)
    return Plan(action, tuple(segments), clock, state, carries)


async def _await(awaitable):
    return await awaitable


READBACK_TOLERANCE = {"prismatic": 1e-6, "revolute": 1e-6}   # m / rad；运动学求值后读回与计划值的允许差


class Playback:
    """按时间执行一个计划；``at(t)`` 可重复调用，``t`` 为动作开始后的秒数。

    运动学（kinematic）完成判据：每段终了是一个确认步，摆到该段终值并从 stage 读回，读回与计划值在
    :data:`READBACK_TOLERANCE` 内才算到位；只是计时到了或画面播完不算完成。:meth:`advance` 给逐帧推演用：
    帧与帧之间只记关节值，越过确认步时补摆读回；:meth:`finish` 确认剩下的所有步再收尾。
    """

    def __init__(self, mechanism: Mechanism, plan: Plan):
        self.mechanism, self.plan = mechanism, plan
        # 只跟踪计划涉及的关节；其余关节保持原位，不重新校验（例如行程未声明、本动作不动的关节）。
        touched = {name for segment in plan.segments for name in (*segment.targets, *segment.spin, *segment.advance)}
        self._starts: list[tuple[float, dict[str, float]]] = []
        self.ends: list[dict[str, float]] = []   # 每段终了时本段涉及关节的值（spin/advance 已折算）
        clock, values = 0.0, {name: mechanism.positions[name] for name in touched}
        for segment in plan.segments:
            self._starts.append((clock, dict(values)))
            end = dict(segment.targets)
            for name, (v0, v1) in segment.spin.items():
                end[name] = values[name] + (v0 + v1) / 2 * segment.duration
            for name, delta in segment.advance.items():
                end[name] = mechanism.wrapped(name, values[name] + delta)
            values.update(end)
            self.ends.append(end)
            clock += segment.duration
        self._final = dict(values)
        self.confirmed: list[dict[str, Any]] = []   # 已确认的段：{segment, t, readback}
        self._carry_poses: dict[int, dict[str, Any]] = {}   # 第几次搬运 → 两端库位读回后的实际位置
        # 搬运从哪一段开始（计划里搬运时刻就是该段起点）；在该段的确认步核对两端
        self._carry_segment = [next((i for i, (start, _) in enumerate(self._starts) if abs(start - t) <= 1e-9),
                                    len(self._starts) - 1) for t, _, _ in plan.carries]

    def _confirm(self, index: int) -> dict[str, float]:
        """第 ``index`` 段的确认步：摆到段终值、读回并核对；不到位抛 ``readback_mismatch``。"""
        start, begin = self._starts[index]
        targets = {**begin, **self.ends[index]}
        readback = self.mechanism.set_positions(targets)
        for name, planned in self.ends[index].items():
            joint = self.mechanism.joint(name)
            actual = readback[joint.name]
            if abs(actual - planned) > READBACK_TOLERANCE.get(joint.kind, 1e-6):
                raise MechanismError("readback_mismatch",
                                     f"{self.plan.action} 第 {index + 1} 段：{name} 计划 {planned}，读回 {actual}")
        t = start + self.plan.segments[index].duration
        self.confirmed.append({"segment": index, "t": round(t, 6),
                               "readback": {name: readback[self.mechanism.joint(name).name] for name in self.ends[index]}})
        self._carry_checks(index)
        return readback

    def _carry_checks(self, index: int) -> None:
        """搬运所在段的确认步：记下读回后两端库位在实例坐标系里的实际位置（释放时用实际位姿，
        不把物体写到计划位置）；库位在资产里找不到就不算到位。``resource:`` 端点不在本设备资产里，位置留空。"""
        for k, (_, source, target) in enumerate(self.plan.carries):
            if self._carry_segment[k] != index:
                continue
            self._carry_poses[k] = {side: (None if site.startswith("resource:")
                                           else [round(v, 6) for v in self.mechanism.site_position(site)])
                                    for side, site in (("from", source), ("to", target))}

    def advance(self, t: float) -> dict[str, float]:
        """逐帧推演到 ``t``：先确认 ``t`` 之前已经结束、还没确认的段，再记下 ``t`` 时刻的关节值（不摆 stage）。"""
        for index, (start, _) in enumerate(self._starts):
            if len(self.confirmed) > index:
                continue
            if start + self.plan.segments[index].duration <= t:
                self._confirm(index)
            else:
                break
        return self.at(t, project=False)

    def targets_at(self, t: float) -> dict[str, float]:
        """``t`` 时刻计划涉及的各关节的值。"""
        if t >= self.plan.duration or not self._starts:
            return dict(self._final)
        values = dict(self._starts[0][1])
        for segment, (start, begin) in zip(self.plan.segments, self._starts):
            if t < start:
                break
            fraction = 1.0 if segment.duration <= 0 else min(1.0, (t - start) / segment.duration)
            eased = smoothstep(fraction) if segment.ease else fraction
            values = dict(begin)
            for name, target in segment.targets.items():
                values[name] = begin[name] + (target - begin[name]) * eased
            elapsed = fraction * segment.duration
            for name, (v0, v1) in segment.spin.items():
                rate = 0.0 if segment.duration <= 0 else (v1 - v0) / segment.duration
                values[name] = begin[name] + v0 * elapsed + rate * elapsed * elapsed / 2
            for name, delta in segment.advance.items():
                values[name] = self.mechanism.wrapped(name, begin[name] + delta * eased)
        return values

    def at(self, t: float, *, project: bool = True) -> dict[str, float]:
        """摆到 ``t`` 时刻并返回读回的关节值。

        ``project=False`` 只记下该时刻的关节值、不摆 stage（逐帧推演用；结束时 :meth:`finish` 摆位读回），
        返回的是记下的值而不是读回值。"""
        targets = self.targets_at(max(0.0, t))
        return self.mechanism.set_positions(targets) if project else self.mechanism.hold_positions(targets)

    def finish(self) -> dict[str, Any]:
        """确认剩下的所有段（摆位读回并核对到位），写入语义状态，返回运动学完成证据。"""
        readback = self.mechanism.read()
        for index in range(len(self.confirmed), len(self.plan.segments)):
            readback = self._confirm(index)
        if not self.plan.segments:
            readback = self.mechanism.set_positions({})
        self.mechanism.state.update(self.plan.state)
        carries = [{"t": t, "from": source, "to": target, "basis": "kinematic",
                    "from_position": self._carry_poses.get(k, {}).get("from"),
                    "to_position": self._carry_poses.get(k, {}).get("to")}
                   for k, (t, source, target) in enumerate(self.plan.carries)]
        return {"action": self.plan.action, "duration_s": self.plan.duration, "positions_si": readback,
                "state": dict(self.mechanism.state), "evidence": "kinematic",
                "confirmed_steps": list(self.confirmed), "carries": carries}


def run_action(mechanism: Mechanism, action: str, parameters: Mapping[str, Any] | None = None, *,
               rate_hz: float = 20.0, materials: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """计划并按固定采样率执行一次动作，返回逐帧读回和完成证据（独立测试用）。"""
    if rate_hz <= 0:
        raise MechanismError("rate_invalid", repr(rate_hz))
    plan = plan_action(mechanism, action, parameters, materials=materials)
    playback = Playback(mechanism, plan)
    frames = []
    for index in range(int(math.floor(plan.duration * rate_hz)) + 1):
        t = index / rate_hz
        frames.append({"t": round(t, 6), "positions_si": playback.at(t)})
    return {"plan": {"action": plan.action, "duration_s": plan.duration,
                     "segments": [{"targets": dict(end), "duration_s": s.duration,
                          **({"spin": {n: list(v) for n, v in s.spin.items()}} if s.spin else {}),
                          **({"advance": dict(s.advance)} if s.advance else {})}
                         for s, end in zip(plan.segments, playback.ends)]},
            "frames": frames, "result": playback.finish()}
