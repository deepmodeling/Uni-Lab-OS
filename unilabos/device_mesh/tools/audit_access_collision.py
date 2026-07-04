#!/usr/bin/env python3
"""Audit simplified device xacros for access points embedded in base collisions.

This script intentionally handles the limited xacro patterns used in the
fallback `_phage_display` device meshes:
- macro params with default numeric expressions
- fixed joints
- box collision geometry

It does not expand full ROS xacro semantics. The goal is to flag the planning
regressions we care about in these simplified macros.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


ACCESS_SUFFIXES = (
    "access_link",
    "slot_access",
    "socketTypeGenericSbsFootprint",
)

ALLOWED_COLLISION_STRATEGIES = {
    "opening_cutout",
    "fixed_extended_tray",
    "exposed_work_surface",
}

TRAY_HINTS = (
    "tray_link",
    "drawer_link",
    "stage_link",
    "loading_tray_link",
    "loader_stage_link",
    "waste_drawer_link",
)


@dataclass
class Box:
    center: tuple[float, float, float]
    size: tuple[float, float, float]

    def contains(self, point: tuple[float, float, float], margin: float = 1e-6) -> bool:
        return all(
            abs(point[i] - self.center[i]) <= self.size[i] / 2.0 - margin
            for i in range(3)
        )


def parse_params(params: str) -> dict[str, float]:
    result: dict[str, float] = {"pi": math.pi}
    for raw_line in params.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        for key, value in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*:=\s*([^ ]+)", line):
            try:
                result[key] = float(eval_expr(value, result))
            except Exception:
                # Keep going if a default cannot be resolved yet.
                pass
    return result


def eval_expr(raw: str | None, variables: dict[str, float]) -> float:
    if raw is None:
        return 0.0
    text = raw.strip()
    if text.startswith("${") and text.endswith("}"):
        text = text[2:-1].strip()
    allowed = {**variables, "pi": math.pi}
    return float(eval(text, {"__builtins__": {}}, allowed))


def eval_xyz(raw: str | None, variables: dict[str, float]) -> tuple[float, float, float]:
    if raw is None:
        return (0.0, 0.0, 0.0)
    parts = re.findall(r"\$\{[^}]+\}|[^ ]+", raw.strip())
    if len(parts) != 3:
        raise ValueError(f"Expected xyz triplet, got: {raw}")
    return tuple(eval_expr(part, variables) for part in parts)  # type: ignore[return-value]


def parse_macro(path: Path) -> tuple[dict[str, float], ET.Element]:
    tree = ET.parse(path)
    root = tree.getroot()
    macro = root.find("{http://www.ros.org/wiki/xacro}macro")
    if macro is None:
        raise ValueError(f"No xacro:macro found in {path}")
    variables = parse_params(macro.attrib.get("params", ""))
    parse_properties(macro, variables)
    return variables, macro


def parse_properties(macro: ET.Element, variables: dict[str, float]) -> None:
    pending: list[tuple[str, str]] = []
    for child in macro:
        if child.tag != "{http://www.ros.org/wiki/xacro}property":
            continue
        name = child.attrib.get("name")
        value = child.attrib.get("value")
        if name and value:
            pending.append((name, value))

    while pending:
        progressed = False
        next_pending: list[tuple[str, str]] = []
        for name, value in pending:
            try:
                variables[name] = eval_expr(value, variables)
                progressed = True
            except Exception:
                next_pending.append((name, value))
        if not progressed:
            unresolved = ", ".join(name for name, _ in next_pending)
            raise ValueError(f"Could not resolve xacro properties: {unresolved}")
        pending = next_pending


def iter_children(element: ET.Element, tag: str) -> Iterable[ET.Element]:
    for child in element:
        if child.tag.endswith(tag):
            yield child


def collect_base_collisions(macro: ET.Element, variables: dict[str, float]) -> list[Box]:
    boxes: list[Box] = []
    for link in iter_children(macro, "link"):
        name = link.attrib.get("name", "")
        if not name.endswith("base_link"):
            continue
        for collision in iter_children(link, "collision"):
            origin_el = next(iter_children(collision, "origin"), None)
            geom_el = next(iter_children(collision, "geometry"), None)
            if geom_el is None:
                continue
            box_el = next(iter_children(geom_el, "box"), None)
            if box_el is None:
                continue
            center = eval_xyz(origin_el.attrib.get("xyz") if origin_el is not None else None, variables)
            size = eval_xyz(box_el.attrib.get("size"), variables)
            boxes.append(Box(center=center, size=size))
    return boxes


def collect_fixed_joints(
    macro: ET.Element, variables: dict[str, float]
) -> dict[str, tuple[str, tuple[float, float, float]]]:
    joints: dict[str, tuple[str, tuple[float, float, float]]] = {}
    for joint in iter_children(macro, "joint"):
        if joint.attrib.get("type") != "fixed":
            continue
        child_el = next(iter_children(joint, "child"), None)
        parent_el = next(iter_children(joint, "parent"), None)
        if child_el is None or parent_el is None:
            continue
        child = child_el.attrib.get("link")
        parent = parent_el.attrib.get("link")
        if not child or not parent:
            continue
        origin_el = next(iter_children(joint, "origin"), None)
        xyz = eval_xyz(origin_el.attrib.get("xyz") if origin_el is not None else None, variables)
        joints[child] = (parent, xyz)
    return joints


def resolve_to_base(
    link_name: str,
    joints: dict[str, tuple[str, tuple[float, float, float]]],
) -> tuple[str | None, tuple[float, float, float]]:
    total = [0.0, 0.0, 0.0]
    seen: set[str] = set()
    current = link_name
    while current in joints:
        if current in seen:
            raise ValueError(f"Joint cycle detected at {current}")
        seen.add(current)
        parent, xyz = joints[current]
        total = [total[i] + xyz[i] for i in range(3)]
        current = parent
        if current.endswith("base_link"):
            return current, (total[0], total[1], total[2])
    return None, (total[0], total[1], total[2])


def audit_device(path: Path) -> list[str]:
    variables, macro = parse_macro(path)
    boxes = collect_base_collisions(macro, variables)
    joints = collect_fixed_joints(macro, variables)
    problems: list[str] = []
    for link in iter_children(macro, "link"):
        name = link.attrib.get("name", "")
        if not name.endswith(ACCESS_SUFFIXES):
            continue
        parent, point = resolve_to_base(name, joints)
        if parent is None:
            continue
        if any(box.contains(point) for box in boxes):
            problems.append(f"{path}: access link {name} resolves inside base collision at {point}")
        lower = name.lower()
        tray_backed = any(token in lower for token in ("tray", "drawer", "loader_stage"))
        if tray_backed:
            parent_name = joints.get(name, ("", (0.0, 0.0, 0.0)))[0]
            if parent_name.endswith("base_link"):
                problems.append(f"{path}: tray-backed access link {name} is parented directly to base_link")
    return problems


def audit_metadata(path: Path) -> list[str]:
    problems: list[str] = []
    try:
        data = json.loads(path.read_text())
    except Exception as exc:  # pragma: no cover - already useful in output
        return [f"{path}: invalid JSON: {exc}"]

    def visit_access_points(items: object, context: str) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("linkSuffix") or "<unnamed>"
            if "collision_strategy" not in item:
                problems.append(f"{path}: {context} entry {name} missing collision_strategy")
                continue
            strategy = item.get("collision_strategy")
            if strategy not in ALLOWED_COLLISION_STRATEGIES:
                problems.append(
                    f"{path}: {context} entry {name} has invalid collision_strategy {strategy!r}"
                )

    visit_access_points(data.get("access_points"), "access_points")
    visit_access_points(data.get("accessFrames"), "accessFrames")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+", help="macro_device.xacro paths or device directories")
    args = parser.parse_args()

    macro_paths: list[Path] = []
    for raw in args.paths:
        path = Path(raw)
        if path.is_dir():
            macro_paths.append(path / "macro_device.xacro")
        else:
            macro_paths.append(path)

    problems: list[str] = []
    for macro_path in macro_paths:
        problems.extend(audit_device(macro_path))
        meta_path = macro_path.with_name("meta.json")
        if meta_path.exists():
            problems.extend(audit_metadata(meta_path))

    if problems:
        for problem in problems:
            print(problem)
        return 1

    print(f"OK {len(macro_paths)} device macro(s) passed access collision audit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
