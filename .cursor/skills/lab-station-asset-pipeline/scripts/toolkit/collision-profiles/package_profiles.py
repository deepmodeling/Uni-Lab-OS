"""Append collision-profile revisions using the existing native asset packager."""
import argparse
import copy
import json
import os
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "asset-library"))
import native_package as native
from build_native_library import export_package, rebuild_index


def package(library, candidates, stage, work):
    library, candidates, stage, work = map(Path, (library, candidates, stage, work))
    if stage.exists() or work.exists():
        raise FileExistsError("Use fresh staging and work directories")
    rows = native.read(candidates / "report-v2.json")
    validation = native.read(candidates / "load-validation.json")
    checked = {(r["asset_id"], r["mode"]) for r in validation if r["status"] == "passed"}
    for row in rows:
        if row["status"] != "built" or any((row["asset_id"], mode) not in checked for mode in ("exact", "outer")):
            raise ValueError("Unvalidated collision candidate")
    shutil.copytree(library, stage, copy_function=os.link)
    (stage / "library.json").unlink()
    work.mkdir(parents=True)
    index = native.read(library / "library.json")
    changes = []
    for row in rows:
        old = library / row["source"]
        previous = native.verify(old)
        body = native.package(old)
        record = next(r for r in index["records"] if r["catalog_id"] == body["asset_id"])
        old_target = next(t for t in record["targets"] if t["package_path"] == row["source"])
        path = Path(row["source"])
        slug, revision = path.name.rsplit("__r", 1)
        revision = int(revision) + 1
        while (stage / path.parent / f"{slug}__r{revision:03d}").exists():
            revision += 1
        relative = (path.parent / f"{slug}__r{revision:03d}").as_posix()
        source = work / path.name
        shutil.copytree(old, source)
        candidate = Path(row.get("candidate_path", str(candidates / path.name)))
        for mode in ("source", "exact", "outer"):
            shutil.copytree(candidate / mode, source / "simready_usd" / "collision" / mode)
        files = [p.relative_to(source).as_posix() for p in (source / "simready_usd").rglob("*")
                 if p.is_file() and p.relative_to(source).as_posix() not in (native.ADAPTER, native.READINESS)]
        body["previous_package_digest"] = previous["package_digest"]
        body["collision_profiles"] = {
            "frame": "asset_local_with_explicit_rigid_body_frames",
            "source_package_digest": previous["package_digest"],
            "exact": "simready_usd/collision/exact/planning-geometry.json",
            "outer": "simready_usd/collision/outer/planning-geometry.json",
            "outer_hit_policy": "confirm_with_exact; never_accept_or_reject_from_outer_hit_alone",
            "qualification": "geometry_and_loader_checked; composed_task_not_run",
            "statistics": {m: row[m] for m in ("exact", "outer")},
        }
        body["entrypoints"].update(collision_exact=body["collision_profiles"]["exact"],
                                   collision_outer=body["collision_profiles"]["outer"])
        table = native.read(old / native.READINESS)
        table["groups"].append({"id": "planning_collision_profiles", "label": "规划碰撞配置", "items": [
            {"id": "profile_loading", "label": "局部几何与运行时加载", "status": "passed", "issue": ""},
            {"id": "composed_motion", "label": "组装后轨迹与接触资格", "status": "not_run", "issue": "资产入库不授予工站轨迹或动力学资格"}]})
        updated, mapping = export_package(source, stage / relative, body, table, files, thumbnail=None)
        assert sorted(x["sha256"] for x in body["usd_dependencies"]) == sorted(x["sha256"] for x in updated["usd_dependencies"])
        target = copy.deepcopy(old_target)
        target.update(package_path=relative, package_digest=native.digest(stage / relative / native.MANIFEST))
        for entry in target["entrypoints"].values():
            entry["path"] = mapping[entry["path"]]
            entry["sha256"] = native.digest(stage / relative / entry["path"])
        record["targets"] = [target, *record["targets"]]
        record["supplemental"] = {"metadata_path": relative + "/" + native.ADAPTER}
        index["packages"].append({"asset_id": body["asset_id"], "path": relative})
        changes.append({"asset_id": body["asset_id"], "previous": old_target, "target": target,
                        "visual_geometry_changed": False})
        print("PACKAGED", relative, flush=True)
    digest = rebuild_index(stage, index)
    result = {"status": "staged", "index_sha256": digest, "changes": changes}
    native.write(work / "packaging-report.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("library", "candidates", "stage", "work"):
        parser.add_argument("--" + key, required=True)
    package(**vars(parser.parse_args()))
