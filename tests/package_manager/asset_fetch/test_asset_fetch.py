"""资产拉取（Asset Fetch）：按锁从资产库存储取 NVIDIA 工程包并校验。"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from unilabos.package_manager.package_distribution.adapters.asset_store import LocalMirrorStore
from unilabos.package_manager.package_distribution.asset_fetch import (
    AssetFetchError,
    AssetIntegrityError,
    fetch_lock,
    fetch_package,
    verify_package,
)
from unilabos.package_manager.package_distribution.asset_fetch import native_package as native

ASSET_ID = "28f8c9b9-e89d-40da-bf12-2125d2a4fd11"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _make_package(root: Path, name: str = "转台__r002") -> tuple[Path, str]:
    """按资产库 finalize 规则生成一个最小合法工程包，返回 (包目录, package_digest)。"""

    package = root / name
    usd_rel = f"simready_usd/{name}.usda"
    usd = b"#usda 1.0\n"
    mesh = b"solid rotor\nendsolid\n"
    _write(package / usd_rel, usd)
    _write(package / "simready_usd/meshes/rotor.stl", mesh)
    closure = [{"path": "simready_usd/meshes/rotor.stl", "sha256": _sha(mesh)}]
    adapter = {
        "asset_id": ASSET_ID,
        "entrypoints": {"usd": usd_rel},
        "usd_dependencies": closure,
        "schema": "unilab.usd-adapter/1",
    }
    _write(package / native.ADAPTER, native.canonical(adapter))
    readiness = {"binding": {"source_usd_sha256": _sha(usd), "usd_closure_sha256": _sha(native.canonical(closure))}}
    _write(package / native.READINESS, native.canonical(readiness))
    items = [
        {"relative_path": path.relative_to(package).as_posix(), "size": path.stat().st_size, "hash": {"sha256": _sha(path.read_bytes())}}
        for path in sorted((package / "simready_usd").rglob("*"))
        if path.is_file()
    ]
    _write(package / native.BOM, native.canonical({"format_version": "1.0", "items": items}))
    _write(package / native.ROOTS, native.canonical({"format_version": "1.0", "entries": [usd_rel]}))
    manifest = {
        "format_version": "1.0",
        "package_id": ASSET_ID,
        "license": "NOASSERTION",
        "description": name,
        "content_hash": {"sha256": native.content_hash(items)},
        "metadata": [
            {"name": Path(rel).name, "hash": {"sha256": _sha((package / rel).read_bytes())}}
            for rel in (native.BOM, native.ROOTS)
        ],
    }
    manifest["package_hash"] = {"sha256": native.package_hash(manifest)}
    _write(package / native.MANIFEST, native.canonical(manifest))
    return package, _sha((package / native.MANIFEST).read_bytes())


def _mirror(tmp_path: Path) -> tuple[LocalMirrorStore, str, Path]:
    mirror = tmp_path / "mirror"
    rel = "04_设备/bioyond/转台__r002"
    package, digest = _make_package(mirror / "04_设备/bioyond")
    _write(
        mirror / "library.json",
        json.dumps(
            {"schema": "unilab.usd-library/1", "packages": [{"asset_id": ASSET_ID, "package_digest": digest, "path": rel}]},
            ensure_ascii=False,
        ).encode(),
    )
    lock = tmp_path / "assets.lock.json"
    lock.write_text(
        json.dumps(
            {
                "schema": "unilab.asset-dependencies/1",
                "package": "demo",
                "dependencies": [{"key": "turntable", "asset_id": ASSET_ID, "package_digest": digest}],
            }
        ),
        encoding="utf-8",
    )
    return LocalMirrorStore(mirror), digest, lock


def test_generated_package_passes_native_verification(tmp_path: Path) -> None:
    package, digest = _make_package(tmp_path)
    assert verify_package(package, digest)["asset_id"] == ASSET_ID


def test_fetch_lock_downloads_verifies_and_hits_cache(tmp_path: Path) -> None:
    store, digest, lock = _mirror(tmp_path)
    cache = tmp_path / "cache"
    first = fetch_lock(store, lock, cache)
    assert first["packages"][0]["action"] == "downloaded"
    folder = Path(first["packages"][0]["path"])
    assert folder.name == "转台__r002"
    assert verify_package(folder, digest)["status"] == "passed"
    assert not any((cache / ".staging").iterdir())
    second = fetch_lock(store, lock, cache)
    assert second["packages"][0]["action"] == "cache_hit"


def test_tampered_remote_file_is_rejected_without_partial_cache(tmp_path: Path) -> None:
    store, digest, _ = _mirror(tmp_path)
    remote = store.root / "04_设备/bioyond/转台__r002/simready_usd/meshes/rotor.stl"
    remote.write_bytes(b"tampered")
    cache = tmp_path / "cache"
    with pytest.raises((AssetFetchError, AssetIntegrityError)):
        fetch_package(store, ASSET_ID, digest, cache)
    assert not (cache / "04_设备").exists()


def test_corrupt_cache_is_reported_not_overwritten(tmp_path: Path) -> None:
    store, digest, _ = _mirror(tmp_path)
    cache = tmp_path / "cache"
    folder, _ = fetch_package(store, ASSET_ID, digest, cache)
    target = folder / "simready_usd/meshes/rotor.stl"
    target.chmod(0o644)
    target.write_bytes(b"changed")
    with pytest.raises(AssetIntegrityError):
        fetch_package(store, ASSET_ID, digest, cache)
    assert target.read_bytes() == b"changed"


def test_unpublished_pin_fails_instead_of_falling_back(tmp_path: Path) -> None:
    store, _, _ = _mirror(tmp_path)
    with pytest.raises(AssetFetchError, match="没有固定版本"):
        fetch_package(store, ASSET_ID, "0" * 64, tmp_path / "cache")


def test_lock_only_selects_keys_and_rejects_unknown(tmp_path: Path) -> None:
    store, _, lock = _mirror(tmp_path)
    assert len(fetch_lock(store, lock, tmp_path / "c", only=["turntable"])["packages"]) == 1
    with pytest.raises(AssetFetchError, match="没有这些键"):
        fetch_lock(store, lock, tmp_path / "c", only=["freezer"])


def test_path_escape_in_index_is_rejected(tmp_path: Path) -> None:
    store, digest, _ = _mirror(tmp_path)
    data = json.loads((store.root / "library.json").read_text(encoding="utf-8"))
    data["packages"][0]["path"] = "../outside"
    (store.root / "library.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(AssetIntegrityError):
        fetch_package(store, ASSET_ID, digest, tmp_path / "cache")
    shutil.rmtree(tmp_path / "cache", ignore_errors=True)


def _lock(path: Path, rows: list[dict]) -> Path:
    path.write_text(json.dumps({"schema": "unilab.asset-dependencies/1", "dependencies": rows}), encoding="utf-8")
    return path


def test_local_package_rows_need_no_store(tmp_path: Path) -> None:
    """锁行写本地工程包路径：不打开存储；给了摘要就校验，摘要不符即失败。"""

    package, digest = _make_package(tmp_path / "local")
    lock = _lock(tmp_path / "assets.lock.json", [
        {"key": "pinned", "path": "local/转台__r002", "package_digest": digest},
        {"key": "dev", "path": str(package)},
    ])

    def no_store():
        raise AssertionError("本地行不应打开存储")

    result = fetch_lock(no_store, lock, tmp_path / "cache")
    assert [(row["key"], row["action"]) for row in result["packages"]] == [("pinned", "local"), ("dev", "local")]
    assert result["packages"][0]["path"] == str(package.resolve())

    _lock(lock, [{"key": "pinned", "path": "local/转台__r002", "package_digest": "0" * 64}])
    with pytest.raises((AssetFetchError, AssetIntegrityError)):
        fetch_lock(None, lock, tmp_path / "cache")


def test_cached_packages_are_used_offline(tmp_path: Path) -> None:
    """缓存里已有的固定包离线可用；缓存缺包时离线模式明确失败，联网才下载。"""

    store, digest, _ = _mirror(tmp_path)
    lock = _lock(tmp_path / "assets.lock.json", [{"key": "turntable", "asset_id": ASSET_ID, "package_digest": digest}])
    cache = tmp_path / "cache"
    with pytest.raises(AssetFetchError, match="离线模式"):
        fetch_lock(None, lock, cache)
    assert fetch_lock(lambda: store, lock, cache)["packages"][0]["action"] == "downloaded"
    assert fetch_lock(None, lock, cache)["packages"][0]["action"] == "cache_hit"
