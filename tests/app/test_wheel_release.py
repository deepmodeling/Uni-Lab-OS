"""配套 wheel 的来源、离线解析、安装安全及 Conda 一致性。"""

from __future__ import annotations

import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tomllib
from zipfile import ZipFile

import pytest
import yaml

from scripts import build_wheel_release as builder
from scripts import install_wheel_release as installer

ROOT = Path(__file__).resolve().parents[2]


def fake_wheel(directory: Path, name="unilabos", version="0.12.4", dependencies=()) -> Path:
    path = directory / f"{name}-{version}-py3-none-any.whl"
    with ZipFile(path, "w") as wheel:
        metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
        metadata += "".join(f"Requires-Dist: {dependency}\n" for dependency in dependencies)
        wheel.writestr(f"{name}-{version}.dist-info/METADATA", metadata)
    return path


def test_downstream_versions_sources_and_patches_match_conda():
    specs = tomllib.loads((ROOT / "recipes/wheels.toml").read_text(encoding="utf-8"))["packages"]
    requirements = (ROOT / "unilabos/utils/requirements.txt").read_text(encoding="utf-8")
    for name, spec in specs.items():
        assert f"{name}=={spec['version']}" in requirements
        assert len(spec["sha256"]) == 64
        assert all((ROOT / patch).is_file() for patch in spec["patches"])
    ot = yaml.safe_load((ROOT / "recipes/opentrons-shared-data/recipe.yaml").read_text(encoding="utf-8"))
    assert specs["opentrons-shared-data"]["version"] == ot["package"]["version"]
    for key in ("url", "sha256"):
        assert specs["opentrons-shared-data"][key] == ot["source"][key]
    assert specs["opentrons-shared-data"]["patches"] == [
        f"recipes/opentrons-shared-data/{patch}" for patch in ot["source"]["patches"]]
    plr = yaml.safe_load((ROOT / ".conda/pylabrobot/recipe.yaml").read_text(encoding="utf-8"))
    assert specs["pylabrobot"]["version"] == plr["package"]["version"]
    assert plr["source"]["rev"] in specs["pylabrobot"]["url"]
    assert specs["pylabrobot"]["patches"] == [f".conda/pylabrobot/{p}" for p in plr["source"]["patches"]]
    assert specs["pylabrobot"]["requires"] == [f"opentrons-shared-data=={ot['package']['version']}"]
    assert (f"opentrons-shared-data =={ot['package']['version']} {ot['build']['string']}"
            in plr["requirements"]["run"])
    assert " @ " not in requirements


@pytest.mark.parametrize("dependencies", [
    [],
    ["opentrons-shared-data>=9.1"],
    ['opentrons-shared-data==9.1.0+unilabos.np2.1; extra == "opentrons"'],
    ["opentrons-shared-data==9.1.0"],
])
def test_plr_wheel_requires_numpy2_opentrons_unconditionally(tmp_path, dependencies):
    spec = {"version": "0.2.2", "requires": ["opentrons-shared-data==9.1.0+unilabos.np2.1"]}
    wheel = fake_wheel(tmp_path, "pylabrobot", spec["version"], dependencies)
    with pytest.raises(RuntimeError, match="直接依赖"):
        builder.verify_companion_metadata(wheel, "pylabrobot", spec)


def test_companion_dependency_metadata_accepts_exact_pin(tmp_path):
    spec = {"version": "0.2.2", "requires": ["opentrons-shared-data==9.1.0+unilabos.np2.1"]}
    wheel = fake_wheel(tmp_path, "pylabrobot", spec["version"], spec["requires"])
    builder.verify_companion_metadata(wheel, "pylabrobot", spec)


@pytest.mark.parametrize("materials_only", [True, False])
def test_single_root_install_does_not_preinstall_dependencies(monkeypatch, tmp_path, materials_only):
    calls = []

    def record(*command, **kwargs):
        assert kwargs["cwd"] == tmp_path
        assert command[1] == "-I"
        calls.append(command)

    monkeypatch.setattr(builder, "run", record)
    root = "pylabrobot==0.2.2" if materials_only else "unilabos==0.12.4"
    builder.verify_single_root(tmp_path / "wheels", tmp_path / "env", tmp_path / "verify.py",
                               root, materials_only=materials_only)
    assert "venv" in calls[0]
    assert calls[1][-1] == root
    assert {"--no-index", "--only-binary=:all:", "--isolated"} <= set(calls[1])
    assert "--no-deps" not in calls[1] and "-r" not in calls[1]
    assert calls[2][-1] == "check"
    assert "--assert-no-ros" in calls[3]
    assert ("--materials-only" in calls[3]) is materials_only
    assert len(calls) == (4 if materials_only else 5)
    if not materials_only:
        assert calls[4][-1] == "--help"


