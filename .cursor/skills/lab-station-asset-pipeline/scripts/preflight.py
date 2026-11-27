#!/usr/bin/env python3
"""Read-only environment and optional source inventory; Python 3.10+."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import stat
import sys


SKIP_NAMES = {
    ".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__",
    ".aws", ".ssh", ".cache", ".pytest_cache", "secrets", "credentials",
}
PRIVATE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".kdbx"}
FORMAT_HINTS = {
    ".step": "step", ".stp": "step", ".sldasm": "solidworks", ".sldprt": "solidworks",
    ".urdf": "urdf", ".xacro": "xacro", ".glb": "glb", ".stl": "stl",
    ".gltf": "gltf", ".obj": "obj", ".fbx": "fbx",
    ".usd": "usd", ".usda": "usd", ".usdc": "usd", ".usdz": "usd",
}


def excluded(name: str) -> bool:
    lower = name.lower()
    return (
        lower in SKIP_NAMES or lower == ".env" or lower.startswith(".env.")
        or lower.startswith(("secrets.", "credentials.", "token."))
        or Path(lower).suffix in PRIVATE_SUFFIXES
    )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def step_header(path: Path) -> dict:
    """Read a bounded prefix only; recognizing this marker is not STEP parsing."""
    with path.open("rb") as stream:
        prefix = stream.read(4096)
    header = prefix.removeprefix(b"\xef\xbb\xbf").lstrip()
    return {
        "status": "recognized" if header.startswith(b"ISO-10303-21;") else "invalid_header",
        "signature": "ISO-10303-21;", "bytes_inspected": len(prefix), "scope": "header_only",
        "note": "Only the leading STEP exchange-file marker is checked; syntax, geometry, and completeness are not validated.",
    }


def inventory(source: Path, hashes: bool) -> dict:
    """List without following symlinks; excluded files are never opened."""
    source = Path(os.path.abspath(source.expanduser()))
    result = {"input": str(source), "sha256_requested": hashes, "entries": [], "errors": []}

    def visit(path: Path, relative: str) -> None:
        entry = {"path": relative}
        if excluded(path.name):
            entry["kind"] = "excluded"
            result["entries"].append(entry)
            return
        try:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                entry["kind"] = "symlink_not_followed"
            elif stat.S_ISDIR(info.st_mode):
                entry["kind"] = "directory"
                result["entries"].append(entry)
                for child in sorted(path.iterdir(), key=lambda item: item.name):
                    visit(child, child.name if relative == "." else relative + "/" + child.name)
                return
            elif stat.S_ISREG(info.st_mode):
                entry.update(kind="file", size=info.st_size, format_hint=FORMAT_HINTS.get(path.suffix.lower(), "unclassified"))
                if entry["format_hint"] == "step":
                    entry["format_check"] = step_header(path)
                    if entry["format_check"]["status"] == "invalid_header":
                        result["errors"].append({"path": relative, "error": "STEP/STP input lacks the leading ISO-10303-21; marker; header check only."})
                if hashes:
                    entry["sha256"] = file_sha256(path)
            else:
                entry["kind"] = "special_file_not_read"
            result["entries"].append(entry)
        except OSError as error:
            result["errors"].append({"path": relative, "error": str(error)})

    visit(source, "." if source.is_dir() and not source.is_symlink() else source.name)
    result["status"] = "failed" if result["errors"] else "listed"
    return result


def module_probe(name: str) -> dict:
    try:
        spec = importlib.util.find_spec(name)
        return {"found": spec is not None, "origin": None if spec is None else spec.origin}
    except (ImportError, ValueError, AttributeError) as error:
        return {"found": False, "error": str(error)}


def route_summary(inputs: list[dict], modules: dict, executables: dict, host_os: str, explicit_blender: dict | None = None) -> list[dict]:
    """Suggest existing tool routes without importing or running any backend."""
    counts = {}
    for source in inputs:
        for entry in source["entries"]:
            if entry.get("kind") == "file":
                hint = entry.get("format_hint", "unclassified")
                counts[hint] = counts.get(hint, 0) + 1
    routes = []
    if counts.get("step"):
        options = {
            "FreeCAD": "detected" if any(executables.get(name) for name in ("FreeCADCmd", "FreeCAD", "freecadcmd", "freecad")) else "missing",
            **{name: "detected" if modules.get(name, {}).get("found") else "missing" for name in ("OCP", "OCC", "cadquery")},
        }
        routes.append({"route": "step", "file_count": counts["step"], "status": "detected" if "detected" in options.values() else "missing", "tool_options": options,
                       "requirement": "One STEP import backend; these are alternatives, not all required.",
                       "next_step": "Confirm an installed backend can import the specific source. Header recognition does not validate STEP geometry."})
    if counts.get("solidworks"):
        routes.append({"route": "solidworks", "file_count": counts["solidworks"], "status": "needs_confirmation", "host_os": host_os,
                       "requirement": "Confirm a Windows host, SolidWorks installation/license, COM access, and complete assembly dependencies manually; none were started or tested."})
    if counts.get("urdf") or counts.get("xacro"):
        needs_xacro = bool(counts.get("xacro"))
        xacro_found = bool(executables.get("xacro") or modules.get("xacro", {}).get("found"))
        routes.append({"route": "urdf_xacro", "file_count": counts.get("urdf", 0) + counts.get("xacro", 0),
                       "status": "missing" if needs_xacro and not xacro_found else "needs_confirmation",
                       "tool_options": {"xml_reader": "python_standard_library", "xacro": "detected" if xacro_found else "missing" if needs_xacro else "not_required_for_plain_urdf"},
                       "next_step": "Expand xacro when present and resolve mesh/package references in the source environment; XML, joints, and dependencies have not been parsed."})
    if counts.get("glb") or counts.get("stl"):
        routes.append({"route": "meshy_retexture_candidate", "file_count": counts.get("glb", 0) + counts.get("stl", 0), "status": "needs_confirmation",
                       "accepted_formats": ["self-contained GLB v2", "STL"],
                       "next_step": "Use the bundled offline Meshy plan to check the input container. This inventory has not validated GLB/STL, read credentials, authorized upload, or submitted a task."})
    other_meshes = sum(counts.get(name, 0) for name in ("gltf", "obj", "fbx"))
    if other_meshes:
        blender_found = bool(executables.get("blender") or (explicit_blender and explicit_blender.get("is_file") and explicit_blender.get("executable")))
        routes.append({"route": "mesh_conversion", "file_count": other_meshes, "status": "detected" if blender_found else "missing", "tool_options": {"Blender": "detected" if blender_found else "missing"},
                       "next_step": "Resolve external materials/resources using the selected importer. The bundled Meshy client requires export to self-contained GLB or STL first."})
    if counts.get("usd"):
        routes.append({"route": "usd_readback", "file_count": counts["usd"], "status": "detected" if modules.get("pxr", {}).get("found") else "missing",
                       "tool_options": {"pxr": "detected" if modules.get("pxr", {}).get("found") else "missing"},
                       "next_step": "Run bundle_check.py verify --usd in an OpenUSD environment for dependency resolution and Stage readback."})
    return routes


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, action="append", default=[], help="Optional file/directory to inventory; repeatable")
    parser.add_argument("--sha256", action="store_true", help="Hash listed regular files (may read large assets)")
    parser.add_argument("--blender", type=Path, help="Optional Blender executable path to inspect without starting it")
    parser.add_argument("--output", type=Path, help="Save the same JSON report; this is the only file written")
    args = parser.parse_args(argv)
    inputs = [inventory(path, args.sha256) for path in args.input]
    report = {
        "schema": "lab-station-preflight/v1",
        "status": "failed" if any(item["errors"] for item in inputs) else "reported",
        "system": {"os": platform.system(), "release": platform.release(), "machine": platform.machine()},
        "python": {"executable": sys.executable, "version": platform.python_version(), "supported": sys.version_info >= (3, 10)},
        "executables": {name: shutil.which(name) for name in ("blender", "FreeCADCmd", "FreeCAD", "freecadcmd", "freecad", "xacro", "usdchecker", "usdcat", "git")},
        "modules": {name: module_probe(name) for name in ("pxr", "OCP", "OCC", "cadquery", "xacro", "trimesh", "numpy", "PIL")},
        "tool_discovery_scope": "PATH executables and current-interpreter module specs only; missing does not prove software is absent from the machine.",
        "inputs": inputs,
        "scope": "Inventory, module specs, and STEP header sniff only. Routes describe tool discovery, not source acceptance. No GUI, probed-module imports, installation, conversion, or runtime qualification.",
    }
    if args.blender:
        candidate = args.blender.expanduser().absolute()
        report["explicit_blender"] = {"path": str(candidate), "is_file": candidate.is_file(), "executable": os.access(candidate, os.X_OK)}
    report["routes"] = route_summary(inputs, report["modules"], report["executables"], report["system"]["os"], report.get("explicit_blender"))
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        output = args.output.expanduser().absolute()
        if output.is_symlink() or excluded(output.name):
            parser.error("--output must not be a symlink or a private/excluded filename")
        if output.exists():
            parser.error("--output already exists; choose a new report path")
        for source in args.input:
            source = source.expanduser().resolve()
            if output.resolve() == source or (source.is_dir() and output.resolve().is_relative_to(source)):
                parser.error("--output must be outside every inventoried input")
        try:
            with output.open("x", encoding="utf-8") as stream:
                stream.write(rendered)
        except OSError as error:
            parser.error(str(error))
    sys.stdout.write(rendered)
    return 1 if report["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
