"""沿 OS 资源树把逐级相对位置串起来，给出资源（板、孔、吸头位…）在台面坐标系里的点。

资产行为类不知道台面上摆了什么；它通过物料视图的 ``materials.point(资源名, 锚点)`` 向这里要点，
再用自己的台面基准和工具点把关节送过去。这里只读节点的 ``parent``、``position``（毫米，相对父节点）和
``config``（尺寸、类型），与 PLR 资源树的含义一致；输出一律为米。链上有非零旋转时拒绝，不做近似。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .mechanism import MechanismError

ANCHORS = ("origin", "top_center", "bottom_center", "center")


def _tip_length(tip: Any) -> float:
    """吸头实物全长（毫米）。

    优先取 ``physical_length_mm``：有的厂家定义（例如官方 PLR 1.0 的 Tecan 吸头）里 ``size_z`` 是固件参数而不是实物长度，
    补全层会另挂这个属性。没有时，旧版 PLR 取 ``total_tip_length``，新版取吸头资源的 ``size_z``。"""
    physical = getattr(tip, "physical_length_mm", None)
    if physical is not None:
        return float(physical)
    length = getattr(tip, "total_tip_length", None)
    return float(tip.get_size_z() if length is None else length)


class ResourcePoints:
    """一张资源图的只读几何查询；台面坐标系取链上 ``type == "deck"`` 的节点（没有则取最顶层的非设备节点）。"""

    def __init__(self, nodes: list[Mapping[str, Any]]):
        self.nodes = {str(node["id"]): node for node in nodes}
        names: dict[str, list[str]] = {}
        for identifier, node in self.nodes.items():
            names.setdefault(str(node.get("name") or identifier), []).append(identifier)
        self._names = {name: ids[0] for name, ids in names.items() if len(ids) == 1}

    @classmethod
    def from_graph(cls, source: str | Path | Mapping[str, Any]) -> "ResourcePoints":
        graph = json.loads(Path(source).read_text(encoding="utf8")) if isinstance(source, (str, Path)) else source
        return cls(list(graph["nodes"]))

    def _node(self, name: str) -> Mapping[str, Any]:
        identifier = name if name in self.nodes else self._names.get(name)
        if identifier is None:
            raise MechanismError("resource_unknown", name)
        return self.nodes[identifier]

    @staticmethod
    def _position(node: Mapping[str, Any]) -> tuple[float, float, float]:
        position = node.get("position") or {}
        if "position" in position:  # 兼容 {"position": {x, y, z}} 的写法
            position = position["position"]
        return tuple(float(position.get(axis, 0.0) or 0.0) for axis in "xyz")

    def _origin_mm(self, name: str) -> tuple[float, float, float]:
        node, total, seen = self._node(name), [0.0, 0.0, 0.0], set()
        while True:
            identifier = str(node["id"])
            if identifier in seen:
                raise MechanismError("resource_tree_cycle", name)
            seen.add(identifier)
            rotation = (node.get("config") or {}).get("rotation") or {}
            if any(abs(float(rotation.get(axis, 0) or 0)) > 1e-9 for axis in "xyz"):
                raise MechanismError("resource_rotation_unsupported", f"{identifier} 带旋转，暂不支持")
            offset = self._position(node)
            total = [total[i] + offset[i] for i in range(3)]
            if node.get("type") == "deck":
                return tuple(total)
            parent = self.nodes.get(str(node.get("parent"))) if node.get("parent") is not None else None
            if parent is None or parent.get("type") == "device":
                return tuple(total)
            node = parent

    def attribute(self, name: str, attribute: str) -> Any:
        """资源的通用几何属性（米）：size_x / size_y / size_z、material_z_thickness、tip_length、fitting_depth。"""
        config = self._node(name).get("config") or {}
        if attribute in ("tip_length", "fitting_depth"):
            tip = config.get("prototype_tip") or (self._node(name).get("data") or {}).get("tip") or {}
            key = "total_tip_length" if attribute == "tip_length" else "fitting_depth"
            if tip.get(key) is None:
                raise MechanismError("material_geometry_undeclared", f"{name} 没有声明 {attribute}")
            return float(tip[key]) / 1000.0
        if attribute in ("size_x", "size_y", "size_z", "material_z_thickness"):
            if config.get(attribute) is None:
                raise MechanismError("material_geometry_undeclared", f"{name} 没有声明 {attribute}")
            return float(config[attribute]) / 1000.0
        raise MechanismError("material_attribute_unknown", attribute)

    def point(self, name: str, anchor: str = "top_center") -> tuple[float, float, float]:
        """资源上一个锚点在台面坐标系里的位置（米）。"""
        if anchor not in ANCHORS:
            raise MechanismError("anchor_unknown", f"{anchor}（可用：{ANCHORS}）")
        x, y, z = (value / 1000.0 for value in self._origin_mm(name))
        if anchor == "origin":
            return (x, y, z)
        x, y = x + self.attribute(name, "size_x") / 2, y + self.attribute(name, "size_y") / 2
        if anchor == "top_center":
            return (x, y, z + self.attribute(name, "size_z"))
        if anchor == "center":
            return (x, y, z + self.attribute(name, "size_z") / 2)
        thickness = (self._node(name).get("config") or {}).get("material_z_thickness") or 0.0
        return (x, y, z + float(thickness) / 1000.0)


class PlrResourcePoints:
    """同一套查询，数据来自一棵运行中的 PLR 资源树（例如适配器搭好的台面，或由资源类展开的孔板）。

    位置直接用 PLR 的绝对坐标（相对传入的根资源），所以和真机驱动算点用的是同一份数据；
    树变了（板被搬走、吸头被取走）查询结果跟着变。只读，不改资源树。
    """

    def __init__(self, root: Any):
        self.root = root

    def _resource(self, name: str) -> Any:
        try:
            return self.root if getattr(self.root, "name", None) == name else self.root.get_resource(name)
        except Exception as error:  # noqa: BLE001 - PLR 用自己的异常类型报找不到
            raise MechanismError("resource_unknown", name) from error

    def attribute(self, name: str, attribute: str) -> Any:
        resource = self._resource(name)
        if attribute in ("tip_length", "fitting_depth"):
            make_tip = getattr(resource, "make_tip", None)
            tip = make_tip() if callable(make_tip) else None
            if tip is None:
                raise MechanismError("material_geometry_undeclared", f"{name} 没有声明 {attribute}")
            return float(_tip_length(tip) if attribute == "tip_length" else tip.fitting_depth) / 1000.0
        if attribute in ("size_x", "size_y", "size_z"):
            return float(getattr(resource, "get_" + attribute)()) / 1000.0
        if attribute == "material_z_thickness":
            value = getattr(resource, "material_z_thickness", None)
            if value is None:
                raise MechanismError("material_geometry_undeclared", f"{name} 没有声明 {attribute}")
            return float(value) / 1000.0
        raise MechanismError("material_attribute_unknown", attribute)

    def point(self, name: str, anchor: str = "top_center") -> tuple[float, float, float]:
        if anchor not in ANCHORS:
            raise MechanismError("anchor_unknown", f"{anchor}（可用：{ANCHORS}）")
        resource = self._resource(name)
        total, node = [0.0, 0.0, 0.0], resource
        while node is not self.root:  # 逐级相对位置相加，到根资源为止（根自己的位置不计）
            if node is None:
                raise MechanismError("resource_unknown", f"{name} 不在这棵资源树里")
            rotation = getattr(node, "rotation", None)
            if rotation is not None and any(abs(float(getattr(rotation, axis, 0) or 0)) > 1e-9 for axis in "xyz"):
                raise MechanismError("resource_rotation_unsupported", f"{node.name} 带旋转，暂不支持")
            if node.location is None:
                raise MechanismError("resource_location_missing", node.name)
            total = [total[i] + getattr(node.location, axis) for i, axis in enumerate("xyz")]
            node = node.parent
        x, y, z = (value / 1000.0 for value in total)
        if anchor == "origin":
            return (x, y, z)
        x, y = x + resource.get_size_x() / 2000.0, y + resource.get_size_y() / 2000.0
        if anchor == "top_center":
            return (x, y, z + resource.get_size_z() / 1000.0)
        if anchor == "center":
            return (x, y, z + resource.get_size_z() / 2000.0)
        return (x, y, z + float(getattr(resource, "material_z_thickness", None) or 0.0) / 1000.0)


class SceneResourcePoints:
    """同一套查询，数据来自 OS 场景图里的物料节点和耗材资产自带的库位（孔位、吸头位）。

    资源名写成 ``节点/库位``（例如 ``plate_1/A1``、``tip_box_1/tip_00``）。节点位姿取图里的值
    （``pose.extra.world_position_mm`` 优先，否则逐级相对位置；只支持绕 Z 的旋转），库位取该节点资产
    ``unilab.json`` 的 ``interfaces_data.sites``（资产坐标系，毫米）。输出在 ``device`` 节点的坐标系里（米），
    再减去 ``frame_origin_m``，这样行为类用自己的台面基准库位换算后正好回到资产坐标系。

    库位上的可选几何（都要资产自己声明，没有就拒绝）：``depth_mm`` 孔口到孔底；
    ``tip_length_mm`` / ``fitting_depth_mm`` 吸头全长与配合深度；``top_offset_mm`` 吸头顶面高出库位点的距离。
    """

    def __init__(self, nodes: list[Mapping[str, Any]], device: str, packages: Mapping[str, str | Path] | None = None,
                 frame_origin_m: tuple[float, float, float] = (0.0, 0.0, 0.0),
                 definitions: Mapping[str, str] | None = None):
        self.nodes = {str(node["id"]): node for node in nodes}
        if device not in self.nodes:
            raise MechanismError("resource_unknown", device)
        self.device, self.frame_origin = device, tuple(float(v) for v in frame_origin_m)
        self._packages = {str(key): Path(value) for key, value in (packages or {}).items()}
        # 资产 ID → 物料定义（``模块:工厂函数``，PLR + @resource）；有定义的资产，孔位和吸头位从物料类按需展开。
        self._definitions = {str(key): str(value) for key, value in (definitions or {}).items()}
        self._sites: dict[str, dict[str, Mapping[str, Any]]] = {}

    @classmethod
    def from_graph(cls, source: str | Path | Mapping[str, Any], device: str,
                   packages: Mapping[str, str | Path] | None = None,
                   frame_origin_m: tuple[float, float, float] = (0.0, 0.0, 0.0),
                   definitions: Mapping[str, str] | None = None) -> "SceneResourcePoints":
        graph = json.loads(Path(source).read_text(encoding="utf8")) if isinstance(source, (str, Path)) else source
        return cls(list(graph["nodes"]), device, packages, frame_origin_m, definitions)

    @staticmethod
    def _from_definition(reference: str) -> dict[str, Mapping[str, Any]]:
        """把物料定义展开一次（带全部孔或吸头位），换成资产坐标系里的库位表。展开的实例只用来算点，不进资源树。"""
        import importlib

        module_name, _, factory = reference.partition(":")
        module = importlib.import_module(module_name)
        instance = getattr(module, factory)("definition", with_items=True)
        origin = getattr(module, "ASSET_ORIGIN_MM", (0.0, 0.0, 0.0))
        points, sites = PlrResourcePoints(instance), {}
        for child in instance.children:
            label = child.name[len(instance.name) + 1:] if child.name.startswith(instance.name + "_") else child.name
            top = points.point(child.name, "top_center")
            site: dict[str, Any] = {"normal": [0, 0, 1], "position_mm": [top[i] * 1000.0 - origin[i] for i in range(3)]}
            if child.get_size_z() > 0:
                site["depth_mm"] = float(child.get_size_z())
            make_tip = getattr(child, "make_tip", None)
            if callable(make_tip):
                tip = make_tip()
                site.update(tip_length_mm=_tip_length(tip), fitting_depth_mm=float(tip.fitting_depth))
            sites[label] = site
        return sites

    def _pose(self, identifier: str) -> tuple[tuple[float, float, float], float]:
        """节点在场景里的位置（毫米）和绕 Z 的朝向（度）。"""
        import math

        chain, node, seen = [], self.nodes.get(identifier), set()
        while node is not None:
            if str(node["id"]) in seen:
                raise MechanismError("resource_tree_cycle", identifier)
            seen.add(str(node["id"]))
            chain.append(node)
            node = self.nodes.get(str(node.get("parent"))) if node.get("parent") is not None else None
        position, yaw = [0.0, 0.0, 0.0], 0.0
        for item in reversed(chain):  # 从根往下：先放父节点，再放子节点
            pose = item.get("pose") or {}
            rotation = pose.get("rotation") or {}
            if any(abs(float(rotation.get(axis, 0) or 0)) > 1e-6 for axis in "xy"):
                raise MechanismError("resource_rotation_unsupported", f"{item['id']} 带绕 X/Y 的旋转，暂不支持")
            world = (pose.get("extra") or {}).get("world_position_mm")
            if world is not None:
                position = [float(world[axis]) for axis in "xyz"]
            else:
                local = pose.get("position") or item.get("position") or {}
                turn = 0.0
                site = self._occupied_site(item)
                if site is not None:
                    # 放在库位里的物料：库位的位姿就是物料原点的位姿（位置和绕 Z 的朝向），物料自己的位姿是零。
                    local = site.get("position") or {}
                    turn = float((site.get("rotation") or {}).get("z", 0) or 0)
                c, s = math.cos(math.radians(yaw)), math.sin(math.radians(yaw))
                x, y, z = (float(local.get(axis, 0) or 0) for axis in "xyz")
                position = [position[0] + c * x - s * y, position[1] + s * x + c * y, position[2] + z]
                yaw += turn
            yaw += float(rotation.get("z", 0) or 0)
        return (position[0], position[1], position[2]), yaw

    def _occupied_site(self, item: Mapping[str, Any]) -> Mapping[str, Any] | None:
        """物料所在的库位（父节点 ``config.sites`` 里与 ``data.initial_site`` 同名的那个）；不在库位里时是 None。"""
        name = (item.get("data") or {}).get("initial_site")
        parent = self.nodes.get(str(item.get("parent"))) if item.get("parent") is not None else None
        if not name or parent is None:
            return None
        return next((site for site in (parent.get("config") or {}).get("sites") or [] if site.get("name") == name), None)

    def _site(self, name: str) -> tuple[str, Mapping[str, Any]]:
        identifier, separator, site = name.partition("/")
        if not separator or identifier not in self.nodes:
            raise MechanismError("resource_unknown", f"{name}（写成 节点/库位）")
        asset = (((self.nodes[identifier].get("data") or {}).get("scene") or {}).get("asset_id"))
        if asset is not None and str(asset) in self._definitions and asset not in self._sites:
            self._sites[asset] = self._from_definition(self._definitions[str(asset)])
        if asset is None or (str(asset) not in self._packages and asset not in self._sites):
            raise MechanismError("resource_asset_unavailable", f"{identifier} 的资产 {asset} 没有物料定义，也没有提供资产包")
        if asset not in self._sites:
            adapter = json.loads((self._packages[str(asset)] / "simready_usd/unilab.json").read_text(encoding="utf8"))
            interfaces = adapter.get("interfaces_data") or {}
            if interfaces.get("units", "mm") != "mm":
                raise MechanismError("resource_units_unsupported", f"{asset} 的库位单位不是毫米")
            self._sites[asset] = {str(item["name"]): item for item in interfaces.get("sites") or [] if "position_mm" in item}
        if site not in self._sites[asset]:
            raise MechanismError("resource_unknown", f"{identifier} 的资产没有库位 {site}")
        return identifier, self._sites[asset][site]

    @staticmethod
    def _declared(name: str, site: Mapping[str, Any], key: str) -> float:
        if site.get(key) is None:
            raise MechanismError("material_geometry_undeclared", f"{name} 的资产库位没有声明 {key}")
        return float(site[key])

    def attribute(self, name: str, attribute: str) -> Any:
        _, site = self._site(name)
        keys = {"tip_length": "tip_length_mm", "fitting_depth": "fitting_depth_mm", "size_z": "depth_mm"}
        if attribute not in keys:
            raise MechanismError("material_attribute_unknown", attribute)
        return self._declared(name, site, keys[attribute]) / 1000.0

    def point(self, name: str, anchor: str = "top_center") -> tuple[float, float, float]:
        import math

        if anchor not in ANCHORS:
            raise MechanismError("anchor_unknown", f"{anchor}（可用：{ANCHORS}）")
        identifier, site = self._site(name)
        normal = site.get("normal") or [0, 0, 1]
        if any(abs(float(a) - b) > 1e-9 for a, b in zip(normal, (0, 0, 1))):
            raise MechanismError("resource_rotation_unsupported", f"{name} 的库位法向不是 +Z，暂不支持")
        x, y, z = (float(v) for v in site["position_mm"])
        if anchor == "top_center":
            z += float(site.get("top_offset_mm") or 0.0)
        elif anchor == "bottom_center":
            z -= self._declared(name, site, "depth_mm")
        elif anchor == "center":
            z -= self._declared(name, site, "depth_mm") / 2
        (px, py, pz), yaw = self._pose(identifier)
        c, s = math.cos(math.radians(yaw)), math.sin(math.radians(yaw))
        world = (px + c * x - s * y, py + s * x + c * y, pz + z)
        (dx, dy, dz), device_yaw = self._pose(self.device)
        c, s = math.cos(math.radians(-device_yaw)), math.sin(math.radians(-device_yaw))
        rx, ry, rz = world[0] - dx, world[1] - dy, world[2] - dz
        local = (c * rx - s * ry, s * rx + c * ry, rz)
        return tuple(local[i] / 1000.0 - self.frame_origin[i] for i in range(3))


def attach_scene(mechanism: Any, spec: Mapping[str, Any], default_device: str | None = None) -> SceneResourcePoints:
    """按场景说明给机构接上点位查询：``{"scene": 图.json, "device": 节点, "frame_site": 台面基准库位, "definitions": {资产 ID: "模块:工厂函数"}}``。

    孔位、吸头位优先从物料定义（PLR + @resource）按需展开；``packages`` 是没有物料定义时直接读资产包库位的退路。

    行为类算点时会加上自己的台面基准库位；这里先减掉它，所以要等机构建好、读到库位位置之后再接。"""
    origin = mechanism.site_position(spec["frame_site"]) if spec.get("frame_site") else (0.0, 0.0, 0.0)
    device = spec.get("device", default_device)
    if device is None:
        raise ValueError("场景说明缺少 device")
    mechanism.resources = SceneResourcePoints.from_graph(Path(spec["scene"]), device, spec.get("packages") or {}, origin,
                                                         spec.get("definitions") or {})
    return mechanism.resources