def test_single_root_preserves_existing_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(builder, "run", lambda *a, **k: pytest.fail("不能复用已有验证环境"))
    with pytest.raises(RuntimeError, match="新目录"):
        builder.verify_single_root(tmp_path / "wheels", tmp_path, tmp_path / "verify.py", "pylabrobot")


@pytest.mark.parametrize("catalog_ok", [True, False])
def test_materials_only_verification_does_not_require_unilabos(monkeypatch, catalog_ok):
    from scripts import verify_installation as verifier

    monkeypatch.setattr(sys, "argv", ["verify_installation.py", "--materials-only", "--assert-no-ros"])
    monkeypatch.setattr(verifier, "check_python_version", lambda: True)
    monkeypatch.setattr(verifier.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(verifier, "check_package", lambda *a: pytest.fail("只装 PLR 不应检查微后端"))
    monkeypatch.setattr(verifier, "check_material_catalog", lambda: catalog_ok)
    assert verifier.main() == (0 if catalog_ok else 1)


def test_lock_uses_exact_downstream_version_and_wheel_hash(tmp_path):
    fake_wheel(tmp_path)
    patch = fake_wheel(tmp_path, "opentrons_shared_data", "9.1.0+unilabos.np2.1")
    lock = builder.lock_wheels(tmp_path)
    assert f"opentrons-shared-data==9.1.0+unilabos.np2.1 --hash=sha256:{installer.sha256(patch)}" in lock
    assert len(lock.splitlines()) == 2


def test_lock_rejects_network_references_even_with_no_index(tmp_path):
    fake_wheel(tmp_path, dependencies=["pylabrobot @ https://example.invalid/archive.tar.gz"])
    with pytest.raises(RuntimeError, match="URL"):
        builder.lock_wheels(tmp_path)


def test_lock_rejects_multiple_versions(tmp_path):
    fake_wheel(tmp_path)
    fake_wheel(tmp_path, version="0.12.2")
    with pytest.raises(RuntimeError, match="多份"):
        builder.lock_wheels(tmp_path)


def test_metadata_ignores_vendored_dist_info(tmp_path):
    wheel = fake_wheel(tmp_path)
    with ZipFile(wheel, "a") as archive:
        archive.writestr("vendor/dependency.dist-info/METADATA", "Name: vendored\nVersion: 1.0\n")
    assert builder.wheel_metadata(wheel)[:2] == ("unilabos", "0.12.4")


def test_source_cache_is_verified_before_reuse(monkeypatch, tmp_path):
    archive = tmp_path / "source.tar.gz"
    archive.write_bytes(b"source")
    digest = installer.sha256(archive)
    archive.rename(tmp_path / f"{digest}.tar.gz")
    monkeypatch.setattr(builder, "urlopen", lambda *a, **k: pytest.fail("缓存命中不应联网"))
    spec = {"sha256": digest, "url": "https://example.invalid/source.tar.gz"}
    builder.download_source(spec, archive, tmp_path)
    assert archive.read_bytes() == b"source"
    (tmp_path / f"{digest}.tar.gz").write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="SHA256"):
        builder.download_source(spec, archive, tmp_path)


def test_download_rejects_hash_mismatch_without_building(monkeypatch, tmp_path):
    monkeypatch.setattr(builder, "urlopen", lambda *a, **k: io.BytesIO(b"wrong-source"))
    with pytest.raises(RuntimeError, match="SHA256"):
        builder.download_source({"url": "https://example.invalid/source", "sha256": "0" * 64}, tmp_path / "s")


def test_extract_rejects_path_traversal(tmp_path):
    source = tmp_path / "source.tar.gz"
    with tarfile.open(source, "w:gz") as archive:
        item = tarfile.TarInfo("../escape")
        item.size = 4
        archive.addfile(item, io.BytesIO(b"oops"))
    with pytest.raises(tarfile.FilterError):
        builder.extract_source(source, tmp_path / "extract")
    assert not (tmp_path / "escape").exists()


