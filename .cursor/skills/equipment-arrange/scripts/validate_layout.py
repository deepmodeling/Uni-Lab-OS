#!/usr/bin/env python3
"""Validate lab layout: device size0 + sibling bbox conflicts.

Usage:
  python validate_layout.py tree.json
  curl .../material/download/{lab_uuid} | python validate_layout.py -

Input: JSON with data.nodes[] or top-level nodes[].
Exit 0 if no size0 devices and no bbox conflicts; else 1.
"""
import json
import sys
from collections import defaultdict


def load_nodes(path):
    raw = sys.stdin.read() if path == "-" else open(path, encoding="utf-8").read()
    data = json.loads(raw)
    if "data" in data and "nodes" in data.get("data", {}):
        return data["data"]["nodes"]
    if "nodes" in data:
        return data["nodes"]
    raise SystemExit("JSON must contain data.nodes or nodes")


def node_depth(node, by_uuid):
    d, cur = 0, node
    while cur.get("parent_uuid") and cur["parent_uuid"] in by_uuid:
        d += 1
        cur = by_uuid[cur["parent_uuid"]]
    return d


def has_model(node):
    m = node.get("model")
    return isinstance(m, dict) and bool(m)


def is_size_zero_device(node):
    if node.get("type") != "device":
        return False
    s = (node.get("pose") or {}).get("size") or {}
    return any((s.get(k, 0) or 0) <= 0 for k in ("width", "height", "depth"))


def footprint_from_bbox(model, pose):
    bb = model.get("bbox") or {}
    mn, mx = bb.get("min", {}), bb.get("max", {})
    p = pose.get("position") or {}
    ox, oy, oz = p.get("x", 0), p.get("y", 0), p.get("z", 0)
    return (
        ox + mn.get("x", 0), oy + mn.get("y", 0), oz + mn.get("z", 0),
        ox + mx.get("x", 0), oy + mx.get("y", 0), oz + mx.get("z", 0),
    )


def footprint_from_pose_size(pose):
    p, s = pose.get("position") or {}, pose.get("size") or {}
    x, y, z = p.get("x", 0), p.get("y", 0), p.get("z", 0)
    w, h, d = s.get("width", 0), s.get("height", 0), s.get("depth", 0)
    return (x, y, z, x + w, y + h, z + d)


def node_footprint(node):
    pose = node.get("pose") or {}
    if has_model(node) and node["model"].get("bbox"):
        return footprint_from_bbox(node["model"], pose), "model.bbox"
    src = "bbox_fallback" if has_model(node) else "pose.size"
    return footprint_from_pose_size(pose), src


def aabb_overlap(a, b):
    return (
        a[0] < b[3] and a[3] > b[0]
        and a[1] < b[4] and a[4] > b[1]
        and a[2] < b[5] and a[5] > b[2]
    )


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "-"
    nodes = load_nodes(path)
    by_uuid = {n["uuid"]: n for n in nodes}
    devices = [n for n in nodes if n.get("type") == "device"]

    print(f"# Layout validation  nodes={len(nodes)}  devices={len(devices)}\n")

    size0 = [n for n in devices if is_size_zero_device(n)]
    print("## size0 devices:", len(size0))
    for n in size0:
        s = (n.get("pose") or {}).get("size") or {}
        print(f"  WARN {n['name']} size=({s.get('width',0)}x{s.get('height',0)}x{s.get('depth',0)})")

    siblings = defaultdict(list)
    for n in nodes:
        siblings[n.get("parent_uuid") or ""].append(n)

    conflicts = []
    for parent, group in siblings.items():
        fps = [(n, *node_footprint(n)) for n in group]
        for i in range(len(fps)):
            for j in range(i + 1, len(fps)):
                na, fa, sa = fps[i][0], fps[i][1], fps[i][2]
                nb, fb, sb = fps[j][0], fps[j][1], fps[j][2]
                if aabb_overlap(fa, fb):
                    pname = by_uuid.get(parent, {}).get("name", parent or "(root)")
                    conflicts.append((na["name"], nb["name"], sa, sb, pname))

    print("\n## bbox conflicts:", len(conflicts))
    for a, b, sa, sb, parent in conflicts:
        print(f"  WARN {a} ({sa}) vs {b} ({sb}) under {parent}")

    print("\n## device check table")
    print("| name | depth | size0 | has_model | footprint |")
    print("|------|-------|-------|-----------|-----------|")
    for n in sorted(devices, key=lambda x: x.get("name", "")):
        dep = node_depth(n, by_uuid)
        s0 = "WARN" if is_size_zero_device(n) else "OK"
        hm = "yes" if has_model(n) else "no"
        _, src = node_footprint(n)
        print(f"| {n['name']} | {dep} | {s0} | {hm} | {src} |")

    if size0 or conflicts:
        sys.exit(1)
    print("\nOK")


if __name__ == "__main__":
    main()
