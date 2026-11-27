"""把部署图装配成引擎无关的 USD 场景，并消费外部校准表。

模块只做三件事：校验部署图、按校准把节点摆到世界坐标、把结果写成 USD。
它不启动仿真、不执行设备动作、不修改部署图，也不依赖 Blender 或 Isaac；
``pxr`` 只在 ``build_stage``/``inspect_assets``/``measure_nodes`` 内延迟导入，
纯几何与校验逻辑用普通解释器即可测试。

坐标合同：

* 部署图位置是**毫米**、z-up，且相对父节点（根节点相对世界原点）；
* 世界位置 = 父链局部位置之和，再叠加校准偏移（全局 + 每节点，单位毫米）；
  父节点带朝向时，子节点的局部位移先按父级世界朝向旋转再累加；
  校准按节点绝对偏移生效，未列入表的节点继承父节点偏移，根节点回落到全局
  偏移，因此仓库内的工位实例跟着所属仓库一起走；
* 朝向与 ``position`` 同级：``rotation`` 是外旋 XYZ（度，行向量 ``M = Mx·My·Mz``），
  ``rotation_matrix_row_major`` 是同口径的精确 3×3 矩阵且优先于角度；两者都没有
  时按单位阵处理，``build_stage`` 因此不写多余的 orient op。``config.rotation``/
  ``config.rotation_matrix_row_major`` 作为兼容入口保留（老图仍可用），节点级声明
  优先；节点级 ``scale``/``rotation_quaternion``/``orient`` 仍然拒绝；
* 资产原点语义按族声明：``frame="bbox_min"``（默认）声明资产作者把包围盒最小角
  放在资产原点，``frame="asset_frame"`` 声明资产保留 CAD/glTF 作者坐标系、
  原点不是包围盒最小角，此时实测偏移只作为 ``origin_offset_mm`` 证据；
  节点的绝对补偿继续用既有校准表（``offset_mm``），不引入第二套偏移机制；
* 仓库 ``config.sites`` 的位置相对所属仓库节点；容器位置与 site 位置同源，
  由 ``site_bindings`` 用 ``occupied_by`` 绑定并核对；
* USD 侧统一 ``metersPerUnit=1``、``upAxis=Z``，只在写 stage 时换算米。
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

AXES = ("x", "y", "z")
MM_PER_M = 1000.0
_UNSUPPORTED_TRANSFORMS = ("scale", "rotation_quaternion", "orient")
_SAFE_PRIM_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_ORIGIN_TOLERANCE_MM = 20.0
_VECTOR = tuple[float, float, float]
_MATRIX3 = tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]
IDENTITY_MATRIX: _MATRIX3 = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
_IDENTITY_EPSILON = 1e-12
_RIGID_TOLERANCE = 1e-3


class AssemblyError(ValueError):
    """装配合同被违反；``problems`` 保留全部违规项而不是只报第一条。"""

    def __init__(self, problems: Sequence[str]) -> None:
        """把违规项拼成稳定消息，并原样保留给调用方与报告。"""
        self.problems = tuple(problems)
        super().__init__("; ".join(self.problems) or "assembly_invalid")


def _add(left: _VECTOR, right: _VECTOR) -> _VECTOR:
    """逐轴相加。"""
    return (left[0] + right[0], left[1] + right[1], left[2] + right[2])


def _sub(left: _VECTOR, right: _VECTOR) -> _VECTOR:
    """逐轴相减。"""
    return (left[0] - right[0], left[1] - right[1], left[2] - right[2])


def _maximum(vector: _VECTOR) -> float:
    """返回三轴绝对值的最大值，用于单一阈值比较。"""
    return max(abs(axis) for axis in vector)


def _round(vector: _VECTOR, digits: int = 3) -> list[float]:
    """按报告精度取整，避免浮点尾数污染证据文件。"""
    return [round(axis, digits) for axis in vector]


def _multiply_matrix3(left: _MATRIX3, right: _MATRIX3) -> _MATRIX3:
    """矩阵相乘 ``left · right``；行向量约定下 ``p·left·right`` 即先左后右。"""
    return tuple(
        tuple(sum(left[row][axis] * right[axis][column] for axis in range(3)) for column in range(3))
        for row in range(3)
    )


def _transpose_matrix3(matrix: _MATRIX3) -> _MATRIX3:
    """转置；旋转矩阵的逆就是它的转置。"""
    return tuple(tuple(matrix[column][row] for column in range(3)) for row in range(3))


def _rotate(vector: _VECTOR, matrix: _MATRIX3) -> _VECTOR:
    """行向量约定下把向量按矩阵旋转：``vector · matrix``。"""
    return tuple(sum(vector[axis] * matrix[axis][column] for axis in range(3)) for column in range(3))


def _cos_sin(radians: float) -> tuple[float, float]:
    """取整到 1e-12：让 90° 的余弦是 ``0.0`` 而不是 ``6.1e-17``，矩阵可读可比对。"""
    return round(math.cos(radians), 12), round(math.sin(radians), 12)


def _euler_xyz_matrix(angles: _VECTOR) -> _MATRIX3:
    """外旋 XYZ（度）编译成行向量旋转矩阵 ``M = Mx·My·Mz``。

    行向量约定（USD 的 ``xformOpOrder`` 与 ``matrix4d`` 都是这个口径）下，
    绕单轴旋转矩阵是列向量约定的转置，所以 ``Rz`` 的第一行是 ``(cos, sin, 0)``。
    """
    cos_x, sin_x = _cos_sin(math.radians(angles[0]))
    cos_y, sin_y = _cos_sin(math.radians(angles[1]))
    cos_z, sin_z = _cos_sin(math.radians(angles[2]))
    rotation_x = ((1.0, 0.0, 0.0), (0.0, cos_x, sin_x), (0.0, -sin_x, cos_x))
    rotation_y = ((cos_y, 0.0, -sin_y), (0.0, 1.0, 0.0), (sin_y, 0.0, cos_y))
    rotation_z = ((cos_z, sin_z, 0.0), (-sin_z, cos_z, 0.0), (0.0, 0.0, 1.0))
    return _multiply_matrix3(_multiply_matrix3(rotation_x, rotation_y), rotation_z)


def _is_identity_matrix(matrix: _MATRIX3, tolerance: float = _IDENTITY_EPSILON) -> bool:
    """判断是否单位阵；旧图不写 orient op 的判据。"""
    return all(abs(matrix[row][column] - IDENTITY_MATRIX[row][column]) <= tolerance
               for row in range(3) for column in range(3))


def _is_rigid(matrix: _MATRIX3, tolerance: float = _RIGID_TOLERANCE) -> bool:
    """旋转矩阵必须正交且行列式为 +1；退化的 3×3 矩阵会导致静默错位。"""
    for row in range(3):
        for column in range(3):
            dot = sum(matrix[row][axis] * matrix[column][axis] for axis in range(3))
            expected = 1.0 if row == column else 0.0
            if abs(dot - expected) > tolerance:
                return False
    (a, b, c), (d, e, f), (g, h, i) = matrix
    return a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g) > 0.0


# --------------------------------------------------------------------------
# 部署图
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Node:
    """部署图中的一个节点；位置是相对父节点的毫米坐标，朝向是相对父节点的行向量矩阵。"""

    id: str
    class_name: str
    kind: str
    parent: str | None
    children: tuple[str, ...]
    position_mm: _VECTOR
    config: Mapping[str, Any] = field(default_factory=dict)
    data: Mapping[str, Any] = field(default_factory=dict)
    rotation_row_major: _MATRIX3 = IDENTITY_MATRIX


@dataclass(frozen=True)
class SceneGraph:
    """已校验的部署图：节点表、根节点、父先于子的稳定顺序与诊断信息。"""

    nodes: Mapping[str, Node]
    roots: tuple[str, ...]
    order: tuple[str, ...]
    diagnostics: tuple[str, ...] = ()

    def class_names(self) -> frozenset[str]:
        """返回图中出现过的全部 class 名。"""
        return frozenset(node.class_name for node in self.nodes.values())

    def kinds(self) -> dict[str, int]:
        """按部署图 ``type`` 统计节点数。"""
        counts: dict[str, int] = {}
        for node in self.nodes.values():
            counts[node.kind or "unknown"] = counts.get(node.kind or "unknown", 0) + 1
        return dict(sorted(counts.items()))


def _vector(value: Any, *, problems: list[str], identity: str, field_name: str = "position") -> _VECTOR | None:
    """把 ``{x,y,z}`` 解析为有限数三元组；不合格时登记问题并返回 None。"""
    if not isinstance(value, Mapping):
        problems.append(f"{field_name}_missing: {identity}")
        return None
    if set(value) != set(AXES):
        problems.append(f"{field_name}_axes_invalid: {identity} {sorted(value)}")
        return None
    axes = tuple(value[axis] for axis in AXES)
    if any(type(number) not in (int, float) or not math.isfinite(number) for number in axes):
        problems.append(f"{field_name}_not_finite: {identity}")
        return None
    return (float(axes[0]), float(axes[1]), float(axes[2]))


def _matrix3(value: Any, *, problems: list[str], identity: str) -> _MATRIX3 | None:
    """把 3×3 行主序列表读成有限数矩阵；形状或数值不合格时登记问题并返回 None。"""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 3:
        problems.append(f"node_rotation_matrix_invalid: {identity}")
        return None
    rows: list[tuple[float, float, float]] = []
    for row in value:
        if not isinstance(row, Sequence) or isinstance(row, (str, bytes)) or len(row) != 3:
            problems.append(f"node_rotation_matrix_invalid: {identity}")
            return None
        if any(type(number) not in (int, float) or not math.isfinite(number) for number in row):
            problems.append(f"node_rotation_not_finite: {identity}")
            return None
        rows.append((float(row[0]), float(row[1]), float(row[2])))
    return (rows[0], rows[1], rows[2])


def _rotation_of(node: Mapping[str, Any], *, problems: list[str], identity: str) -> _MATRIX3:
    """读节点朝向：精确矩阵优先于角度，节点级声明优先于 ``config`` 兼容入口。

    ``rotation_matrix_row_major`` 是 3×3 行主序矩阵（行向量约定），
    ``rotation`` 是外旋 XYZ 角度（度，``M = Mx·My·Mz``）。节点级两者都没有时
    回落到 ``config`` 里的同名键（老图兼容）；都没有则按单位阵。
    """
    for source in (node, node.get("config")):
        if not isinstance(source, Mapping):
            continue
        has_matrix = source.get("rotation_matrix_row_major") is not None
        has_angles = source.get("rotation") is not None
        if not (has_matrix or has_angles):
            continue
        if has_matrix:
            matrix = _matrix3(source["rotation_matrix_row_major"], problems=problems, identity=identity)
            if matrix is None:
                return IDENTITY_MATRIX
            if not _is_rigid(matrix):
                problems.append(f"node_rotation_not_rigid: {identity}")
                return IDENTITY_MATRIX
            return matrix
        angles = _vector(source.get("rotation"), problems=problems, identity=identity,
                         field_name="node_rotation")
        if angles is None:
            return IDENTITY_MATRIX
        return _euler_xyz_matrix(angles)
    return IDENTITY_MATRIX


def _read_raw_nodes(graph: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """把 ``nodes`` 数组读成以 id 为键的表；结构性问题一次报完。"""
    if not isinstance(graph, Mapping):
        raise AssemblyError(("graph_not_object",))
    nodes = graph.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise AssemblyError(("graph_nodes_missing",))
    raw: dict[str, Mapping[str, Any]] = {}
    problems: list[str] = []
    for node in nodes:
        if not isinstance(node, Mapping):
            problems.append("node_not_object")
            continue
        identity = node.get("id")
        if not isinstance(identity, str) or not identity:
            problems.append("node_id_invalid")
            continue
        if identity in raw:
            problems.append(f"node_id_duplicated: {identity}")
            continue
        raw[identity] = node
    if problems:
        raise AssemblyError(tuple(sorted(set(problems))))
    return raw


def read_scene_graph(graph: Mapping[str, Any], *, dangling_parent: str = "error") -> SceneGraph:
    """校验部署图并返回不可变场景图。

    参数：``graph`` 是部署图 JSON 对象；``dangling_parent="error"`` 拒绝父级
    不存在、或子节点未登记在父级 ``children`` 里的节点；``"root"`` 只把
    「父级不在图中」这类节点提升为根，并在 ``diagnostics`` 保留原父级。
    返回：``SceneGraph``，``order`` 保证父节点先于子节点且顺序稳定。
    异常：缺少 nodes、id 缺失/重复、位置非法、朝向非法（形状/非有限/非刚性）、
    父子引用不一致、存在环、出现 ``scale``/``rotation_quaternion``/``orient``
    这类未支持的变换字段时抛出 ``AssemblyError``，并一次列出全部问题。
    """
    if dangling_parent not in ("error", "root"):
        raise AssemblyError((f"dangling_parent_policy_unknown: {dangling_parent}",))
    raw = _read_raw_nodes(graph)
    problems: list[str] = []
    children_of: dict[str, tuple[str, ...]] = {}
    declared_parent: dict[str, str | None] = {}
    for identity, node in raw.items():
        if not isinstance(node.get("class"), str) or not node.get("class"):
            problems.append(f"node_class_invalid: {identity}")
        if "parent" not in node:
            problems.append(f"node_parent_missing: {identity}")
        for name in _UNSUPPORTED_TRANSFORMS:
            if name in node:
                problems.append(f"node_transform_unsupported: {identity} {name}")
        children = node.get("children")
        if not isinstance(children, list) or any(not isinstance(child, str) for child in children):
            problems.append(f"node_children_invalid: {identity}")
            children = []
        if len(children) != len(set(children)):
            problems.append(f"node_children_duplicated: {identity}")
        children_of[identity] = tuple(children)
        declared_parent[identity] = node.get("parent")
    if problems:
        raise AssemblyError(tuple(sorted(set(problems))))

    # 父子引用必须双向一致；悬空父级按策略提升为根，悬空子级一律拒绝。
    parent_of: dict[str, str | None] = {}
    diagnostics: list[str] = []
    for identity, parent in declared_parent.items():
        if parent is None:
            parent_of[identity] = None
            continue
        if not isinstance(parent, str):
            problems.append(f"node_parent_invalid: {identity}")
            continue
        if parent not in raw:
            if dangling_parent == "root":
                diagnostics.append(f"dangling_parent_repaired: {identity} -> {parent}")
                parent_of[identity] = None
            else:
                problems.append(f"node_parent_missing_in_graph: {identity} -> {parent}")
            continue
        parent_of[identity] = parent
        if identity not in children_of[parent]:
            problems.append(f"node_missing_from_parent_children: {identity} -> {parent}")
    for identity, children in children_of.items():
        for child in children:
            if child not in raw:
                problems.append(f"node_child_missing_in_graph: {identity} -> {child}")
            elif raw[child].get("parent") != identity:
                problems.append(f"node_child_parent_mismatch: {identity} -> {child}")
    positions: dict[str, _VECTOR] = {}
    rotations: dict[str, _MATRIX3] = {}
    for identity, node in raw.items():
        position = _vector(node.get("position"), problems=problems, identity=identity)
        if position is not None:
            positions[identity] = position
        rotations[identity] = _rotation_of(node, problems=problems, identity=identity)
    if problems:
        raise AssemblyError(tuple(sorted(set(problems))))

    nodes = {
        identity: Node(
            id=identity,
            class_name=node["class"],
            kind=str(node.get("type", "")),
            parent=parent_of[identity],
            children=children_of[identity],
            position_mm=positions[identity],
            config=MappingProxyType(dict(node.get("config") or {})),
            data=MappingProxyType(dict(node.get("data") or {})),
            rotation_row_major=rotations[identity],
        )
        for identity, node in raw.items()
    }
    roots = tuple(sorted(identity for identity, parent in parent_of.items() if parent is None))
    return SceneGraph(
        nodes=MappingProxyType(nodes),
        roots=roots,
        order=_topological_order(nodes),
        diagnostics=tuple(diagnostics),
    )


def _topological_order(nodes: Mapping[str, Node]) -> tuple[str, ...]:
    """按父先于子的顺序展开节点；存在环时抛 ``AssemblyError``。"""
    order: list[str] = []
    pending = set(nodes)
    while pending:
        ready = sorted(identity for identity in pending if nodes[identity].parent not in pending)
        if not ready:
            raise AssemblyError(tuple(sorted(f"node_parent_cycle: {identity}" for identity in pending)))
        order.extend(ready)
        pending.difference_update(ready)
    return tuple(order)


def _world_frames(scene: SceneGraph) -> dict[str, tuple[_MATRIX3, _VECTOR]]:
    """父链累加，返回 ``(世界朝向, 世界位置)``；子节点局部位移按父级世界朝向旋转。"""
    frames: dict[str, tuple[_MATRIX3, _VECTOR]] = {}
    for identity in scene.order:
        node = scene.nodes[identity]
        if node.parent is None:
            parent_rotation, parent_position = IDENTITY_MATRIX, (0.0, 0.0, 0.0)
        else:
            parent_rotation, parent_position = frames[node.parent]
        frames[identity] = (
            _multiply_matrix3(node.rotation_row_major, parent_rotation),
            _add(parent_position, _rotate(node.position_mm, parent_rotation)),
        )
    return frames


def world_positions(scene: SceneGraph) -> dict[str, _VECTOR]:
    """累加父链局部位置，返回各节点未经校准的世界坐标（毫米）。

    父级带朝向时，子节点的局部位移先按父级世界朝向旋转再累加；没有朝向的
    图（旧图）结果与逐项相加完全相同。
    """
    return {identity: position for identity, (_, position) in _world_frames(scene).items()}


# --------------------------------------------------------------------------
# 类 → 资产/逻辑/缺失 绑定
# --------------------------------------------------------------------------

BINDING_KINDS = ("model", "logical", "missing")
ASSET_FRAMES = ("bbox_min", "asset_frame")


@dataclass(frozen=True)
class Asset:
    """一个 family 的 USD 入口：磁盘文件、文件内默认 prim 与资产帧口径。

    ``frame="bbox_min"``（默认，旧合同）声明资产作者把包围盒最小角放在资产原点，
    ``inspect_assets`` 因此把「最小角离原点超过容差」判为缺陷；
    ``frame="asset_frame"`` 声明资产保留 CAD/glTF 作者坐标系（原点不是包围盒最小角），
    实测偏移只作为 ``origin_offset_mm`` 证据记录。两种口径下节点的绝对补偿都用
    既有校准表 ``offset_mm``，不引入第二套偏移机制。
    """

    path: str
    prim_path: str
    frame: str = "bbox_min"

    def __post_init__(self) -> None:
        """拒绝未登记的资产帧口径。"""
        if self.frame not in ASSET_FRAMES:
            raise AssemblyError((f"asset_frame_unknown: {self.frame}",))


@dataclass(frozen=True)
class ClassBinding:
    """一个 class 的装配策略：引用资产、只保留坐标系，或声明缺失视觉。"""

    kind: str
    family: str | None = None
    asset: Asset | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        """拒绝未知装配类型与自相矛盾的字段组合。"""
        if self.kind not in BINDING_KINDS:
            raise AssemblyError((f"binding_kind_unknown: {self.kind}",))
        if self.kind == "model" and (self.family is None or self.asset is None):
            raise AssemblyError(("binding_model_incomplete",))
        if self.kind != "model" and (self.family is not None or self.asset is not None):
            raise AssemblyError((f"binding_asset_on_{self.kind}",))


def parse_bindings(
    *, family_entries: Mapping[str, Mapping[str, Any]], logical_classes: Iterable[str] = (),
    missing_classes: Mapping[str, str] | None = None,
) -> dict[str, ClassBinding]:
    """把 ``class → family 入口`` 表与逻辑/缺失清单编译成 class 绑定表。

    参数：``family_entries`` 是 ``class → {"path","prim_path","family","frame"}``；
    ``frame`` 可选，缺省 ``"bbox_min"``（见 ``Asset``）；
    ``logical_classes`` 是只保留坐标系、不含几何的 class；``missing_classes``
    是 ``class → 原因``，表示本轮没有可用资产且禁止代用。
    返回：``class → ClassBinding`` 的普通字典。
    异常：同一 class 同时出现在两类清单里，或条目字段不全时抛 ``AssemblyError``。
    """
    missing = dict(missing_classes or {})
    logical = set(logical_classes)
    bindings: dict[str, ClassBinding] = {}
    for class_name, entry in family_entries.items():
        if class_name in missing or class_name in logical:
            raise AssemblyError((f"binding_conflict: {class_name}",))
        path = entry.get("path")
        prim_path = entry.get("prim_path")
        if not isinstance(path, str) or not path or not isinstance(prim_path, str) or not prim_path:
            raise AssemblyError((f"binding_asset_incomplete: {class_name}",))
        bindings[class_name] = ClassBinding(
            kind="model", family=str(entry.get("family") or class_name),
            asset=Asset(path=path, prim_path=prim_path, frame=str(entry.get("frame") or "bbox_min")),
        )
    for class_name in logical:
        if class_name in bindings:
            raise AssemblyError((f"binding_conflict: {class_name}",))
        bindings[class_name] = ClassBinding("logical", reason="logical_only")
    for class_name, reason in missing.items():
        if class_name in bindings:
            raise AssemblyError((f"binding_conflict: {class_name}",))
        bindings[class_name] = ClassBinding("missing", reason=str(reason))
    return bindings


def unknown_classes(scene: SceneGraph, bindings: Mapping[str, ClassBinding]) -> tuple[str, ...]:
    """返回没有任何绑定的 class（装配时必须为零）。"""
    return tuple(sorted(set(scene.class_names()) - set(bindings)))


# --------------------------------------------------------------------------
# 校准
# --------------------------------------------------------------------------

CALIBRATION_SOURCES = ("bbox", "placement")


@dataclass(frozen=True)
class NodeCalibration:
    """单节点校准记录：相对全局偏移的增量、CAD 目标角、来源 prim 与锚定口径。

    ``source`` 区分两种口径：``"bbox"`` 用资产包围盒最小角对齐 CAD 组最小角；
    ``"placement"`` 用总装侧 placement/结构特征（例如机器人基座或导轨）定放置，
    这时 ``target_min_mm`` 记录的是该锚点的目标位置而不是包围盒角。
    """

    offset_mm: _VECTOR
    source_prim: str | None = None
    target_min_mm: _VECTOR | None = None
    source: str = "bbox"
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """拒绝未登记的锚定口径。"""
        if self.source not in CALIBRATION_SOURCES:
            raise AssemblyError((f"calibration_source_unknown: {self.source}",))


@dataclass(frozen=True)
class Calibration:
    """全局偏移 + 每节点偏移（毫米）；两者相加得到节点的有效偏移。"""

    global_offset_mm: _VECTOR = (0.0, 0.0, 0.0)
    nodes: Mapping[str, NodeCalibration] = field(default_factory=lambda: MappingProxyType({}))

    def effective(self, node_id: str) -> _VECTOR:
        """返回节点有效偏移 = 全局偏移 + 该节点偏移（缺省为零）。"""
        entry = self.nodes.get(node_id)
        local = entry.offset_mm if entry is not None else (0.0, 0.0, 0.0)
        return _add(self.global_offset_mm, local)


EMPTY_CALIBRATION = Calibration()


def load_calibration(data: Mapping[str, Any]) -> Calibration:
    """从 JSON 结构读校准表；缺失字段按零偏移处理，非法数值抛 ``AssemblyError``。"""
    if not isinstance(data, Mapping):
        raise AssemblyError(("calibration_not_object",))
    problems: list[str] = []
    global_offset = _vector(data.get("global_offset_mm", dict.fromkeys(AXES, 0.0)),
                            problems=problems, identity="global", field_name="global_offset")
    raw_nodes = data.get("nodes") or {}
    if not isinstance(raw_nodes, Mapping):
        raise AssemblyError(("calibration_nodes_invalid",))
    nodes: dict[str, NodeCalibration] = {}
    for node_id, entry in raw_nodes.items():
        if not isinstance(entry, Mapping):
            problems.append(f"calibration_node_invalid: {node_id}")
            continue
        offset = _vector(entry.get("offset_mm"), problems=problems, identity=node_id, field_name="offset")
        target = entry.get("target_min_mm")
        target_vector = None if target is None else _vector(
            target, problems=problems, identity=node_id, field_name="target")
        if offset is None:
            continue
        nodes[str(node_id)] = NodeCalibration(
            offset_mm=offset,
            source_prim=entry.get("source_prim"),
            target_min_mm=target_vector,
            source=str(entry.get("source") or "bbox"),
            evidence=MappingProxyType(dict(entry.get("evidence") or {})),
        )
    if problems or global_offset is None:
        raise AssemblyError(tuple(sorted(set(problems))) or ("calibration_global_invalid",))
    return Calibration(global_offset_mm=global_offset, nodes=MappingProxyType(nodes))


def dump_calibration(calibration: Calibration) -> dict[str, Any]:
    """把校准表写回可 JSON 序列化的结构（保留来源 prim 与证据块）。"""
    return {
        "global_offset_mm": dict(zip(AXES, _round(calibration.global_offset_mm))),
        "nodes": {
            node_id: {
                "offset_mm": dict(zip(AXES, _round(entry.offset_mm))),
                "source_prim": entry.source_prim,
                "target_min_mm": None if entry.target_min_mm is None else dict(zip(AXES, _round(entry.target_min_mm))),
                "source": entry.source,
                "evidence": dict(entry.evidence),
            }
            for node_id, entry in sorted(calibration.nodes.items())
        },
    }


# --------------------------------------------------------------------------
# 装配计划
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Site:
    """仓库内的一个工位；位置是相对所属仓库节点的毫米坐标。"""

    label: str
    name: str
    position_mm: _VECTOR
    size_mm: _VECTOR | None
    occupied_by: str | None


@dataclass(frozen=True)
class Placement:
    """单个节点的装配结果：prim 路径、世界位姿、偏移与绑定。"""

    node_id: str
    prim_path: str
    parent_id: str | None
    world_mm: _VECTOR
    offset_mm: _VECTOR
    binding: ClassBinding
    offset_source: str = "zero"
    site: Site | None = None
    world_rotation_row_major: _MATRIX3 = IDENTITY_MATRIX

    @property
    def geometry_path(self) -> str:
        """返回用于度量几何的 prim 路径：模型节点取 ``Model``，其余取节点自身。"""
        return f"{self.prim_path}/Model" if self.binding.kind == "model" else self.prim_path


@dataclass(frozen=True)
class AssemblyPlan:
    """装配计划：保持部署图层级，并给出每类计数、site 表与诊断。"""

    scene: SceneGraph
    root_path: str
    placements: Mapping[str, Placement]
    models: tuple[str, ...]
    logical: tuple[str, ...]
    missing: tuple[str, ...]
    sites: Mapping[str, tuple[Site, ...]] = field(default_factory=lambda: MappingProxyType({}))
    diagnostics: tuple[str, ...] = ()

    def counts(self) -> dict[str, int]:
        """返回节点/模型/逻辑/缺失计数，供报告与校验断言使用。"""
        return {
            "nodes": len(self.placements),
            "models": len(self.models),
            "logical": len(self.logical),
            "missing": len(self.missing),
        }

    def missing_reasons(self) -> dict[str, str]:
        """返回缺失视觉节点的原因，供报告逐项列出。"""
        return {identity: self.placements[identity].binding.reason for identity in self.missing}


def prim_name(node_id: str) -> str:
    """把节点 id 映射为稳定 prim 名；不安全的 id 用 UTF-8 十六进制编码避免碰撞。"""
    if _SAFE_PRIM_NAME.match(node_id):
        return node_id
    return "n_" + node_id.encode("utf-8").hex()


def _read_sites(node: Node) -> tuple[Site, ...]:
    """解析仓库节点的 ``config.sites``；非法规格跳过并交由上层统计。"""
    raw = node.config.get("sites")
    if not isinstance(raw, list):
        return ()
    sites: list[Site] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        position = entry.get("position")
        if not isinstance(position, Mapping) or set(position) != set(AXES):
            continue
        size = entry.get("size")
        size_mm = None
        if isinstance(size, Mapping) and {"width", "height", "depth"} <= set(size):
            size_mm = (float(size["width"]), float(size["height"]), float(size["depth"]))
        sites.append(Site(
            label=str(entry.get("label") or entry.get("name") or ""),
            name=str(entry.get("name") or ""),
            position_mm=(float(position["x"]), float(position["y"]), float(position["z"])),
            size_mm=size_mm,
            occupied_by=entry.get("occupied_by") if isinstance(entry.get("occupied_by"), str) else None,
        ))
    return tuple(sites)


def plan_assembly(
    scene: SceneGraph, *, class_bindings: Mapping[str, ClassBinding],
    calibration: Calibration = EMPTY_CALIBRATION, root_path: str = "/World/SZLab",
    node_bindings: Mapping[str, ClassBinding] | None = None,
) -> AssemblyPlan:
    """按绑定表与校准表生成装配计划；未知 class 一律拒绝。

    参数：``node_bindings`` 可按节点覆盖 class 级绑定（例如一个 class 对应多种
    family，或纯坐标 frame 借用静态模块的 class）；只要节点被覆盖，就不再要求
    ``class_bindings`` 里有对应 class 键，因此 class 表可以只保留无歧义的部分。
    返回：``AssemblyPlan``；``placements`` 的世界位置已含校准偏移，世界朝向
    由父链合成（节点级 ``rotation``/``rotation_matrix_row_major``）。
    异常：未被覆盖的节点其 class 未绑定、parent 未出现在计划里、根路径非法时
    抛 ``AssemblyError``。
    """
    if not root_path.startswith("/") or not root_path.strip("/"):
        raise AssemblyError((f"root_path_invalid: {root_path}",))
    overrides = dict(node_bindings or {})
    problems: list[str] = []
    for identity, node in scene.nodes.items():
        if identity not in overrides and node.class_name not in class_bindings:
            problems.append(f"class_unbound: {identity} {node.class_name}")
    if problems:
        raise AssemblyError(tuple(sorted(set(problems))))

    frames = _world_frames(scene)
    base = {identity: position for identity, (_, position) in frames.items()}
    sites = {identity: _read_sites(node) for identity, node in scene.nodes.items()}
    site_of_container: dict[str, Site] = {}
    for entries in sites.values():
        for site in entries:
            if site.occupied_by:
                site_of_container[site.occupied_by] = site

    placements: dict[str, Placement] = {}
    paths: dict[str, str] = {}
    offsets: dict[str, _VECTOR] = {}
    sources: dict[str, str] = {}
    models: list[str] = []
    logical: list[str] = []
    missing: list[str] = []
    for identity in scene.order:
        node = scene.nodes[identity]
        binding = overrides[identity] if identity in overrides else class_bindings[node.class_name]
        parent_path = paths.get(node.parent) if node.parent is not None else None
        if node.parent is not None and parent_path is None:
            problems.append(f"parent_not_planned: {identity} -> {node.parent}")
            continue
        path = f"{parent_path or root_path.rstrip('/')}/{prim_name(identity)}"
        # 校准按节点绝对偏移生效；未列出的节点继承父节点偏移，根节点回落到全局偏移。
        if identity in calibration.nodes:
            offset = calibration.effective(identity)
            source = "calibration"
        elif node.parent is not None:
            offset, source = offsets[node.parent], "inherited"
        else:
            offset, source = calibration.global_offset_mm, "global"
        offsets[identity] = offset
        sources[identity] = source
        placements[identity] = Placement(
            node_id=identity,
            prim_path=path,
            parent_id=node.parent,
            world_mm=_add(base[identity], offset),
            offset_mm=offset,
            binding=binding,
            offset_source=source,
            site=site_of_container.get(identity),
            world_rotation_row_major=frames[identity][0],
        )
        paths[identity] = path
        if binding.kind == "model":
            models.append(identity)
        elif binding.kind == "logical":
            logical.append(identity)
        else:
            missing.append(identity)
    if problems:
        raise AssemblyError(tuple(sorted(set(problems))))
    return AssemblyPlan(
        scene=scene, root_path=root_path, placements=MappingProxyType(placements),
        models=tuple(models), logical=tuple(logical), missing=tuple(missing),
        sites=MappingProxyType({identity: entries for identity, entries in sites.items() if entries}),
        diagnostics=tuple(scene.diagnostics),
    )


def site_bindings(plan: AssemblyPlan) -> dict[str, dict[str, Any]]:
    """返回容器节点到 site 的绑定核对结果，含 site 局部坐标与差值（毫米）。

    部署图把容器位置写成相对仓库节点的坐标，``occupied_by`` 是同一工位的
    反向引用；两者不一致说明站点表与实例表已漂移，报告保留差值而不是改正它。
    """
    result: dict[str, dict[str, Any]] = {}
    for identity, placement in plan.placements.items():
        site = placement.site
        if site is None:
            continue
        local = plan.scene.nodes[identity].position_mm
        result[identity] = {
            "site_label": site.label,
            "site_name": site.name,
            "site_local_mm": _round(site.position_mm),
            "node_local_mm": _round(local),
            "delta_mm": _round(_sub(local, site.position_mm)),
            "site_size_mm": None if site.size_mm is None else _round(site.size_mm),
        }
    return result


# --------------------------------------------------------------------------
# USD：写 stage / 资产检查 / 实测
# --------------------------------------------------------------------------


def _pxr():
    """延迟导入 pxr；普通解释器没有 USD 时给出可读错误。"""
    try:
        from pxr import Gf, Sdf, Usd, UsdGeom  # noqa: PLC0415 - 延迟到真正写 stage 时
    except ImportError as error:  # pragma: no cover - 取决于运行解释器
        raise AssemblyError(("pxr_unavailable",)) from error
    return Gf, Sdf, Usd, UsdGeom


def _orientation_quaternion(Gf, matrix: _MATRIX3):
    """行向量旋转矩阵 → ``Gf.Quatd``，供 ``xformOp:orient`` 使用。

    ``xformOp:orient`` 与 ``xformOp:translate`` 一起按行向量复合（``p·R·T``），
    这里按行主序把矩阵交给 ``Gf.Matrix4d`` 再取旋转四元数；stage 复现的正是
    同一个 ``v·R``，口径由 orient 用例逐项比对世界矩阵兜住。
    """
    rows = [list(matrix[0]) + [0.0], list(matrix[1]) + [0.0], list(matrix[2]) + [0.0],
            [0.0, 0.0, 0.0, 1.0]]
    return Gf.Matrix4d(*[value for row in rows for value in row]).ExtractRotationQuat()


def build_stage(plan: AssemblyPlan, *, meters_per_unit: float = 1.0, up_axis: str = "Z"):
    """把装配计划写成 USD stage；层级、局部位姿与来源标注都来自计划。

    每个节点先写 ``xformOp:translate`` 再写 ``xformOp:orient``：``xformOpOrder``
    里先列出的 op 是最外层，于是局部变换是 ``p·R_local + t_local``——绕节点自身
    原点旋转后再平移到父级。局部平移与局部朝向都是相对父节点的量，父级带朝向时
    按父级世界朝向的逆换算。没有朝向的节点不写 orient op，因此旧图产物与逐项
    平移的旧行为逐字节一致。

    参数：``meters_per_unit`` 只接受 1.0（资产与校准都是米/毫米的固定合同），
    ``up_axis`` 只接受 ``"Z"``。
    返回：``Usd.Stage``（内存 stage，调用方决定导出）。
    异常：单位或坐标轴不符时抛 ``AssemblyError``。
    """
    Gf, Sdf, Usd, UsdGeom = _pxr()
    if meters_per_unit != 1.0:
        raise AssemblyError((f"meters_per_unit_unsupported: {meters_per_unit}",))
    if up_axis != "Z":
        raise AssemblyError((f"up_axis_unsupported: {up_axis}",))
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageMetersPerUnit(stage, meters_per_unit)
    UsdGeom.SetStageUpAxis(stage, up_axis)
    root = Sdf.Path(plan.root_path)
    UsdGeom.Xform.Define(stage, root)
    stage.GetPrimAtPath(root).SetCustomDataByKey("purpose", "graph_assembly")
    stage.GetPrimAtPath(root).SetCustomDataByKey("physics_qualified", False)
    stage.SetDefaultPrim(stage.GetPrimAtPath(root.GetPrefixes()[0]))
    site_map = site_bindings(plan)
    for identity in plan.scene.order:
        placement = plan.placements[identity]
        node = plan.scene.nodes[identity]
        frame = UsdGeom.Xform.Define(stage, placement.prim_path)
        # 局部位姿 = 世界位姿相对父级世界位姿的逆；父级无朝向时退化为逐项相减。
        if placement.parent_id is None:
            parent_mm, parent_inverse = (0.0, 0.0, 0.0), IDENTITY_MATRIX
        else:
            parent = plan.placements[placement.parent_id]
            parent_mm = parent.world_mm
            parent_inverse = _transpose_matrix3(parent.world_rotation_row_major)
        local = _rotate(_sub(placement.world_mm, parent_mm), parent_inverse)
        local_rotation = _multiply_matrix3(placement.world_rotation_row_major, parent_inverse)
        # 先写 translate 再写 orient：USD 的 xformOpOrder 里先列出的 op 是最外层，
        # 于是局部变换是 p·R_local + t_local——绕节点自身原点旋转后再平移到父级。
        frame.AddTranslateOp().Set(Gf.Vec3d(*(axis / MM_PER_M for axis in local)))
        if not _is_identity_matrix(local_rotation):
            # 显式要求双精度：AddOrientOp 默认写 quatf，会在毫米级留下可见误差。
            frame.AddOrientOp(precision=UsdGeom.XformOp.PrecisionDouble).Set(
                _orientation_quaternion(Gf, local_rotation))
        prim = frame.GetPrim()
        prim.SetCustomDataByKey("source_node_id", identity)
        prim.SetCustomDataByKey("source_class", node.class_name)
        prim.SetCustomDataByKey("source_kind", node.kind)
        prim.SetCustomDataByKey("world_mm", Gf.Vec3d(*placement.world_mm))
        prim.SetCustomDataByKey("calibration_offset_mm", Gf.Vec3d(*placement.offset_mm))
        prim.SetCustomDataByKey("binding", placement.binding.kind)
        if placement.binding.kind == "model":
            model = stage.DefinePrim(Sdf.Path(placement.prim_path).AppendChild("Model"), "Xform")
            asset = placement.binding.asset
            if not model.GetReferences().AddReference(asset.path, asset.prim_path):
                raise AssemblyError((f"asset_reference_failed: {identity} {asset.path}",))
            model.SetCustomDataByKey("family_id", placement.binding.family)
            model.SetCustomDataByKey("asset_path", asset.path)
            model.SetCustomDataByKey("asset_prim", asset.prim_path)
        elif placement.binding.kind == "missing":
            prim.SetCustomDataByKey("missing_visual", placement.binding.reason)
        else:
            prim.SetCustomDataByKey("logical_only", True)
        if identity in site_map:
            prim.SetCustomDataByKey("site", json.dumps(site_map[identity], ensure_ascii=False, sort_keys=True))
        if identity in plan.sites:
            payload = [
                {"label": site.label, "name": site.name, "position_mm": _round(site.position_mm),
                 "size_mm": None if site.size_mm is None else _round(site.size_mm),
                 "occupied_by": site.occupied_by}
                for site in plan.sites[identity]
            ]
            prim.SetCustomDataByKey("sites", json.dumps(payload, ensure_ascii=False, sort_keys=True))
            prim.SetCustomDataByKey("sites_frame", "node_local_mm")
    stage.GetPrimAtPath(root).SetCustomDataByKey("node_count", len(plan.placements))
    stage.GetPrimAtPath(root).SetCustomDataByKey("model_count", len(plan.models))
    stage.GetPrimAtPath(root).SetCustomDataByKey("missing_visual_count", len(plan.missing))
    return stage


def inspect_assets(assets: Mapping[str, Asset], *, origin_tolerance_mm: float = _ORIGIN_TOLERANCE_MM) -> dict[str, dict[str, Any]]:
    """检查 family 资产的入口、单位、坐标轴与资产帧口径。

    返回：``family → {ok, prim_path, bbox_min_mm, bbox_max_mm, size_mm, origin_mm,
    origin_offset_mm, asset_frame, problems}``；``origin_offset_mm`` 是实测包围盒
    最小角（即资产原点相对几何的偏移）。``frame="bbox_min"``（默认）声明最小角
    就在资产原点，超过 ``origin_tolerance_mm`` 记为 ``asset_origin_offset_mm``
    问题（资产原点不在最小角）；``frame="asset_frame"`` 声明资产保留作者坐标系，
    实测偏移只作为证据，不判为问题。
    """
    _, Sdf, Usd, UsdGeom = _pxr()
    bbox_cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), ["default"])
    report: dict[str, dict[str, Any]] = {}
    for family, asset in sorted(assets.items()):
        problems: list[str] = []
        entry: dict[str, Any] = {"path": asset.path, "declared_prim": asset.prim_path,
                                 "ok": False, "asset_frame": asset.frame, "problems": problems}
        report[family] = entry
        stage = Usd.Stage.Open(asset.path)
        if stage is None:
            problems.append("asset_open_failed")
            continue
        default = stage.GetDefaultPrim()
        if not default:
            problems.append("asset_default_prim_missing")
            continue
        entry["prim_path"] = default.GetPath().pathString
        if asset.prim_path and asset.prim_path != default.GetPath().pathString:
            problems.append(f"asset_prim_mismatch: {asset.prim_path}")
        if not default.IsA(UsdGeom.Xform):
            problems.append("asset_default_prim_not_xform")
        entry["meters_per_unit"] = UsdGeom.GetStageMetersPerUnit(stage)
        entry["up_axis"] = UsdGeom.GetStageUpAxis(stage)
        if UsdGeom.GetStageMetersPerUnit(stage) != 1.0:
            problems.append(f"asset_meters_per_unit: {entry['meters_per_unit']}")
        if UsdGeom.GetStageUpAxis(stage) != "Z":
            problems.append(f"asset_up_axis: {entry['up_axis']}")
        box = bbox_cache.ComputeWorldBound(default).ComputeAlignedRange()
        if box.IsEmpty():
            problems.append("asset_bbox_empty")
            continue
        low, high = box.GetMin(), box.GetMax()
        size = (high - low)
        entry["bbox_min_mm"] = _round((low[0] * MM_PER_M, low[1] * MM_PER_M, low[2] * MM_PER_M))
        entry["bbox_max_mm"] = _round((high[0] * MM_PER_M, high[1] * MM_PER_M, high[2] * MM_PER_M))
        entry["size_mm"] = _round((size[0] * MM_PER_M, size[1] * MM_PER_M, size[2] * MM_PER_M))
        entry["origin_mm"] = entry["bbox_min_mm"]
        entry["origin_offset_mm"] = entry["bbox_min_mm"]
        if asset.frame == "bbox_min" and _maximum(entry["bbox_min_mm"]) > origin_tolerance_mm:
            problems.append(f"asset_origin_offset_mm: {entry['bbox_min_mm']}")
        entry["ok"] = not problems
    return report


def measure_nodes(stage, plan: AssemblyPlan) -> dict[str, dict[str, Any]]:
    """实测 stage 中每个节点的世界包围盒（毫米），用于校准残差。

    模型节点量的是 ``<节点>/Model`` 的资产几何，避免同节点下的工位实例把包围盒
    撑大；逻辑/缺失节点自身没有几何，只记录存在性——它们的子节点（例如仓库里的
    容器实例）不是这个节点的几何，不能被并进来当成本节点的包围盒。
    """
    _, Sdf, Usd, UsdGeom = _pxr()
    bbox_cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), ["default"])
    result: dict[str, dict[str, Any]] = {}
    for identity, placement in plan.placements.items():
        prim = stage.GetPrimAtPath(Sdf.Path(placement.geometry_path))
        if not prim:
            result[identity] = {"present": False}
            continue
        if placement.binding.kind != "model":
            # 逻辑/缺失节点没有自身几何；子节点实例不属于它的包围盒。
            result[identity] = {"present": True, "own_geometry": False}
            continue
        box = bbox_cache.ComputeWorldBound(prim).ComputeAlignedRange()
        if box.IsEmpty():
            result[identity] = {"present": True, "bbox_empty": True}
            continue
        low, high = box.GetMin(), box.GetMax()
        result[identity] = {
            "present": True,
            "geometry_path": placement.geometry_path,
            "bbox_min_mm": _round((low[0] * MM_PER_M, low[1] * MM_PER_M, low[2] * MM_PER_M)),
            "bbox_max_mm": _round((high[0] * MM_PER_M, high[1] * MM_PER_M, high[2] * MM_PER_M)),
            "size_mm": _round(((high[0] - low[0]) * MM_PER_M, (high[1] - low[1]) * MM_PER_M,
                               (high[2] - low[2]) * MM_PER_M)),
        }
    return result


def calibration_report(
    plan: AssemblyPlan, measured: Mapping[str, Mapping[str, Any]],
    calibration: Calibration = EMPTY_CALIBRATION, *, tolerance_mm: float = _ORIGIN_TOLERANCE_MM,
) -> list[dict[str, Any]]:
    """逐节点比较实测包围盒最小角与校准目标角，返回残差与判决。

    残差 = 实测最小角 − 目标最小角；目标来自校准表的 ``target_min_mm``。
    没有目标（未参与校准的节点）时残差为 null，判决为 ``uncalibrated``。
    判决 ``ok`` 表示残差三轴都不超过 ``tolerance_mm``。
    """
    report: list[dict[str, Any]] = []
    for identity in plan.scene.order:
        placement = plan.placements[identity]
        entry = calibration.nodes.get(identity)
        measured_entry = measured.get(identity, {})
        row: dict[str, Any] = {
            "node": identity,
            "binding": placement.binding.kind,
            "family": placement.binding.family,
            "offset_source": placement.offset_source,
            "world_mm": _round(placement.world_mm),
            "offset_mm": _round(placement.offset_mm),
            "source": "none" if entry is None else entry.source,
            "source_prim": None if entry is None else entry.source_prim,
            "target_min_mm": None if entry is None or entry.target_min_mm is None else _round(entry.target_min_mm),
            "measured_min_mm": measured_entry.get("bbox_min_mm"),
            "measured_size_mm": measured_entry.get("size_mm"),
        }
        target = None if entry is None else entry.target_min_mm
        measured_min = measured_entry.get("bbox_min_mm")
        if target is None or measured_min is None:
            row["residual_mm"] = None
            row["verdict"] = "uncalibrated"
        elif entry is not None and entry.source != "bbox":
            # placement 口径的目标点是锚点（如机器人导轨角），不是整个资产的包围盒角；
            # 这里只保留实测值，锚点残差由调用方按锚点 prim 另算，不套 bbox 的 20 mm 判决。
            row["residual_mm"] = None
            row["verdict"] = "placement_anchored"
        else:
            residual = tuple(measured_min[axis] - target[axis] for axis in range(3))
            row["residual_mm"] = _round(residual)
            row["residual_max_mm"] = round(_maximum(residual), 3)
            row["verdict"] = "ok" if _maximum(residual) <= tolerance_mm else "residual_exceeds_tolerance"
        report.append(row)
    return report




def inspection_asset(source, *, label="asset") -> Asset:
    """Validate a saved, metre/Z-up, disabled-physics source before read-only composition."""
    _, _, Usd, UsdGeom = _pxr()
    default = source.GetDefaultPrim()
    if not default or not default.IsA(UsdGeom.Xform):
        raise ValueError("Inspection asset requires an Xform default prim: " + label)
    if UsdGeom.GetStageMetersPerUnit(source) != 1 or UsdGeom.GetStageUpAxis(source) != "Z":
        raise ValueError("Inspection asset units/up axis differ: " + label)
    layer = source.GetRootLayer()
    if not layer.realPath:
        raise ValueError("Inspection asset must be a reopenable file: " + label)
    for used in source.GetUsedLayers():
        if (used.anonymous and not used.empty) or (not used.anonymous and used.dirty):
            raise ValueError("Inspection asset has unsaved or anonymous edits: " + label)
    for prim in Usd.PrimRange(default):
        for name in ("physics:collisionEnabled", "physics:rigidBodyEnabled", "physics:jointEnabled"):
            attr = prim.GetAttribute(name)
            if attr and (attr.Get() is not False or attr.GetNumTimeSamples()):
                raise ValueError("Inspection asset has enabled/animated physics: " + label)
    return Asset(layer.realPath, str(default.GetPath()), frame="asset_frame")
