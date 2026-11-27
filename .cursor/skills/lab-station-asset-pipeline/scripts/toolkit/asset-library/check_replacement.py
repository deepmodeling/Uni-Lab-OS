"""Check package closure and explicitly scoped replacement invariants, without saving USD."""
from pathlib import Path, PureWindowsPath
import numpy as np
from pxr import Sdf, Usd, UsdGeom, UsdUtils
from p0_common import checked, cli, digest, finite, read, require, resolve, result, scoped


def matrix(stage, path):
    prim = stage.GetPrimAtPath(path)
    require(bool(prim), "Missing frame: " + str(path))
    return np.asarray(UsdGeom.XformCache().GetLocalToWorldTransform(prim))


def open_stage(path):
    stage = Usd.Stage.Open(str(path))
    require(bool(stage), "Cannot open USD: " + str(path))
    return stage


def closure(spec, base):
    root = resolve(base, spec["root"])
    entry = resolve(root, spec["entrypoint"])
    require(entry.is_relative_to(root), "Entrypoint outside package")
    if spec.get("native_digest"):
        from native_package import verify
        verify(root, spec["native_digest"])
    else:
        require(spec.get("files"), "Supply native_digest or complete file pins")
        paths = [f["path"] for f in spec["files"]]
        require(len(set(paths)) == len(paths), "Duplicate file pin")
        require({p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()} == set(paths), "File pin scope differs")
        for pin in spec["files"]:
            path = resolve(root, pin["path"])
            require(path.is_relative_to(root) and digest(path) == pin["sha256"], "File escaped/changed: " + pin["path"])
    layers, assets, missing = UsdUtils.ComputeAllDependencies(str(entry))
    require(not missing, "Unresolved dependencies: " + str(missing))
    require(all(Path(l.realPath).resolve().is_relative_to(root) for l in layers), "USD layer escapes package")
    require(all(Path(p).resolve().is_relative_to(root) for p in assets), "Asset/texture escapes package")
    # Collect paths from an anonymous copy; never edit the immutable source layer.
    authored = []
    for layer in layers:
        scratch = Sdf.Layer.CreateAnonymous()
        scratch.TransferContent(layer)
        def collect(value):
            if value:
                authored.append(value)
            return value
        UsdUtils.ModifyAssetPaths(scratch, collect)
    require(all(not Path(p).is_absolute() and not PureWindowsPath(p).is_absolute()
                and "://" not in p for p in authored), "Absolute or remote authored dependency")
    stage = open_stage(entry)
    require(stage.GetDefaultPrim().IsValid(), "No default prim")
    return {"layers": len(layers), "external_assets": len(assets), "relative_paths": len(authored),
            "qualification": "current composed dependency closure; unselected variants not enumerated"}


def surfaces(stage, root):
    require(stage.GetPrimAtPath(root).IsValid(), "Missing geometry root")
    cache = UsdGeom.XformCache()
    chunks = {}
    for prim in Usd.PrimRange(stage.GetPrimAtPath(root), Usd.TraverseInstanceProxies()):
        if not prim.IsA(UsdGeom.Mesh):
            continue
        mesh = UsdGeom.Mesh(prim)
        if mesh.GetPurposeAttr().Get() in ("guide", "proxy"):
            continue
        counts = np.asarray(mesh.GetFaceVertexCountsAttr().Get())
        require(np.all(counts >= 3), "Degenerate polygon: " + str(prim.GetPath()))
        pts = np.asarray(mesh.GetPointsAttr().Get(), dtype=float)
        m = np.asarray(cache.GetLocalToWorldTransform(prim))
        require(np.isfinite(pts).all() and np.isfinite(m).all(), "Nonfinite geometry")
        pts = (pts @ m[:3, :3] + m[3, :3]) * UsdGeom.GetStageMetersPerUnit(stage)
        indices = np.asarray(mesh.GetFaceVertexIndicesAttr().Get())
        starts = np.concatenate(([0], np.cumsum(counts[:-1])))
        for size in np.unique(counts):
            offsets = starts[counts == size, None] + np.arange(size)
            chunks.setdefault(int(size), []).append(pts[indices[offsets]])
    require(bool(chunks), "No mesh geometry in requested subtree")
    return {size: np.concatenate(values) for size, values in chunks.items()}


