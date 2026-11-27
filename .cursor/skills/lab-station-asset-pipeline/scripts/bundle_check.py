#!/usr/bin/env python3
"""Seal/verify file integrity and optionally read USD dependencies; Python 3.10+."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys


SCHEMA = "lab-station-bundle-sha256/v1"
DEFAULT_MANIFEST = "bundle.sha256.json"
FORBIDDEN_NAMES = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache",
    ".aws", ".ssh", "secrets", "secret", "credentials", "token", "tokens",
    "cache", "caches",
}
FORBIDDEN_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".kdbx", ".sqlite", ".sqlite3", ".db", ".pid", ".sock", ".pyc"}
USD_SUFFIXES = {".usd", ".usda", ".usdc", ".usdz"}


def forbidden(name: str) -> bool:
    lower = name.lower()
    return (
        lower in FORBIDDEN_NAMES or lower == ".env" or lower.startswith(".env.")
        or lower.startswith(("secrets.", "credentials.", "token.", "private-key."))
        or Path(lower).suffix in FORBIDDEN_SUFFIXES
    )


def safe_relative(value: object) -> bool:
    return isinstance(value, str) and bool(value) and not any(char in value for char in ("\\", ":", "\x00")) and all(part not in ("", ".", "..") for part in value.split("/"))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def inventory(root: Path, manifest_name: str) -> tuple[dict, list[str]]:
    files, errors = {}, []

    def visit(directory: Path) -> None:
        try:
            children = sorted(directory.iterdir(), key=lambda path: path.name)
        except OSError as error:
            errors.append(f"Cannot list {directory.relative_to(root)}: {error}")
            return
        for path in children:
            relative = path.relative_to(root).as_posix()
            try:
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode):
                    errors.append(f"Symlink forbidden: {relative}")
                    continue
                if forbidden(path.name):
                    errors.append(f"Private/runtime item forbidden: {relative}")
                    continue
                if not safe_relative(relative):
                    errors.append(f"Nonportable path forbidden: {relative!r}")
                    continue
                if relative == manifest_name:
                    if not stat.S_ISREG(info.st_mode):
                        errors.append(f"Manifest must be a regular file: {relative}")
                    continue
                if stat.S_ISDIR(info.st_mode):
                    visit(path)
                elif stat.S_ISREG(info.st_mode):
                    files[relative] = {"path": relative, "size": info.st_size, "sha256": file_sha256(path)}
                else:
                    errors.append(f"Special file forbidden: {relative}")
            except OSError as error:
                errors.append(f"Cannot read {relative}: {error}")

    visit(root)
    return files, errors


def read_manifest(path: Path, manifest_name: str) -> tuple[dict, list[str]]:
    errors, files = [], {}
    if path.is_symlink():
        return {}, ["Manifest must not be a symlink"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        return {}, [f"Cannot read manifest: {error}"]
    if not isinstance(data, dict) or data.get("schema") != SCHEMA or not isinstance(data.get("files"), list):
        return {}, [f"Expected {SCHEMA} manifest with a files array"]
    for item in data["files"]:
        if not isinstance(item, dict):
            errors.append("Manifest file entry must be an object")
            continue
        relative = item.get("path")
        if not safe_relative(relative):
            errors.append(f"Unsafe manifest path: {relative!r}")
            continue
        if relative == manifest_name or any(forbidden(part) for part in relative.split("/")):
            errors.append(f"Forbidden manifest path: {relative}")
            continue
        if relative in files:
            errors.append(f"Duplicate manifest path: {relative}")
            continue
        size, digest = item.get("size"), item.get("sha256")
        if type(size) is not int or size < 0 or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            errors.append(f"Invalid size/hash for {relative}")
            continue
        files[relative] = item
    return files, errors


def inspect_usd(root: Path, filenames: list[str], requested_entries: list[str]) -> dict:
    entries = requested_entries or [name for name in filenames if Path(name).suffix.lower() in USD_SUFFIXES]
    errors = []
    for entry in entries:
        if not safe_relative(entry) or entry not in filenames or Path(entry).suffix.lower() not in USD_SUFFIXES:
            errors.append(f"USD entry must be a listed relative USD file: {entry!r}")
    if errors:
        return {"status": "failed", "errors": errors, "entries": []}
    if not entries:
        return {"status": "not_run", "reason": "No USD entries found", "entries": []}
    try:
        from pxr import Sdf, Usd, UsdUtils
    except ImportError as error:
        return {"status": "not_run", "reason": f"OpenUSD pxr unavailable: {error}", "entries": entries}
    results = []
    for relative in entries:
        entry = root / relative
        result = {"entry": relative, "stage_opened": False, "dependencies": [], "errors": []}
        try:
            layers, assets, unresolved = UsdUtils.ComputeAllDependencies(Sdf.AssetPath(str(entry)))
            for unresolved_path in unresolved:
                result["errors"].append(f"Unresolved dependency: {unresolved_path}")
            dependencies = [str(layer.realPath or layer.identifier) for layer in layers] + [str(asset) for asset in assets]
            for dependency in sorted(set(dependencies)):
                # A package-relative asset is carried by its outer .usdz file.
                outer_path = dependency.split("[", 1)[0]
                if not outer_path or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", outer_path) or outer_path.startswith("anon:"):
                    result["errors"].append(f"Non-filesystem dependency not qualified: {dependency}")
                    continue
                candidate = Path(outer_path)
                if not candidate.is_absolute():
                    candidate = entry.parent / candidate
                resolved = candidate.resolve()
                if not resolved.is_relative_to(root):
                    result["errors"].append(f"Dependency outside bundle: {dependency}")
                elif not resolved.is_file():
                    result["errors"].append(f"Missing dependency file: {dependency}")
                elif resolved.relative_to(root).as_posix() not in filenames:
                    result["errors"].append(f"Dependency not listed in manifest: {dependency}")
                else:
                    result["dependencies"].append(resolved.relative_to(root).as_posix())
            stage = Usd.Stage.Open(str(entry), load=Usd.Stage.LoadAll)
            result["stage_opened"] = stage is not None
            if stage is None:
                result["errors"].append("Usd.Stage.Open returned no stage")
            else:
                result["prim_count"] = sum(1 for _ in stage.Traverse())
        except Exception as error:
            result["errors"].append(f"USD readback failed: {type(error).__name__}: {error}")
        result["dependencies"] = sorted(set(result["dependencies"]))
        result["status"] = "failed" if result["errors"] else "passed"
        results.append(result)
    return {"status": "failed" if any(item["errors"] for item in results) else "passed", "entries": results, "scope": "OpenUSD dependency resolution and Stage readback; no rendering, physics, or task qualification."}


def check_bundle(operation: str, bundle: Path, manifest_name: str = DEFAULT_MANIFEST, replace: bool = False, usd: bool = False, usd_entries: list[str] | None = None) -> dict:
    report = {"schema": "lab-station-bundle-check/v1", "operation": operation, "bundle": str(bundle.absolute()), "manifest": manifest_name, "status": "failed", "integrity": {"status": "failed", "errors": []}, "usd": {"status": "not_requested"}}
    errors = report["integrity"]["errors"]
    if operation not in ("seal", "verify"):
        errors.append("Operation must be seal or verify")
        return report
    if not safe_relative(manifest_name) or "/" in manifest_name or forbidden(manifest_name):
        errors.append("Manifest name must be a safe root-level filename")
        return report
    if bundle.is_symlink() or not bundle.is_dir():
        errors.append("Bundle must be an existing directory, not a symlink")
        return report
    root = bundle.resolve()
    report["bundle"] = str(root)
    manifest = root / manifest_name
    actual, scan_errors = inventory(root, manifest_name)
    errors.extend(scan_errors)
    if operation == "seal":
        if manifest.exists() and not replace:
            errors.append("Manifest already exists; use --replace only for an intentional new seal")
        if not errors:
            data = {"schema": SCHEMA, "created_utc": datetime.now(timezone.utc).isoformat(), "files": [actual[name] for name in sorted(actual)], "scope": "File integrity only; USD dependency closure requires a separate --usd check."}
            try:
                with manifest.open("w" if replace else "x", encoding="utf-8") as stream:
                    json.dump(data, stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
                report["manifest_written"] = True
            except OSError as error:
                errors.append(f"Cannot write manifest: {error}")
    else:
        expected, manifest_errors = read_manifest(manifest, manifest_name)
        errors.extend(manifest_errors)
        for name in sorted(set(expected) - set(actual)):
            errors.append(f"Missing file: {name}")
        for name in sorted(set(actual) - set(expected)):
            errors.append(f"Extra file: {name}")
        for name in sorted(set(actual) & set(expected)):
            if actual[name]["sha256"] != expected[name]["sha256"] or actual[name]["size"] != expected[name]["size"]:
                errors.append(f"Hash/size mismatch: {name}")
    report["integrity"].update(status="failed" if errors else "passed", file_count=len(actual))
    if usd:
        report["usd"] = {"status": "not_run", "reason": "File integrity checks failed"} if errors else inspect_usd(root, sorted(actual), usd_entries or [])
    report["status"] = "failed" if errors or report["usd"]["status"] == "failed" else "incomplete" if report["usd"]["status"] == "not_run" else "passed"
    return report


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("seal", "verify"))
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST, help="Root-level manifest filename")
    parser.add_argument("--replace", action="store_true", help="Intentionally replace an existing seal")
    parser.add_argument("--usd", action="store_true", help="Run optional OpenUSD dependency resolution and Stage readback")
    parser.add_argument("--usd-entry", action="append", default=[], help="Relative USD entry; repeatable, otherwise checks all USD files")
    args = parser.parse_args(argv)
    if args.usd_entry and not args.usd:
        parser.error("--usd-entry requires --usd")
    if args.replace and args.operation != "seal":
        parser.error("--replace applies only to seal")
    report = check_bundle(args.operation, args.bundle.expanduser(), args.manifest, args.replace, args.usd, args.usd_entry)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return {"passed": 0, "failed": 1, "incomplete": 2}[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
