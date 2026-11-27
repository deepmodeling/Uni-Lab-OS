"""单独试跑资产行为类：只加载一个资产包，不需要驱动、设备图或工站。

    python -m unilabos.simulation try --package PKG                 # 列出关节和机构动作
    python -m unilabos.simulation try --package PKG --action open_door --action 'rotate={"angle_deg": 90}'

行为类默认取资产包 ``unilab.json`` 的 ``behavior_binding.implementation``；开发中的类用
``--behavior 文件.py:类名`` 指定。本命令会导入并执行行为源码，只对可信的资产使用。
"""
from __future__ import annotations

import importlib.util
import inspect
import json
import sys
from pathlib import Path
from typing import Any

from .mechanism import Mechanism, MechanismError, action_parameters, mechanism_actions, run_action

# 试跑时故意把实例装在非原点、带旋转的位置：行为如果误用世界坐标或改动安装变换，就会暴露出来。
DEFAULT_MOUNT = (1.2, -0.4, 0.3, 31.0)


def load_behavior(reference: str):
    """按 ``文件.py:类名`` 导入行为类（零导入的普通类，无参构造）。"""
    path, _, name = reference.rpartition(":")
    if not path or not name:
        raise ValueError("--behavior 的格式是 文件.py:类名")
    source = Path(path).resolve()
    spec = importlib.util.spec_from_file_location("unilab_asset_behavior_" + source.stem, source)
    if spec is None or spec.loader is None:
        raise ValueError("无法导入行为源码：" + str(source))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    cls = getattr(module, name, None)
    if not inspect.isclass(cls):
        raise ValueError(f"{source} 里没有类 {name}")
    return cls


def package_behavior(package: Path) -> str:
    """资产包声明的行为类引用（``文件.py:类名``）。"""
    metadata = json.loads((package / "simready_usd" / "unilab.json").read_text(encoding="utf8"))
    binding = metadata.get("behavior_binding") or {}
    implementation = binding.get("implementation")
    if not isinstance(implementation, str) or ":" not in implementation:
        raise ValueError("资产包没有声明行为类，请用 --behavior 指定")
    module, name = implementation.split(":")
    return f"{package / binding['source_root'] / (module.replace('.', '/') + '.py')}:{name}"


def open_instance(package: Path, mount=DEFAULT_MOUNT):
    """在内存 stage 里按给定安装位姿引用资产包的 USD，返回 (stage, 实例根)。"""
    from pxr import Usd, UsdGeom

    metadata = json.loads((package / "simready_usd" / "unilab.json").read_text(encoding="utf8"))
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageMetersPerUnit(stage, 1)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    prim = UsdGeom.Xform.Define(stage, "/Asset")
    prim.AddTranslateOp().Set(tuple(mount[:3]))
    prim.AddRotateZOp().Set(float(mount[3]))
    prim.GetPrim().GetReferences().AddReference(str(package / metadata["entrypoints"]["usd"]))
    return stage, "/Asset"


def _signature(method) -> str:
    return "(" + ", ".join(action_parameters(method)) + ")"


def try_actions(package: Path, behavior: str | None, actions: list[tuple[str, dict[str, Any]]], *,
                rate_hz: float = 20.0, mount=DEFAULT_MOUNT,
                materials: dict[str, dict[str, Any]] | None = None, resources: Path | None = None,
                scene: dict[str, Any] | None = None) -> dict[str, Any]:
    """试跑一组动作，返回报告；动作被资产拒绝时记录原因并停止后续动作。"""
    from pxr import UsdGeom
    package = Path(package).resolve()
    reference = behavior or package_behavior(package)
    cls = load_behavior(reference)
    stage, root = open_instance(package, mount)
    installation = UsdGeom.Xformable(stage.GetPrimAtPath(root)).GetLocalTransformation()
    resource_points = None
    if resources is not None:
        from .mechanism_resources import ResourcePoints

        resource_points = ResourcePoints.from_graph(resources)
    mechanism = Mechanism(stage, root, cls(), resource_points)
    if scene is not None:
        from .mechanism_resources import attach_scene

        attach_scene(mechanism, scene)
    available = mechanism_actions(mechanism)
    report: dict[str, Any] = {
        "schema": "unilab.mechanism-try/1", "package": str(package), "behavior": reference,
        "qualification": "kinematic_geometry_only",
        "joints": {name: {"kind": j.kind, "lower": j.lower, "upper": j.upper} for name, j in mechanism.joints.items()},
        "sites": len(mechanism.sites),
        "actions": {name: {"signature": _signature(method), "description": inspect.getdoc(method) or ""}
                    for name, method in sorted(available.items())},
        "runs": [], "status": "listed" if not actions else "passed",
    }
    for name, parameters in actions:
        try:
            run = run_action(mechanism, name, parameters, rate_hz=rate_hz, materials=materials)
        except (MechanismError, TypeError, ValueError) as error:
            report["runs"].append({"action": name, "parameters": parameters, "status": "rejected",
                                   "code": getattr(error, "code", "parameters_invalid"), "reason": str(error)})
            report["status"] = "rejected"
            break
        result = run["result"]
        final_target = {}
        for segment in run["plan"]["segments"]:
            final_target.update(segment["targets"])
        deviation = max((abs(result["positions_si"][n] - q) for n, q in final_target.items()), default=0.0)
        moved = any(abs(frame["positions_si"][n] - run["frames"][0]["positions_si"][n]) > 1e-9
                    for frame in run["frames"] for n in frame["positions_si"]) or deviation > 0
        entry = {"action": name, "parameters": parameters, "status": "passed", "duration_s": run["plan"]["duration_s"],
                 "segments": run["plan"]["segments"], "frames": len(run["frames"]), "final": result["positions_si"],
                 "state": result["state"], "readback_max_deviation": deviation, "geometry_changed": bool(moved),
                 "evidence": result["evidence"], "carries": result["carries"], "trajectory": run["frames"]}
        if deviation > 1e-6:
            entry.update(status="failed", reason="读回的关节值与计划终值不一致")
            report["status"] = "failed"
        report["runs"].append(entry)
        if entry["status"] != "passed":
            break
    preserved = UsdGeom.Xformable(stage.GetPrimAtPath(root)).GetLocalTransformation() == installation
    report["installation_preserved"] = bool(preserved)
    if not preserved:
        report["status"] = "failed"
    return report


def parse_action(text: str) -> tuple[str, dict[str, Any]]:
    """``名字`` 或 ``名字={JSON 对象}``。"""
    name, separator, raw = text.partition("=")
    parameters = json.loads(raw) if separator else {}
    if not name or not isinstance(parameters, dict):
        raise ValueError("--action 的格式是 名字 或 名字={\"参数\": 值}")
    return name, parameters


def main(args) -> int:
    try:
        materials = dict(parse_action(item) for item in getattr(args, "material", None) or [])
        report = try_actions(args.package, args.behavior, [parse_action(a) for a in args.action or []],
                             rate_hz=args.rate, materials=materials, resources=getattr(args, "resources", None),
                             scene=json.loads(args.scene.read_text(encoding="utf8")) if getattr(args, "scene", None) else None)
    except (ValueError, OSError, KeyError) as error:
        print(json.dumps({"status": "error", "reason": str(error)}, ensure_ascii=False))
        return 2
    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
    summary = {key: value for key, value in report.items() if key != "runs"}
    summary["runs"] = [{k: v for k, v in run.items() if k != "trajectory"} for run in report["runs"]]
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if report["status"] in ("listed", "passed") else 1
