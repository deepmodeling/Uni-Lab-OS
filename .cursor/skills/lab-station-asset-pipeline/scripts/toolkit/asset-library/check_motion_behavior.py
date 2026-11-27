"""Independently observe pinned USD behavior on renamed, transformed twin instances.

Trusted behavior is opt-in Python execution, not sandboxed. Sources are never saved.
The minimal adapter protocol is factory(stage, root).set_positions(dict[str, SI])
and reset(). No dependency on a station builder or on behavior-internal state.
"""
import importlib.util
import math
import sys
from pathlib import Path
import numpy as np
from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics
from p0_common import checked, cli, digest, finite, require, resolve, result, scoped


def world(stage, path):
    return UsdGeom.XformCache().GetLocalToWorldTransform(stage.GetPrimAtPath(path))


def frame(prim, index):
    q = prim.GetAttribute("physics:localRot" + str(index)).Get()
    p = prim.GetAttribute("physics:localPos" + str(index)).Get()
    require(q is not None and p is not None, "Missing joint frame")
    require(np.isfinite([*p, q.GetReal(), *q.GetImaginary()]).all(), "Nonfinite joint frame")
    require(abs(q.GetLength() - 1) < 1e-5, "Joint frame quaternion not normalized")
    return Gf.Matrix4d(1).SetRotate(Gf.Rotation(Gf.Quatd(q.GetReal(), Gf.Vec3d(q.GetImaginary())))) * Gf.Matrix4d(1).SetTranslate(Gf.Vec3d(p))


def snap(stage, root):
    cache = UsdGeom.XformCache()
    return {str(p.GetPath()): np.asarray(cache.GetLocalToWorldTransform(p)) for p in
            Usd.PrimRange(stage.GetPrimAtPath(root), Usd.TraverseInstanceProxies()) if UsdGeom.Xformable(p)}


def same(a, b, tolerance):
    return a.keys() == b.keys() and all(np.isfinite(b[k]).all() and np.max(np.abs(a[k] - b[k])) <= tolerance for k in a)


