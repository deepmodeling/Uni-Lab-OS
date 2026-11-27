# -*- coding: utf-8 -*-
"""Up-axis gate for lab-station assets, scenes and SolidWorks GLB conversions.

Checks every USD deliverable for Z-up metres, and (optionally) proves with landmarks
that the geometry itself - not only the ``upAxis`` metadata - is Z-up.  A SolidWorks
GLB keeps CAD Z-up coordinates while following the glTF Y-up convention; importing it
in Blender without undoing the basis change tips the station onto its back while the
USD still declares ``upAxis = Z``.  Only geometry checks catch that.

    python verify_up_axis.py <usd-or-dir> ... --require-floor
    python verify_up_axis.py work/usd/scene_assembly.usda \
        --glb work/station.glb --landmark 脚轮 --landmark 脚杯 --top 三色灯 --top 上罩

Exit code 1 when any subject fails.  ``--glb`` compares the raw glTF bounding box with
the USD bounding box: a direct match is aligned, a Y/Z swapped match is a Y-up failure.
"""
import argparse
import glob
import json
import os
import struct
import sys

import numpy as np

SKIP_DIRS = {"node_modules", ".git", "__pycache__"}


def subjects(paths):
    for p in paths:
        if os.path.isdir(p):
            for root, dirs, files in os.walk(p):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
                for f in files:
                    if f.lower().endswith((".usd", ".usda", ".usdc", ".usdz")):
                        yield os.path.join(root, f)
        elif any(ch in p for ch in "*?"):
            for f in sorted(glob.glob(p)):
                yield f
        else:
            yield p


# --------------------------------------------------------------------- USD side
def usd_facts(path):
    from pxr import Usd, UsdGeom

    st = Usd.Stage.Open(path)
    if st is None:
        raise ValueError("cannot open USD")
    up = str(UsdGeom.GetStageUpAxis(st))
    mpu = float(UsdGeom.GetStageMetersPerUnit(st))
    cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), ["default", "render"])
    box = cache.ComputeWorldBound(st.GetPseudoRoot()).ComputeAlignedRange()
    size = [float(v) for v in box.GetSize()]
    lo = [float(v) for v in box.GetMin()]
    meshes = sum(1 for p in st.Traverse() if p.IsA(UsdGeom.Mesh))
    return {"up_axis": up, "meters_per_unit": mpu, "meshes": meshes,
            "size_m": size, "min_m": lo, "max_m": [float(v) for v in box.GetMax()]}


# --------------------------------------------------------------------- GLB side
def glb_load(path):
    with open(path, "rb") as fh:
        magic, ver, length = struct.unpack("<4sII", fh.read(12))
        if magic != b"glTF":
            raise ValueError("not a GLB: " + str(magic))
        doc = None
        while fh.tell() < length:
            clen, ctype = struct.unpack("<II", fh.read(8))
            if ctype == 0x4E4F534A:
                doc = json.loads(fh.read(clen).decode("utf-8"))
            else:
                fh.seek(clen, 1)
    if doc is None:
        raise ValueError("GLB has no JSON chunk")
    return doc


