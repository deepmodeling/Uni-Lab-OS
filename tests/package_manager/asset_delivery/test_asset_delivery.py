"""资产交付：按锁落盘模型交付，并在模型目录之间共享字节相同的贴图。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from unilabos.package_manager.package_distribution.asset_delivery import (
    SHARED_TEXTURE_DIR,
    check_materialized,
    load_manifest,
    materialize_lock,
)

Sdf = pytest.importorskip("pxr.Sdf")

PACKAGE = "demo_pkg"
SAME = b"same PNG bytes"


def _layer(path: Path, reference: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#usda 1.0\ndef Shader "Texture" {{\n    asset inputs:file = @{reference}@\n}}\n', encoding="utf-8")


def _package(root: Path, name: str, layer_rel: str, texture_rel: str, texture: bytes, web: bool) -> Path:
    """最小工程包：完整入口 + 一张贴图；``web`` 时另带同布局的轻量档。"""

    package = root / name
    simready = package / "simready_usd"
    entry = f"simready_usd/{name}.usda"
    _layer(package / entry, "./" + texture_rel)
    (simready / texture_rel).parent.mkdir(parents=True, exist_ok=True)
    (simready / texture_rel).write_bytes(texture)
    adapter = {"entrypoints": {"usd": entry}, "usd_dependencies": [{"path": f"simready_usd/{texture_rel}"}]}
    if web:
        base = simready / "profiles/web/usd"
        _layer(base / f"{name}.usda", "./" + layer_rel)
        (base / layer_rel).parent.mkdir(parents=True, exist_ok=True)
        (base / layer_rel).write_bytes(texture)
        web_entry = f"simready_usd/profiles/web/usd/{name}.usda"
        adapter["entrypoints"]["web_profile"] = web_entry
        (simready / "profiles/web/dependency_closure.json").write_text(json.dumps({
            "usd_sha256": hashlib.sha256((package / web_entry).read_bytes()).hexdigest(),
            "layers": [], "asset_paths": [f"simready_usd/profiles/web/usd/{layer_rel}"],
        }))
    (simready / "unilab.json").write_text(json.dumps(adapter))
    return package


def _rows(*keys: str) -> list[dict]:
    return [{"key": key, "asset_id": key, "package_digest": "0" * 64, "model_dir": f"{PACKAGE}/models/{key}"} for key in keys]


def _fetched(packages: dict[str, Path]) -> list[dict]:
    return [{"key": key, "path": str(path)} for key, path in packages.items()]


def _authored(layer: Path) -> str:
    opened = Sdf.Layer.OpenAsAnonymous(str(layer))
    attribute = opened.GetAttributeAtPath("/Texture.inputs:file")
    return attribute.default.path


def test_materialize_shares_identical_textures_across_models_and_locks(tmp_path: Path) -> None:
    """两个锁分次落盘：相同贴图只留一份共享文件，引用改写，副本核对通过。"""

    cache, project = tmp_path / "cache", tmp_path / "project"
    a = _package(cache, "A", "textures/a.png", "textures/a.png", SAME, web=True)
    b = _package(cache, "B", "dependencies/deep/b.png", "b.png", SAME, web=True)
    c = _package(cache, "C", "unique.png", "unique.png", b"another PNG", web=False)
    shared = project / PACKAGE / SHARED_TEXTURE_DIR / (hashlib.sha256(SAME).hexdigest() + ".png")

    first = materialize_lock(_rows("A", "C"), _fetched({"A": a, "C": c}), project)
    assert first["shared_textures"]["pruned_png_files"] == 0  # 只有一份时不共享
    assert not shared.exists()

    second = materialize_lock(_rows("B"), _fetched({"B": b}), project)
    assert second["shared_textures"] == {**second["shared_textures"], "rewritten_layers": 2, "pruned_png_files": 2,
                                         "shared_png_files": 1}
    assert shared.read_bytes() == SAME
    for layer in (project / PACKAGE / "models/A/usd/A.usda", project / PACKAGE / "models/B/usd/B.usda"):
        assert (layer.parent / _authored(layer)).resolve() == shared
    assert not (project / PACKAGE / "models/A/usd/textures/a.png").exists()
    assert (project / PACKAGE / "models/C/usd/unique.png").read_bytes() == b"another PNG"
    assert sorted(load_manifest(shared.parent)["roots"]) == [f"{PACKAGE}/models/{k}/usd" for k in "ABC"]

    assert check_materialized(_rows("A", "C"), _fetched({"A": a, "C": c}), project) == []
    assert check_materialized(_rows("B"), _fetched({"B": b}), project) == []

    # 只重新落 A：A 回到原件后再次共享，B 的引用和共享文件保持；结果与一次性落盘一致。
    materialize_lock(_rows("A"), _fetched({"A": a}), project)
    assert (project / PACKAGE / "models/A/usd/A.usda").parent.joinpath(
        _authored(project / PACKAGE / "models/A/usd/A.usda")).resolve() == shared
    assert check_materialized(_rows("A", "B"), _fetched({"A": a, "B": b}), project) == []

    # 被篡改的共享贴图与改写过的层都要被核对出来。
    shared.chmod(0o644)
    shared.write_bytes(b"tampered")
    problems = check_materialized(_rows("A", "B"), _fetched({"A": a, "B": b}), project)
    assert any("共享贴图内容与文件名不符" in p for p in problems)


def test_unshared_texture_is_removed_once_no_layer_references_it(tmp_path: Path) -> None:
    """重新落盘后不再重复的贴图：共享文件没有引用就删掉，副本回到模型目录。"""

    cache, project = tmp_path / "cache", tmp_path / "project"
    a = _package(cache, "A", "a.png", "a.png", SAME, web=False)
    b = _package(cache, "B", "b.png", "b.png", SAME, web=False)
    materialize_lock(_rows("A", "B"), _fetched({"A": a, "B": b}), project)
    shared_dir = project / PACKAGE / SHARED_TEXTURE_DIR
    assert len(list(shared_dir.glob("*.png"))) == 1

    b2 = _package(cache / "v2", "B", "b.png", "b.png", b"new B texture", web=False)
    stats = materialize_lock(_rows("B"), _fetched({"B": b2}), project)["shared_textures"]
    # A 仍引用共享文件，所以保留；B 的新贴图独有，不共享。
    assert stats["shared_png_files"] == 1
    assert (project / PACKAGE / "models/B/usd/b.png").read_bytes() == b"new B texture"
    assert check_materialized(_rows("A", "B"), _fetched({"A": a, "B": b2}), project) == []


def test_missing_texture_reference_fails_closed(tmp_path: Path) -> None:
    cache, project = tmp_path / "cache", tmp_path / "project"
    a = _package(cache, "A", "a.png", "a.png", SAME, web=False)
    b = _package(cache, "B", "b.png", "b.png", SAME, web=False)
    (b / "simready_usd/b.png").unlink()
    with pytest.raises(ValueError, match="贴图引用越界或缺失"):
        materialize_lock(_rows("A", "B"), _fetched({"A": a, "B": b}), project)


def test_share_command_covers_models_kept_in_the_package(tmp_path: Path, capsys) -> None:
    """不走资产库、直接放在领域包里的模型：``assets share --root`` 同样共享相同贴图。"""

    from unilabos.package_manager.cli import _cmd_assets_share

    project = tmp_path / "project"
    for name in ("left", "right"):
        _layer(project / PACKAGE / f"models/{name}/usd/{name}.usda", f"./tex/{name}.png")
        (project / PACKAGE / f"models/{name}/usd/tex").mkdir(parents=True, exist_ok=True)
        (project / PACKAGE / f"models/{name}/usd/tex/{name}.png").write_bytes(SAME)

    _cmd_assets_share({"project": str(project), "root": [f"{PACKAGE}/models/left/usd", f"{PACKAGE}/models/right/usd"]})
    stats = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert stats["pruned_png_files"] == 2 and stats["shared_png_files"] == 1
    shared = project / PACKAGE / SHARED_TEXTURE_DIR / (hashlib.sha256(SAME).hexdigest() + ".png")
    layer = project / PACKAGE / "models/left/usd/left.usda"
    assert (layer.parent / _authored(layer)).resolve() == shared
