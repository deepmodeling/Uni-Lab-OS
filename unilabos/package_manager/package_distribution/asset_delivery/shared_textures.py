"""在同一导入包的多个模型目录之间共享字节相同的 PNG 贴图。

不同资产包各自带一份同样的贴图（资产库要求每包自洽），落到同一个领域包后会被前端重复
下载。这里把字节相同、出现不止一处的 PNG 收成 ``<导入包>/_shared_textures/<sha256>.png``
一份，改写引用它们的 USD 层，删掉被收走的副本。贴图字节不变（文件名即内容摘要，模型读取
接口按摘要放行并复核）；USD 只改贴图引用路径。

共享清单 ``_shared_textures/manifest.json`` 记录参与共享的模型目录、每个被改写的层的源摘要
和改写后摘要、每个被收走的副本的摘要，供 ``check_materialized`` 证明副本仍来自锁定包。
多个资产锁分次落盘时，后一次会连同清单里已有的模型目录一起重新共享。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

SHARED_TEXTURE_DIR = "_shared_textures"
MANIFEST = "manifest.json"
MANIFEST_SCHEMA = "unilab.shared-textures/1"
USD_SUFFIXES = {".usd", ".usda", ".usdc"}


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_manifest(shared_dir: Path) -> dict[str, Any]:
    path = shared_dir / MANIFEST
    if not path.is_file():
        return {"schema": MANIFEST_SCHEMA, "roots": [], "layers": {}, "pruned": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != MANIFEST_SCHEMA:
        raise ValueError(f"共享贴图清单格式不符：{path}")
    return data


def _rel(project: Path, path: Path) -> str:
    return path.relative_to(project).as_posix()


def share_textures(project: Path, import_package: str, roots: Iterable[PurePosixPath | str]) -> dict[str, Any]:
    """共享 ``roots``（及清单里已登记、仍存在的模型目录）里重复的 PNG。

    参数：``project`` 领域包项目根；``import_package`` 导入包名；``roots`` 项目相对模型目录。
    返回：统计 ``{rewritten_layers, pruned_png_files, shared_png_files, png_bytes_before, png_bytes_after}``。
    异常：USD 层打不开、贴图引用越界或缺失、共享目录里有内容与名字不符的文件时抛 ``ValueError``。
    """

    from pxr import Sdf, UsdUtils

    project = Path(project).resolve()
    shared_dir = project / import_package / SHARED_TEXTURE_DIR
    manifest = load_manifest(shared_dir)
    all_roots = sorted({str(PurePosixPath(r)) for r in roots} | set(manifest["roots"]))
    for root in all_roots:
        if PurePosixPath(root).parts[:1] != (import_package,):
            raise ValueError(f"模型目录不在导入包 {import_package} 内：{root}")
    live_roots = [root for root in all_roots if (project / root).is_dir()]

    layers: list[Path] = []
    pngs: dict[Path, str] = {}
    by_digest: dict[str, list[Path]] = defaultdict(list)
    for root in live_roots:
        for path in sorted((project / root).rglob("*")):
            if path.is_symlink():
                raise ValueError(f"模型目录里不允许符号链接：{_rel(project, path)}")
            if not path.is_file():
                continue
            suffix = path.suffix.lower()
            if suffix in USD_SUFFIXES:
                layers.append(path)
            elif suffix == ".png":
                pngs[path] = digest = _digest(path)
                by_digest[digest].append(path)
    existing = {}
    for png in shared_dir.glob("*.png"):
        if _digest(png) != png.stem:
            raise ValueError(f"共享贴图内容与文件名不符：{png.name}")
        existing[png.stem] = png
    png_bytes_before = sum(p.stat().st_size for p in pngs) + sum(p.stat().st_size for p in existing.values())
    shared_digests = {d for d, paths in by_digest.items() if len(paths) > 1 or d in existing}

    used: set[str] = set()
    pruned: set[Path] = set()
    rewritten = 0
    for layer_path in layers:
        layer = Sdf.Layer.OpenAsAnonymous(str(layer_path))
        if layer is None:
            raise ValueError(f"打不开 USD 层：{_rel(project, layer_path)}")
        changed = False

        def redirect(reference: str) -> str:
            nonlocal changed
            if not reference.lower().endswith(".png"):
                return reference
            target = (layer_path.parent / reference).resolve()
            if PurePosixPath(reference).is_absolute() or not target.is_relative_to(project) or not target.is_file():
                raise ValueError(f"{_rel(project, layer_path)} 的贴图引用越界或缺失：{reference}")
            if target.parent == shared_dir:
                used.add(target.stem)
                return reference
            digest = pngs.get(target)
            if digest is None or digest not in shared_digests:
                return reference
            shared = shared_dir / f"{digest}.png"
            if not shared.exists():
                shared_dir.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(target, shared)
                existing[digest] = shared
            used.add(digest)
            pruned.add(target)
            changed = True
            return Path(os.path.relpath(shared, layer_path.parent)).as_posix()

        UsdUtils.ModifyAssetPaths(layer, redirect)
        if not changed:
            continue
        before = _digest(layer_path)
        # usdc 原地保存是追加写；导出成新文件再替换。
        temporary = layer_path.with_name(layer_path.stem + ".__shared__" + layer_path.suffix)
        if not layer.Export(str(temporary)):
            raise ValueError(f"写不出改写后的 USD 层：{_rel(project, layer_path)}")
        os.replace(temporary, layer_path)
        rel = _rel(project, layer_path)
        previous = manifest["layers"].get(rel)
        source = previous["source_sha256"] if previous and previous["sha256"] == before else before
        manifest["layers"][rel] = {"source_sha256": source, "sha256": _digest(layer_path)}
        rewritten += 1

    for path in pruned:
        manifest["pruned"][_rel(project, path)] = pngs[path]
        path.unlink()
    for digest, png in list(existing.items()):
        if digest not in used:
            png.unlink()
            del existing[digest]
    manifest["roots"] = live_roots
    manifest["layers"] = {k: v for k, v in sorted(manifest["layers"].items()) if (project / k).is_file()}
    manifest["pruned"] = {k: v for k, v in sorted(manifest["pruned"].items())
                          if not (project / k).exists() and v in existing}
    if live_roots:  # 没有重复也登记模型目录，后续分次落盘的锁才能与它们共享
        shared_dir.mkdir(parents=True, exist_ok=True)
        (shared_dir / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    png_bytes_after = sum(p.stat().st_size for p in pngs if p not in pruned) + sum(
        p.stat().st_size for p in existing.values())
    return {
        "rewritten_layers": rewritten,
        "pruned_png_files": len(pruned),
        "shared_png_files": len(existing),
        "png_bytes_before": png_bytes_before,
        "png_bytes_after": png_bytes_after,
    }


__all__ = ["MANIFEST", "SHARED_TEXTURE_DIR", "load_manifest", "share_textures"]