@pytest.fixture
def bundle(tmp_path):
    wheels = tmp_path / "wheelhouse"
    wheels.mkdir()
    fake_wheel(wheels)
    (tmp_path / "requirements.lock").write_text(builder.lock_wheels(wheels), encoding="utf-8")
    (tmp_path / "verify_installation.py").write_text("pass", encoding="utf-8")
    manifest = {"schema_version": 1, "kind": "offline-wheelhouse", "version": "0.12.4",
                "python": "3.14", "platform": installer.platform_id(),
                "files": {p.relative_to(tmp_path).as_posix(): installer.sha256(p)
                          for p in tmp_path.rglob("*") if p.is_file()}}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return tmp_path


def test_bundle_rejects_tampering_before_creating_environment(bundle, monkeypatch):
    monkeypatch.setattr(installer, "__file__", str(bundle / "install_wheel_release.py"))
    monkeypatch.setattr(sys, "argv", ["install_wheel_release.py"])
    monkeypatch.setattr(installer.venv, "EnvBuilder", lambda **k: pytest.fail("校验失败不能创建环境"))
    (bundle / "requirements.lock").write_text("tampered", encoding="utf-8")
    with pytest.raises(RuntimeError, match="SHA256"):
        installer.main()


def test_bundle_rejects_free_threaded_abi(bundle, monkeypatch):
    """cp314 的离线原生包不能安装到独立 ABI 的 cp314t 解释器。"""
    monkeypatch.setattr(installer.sysconfig, "get_config_var", lambda name: 1)
    with pytest.raises(RuntimeError, match="cp314t"):
        installer.verify_bundle(bundle)


def test_bundle_rejects_unlisted_wheels(bundle):
    fake_wheel(bundle / "wheelhouse", "unexpected")
    with pytest.raises(RuntimeError, match="清单外"):
        installer.verify_bundle(bundle)


def test_bundle_rejects_wrong_platform(bundle):
    path = bundle / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["platform"] = "wrong-platform"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RuntimeError, match="平台不匹配"):
        installer.verify_bundle(bundle)


def test_installer_preserves_existing_environment(bundle, monkeypatch):
    monkeypatch.setattr(installer, "__file__", str(bundle / "install_wheel_release.py"))
    monkeypatch.setattr(sys, "argv", ["install_wheel_release.py"])
    prefix = bundle / ".venv"
    prefix.mkdir()
    sentinel = prefix / "keep.txt"
    sentinel.write_text("user data")
    with pytest.raises(RuntimeError, match="不会覆盖"):
        installer.main()
    assert sentinel.read_text() == "user data"


def test_installer_is_offline_hash_locked_and_outside_checkout(bundle, monkeypatch):
    monkeypatch.setattr(installer, "__file__", str(bundle / "install_wheel_release.py"))
    monkeypatch.setattr(sys, "argv", ["install_wheel_release.py"])
    monkeypatch.setattr(installer.venv.EnvBuilder, "create", lambda *a: None)
    commands = []

    def record(command, **kwargs):
        commands.append(command)
        assert kwargs["cwd"] == bundle
        assert command[1] == "-I"

    monkeypatch.setattr(installer.subprocess, "run", record)
    assert installer.main() == 0
    assert {"--no-index", "--require-hashes", "--only-binary=:all:", "--isolated"} <= set(commands[0])
    assert "check" in commands[1]
    assert "--assert-no-ros" in commands[2]
    assert "--help" in commands[3]


def test_environment_bootstrap_accepts_companion_wheelhouse(monkeypatch, tmp_path):
    from unilabos.utils import environment_check

    monkeypatch.setenv("UNILABOS_WHEELHOUSE", str(tmp_path))
    for tool in ("pip", "uv"):
        command = environment_check._install_command(tool, "opentrons-shared-data==9.1.0+unilabos.np2.1", False, False)
        assert command[-2:] == ["--find-links", str(tmp_path)]


def test_source_release_refuses_dirty_worktree(monkeypatch, tmp_path):
    monkeypatch.setattr(builder.subprocess, "check_output", lambda cmd, **k: " M setup.py\n" if "status" in cmd else "a" * 40)
    with pytest.raises(RuntimeError, match="未提交"):
        builder.snapshot_source(ROOT, tmp_path, local_test=False)


def test_opentrons_material_catalog_is_actually_constructed():
    from scripts.verify_installation import check_material_catalog

    assert check_material_catalog()
