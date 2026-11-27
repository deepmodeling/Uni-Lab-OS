"""出片场景说明：哪些节点、各用哪个 USD、世界位姿、关节属性 ↔ USD 关节名。

从运行中的 OS 读（物料图里的 ``rendering.model`` 与位姿、观测快照里的 ``descriptor.joints``），
定格成 JSON，与冻结时间线一起交给引擎适配器；适配器只按它加载，不再访问 OS。
模型路径映射到本机包目录：``/api/v1/material-models/<package>/<相对路径>`` → ``<package_root>/<相对路径>``。
"""

from __future__ import annotations

import json
import math
import urllib.request
from pathlib import Path
from typing import Any

SCHEMA = "unilab.render-scene/1"
_MODEL_PREFIX = "/api/v1/material-models/"


def _get(base: str, path: str) -> Any:
    with urllib.request.urlopen(base.rstrip("/") + path, timeout=120) as response:
        body = json.loads(response.read().decode("utf-8"))
    return body.get("data", body)


def _matrix(position: list[float], rotation_deg: list[float]) -> list[list[float]]:
    """mm + XYZ 欧拉（R = Rx·Ry·Rz，与图生成器一致）。"""
    x, y, z = (math.radians(value) for value in rotation_deg)
    cx, sx, cy, sy, cz, sz = math.cos(x), math.sin(x), math.cos(y), math.sin(y), math.cos(z), math.sin(z)
    rotation = [[cy * cz, -cy * sz, sy],
                [sx * sy * cz + cx * sz, -sx * sy * sz + cx * cz, -sx * cy],
                [-cx * sy * cz + sx * sz, cx * sy * sz + sx * cz, cx * cy]]
    return [rotation[i] + [position[i]] for i in range(3)] + [[0.0, 0.0, 0.0, 1.0]]


def _multiply(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def build_scene(graph: dict[str, Any], snapshot: dict[str, Any], *, package_roots: dict[str, str],
                nodes: set[str] | None = None) -> dict[str, Any]:
    """``graph`` 是统一图（节点 pose 相对父节点，mm）；``package_roots`` 包名 → 本机目录。"""
    by_id = {node["id"]: node for node in graph["nodes"]}
    joints = {item["id"]: (item.get("descriptor") or {}).get("joints") or {}
              for item in snapshot.get("nodes") or [] if item.get("id")}

    def world(node_id: str) -> list[list[float]]:
        chain, current = [], by_id.get(node_id)
        while current is not None:
            pose = current.get("pose") or {}
            chain.append(_matrix([float((pose.get("position") or {}).get(a, 0.0)) for a in "xyz"],
                                 [float((pose.get("rotation") or {}).get(a, 0.0)) for a in "xyz"]))
            current = by_id.get(current.get("parent")) if current.get("parent") else None
        result = chain.pop()
        while chain:
            result = _multiply(result, chain.pop())
        return result

    items = []
    for node in graph["nodes"]:
        if nodes is not None and node["id"] not in nodes:
            continue
        model = ((node.get("config") or {}).get("rendering") or {}).get("model") or node.get("model") or {}
        path = model.get("path") if isinstance(model, dict) else None
        if not path or not str(path).lower().split("?")[0].endswith((".usd", ".usda", ".usdc", ".usdz")):
            continue
        relative = str(path).split(_MODEL_PREFIX, 1)[-1]
        package, _, rest = relative.partition("/")
        root = package_roots.get(package)
        if root is None:
            continue
        items.append({"node": node["id"], "usd": str(Path(root) / urllib.request.url2pathname(rest)),
                      "world_mm": world(node["id"]), "joints": joints.get(node["id"], {})})
    return {"schema": SCHEMA, "units": "mm", "up_axis": "Z", "nodes": items}


def scene_from_backend(backend: str, graph_path: Path, *, package_roots: dict[str, str],
                       nodes: set[str] | None = None) -> dict[str, Any]:
    """统一图来自文件（位姿与工作区图一致），模型与关节从运行中的 OS 读。"""
    graph = json.loads(Path(graph_path).read_text(encoding="utf-8"))
    materials = _get(backend, "/materials/graph")
    rendering = {}
    for item in materials.get("nodes") or []:
        material = item.get("material") or item
        node_id = (material.get("meta_data") or {}).get("source_node_id")
        model = ((material.get("config") or {}).get("rendering") or {}).get("model")
        if node_id and model:
            rendering[node_id] = model
    for node in graph["nodes"]:
        if node["id"] in rendering:
            node.setdefault("config", {}).setdefault("rendering", {})["model"] = rendering[node["id"]]
    snapshot = _get(backend, "/device-state/observations/snapshot")
    return build_scene(graph, snapshot, package_roots=package_roots, nodes=nodes)
