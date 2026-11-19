"""驱动包自带的设备图：发现与一键启动。

示例设备包（``LabDevice*Demo``）把随包图作为 setuptools data-files 装到
``share/<包名>/graph/*.json``（LAN demo 是 ``examples``）；源码 / editable 安装时图
就在仓库根目录的同名子目录。这里把它们枚举给前端，并能直接以受管设备进程
（本机 Slave 子进程，见 :mod:`~unilabos.server.services.device_processes`）拉起来：
纯设备图（demo 图都是）不需要 Host 重启就能运行；包里的 ``@workflow`` 模板仍要
等 Host 重启（驱动包台账 ``restart_required``）后才会上报。

本模块只读驱动包台账、只用设备进程服务的公开方法，是两者之间的桥。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from unilabos.server.services.device_processes import DeviceProcessService
from unilabos.server.services.driver_packages import (
    DriverPackageError,
    DriverPackageRecord,
    load_ledger,
)
from unilabos.utils import logger

#: 包内 / 仓库内约定存放设备图的目录名。
GRAPH_DIR_NAMES = ("graph", "graphs", "examples")


def _normalize(name: str) -> str:
    return name.strip().lower().replace("-", "_")


def _distribution_json_files(dist_name: str) -> List[Path]:
    """已安装分发 RECORD 里位于 ``share/`` 或图目录下的 JSON 文件（data-files 安装形态）。"""

    from importlib.metadata import PackageNotFoundError, distribution

    try:
        dist = distribution(dist_name)
    except PackageNotFoundError:
        return []
    found: List[Path] = []
    for entry in dist.files or []:
        if entry.suffix.lower() != ".json":
            continue
        parts = set(entry.parts)
        if "share" not in parts and not parts.intersection(GRAPH_DIR_NAMES):
            continue
        try:
            path = Path(str(dist.locate_file(entry))).resolve()
        except (OSError, ValueError):
            continue
        if path.is_file():
            found.append(path)
    return found


def _source_json_files(package_dirs: List[str], package_root: str = "") -> List[Path]:
    """优先使用登记的源码根，兼容 src 布局；旧台账才由包目录推断根。"""

    found: List[Path] = []
    roots = [Path(package_root).resolve()] if package_root else list(dict.fromkeys(
        Path(item).resolve().parent for item in package_dirs
    ))
    for root in roots:
        for dirname in GRAPH_DIR_NAMES:
            directory = root / dirname
            if directory.is_dir():
                found.extend(sorted(path for path in directory.glob("*.json") if path.is_file()))
    return found


def _load_graph(path: Path) -> Optional[Dict[str, Any]]:
    """只接受 node-link 设备图：``nodes`` 里每个节点都有 id 与 class / template_name。"""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    nodes = payload.get("nodes") if isinstance(payload, dict) else None
    if not isinstance(nodes, list) or not nodes:
        return None
    for node in nodes:
        if not isinstance(node, dict) or not node.get("id"):
            return None
        if not (node.get("class") or node.get("template_name")):
            return None
    return payload


def _graph_entry(name: str, path: Path, payload: Dict[str, Any], source: str) -> Dict[str, Any]:
    nodes = payload["nodes"]
    return {
        "name": name,
        "path": str(path),
        "source": source,
        "device_only": all(str(node.get("type") or "device") == "device" for node in nodes),
        "devices": [
            {"id": str(node["id"]), "class": str(node.get("class") or node.get("template_name") or "")}
            for node in nodes
        ],
    }


def package_record(working_dir: str | Path, name: str) -> DriverPackageRecord:
    record = load_ledger(working_dir).get(_normalize(name))
    if record is None:
        raise DriverPackageError(f"driver package not found: {name}")
    return record


def list_bundled_graphs(working_dir: str | Path, name: str) -> List[Dict[str, Any]]:
    """源码登记的包只使用该源码根的图；无源码身份的旧台账兼容分发文件。"""

    record = package_record(working_dir, name)
    entries: Dict[str, Dict[str, Any]] = {}
    sources = [("source", _source_json_files(record.package_dirs, record.package_root))]
    if not record.package_root and not record.source_kind:
        sources.insert(0, ("dist", _distribution_json_files(record.name)))
    for source, paths in sources:
        for path in paths:
            graph_name = path.stem
            if graph_name in entries:
                continue
            payload = _load_graph(path)
            if payload is None:
                continue
            entries[graph_name] = _graph_entry(graph_name, path, payload, source)
    return sorted(entries.values(), key=lambda item: item["name"])


def bundled_graph_payload(working_dir: str | Path, name: str, graph_name: str) -> Dict[str, Any]:
    for entry in list_bundled_graphs(working_dir, name):
        if entry["name"] == graph_name:
            payload = _load_graph(Path(entry["path"]))
            if payload is not None:
                return payload
    raise DriverPackageError(f"bundled graph not found: {name}/{graph_name}")


def process_name(package_name: str, graph_name: str) -> str:
    """随包图对应的受管进程名；同一张图反复启动复用同一条进程。"""

    return f"{package_name}/{graph_name}"


def launch_bundled_graph(
    working_dir: str | Path,
    name: str,
    graph_name: str,
    *,
    processes: DeviceProcessService,
) -> Dict[str, Any]:
    """把随包设备图作为受管进程拉起：已有同名进程则更新规格后重启，否则新建并启动。"""

    record = package_record(working_dir, name)
    payload = bundled_graph_payload(working_dir, name, graph_name)
    nodes: List[Dict[str, Any]] = []
    for node in payload["nodes"]:
        if str(node.get("type") or "device") != "device":
            raise DriverPackageError(
                f"随包图 {graph_name} 含非设备节点 {node.get('id')}，不能作为受管设备进程启动；"
                "请用 unilab -g 作为启动图加载"
            )
        item = dict(node)
        item.setdefault("class", item.get("template_name"))
        nodes.append(item)
    spec_payload = {
        "name": process_name(record.name, graph_name),
        "graph_nodes": nodes,
        "package_names": [record.name],
        "external_only": True,
    }
    existing = next(
        (view for view in processes.list() if view.get("name") == spec_payload["name"]), None
    )
    if existing is not None:
        processes.update(existing["id"], spec_payload)
        view = processes.restart(existing["id"])
        created = False
    else:
        view = processes.create(spec_payload)
        view = processes.start(view["id"])
        created = True
    logger.info(
        "[DriverPackageGraphs] %s 随包图 %s -> 受管进程 %s (%s)",
        record.name,
        graph_name,
        view["id"][:8],
        "新建" if created else "重启",
    )
    return {"created": created, "process": view}


__all__ = [
    "GRAPH_DIR_NAMES",
    "bundled_graph_payload",
    "launch_bundled_graph",
    "list_bundled_graphs",
    "package_record",
    "process_name",
]
