"""无 ROS 环境的公开安装/CLI 契约；即使开发机有 ROS，也禁止隐式导入。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROS_IMPORT_GUARD = '''
import importlib.abc
import sys
class NoROS(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        root = fullname.split('.')[0]
        if root in {'rclpy', 'rosidl_runtime_py', 'rosidl_parser', 'unilabos_msgs',
                    'std_msgs', 'std_srvs', 'geometry_msgs', 'sensor_msgs',
                    'builtin_interfaces', 'action_msgs', 'control_msgs',
                    'tf2_ros', 'cv_bridge', 'ament_index_python'}:
            raise ModuleNotFoundError('当前环境未安装 ROS: ' + fullname, name=fullname)
sys.meta_path.insert(0, NoROS())
'''


def run_python(source: str, *args: str) -> subprocess.CompletedProcess:
    result = subprocess.run(
        [sys.executable, "-c", ROS_IMPORT_GUARD + source, *args],
        text=True, encoding="utf-8", capture_output=True, timeout=90,
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def test_default_runtime_imports_do_not_need_ros():
    run_python('''
from unilabos.config.config import BasicConfig
from unilabos.app.cli.parser import build_parser
assert BasicConfig.backend == build_parser().parse_args([]).backend == 'hostlink'
import unilabos.resources.graphio
import unilabos.registry.registry
import unilabos.server.api.app
import unilabos.backend.hostlink.local_runtime
''')


@pytest.mark.parametrize("module", ["unilabos", "unilabos.app.main"])
def test_module_entrypoint_help_without_ros(module):
    run_python("import runpy\nsys.argv = ['unilab', '--help']\nrunpy.run_module("
               + repr(module) + ", run_name='__main__')\n")


@pytest.mark.parametrize("backend_args", [[], ["--backend", "hostlink"], ["--backend", "ros2"]])
def test_graph_create_is_backend_neutral_without_ros(tmp_path, backend_args):
    package = tmp_path / "no_ros_device"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "device.py").write_text('''
from unilabos.registry.decorators import device, action
@device(id="no_ros_counter")
class Counter:
    @action()
    def add(self, amount: int = 1) -> int:
        return amount
''', encoding="utf-8")
    target = tmp_path / "graph.json"
    argv = ["unilab", *backend_args, "graph", "create", "--devices", str(package), "-o", str(target)]
    run_python("import runpy\nsys.argv = " + repr(argv) + "\nrunpy.run_module('unilabos', run_name='__main__')\n")
    assert [node["id"] for node in json.loads(target.read_text(encoding="utf-8"))["nodes"]] == ["no_ros_counter"]


def test_conda_build_default_has_no_ros_channels():
    from scripts.build_conda_release import FULL_SUPPORT_RECIPES, recipes

    assert recipes() == [(name, []) for name in (
        "opentrons-shared-data", "msgcenterpy", "pylabrobot", "mcp", "base"
    )]
    assert recipes("jazzy")[-1] == ("ros2", ["robostack-jazzy"])
    with pytest.raises(ValueError, match="显式"):
        recipes(full=True)
    with pytest.raises(ValueError, match="显式"):
        recipes(extensions_only=True)
    assert recipes("jazzy", full=True, extensions_only=True) == [
        *((name, []) for name in FULL_SUPPORT_RECIPES),
        ("msgs", ["robostack-jazzy"]),
        ("ros2", ["robostack-jazzy"]), ("full", ["robostack-jazzy"]),
    ]


def test_extension_upload_does_not_republish_default_packages(monkeypatch, tmp_path):
    from scripts import build_conda_release

    for name in ("unilabos-0.12.4-py314_0.conda", "unilabos-full-0.12.4-jazzy_0.conda"):
        package = tmp_path / "win-64" / name
        package.parent.mkdir(exist_ok=True)
        package.touch()
    support = tmp_path / "noarch" / "rinoh-typeface-dejavuserif-0.1.3-py_0.conda"
    support.parent.mkdir()
    support.touch()
    monkeypatch.setenv("ANACONDA_API_TOKEN", "test-token-not-a-secret")
    monkeypatch.setattr(sys, "argv", ["build_conda_release.py", "upload", "--platform", "win-64",
                                     "--ros-distros", "jazzy", "--full", "--extensions-only",
                                     "--output-dir", str(tmp_path)])
    uploads = []
    monkeypatch.setattr(build_conda_release.subprocess, "run",
                        lambda command, **kw: uploads.append(Path(command[-1]).name)
                        or subprocess.CompletedProcess(command, 0))
    build_conda_release.main()
    assert uploads == [support.name, "unilabos-full-0.12.4-jazzy_0.conda"]


def test_conda_abi_upgrade_builds_message_packages_before_consumers():
    """消息扩展不能靠频道中尚不存在或 ABI 不同的产物通过求解。"""
    from scripts.build_conda_release import EXTERNAL_RECIPES, recipes

    ordered = [name for name, _ in recipes("jazzy,humble", full=True)]
    assert ordered.index("opentrons-shared-data") < ordered.index("pylabrobot")
    for suffix in ("", "-humble"):
        assert ordered.index(f"msgs{suffix}") < ordered.index(f"ros2{suffix}")
        assert ordered.index(f"ros2{suffix}") < ordered.index(f"full{suffix}")
        assert (Path(__file__).resolve().parents[2] / EXTERNAL_RECIPES[f"msgs{suffix}"]).is_file()


def test_conda_upload_reuses_dependencies_without_overwriting_framework(monkeypatch, tmp_path):
    from scripts import build_conda_release

    packages = {
        "noarch/msgcenterpy-0.1.8-py_0.conda": True,
        "win-64/mcp-1.30.0-py314_0.conda": True,
        "win-64/unilabos-0.12.4-py314_0.conda": False,
    }
    for name in packages:
        package = tmp_path / name
        package.parent.mkdir(parents=True, exist_ok=True)
        package.touch()
    monkeypatch.setenv("ANACONDA_API_TOKEN", "test-token-not-a-secret")
    monkeypatch.setattr(sys, "argv", ["build_conda_release.py", "upload", "--platform", "win-64",
                                    "--output-dir", str(tmp_path)])
    commands = []

    def record_upload(command, **kwargs):
        commands.append(command)
        assert kwargs["env"]["ANACONDA_CLIENT_FORCE_STANDALONE"] == "1"
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(build_conda_release.subprocess, "run", record_upload)
    build_conda_release.main()
    assert len(commands) == len(packages)
    for command in commands:
        relative = Path(command[-1]).relative_to(tmp_path).as_posix()
        assert ("--skip-existing" in command) is packages[relative]
        assert "--force" not in command


def test_verification_failure_is_not_reported_as_success(monkeypatch):
    import scripts.verify_installation as verifier
    import unilabos.utils.environment_check as checker

    monkeypatch.setattr(sys, "argv", ["verify_installation.py"])
    monkeypatch.setattr(verifier, "check_package", lambda *args: True)
    monkeypatch.setattr(checker, "check_environment", lambda **kwargs: False)
    assert verifier.main() == 1


@pytest.mark.parametrize("installed, expected", [("0.1.0", False), ("0.0.39", True), ("0.0.38", False)])
def test_environment_verifies_all_version_bounds_after_install(monkeypatch, installed, expected):
    import importlib.metadata
    from unilabos.utils.environment_check import EnvironmentChecker

    checker = EnvironmentChecker()
    checker.required_packages = {"sqlmodel": checker.required_packages["sqlmodel"]}
    monkeypatch.setattr(checker, "check_package_installed", lambda name: True)
    monkeypatch.setattr(importlib.metadata, "version", lambda name: installed)
    assert checker.verify_installation() is expected
    assert bool(checker.packages_need_upgrade) is (not expected)


def test_environment_uses_serial_import_for_pyserial():
    from unilabos.utils.environment_check import EnvironmentChecker

    checker = EnvironmentChecker()
    assert "serial" in checker.required_packages
    assert "pyserial" not in checker.required_packages
    assert checker.requirements["serial"].name == "pyserial"


def test_environment_does_not_import_modules_before_dependencies_are_complete(monkeypatch):
    from importlib import metadata
    from unilabos.utils.environment_check import EnvironmentChecker

    checker = EnvironmentChecker()
    checker.required_packages = {name: checker.required_packages[name] for name in ("numpy", "yaml")}

    def version(name):
        if name == "PyYAML":
            raise metadata.PackageNotFoundError(name)
        return "2.5.3"

    monkeypatch.setattr(metadata, "version", version)
    monkeypatch.setattr(checker, "check_package_installed", lambda name: pytest.fail("依赖未齐不应导入模块"))
    assert not checker.check_all_packages()
    assert checker.missing_packages == [("yaml", "PyYAML>=6")]


def test_plr_chinese_mirror_preserves_commit_and_falls_back(monkeypatch):
    from unilabos.utils import environment_check as checker

    # 配套发行版已用本地版本号；保留外部设备包显式源码依赖的同提交镜像转换。
    requirement = "pylabrobot @ https://github.com/Xuwznln/pylabrobot/archive/6285d662effa972be97a781ba7493553f2ba94ee.tar.gz"
    candidates = checker._requirement_candidates(requirement, True)
    assert len(candidates) == 2
    assert "gitee.com/xuwznln/pylabrobot/repository/archive/" in candidates[0]
    assert candidates[0].rsplit("/", 1)[1] == candidates[1].rsplit("/", 1)[1]
    assert candidates[1] == requirement
    assert checker._requirement_candidates(requirement, False) == [requirement]
    assert checker._requirement_candidates("msgcenterpy>=0.1.8", True) == ["msgcenterpy>=0.1.8"]
    monkeypatch.setattr(checker, "_is_chinese_locale", lambda: True)
    monkeypatch.setattr(checker, "_installer_candidates", lambda: ["pip"])
    calls = []

    def install(command, **kwargs):
        calls.append(command)
        is_mirror = any("gitee.com" in part for part in command)
        return subprocess.CompletedProcess(command, 1 if is_mirror else 0, "", "mirror 404" if is_mirror else "")

    monkeypatch.setattr(checker.subprocess, "run", install)
    assert checker._install_packages([requirement])
    assert len(calls) == 2
    assert candidates[0] in calls[0]
    assert candidates[1] in calls[1]


def test_bootstrap_does_not_install_when_readonly(monkeypatch):
    import builtins
    from unilabos.utils import environment_check as checker

    original = builtins.__import__

    def without_packaging(name, *args, **kwargs):
        if name == "packaging.requirements":
            raise ModuleNotFoundError(name)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_packaging)
    monkeypatch.setattr(checker, "_install_packages", lambda *a, **k: pytest.fail("只读检查不应补装"))
    assert not checker.check_environment(auto_install=False)


def test_bootstrap_installs_parser_before_reading_packaged_requirements(monkeypatch):
    import builtins
    from types import SimpleNamespace
    from unilabos.utils import environment_check as checker

    original = builtins.__import__
    installed = False

    def without_packaging(name, *args, **kwargs):
        if name == "packaging.requirements" and not installed:
            raise ModuleNotFoundError(name)
        return original(name, *args, **kwargs)

    def install(packages, **kwargs):
        nonlocal installed
        assert packages == ["packaging>=24"]
        installed = True
        return True

    def environment():
        assert installed
        return SimpleNamespace(check_all_packages=lambda: True)

    monkeypatch.setattr(builtins, "__import__", without_packaging)
    monkeypatch.setattr(checker, "_install_packages", install)
    monkeypatch.setattr(checker, "EnvironmentChecker", environment)
    assert checker.check_environment()


def test_post_install_verification_refreshes_pth_paths(monkeypatch):
    from unilabos.utils import environment_check as checker

    environment = checker.EnvironmentChecker()
    calls = []
    monkeypatch.setattr(checker.site, "getsitepackages", lambda: ["/new-site-packages"])
    monkeypatch.setattr(checker.site, "addsitedir", calls.append)

    def check_all():
        assert calls == ["/new-site-packages"]
        return True

    monkeypatch.setattr(environment, "check_all_packages", check_all)
    assert environment.verify_installation()


def test_developer_install_targets_current_interpreter_and_respects_no_deps(monkeypatch, tmp_path):
    from scripts import dev_install

    monkeypatch.setattr(dev_install.shutil, "which", lambda executable: "/tools/uv")
    command = dev_install.build_command(tmp_path, use_pip=False, skip_deps=False, mirror=False)
    assert command[:5] == ["uv", "pip", "install", "--python", sys.executable]
    assert "--no-deps" not in command
    command = dev_install.build_command(tmp_path, use_pip=True, skip_deps=True, mirror=True)
    assert command[:4] == [sys.executable, "-m", "pip", "install"]
    assert "--no-deps" in command
    assert command[-2:] == ["--index-url", dev_install.TSINGHUA_MIRROR]


@pytest.mark.parametrize("extra", ["ros2", "full"])
def test_developer_install_supports_only_public_profiles(tmp_path, extra):
    from scripts import dev_install

    command = dev_install.build_command(tmp_path, use_pip=True, skip_deps=False, mirror=False, extras=extra)
    assert str(tmp_path) + f"[{extra}]" in command
    for obsolete in ("dev", "docs", "test", "drivers", "dora"):
        with pytest.raises(ValueError, match="未知"):
            dev_install.build_command(tmp_path, use_pip=True, skip_deps=False, mirror=False, extras=obsolete)


def test_full_profile_contains_all_python_development_dependencies(monkeypatch):
    import runpy
    import setuptools
    from packaging.requirements import Requirement

    root = Path(__file__).resolve().parents[2]
    captured = {}
    monkeypatch.setattr(setuptools, "setup", lambda **kwargs: captured.update(kwargs))
    runpy.run_path(str(root / "setup.py"))
    extras = captured["extras_require"]
    assert set(extras) == {"ros2", "full"}
    assert set(extras["ros2"]) <= set(extras["full"])
    names = {Requirement(raw).name for raw in extras["full"]}
    assert {"pytest", "pytest-asyncio", "build", "ruff", "ipython", "jupyterlab", "pandas", "opcua"} <= names
    docs = (root / "unilabos/utils/requirements-docs.txt").read_text(encoding="utf-8")
    assert {line.strip() for line in docs.splitlines() if line.strip() and not line.startswith("#")} <= set(extras["full"])
    assert "-r ../unilabos/utils/requirements-docs.txt" in (root / "docs/requirements.txt").read_text()
    assert not any("ros-" in raw or "rclpy" in raw for raw in captured["install_requires"])
    for raw in extras["full"]:
        requirement = Requirement(raw)
        if requirement.name in {"pyautogui", "pywinauto"}:
            assert requirement.marker.evaluate({"sys_platform": "win32"})
            assert not requirement.marker.evaluate({"sys_platform": "linux"})
            assert not requirement.marker.evaluate({"sys_platform": "darwin"})


def test_full_conda_profiles_cover_python_extras(monkeypatch):
    import runpy
    import setuptools
    import yaml
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
    from scripts.build_conda_release import recipes

    root = Path(__file__).resolve().parents[2]
    captured = {}
    monkeypatch.setattr(setuptools, "setup", lambda **kwargs: captured.update(kwargs))
    runpy.run_path(str(root / "setup.py"))
    conda_names = {"build": "python-build", "matplotlib": "matplotlib-base"}
    for distro, suffix in (("jazzy", ""), ("humble", "-humble")):
        assert ("pprp", []) in recipes(distro, full=True)
        full = yaml.safe_load((root / f".conda/full{suffix}/recipe.yaml").read_text())
        ros2 = yaml.safe_load((root / f".conda/ros2{suffix}/recipe.yaml").read_text())
        # Windows SDK 的选择使 full 成为平台包，不能把它发布成同名不同内容的 noarch。
        assert "noarch" not in full["build"]
        dependencies = full["requirements"]["run"] + ros2["requirements"]["run"]
        names = {canonicalize_name((dep["then"] if isinstance(dep, dict) else dep).split("::")[-1].split()[0])
                 for dep in dependencies}
        for raw in captured["extras_require"]["full"]:
            requirement = Requirement(raw)
            assert canonicalize_name(conda_names.get(requirement.name, requirement.name)) in names


def test_full_conda_support_packages_and_closed_dependency_validation():
    import yaml
    from scripts.build_conda_release import FULL_SUPPORT_RECIPES

    root = Path(__file__).resolve().parents[2]
    for name in FULL_SUPPORT_RECIPES:
        recipe = yaml.safe_load((root / f".conda/{name}/recipe.yaml").read_text(encoding="utf-8"))
        assert recipe["package"]["name"] == name
        assert recipe["build"]["noarch"] == "python"
        sources = recipe["source"] if isinstance(recipe["source"], list) else [recipe["source"]]
        assert all(len(source["sha256"]) == 64 for source in sources)
    for suffix in ("", "-humble"):
        full = yaml.safe_load((root / f".conda/full{suffix}/recipe.yaml").read_text(encoding="utf-8"))
        dependencies = full["requirements"]["run"]
        assert "colcon-notification" in dependencies
        assert set(FULL_SUPPORT_RECIPES) <= {dep.split()[0] for dep in dependencies if isinstance(dep, str)}
        assert {"if": "win", "then": "pyautogui >=0.9.54"} in dependencies
        assert any(test.get("python", {}).get("pip_check") is True for test in full["tests"])
        commands = next(test["script"] for test in full["tests"] if "script" in test)
        windows = next(command for command in commands if isinstance(command, dict) and command.get("if") == "win")
        assert all("%USERPROFILE%\\AppData\\" in command for command in windows["then"])
        assert any("import rinoh_typeface_dejavuserif" in command for command in commands if isinstance(command, str))
        assert "patch_rclpy_dll_windows" in commands[0]


def test_offline_release_rejects_wrong_source_before_installing(monkeypatch, tmp_path):
    from scripts import pack_conda_release

    monkeypatch.setattr(sys, "argv", ["pack_conda_release.py", "--platform", "win-64",
                        "--source-sha", "1" * 40, "--prefix", str(tmp_path / "unused")])
    monkeypatch.setattr(pack_conda_release.subprocess, "check_output", lambda *a, **kw: "2" * 40)
    with pytest.raises(RuntimeError, match="SHA"):
        pack_conda_release.main()