def compare_triangles(a, b, tolerance):
    """One-to-one triangle multiset test, including duplicate multiplicities.

    Preserves source triangle vertex order; retriangulation needs its own evidence.
    A nearest-neighbor-only test would miss redistributed duplicate faces.
    """
    from scipy.spatial import cKDTree
    from scipy.sparse import csr_matrix
    from scipy.sparse.csgraph import maximum_bipartite_matching
    require(a.shape == b.shape, "Triangle count changed (missing/duplicated geometry)")
    a, b = a.reshape(len(a), -1), b.reshape(len(b), -1)
    tree = cKDTree(b)
    distances, matches = tree.query(a, p=np.inf)
    require(float(distances.max()) <= tolerance, "Zero-pose triangle surface changed")
    if len(set(matches.tolist())) != len(matches):
        candidates = tree.query_ball_point(a, tolerance, p=np.inf)
        indices = np.concatenate(candidates)
        indptr = np.concatenate(([0], np.cumsum([len(c) for c in candidates])))
        graph = csr_matrix((np.ones(len(indices), dtype=np.int8), indices, indptr), shape=(len(a), len(b)))
        matches = maximum_bipartite_matching(graph, perm_type="column")
        require(np.all(matches >= 0), "Triangle multiplicity differs; no one-to-one surface match")
    return float(np.max(np.abs(a - b[matches])))


def replacement(spec, base):
    a, b = (open_stage(resolve(base, spec[k]["usd"])) for k in ("before", "after"))
    require(UsdGeom.GetStageMetersPerUnit(a) == UsdGeom.GetStageMetersPerUnit(b) == 1, "Replacement requires metre stages")
    require(UsdGeom.GetStageUpAxis(a) == UsdGeom.GetStageUpAxis(b), "Stage up-axis changed")
    tol = spec["tolerance_m"]
    require(finite(tol) and tol > 0, "Positive geometry tolerance required")
    ra, rb = spec["before"]["prim"], spec["after"]["prim"]
    for stage, root in ((a, ra), (b, rb)):
        errors = [str(e) for e in stage.GetCompositionErrors()
                  if e.rootSite.path.HasPrefix(Sdf.Path(root)) or Sdf.Path(root).HasPrefix(e.rootSite.path)]
        require(not errors, "Subtree composition unresolved: " + " | ".join(errors))
    frame_tol = spec.get("matrix_tolerance", 1e-8)
    require(finite(frame_tol) and frame_tol > 0, "Positive matrix tolerance required")
    frames = [{"before": ra, "after": rb}] + spec.get("frames", [])
    errors = []
    for frame in frames:
        error = float(np.max(np.abs(matrix(a, frame["before"]) - matrix(b, frame["after"]))))
        require(error <= frame_tol, "Installation/site frame changed: " + str(frame))
        errors.append(error)
    old, new = surfaces(a, ra), surfaces(b, rb)
    require(old.keys() == new.keys(), "Polygon arity changed; retriangulation requires separate source correspondence")
    geometry = max(compare_triangles(old[k], new[k], tol) for k in old)
    return {"geometry_error_m": geometry, "frame_checks": len(frames), "maximum_matrix_error": max(errors)}


def remove_pointer(data, pointer):
    require(pointer.startswith("/") and pointer != "/", "Whitelist must name a concrete field")
    parts = [p.replace("~1", "/").replace("~0", "~") for p in pointer.split("/")[1:]]
    for key in parts[:-1]:
        if isinstance(data, list):
            if not key.isdecimal() or int(key) >= len(data):return False
            data = data[int(key)]
        else:
            if key not in data:return False
            data = data[key]
    if isinstance(data, list):
        require(False, "Whitelist list items by projected stable ID, not deletion of array elements")
    return data.pop(parts[-1], None) is not None


def json_invariants(spec, base):
    a, b = read(resolve(base, spec["before"])), read(resolve(base, spec["after"]))
    whitelist = spec.get("allowed_changes", [])
    require(len(set(whitelist)) == len(whitelist), "Duplicate whitelist")
    for pointer in whitelist:
        found_a = remove_pointer(a, pointer)
        found_b = remove_pointer(b, pointer)
        require(found_a or found_b, "Unused whitelist entry: " + pointer)
    require(a == b, "Unapproved JSON changes (installation, pickup, node set or other protected values)")
    return {"allowed_changes": whitelist, "all_other_values_unchanged": True}


def check(document, base):
    require(document["schema"] == "asset-replacement-checks/1", "Unsupported replacement contract")
    rows = document["checks"]
    scoped(document, rows)
    handlers = {"package": closure, "replacement": replacement, "json_invariants": json_invariants}
    return result([checked(r["id"], lambda r=r: handlers[r["kind"]](r, base)) for r in rows])


if __name__ == "__main__":
    raise SystemExit(cli(__doc__, check))