def glb_local(nd):
    if "matrix" in nd:
        return np.asarray(nd["matrix"], dtype=float).reshape(4, 4).T
    T = np.eye(4)
    if "translation" in nd:
        T[:3, 3] = nd["translation"]
    R = np.eye(4)
    if "rotation" in nd:
        x, y, z, w = nd["rotation"]
        R[:3, :3] = np.array([
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
    S = np.eye(4)
    if "scale" in nd:
        S[:3, :3] = np.diag(nd["scale"])
    return T @ R @ S


def glb_scan(path):
    """Return {name: (min3, max3)} in raw glTF coordinates plus the scene bbox."""
    doc = glb_load(path)
    nodes, acc, meshes = doc["nodes"], doc.get("accessors") or [], doc.get("meshes") or []
    roots = doc["scenes"][doc.get("scene", 0)]["nodes"]
    out = {}

    def walk(i, W, prefix):
        W = W @ glb_local(nodes[i])
        name = (prefix + "/" if prefix else "") + (nodes[i].get("name") or "")
        mi = nodes[i].get("mesh")
        lo = np.full(3, np.inf)
        hi = np.full(3, -np.inf)
        if mi is not None:
            for p in meshes[mi].get("primitives") or []:
                a = acc[p["attributes"]["POSITION"]]
                for cx in (a["min"][0], a["max"][0]):
                    for cy in (a["min"][1], a["max"][1]):
                        for cz in (a["min"][2], a["max"][2]):
                            v = W @ np.array([cx, cy, cz, 1.0])
                            lo = np.minimum(lo, v[:3])
                            hi = np.maximum(hi, v[:3])
        if np.isfinite(lo).all():
            out[name] = (lo, hi)
        for ch in nodes[i].get("children") or []:
            walk(ch, W, name)

    for r in roots:
        walk(r, np.eye(4), "")
    if not out:
        raise ValueError("no mesh geometry in GLB")
    lo = np.min([v[0] for v in out.values()], axis=0)
    hi = np.max([v[1] for v in out.values()], axis=0)
    return doc, out, lo, hi


def landmark_report(out, lo, hi, landmarks, tops, tol=0.005):
    problems = []
    info = {"landmarks": {}, "tops": {}}
    for key in landmarks:
        rows = [(n, v) for n, v in out.items() if key in n]
        if len(rows) < 2:
            problems.append("landmark %r: only %d match(es)" % (key, len(rows)))
            continue
        mins = np.array([v[0] for _, v in rows])
        spread = mins.max(axis=0) - mins.min(axis=0)
        axis = int(np.argmin(spread))
        flat = float(spread[axis])
        near_bottom = abs(float(mins[:, axis].mean()) - float(lo[axis])) <= \
            max(tol, 0.02 * float(hi[axis] - lo[axis]))
        info["landmarks"][key] = {"count": len(rows), "plane_axis": "XYZ"[axis],
                                  "spread_m": round(flat, 4),
                                  "at_bottom": bool(near_bottom)}
        if axis != 2:
            problems.append("%r parts are coplanar in %s, not Z -> geometry is not Z-up"
                            % (key, "XYZ"[axis]))
        elif not near_bottom:
            problems.append("%r parts are not at the bottom Z plane" % key)
    height = float(hi[2] - lo[2])
    for key in tops:
        rows = [(n, v) for n, v in out.items() if key in n]
        if not rows:
            problems.append("top %r: no match" % key)
            continue
        top = max(float(v[1][2]) for _, v in rows)
        ok = top >= float(hi[2]) - max(tol, 0.05 * height)
        info["tops"][key] = {"count": len(rows), "max_z_m": round(top, 4),
                             "station_max_z_m": round(float(hi[2]), 4), "is_top": bool(ok)}
        if not ok:
            problems.append("%r is not at the top Z of the station" % key)
    return problems, info


def bbox_size(lo, hi):
    return [float(v) for v in (np.asarray(hi) - np.asarray(lo))]


def align(a, b, tol=0.005):
    if len(a) != 3 or len(b) != 3:
        return "unknown"
    direct = max(abs(x - y) for x, y in zip(a, b))
    swapped = max(abs(a[0] - b[0]), abs(a[1] - b[2]), abs(a[2] - b[1]))
    if direct <= tol:
        return "aligned"
    if swapped <= tol:
        return "yz_swapped"
    return "subset_or_other"


def check_usd(path, floor_tol, require_floor):
    problems = []
    try:
        facts = usd_facts(path)
    except Exception as exc:  # noqa: BLE001
        return {"path": path, "status": "failed", "problems": [repr(exc)]}
    if facts["up_axis"] != "Z":
        problems.append("upAxis=%s" % facts["up_axis"])
    if abs(facts["meters_per_unit"] - 1.0) > 1e-9:
        problems.append("metersPerUnit=%s" % facts["meters_per_unit"])
    if facts["meshes"] == 0:
        problems.append("no mesh prims (unresolved references?)")
    size = facts["size_m"]
    if facts["meshes"] and size[2] < 0.02 and max(size[0], size[1]) > 0.5:
        problems.append("flat in Z %s - likely Y-up content" % [round(v, 3) for v in size])
    if require_floor and facts["meshes"] and abs(facts["min_m"][2]) > floor_tol:
        problems.append("floor minZ=%.4f" % facts["min_m"][2])
    facts.update(path=path, status="passed" if not problems else "failed",
                 problems=problems, tallest_axis="XYZ"[int(np.argmax(size))]
                 if facts["meshes"] else None)
    return facts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--json", default=None)
    ap.add_argument("--glb", default=None, help="reference GLB of the same station")
    ap.add_argument("--landmark", action="append", default=[],
                    help="name substring of ground-contact parts (casters/feet)")
    ap.add_argument("--top", action="append", default=[],
                    help="name substring of parts that must sit at the highest Z")
    ap.add_argument("--require-floor", action="store_true")
    ap.add_argument("--floor-tol", type=float, default=0.002)
    a = ap.parse_args()
    if not a.paths and not a.glb:
        ap.error("provide USD paths and/or --glb")

    rows = []
    glb_info = None
    glb_problems = []
    if a.glb:
        doc, out, lo, hi = glb_scan(a.glb)
        glb_info = {"path": a.glb, "nodes": len(out),
                    "raw_size_m": [round(v, 4) for v in bbox_size(lo, hi)],
                    "raw_min_m": [round(float(v), 4) for v in lo],
                    "raw_max_m": [round(float(v), 4) for v in hi]}
        if a.landmark or a.top:
            glb_problems, extra = landmark_report(out, lo, hi, a.landmark, a.top)
            glb_info.update(extra)

    for p in subjects(a.paths):
        row = check_usd(p, a.floor_tol, a.require_floor)
        if glb_info and row.get("status") != "failed":
            verdict = align(row["size_m"], glb_info["raw_size_m"])
            row["glb_alignment"] = verdict
            if verdict == "yz_swapped":
                row["status"] = "failed"
                row.setdefault("problems", []).append(
                    "bbox is the glTF bbox with Y/Z swapped -> Y-up (station lying)")
            elif verdict == "subset_or_other" and a.require_floor:
                row.setdefault("problems", []).append("bbox does not match the GLB")
        rows.append(row)
        flag = "OK  " if row["status"] == "passed" else "FAIL"
        print("%s %-6s %-4s mpu=%-4s meshes=%-5s size=%-28s %s" % (
            flag, row.get("up_axis", "?"), row.get("tallest_axis") or "?",
            row.get("meters_per_unit", "?"), row.get("meshes", "?"),
            row.get("size_m", "?"), row.get("problems", "")))
        if row.get("glb_alignment"):
            print("       glb alignment: %s" % row["glb_alignment"])

    if glb_info:
        print("\nGLB %s" % glb_info["path"])
        print("  raw size (glTF XYZ): %s  min %s  max %s" % (
            glb_info["raw_size_m"], glb_info["raw_min_m"], glb_info["raw_max_m"]))
        for key, v in (glb_info.get("landmarks") or {}).items():
            print("  landmark %-6s n=%-3d plane=%s spread=%.4f m bottom=%s" % (
                key, v["count"], v["plane_axis"], v["spread_m"], v["at_bottom"]))
        for key, v in (glb_info.get("tops") or {}).items():
            print("  top      %-6s n=%-3d maxZ=%.4f stationMaxZ=%.4f at_top=%s" % (
                key, v["count"], v["max_z_m"], v["station_max_z_m"], v["is_top"]))
        for p in glb_problems:
            print("  FAIL %s" % p)

    failed = [r for r in rows if r["status"] != "passed"] + \
             [{"path": "glb:" + (a.glb or ""), "problems": glb_problems}] * (1 if glb_problems else 0)
    print("\n%d USD subject(s), %d failed; GLB problems: %d" % (
        len(rows), len([r for r in rows if r["status"] != "passed"]), len(glb_problems)))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump({"schema": "unilab.up-axis-gate/2", "subjects": len(rows),
                       "failed": len(failed), "glb": glb_info, "glb_problems": glb_problems,
                       "rows": rows}, f, ensure_ascii=False, indent=1)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
