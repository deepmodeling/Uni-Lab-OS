"""把已拉取校验的固定工程包落到领域包的模型目录，并在模型之间共享相同贴图。

锁行的 ``model_dir``（导入包内的模型目录，例如 ``pylabrobot_unilab/devices/x/models/y``）
决定落盘位置：``<项目>/<model_dir>/usd/<model_subdir>``。``web`` 档取包里的前端轻量档
（``simready_usd/profiles/web``，入口 ``web_profile``），没有就回落完整空间入口；
``spatial`` 档总取完整入口。拷贝逐字节，不改任何文件；共享贴图见 ``shared_textures``。
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .shared_textures import SHARED_TEXTURE_DIR, load_manifest, share_textures

WEB_PROFILE = "simready_usd/profiles/web"
SKIP_SUFFIXES = {".blend", ".blend1"}
PROFILES = ("web", "spatial")


class AssetDeliveryError(ValueError):
    """锁行、包内交付文件或已落盘副本不满足交付合同。"""


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _web_profile_files(package: Path, adapter: dict[str, Any]) -> dict[str, Path] | None:
    """前端轻量档自己的闭包，键与完整入口的交付布局一致。"""

    entry = adapter.get("entrypoints", {}).get("web_profile")
    closure_path = package / WEB_PROFILE / "dependency_closure.json"
    if not entry or not closure_path.is_file():
        return None
    closure = json.loads(closure_path.read_text(encoding="utf-8"))
    if closure.get("usd_sha256") != _digest(package / entry):
        raise AssetDeliveryError(f"{package.name}: 轻量档依赖闭包与入口不符")
    base = PurePosixPath(WEB_PROFILE, "usd")
    files: dict[str, Path] = {}
    for rel in sorted({entry, *closure.get("layers", []), *closure.get("asset_paths", [])}):
        source = package / rel
        if not source.is_file():
            raise AssetDeliveryError(f"{package.name}: 轻量档缺文件 {rel}")
        files[PurePosixPath(rel).relative_to(base).as_posix()] = source
    # 设备类的模型入口跟完整入口的文件名。后来的修订只给完整入口改了名、轻量档按字节沿用时，
    # 落盘让轻量档的根文件用完整入口的名字，类就不用改。
    web_root = PurePosixPath(entry).relative_to(base).as_posix()
    spatial_root = PurePosixPath(adapter["entrypoints"]["usd"]).relative_to("simready_usd").as_posix()
    if web_root != spatial_root:
        if spatial_root in files:
            raise AssetDeliveryError(f"{package.name}: 轻量档里已有与完整入口同名的文件")
        files[spatial_root] = files.pop(web_root)
    return files


def delivery_files(package: Path, profile: str = "web") -> dict[str, Path]:
    """工程包的 USD 交付闭包：``{落盘相对路径: 包内源文件}``。"""

    if profile not in PROFILES:
        raise AssetDeliveryError(f"未知交付档：{profile}")
    adapter = json.loads((package / "simready_usd/unilab.json").read_text(encoding="utf-8"))
    if profile == "web":
        files = _web_profile_files(package, adapter)
        if files is not None:
            return files
    wanted = {adapter["entrypoints"]["usd"]} | {row["path"] for row in adapter.get("usd_dependencies", [])}
    files = {}
    for rel in sorted(wanted):
        source = package / rel
        if source.suffix.lower() in SKIP_SUFFIXES or not source.is_file():
            continue
        files[PurePosixPath(rel).relative_to("simready_usd").as_posix()] = source
    return files


def _model_root(row: dict[str, Any]) -> PurePosixPath:
    model_dir = PurePosixPath(row["model_dir"])
    if model_dir.is_absolute() or any(part in ("", ".", "..") for part in model_dir.parts) or "\\" in row["model_dir"]:
        raise AssetDeliveryError(f"{row.get('key')}: model_dir 必须是导入包内的 POSIX 相对路径")
    return model_dir / "usd"


def _targets(rows: Iterable[dict[str, Any]], packages: Iterable[dict[str, Any]]):
    by_key = {row.get("key") or row.get("owner_registry_id") or row.get("asset_id") or row.get("path"): row
              for row in rows}
    for fetched in packages:
        row = by_key[fetched["key"]]
        if not row.get("model_dir"):
            continue  # 锁里没有对应模型目录的包（例如耗材角色）只拉取校验
        root = _model_root(row)
        subdir = row.get("model_subdir", "")
        if subdir and any(part in ("", ".", "..") for part in PurePosixPath(subdir).parts):
            raise AssetDeliveryError(f"{fetched['key']}: model_subdir 非法")
        yield fetched["key"], row, Path(fetched["path"]), root, root / subdir if subdir else root


def _import_package(roots: Iterable[PurePosixPath]) -> str:
    names = {root.parts[0] for root in roots}
    if len(names) != 1:
        raise AssetDeliveryError("锁里的模型目录必须同属一个导入包：" + ", ".join(sorted(names)))
    return names.pop()


def materialize_lock(
    rows: list[dict[str, Any]],
    packages: list[dict[str, Any]],
    project: Path,
    *,
    profile: str = "web",
    share: bool = True,
) -> dict[str, Any]:
    """把 ``fetch_lock`` 拉到的包按锁行落到 ``project`` 的模型目录。

    参数：``rows`` 是资产锁行，``packages`` 是 ``fetch_lock`` 回执里的包，``project`` 是领域包
    项目根。``share`` 为真时落盘后在导入包内所有已共享过的和本次的模型目录间共享相同 PNG。
    返回：``{"status", "packages": [...], "shared_textures": {...}}``。
    """

    project = Path(project).resolve()
    receipts, roots = [], set()
    for key, row, package, root, target_rel in _targets(rows, packages):
        files = delivery_files(package, profile)
        target = project / target_rel
        if target.exists():
            shutil.rmtree(target)
        for rel, source in files.items():
            destination = target / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        roots.add(root)
        receipts.append({"key": key, "target": target_rel.as_posix(), "files": len(files),
                         "bytes": sum(p.stat().st_size for p in files.values())})
    result: dict[str, Any] = {"status": "passed", "profile": profile, "packages": receipts}
    if share and roots:
        result["shared_textures"] = share_textures(project, _import_package(roots), sorted(roots))
    return result


def check_materialized(
    rows: list[dict[str, Any]],
    packages: list[dict[str, Any]],
    project: Path,
    *,
    profile: str = "web",
) -> list[str]:
    """核对已落盘副本仍是锁定包的交付文件。返回问题列表（空即通过）。

    共享过的副本按共享清单核对：被收走的 PNG 必须在共享目录里有同摘要的文件；被改写引用的
    USD 层必须由同一源文件改写而来、且当前字节与清单记录一致。
    """

    project = Path(project).resolve()
    problems: list[str] = []
    targets = list(_targets(rows, packages))
    if not targets:
        return problems
    shared_dir = project / _import_package(root for *_, root, _ in targets) / SHARED_TEXTURE_DIR
    manifest = load_manifest(shared_dir)
    for png in shared_dir.glob("*.png"):
        if _digest(png) != png.stem:
            problems.append(f"共享贴图内容与文件名不符：{png.name}")
    for key, _row, package, _root, target_rel in targets:
        for rel, source in delivery_files(package, profile).items():
            copy_rel = (target_rel / rel).as_posix()
            copy = project / copy_rel
            expected = _digest(source)
            if copy.is_file():
                actual = _digest(copy)
                if actual == expected:
                    continue
                layer = manifest["layers"].get(copy_rel)
                if layer and layer["source_sha256"] == expected and layer["sha256"] == actual:
                    continue
            elif manifest["pruned"].get(copy_rel) == expected and (shared_dir / f"{expected}.png").is_file():
                continue
            problems.append(f"{key}: {copy_rel} 缺失或与锁定包不符")
    return problems


__all__ = [
    "AssetDeliveryError",
    "PROFILES",
    "check_materialized",
    "delivery_files",
    "materialize_lock",
]