def run_asset(spec, base, execute):
    root = resolve(base, spec["package_root"])
    usd = resolve(root, spec["entrypoint"])
    require(usd.is_relative_to(root) and digest(usd) == spec["usd_sha256"], "USD outside package or digest differs")
    behavior = spec["behavior"]
    files = behavior["files"]
    require(files and len({f["path"] for f in files}) == len(files), "Missing/duplicate behavior pins")
    before_hashes = {}
    for pin in files:
        path = resolve(root, pin["path"])
        require(path.is_relative_to(root) and digest(path) == pin["sha256"], "Behavior source outside package or digest differs")
        before_hashes[path] = pin["sha256"]
    module_path = resolve(root, behavior["module"])
    require(module_path in before_hashes, "Behavior entry not pinned")
    source = Usd.Stage.Open(str(usd))
    require(bool(source) and source.GetDefaultPrim().IsValid(), "Missing USD default prim")
    require(UsdGeom.GetStageMetersPerUnit(source) == 1, "Behavior SI contract requires metre stage")
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageMetersPerUnit(stage, 1)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.GetStageUpAxis(source))
    for name, xyz, rxyz in (("RenamedA", (.31, -.27, .42), (13, 0, 37)),
                            ("RenamedB", (-.81, .63, .17), (-23, 11, -17))):
        x = UsdGeom.Xform.Define(stage, "/" + name)
        x.GetPrim().GetReferences().AddReference(str(usd))
        x.AddTranslateOp(opSuffix="p0Mount").Set(Gf.Vec3d(*xyz))
        x.AddRotateXYZOp(opSuffix="p0Mount").Set(Gf.Vec3f(*rxyz))
    joints = {}
    bodies = set()
    for prim in Usd.PrimRange(stage.GetPrimAtPath("/RenamedA")):
        if not prim.IsA(UsdPhysics.Joint):continue
        require(prim.IsA(UsdPhysics.RevoluteJoint) or prim.IsA(UsdPhysics.PrismaticJoint), "Unsupported joint type: " + str(prim.GetPath()))
        name = str(prim.GetPath()).removeprefix("/RenamedA/")
        targets = [prim.GetRelationship("physics:body" + str(i)).GetTargets() for i in (0, 1)]
        require(all(len(t) == 1 for t in targets), "Explicit body0 and body1 required")
        parent, child = (str(t[0]) for t in targets)
        require(parent != child and all(Sdf.Path(t).HasPrefix(Sdf.Path("/RenamedA")) and stage.GetPrimAtPath(t).IsValid() for t in (parent, child)), "Invalid or cross-instance body target")
        require(child not in bodies, "Multiple joints write one body; use an intermediate link or explicit coupled adapter")
        bodies.add(child)
        require(prim.GetAttribute("physics:axis").Get() in ("X", "Y", "Z"), "Invalid joint axis")
        require(prim.GetAttribute("physics:jointEnabled").Get() is False, "Kinematic audit requires disabled solver; physics is a separate route")
        lo, hi = (prim.GetAttribute("physics:" + k + "Limit").Get() for k in ("lower", "upper"))
        require(lo is not None and hi is not None and not math.isnan(lo) and not math.isnan(hi) and lo <= 0 <= hi and lo < hi, "Invalid limits/zero; this protocol requires zero inside nonzero motion range")
        rev = prim.IsA(UsdPhysics.RevoluteJoint)
        if rev:lo, hi = math.radians(lo), math.radians(hi)
        joints[name] = (prim, parent, child, frame(prim, 0), frame(prim, 1), lo, hi, rev)
    expected = spec["expected_joints"]
    require(expected and len(set(expected)) == len(expected) and set(expected) == set(joints), "Joint scope differs or is empty")
    sites = spec.get("sites", [])
    for site in sites:
        require(site["owner_joint"] in joints, "Unknown site owner")
        require(stage.GetPrimAtPath("/RenamedA/" + site["path"]).IsValid(), "Missing live site")
    if not execute:
        return {"status": "incomplete", "joints": len(joints), "reason": "Static structure checked; use --execute-behavior for trusted source to observe motion"}
    tol = spec.get("matrix_tolerance", 3e-6)
    require(finite(tol) and tol > 0, "Positive matrix tolerance required")
    module_spec = importlib.util.spec_from_file_location("_p0_behavior_" + digest(module_path)[:16], module_path)
    module = importlib.util.module_from_spec(module_spec)
    prior_path = list(sys.path)
    sys.path.insert(0, str(module_path.parent))
    try:
        module_spec.loader.exec_module(module)
        factory = getattr(module, behavior["factory"])
        initial_a, initial_b = snap(stage, "/RenamedA"), snap(stage, "/RenamedB")
        first, second = factory(stage, "/RenamedA"), factory(stage, "/RenamedB")
        require(same(initial_a, snap(stage, "/RenamedA"), tol) and same(initial_b, snap(stage, "/RenamedB"), tol), "Constructor changed zero geometry")
        def reset():
            first.reset()
            require(same(initial_a, snap(stage, "/RenamedA"), tol), "Reset changed zero transforms")
            require(same(initial_b, snap(stage, "/RenamedB"), tol), "Other instance changed")
        reset()
        site_relative = {s["path"]: world(stage, "/RenamedA/" + s["path"]) * world(stage, joints[s["owner_joint"]][2]).GetInverse() for s in sites}
        def observe(values):
            maximum = 0.0
            for name, (p, parent, child, f0, f1, lo, hi, rev) in joints.items():
                q = values.get(name, 0.0)
                axis = Gf.Vec3d(*{"X": (1, 0, 0), "Y": (0, 1, 0), "Z": (0, 0, 1)}[p.GetAttribute("physics:axis").Get()])
                expected_delta = (Gf.Matrix4d(1).SetRotate(Gf.Rotation(axis, math.degrees(q))) if rev else Gf.Matrix4d(1).SetTranslate(axis * q))
                actual_delta = f1 * world(stage, child) * world(stage, parent).GetInverse() * f0.GetInverse()
                error = float(np.max(np.abs(np.asarray(actual_delta) - np.asarray(expected_delta))))
                require(math.isfinite(error) and error <= tol, "Actual body motion disagrees with joint frame: " + name)
                maximum = max(maximum, error)
            require(same(initial_b, snap(stage, "/RenamedB"), tol), "Cross-instance motion")
            require(np.max(np.abs(initial_a["/RenamedA"] - np.asarray(world(stage, "/RenamedA")))) <= tol, "Installation root moved")
            for s in sites:
                relative = world(stage, "/RenamedA/" + s["path"]) * world(stage, joints[s["owner_joint"]][2]).GetInverse()
                require(Gf.IsClose(relative, site_relative[s["path"]], tol), "Live site did not follow owner: " + s["path"])
            return maximum
        samples, observations = {}, []
        for name, (_, _, child, _, _, lo, hi, rev) in joints.items():
            step = .05 if rev else .001
            q = min(step, hi / 2) if hi > 0 else max(-step, lo / 2)
            require(math.isfinite(q) and q != 0, "No safe nonzero sample")
            first.set_positions({name: q})
            require(np.max(np.abs(initial_a[child] - np.asarray(world(stage, child)))) > min(tol / 10, abs(q) / 10), "Command accepted but body did not move")
            observations.append({"joint": name, "sample_si": q, "matrix_error": observe({name: q})})
            reset()
            for limit in (lo, hi):
                if math.isfinite(limit):
                    first.set_positions({name: limit}); observe({name: limit}); reset()
            invalid = hi + .001 if math.isfinite(hi) else lo - .001 if math.isfinite(lo) else None
            if invalid is not None:
                try:first.set_positions({name: invalid})
                except (ValueError, KeyError):pass
                else:raise ValueError("Out-of-range target accepted")
                require(same(initial_a, snap(stage, "/RenamedA"), tol), "Rejected target partially mutated stage")
            samples[name] = q
        for ratio in (.25, .5, 1, .5, 0):
            values = {n: q * ratio for n, q in samples.items()}
            first.set_positions(values); observe(values)
        reset()
        name = next(iter(joints))
        for invalid in ({name: float("nan")}, {name: float("inf")}, {name: samples[name], "__unknown_joint__": 1.0}):
            try:first.set_positions(invalid)
            except (ValueError, KeyError):pass
            else:raise ValueError("Invalid target accepted")
            require(same(initial_a, snap(stage, "/RenamedA"), tol) and same(initial_b, snap(stage, "/RenamedB"), tol), "Invalid command partially mutated stage")
        # Prove the second instance is operable too, not just inert.
        second.set_positions(samples)
        require(same(initial_a, snap(stage, "/RenamedA"), tol), "Reverse instance cross-talk")
        require(not same(initial_b, snap(stage, "/RenamedB"), tol / 10), "Second instance did not move")
        second.reset()
        require(same(initial_b, snap(stage, "/RenamedB"), tol), "Second reset drift")
    finally:
        sys.path[:] = prior_path
    require(digest(usd) == spec["usd_sha256"] and all(digest(p) == h for p, h in before_hashes.items()), "Behavior changed immutable source files")
    return {"joints": len(joints), "sites": len(sites), "observations": observations,
            "renamed_rotated_dual_instances": True, "invalid_targets_atomic": True,
            "limits": "authored bounds tested; physical calibration not inferred", "physics": "not_run"}


def check(document, base, execute=False):
    require(document["schema"] == "asset-motion-checks/1", "Unsupported motion contract")
    rows = document["assets"]
    scoped(document, rows)
    return result([checked(r["id"], lambda r=r: run_asset(r, base, execute)) for r in rows])


if __name__ == "__main__":
    raise SystemExit(cli(__doc__, check, behavior=True))
