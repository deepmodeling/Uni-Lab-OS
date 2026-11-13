"""``--check_mode`` 的退出码：AST 扫描失败的文件与注册类导入失败都必须让校验失败。"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from unilabos.registry.ast_registry_scanner import scan_directory

REPO_ROOT = Path(__file__).resolve().parents[2]


def _write(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source), encoding="utf-8")


def _good_device(pkg: Path) -> None:
    _write(
        pkg / "good.py",
        """
        from unilabos.registry.decorators import action, device


        @device(id="check_good", category=["check"])
        class GoodDevice:
            def __init__(self, **kwargs):
                pass

            @action(description="ping")
            def ping(self) -> bool:
                return True
        """,
    )


def _broken_scan_device(pkg: Path) -> None:
    # typed 合同不接受 action_type：AST 扫描对这个文件抛错
    _write(
        pkg / "broken_scan.py",
        """
        from unilabos.registry.decorators import action, device


        @device(id="check_broken_scan", category=["check"])
        class BrokenScan:
            @action(contract="typed", action_type="SendCmd")
            def run(self) -> None:
                return None
        """,
    )


def _broken_import_device(pkg: Path) -> None:
    _write(
        pkg / "broken_import.py",
        """
        import a_module_that_does_not_exist_anywhere  # noqa: F401

        from unilabos.registry.decorators import action, device


        @device(id="check_broken_import", category=["check"])
        class BrokenImport:
            @action(description="ping")
            def ping(self) -> bool:
                return True
        """,
    )


def _ros_only_device(pkg: Path) -> None:
    _write(
        pkg / "ros_only.py",
        """
        import a_ros_module_that_is_not_installed  # noqa: F401

        from unilabos.registry.decorators import action, device


        @device(id="check_ros_only", category=["check"], supported_backends=["ros2"])
        class RosOnly:
            @action(description="ping")
            def ping(self) -> bool:
                return True
        """,
    )


def _package(tmp_path: Path, *builders) -> Path:
    pkg = tmp_path / "check_pkg"
    _write(pkg / "__init__.py", "")
    for build in builders:
        build(pkg)
    return pkg


def _check_mode(pkg: Path, tmp_path: Path) -> subprocess.CompletedProcess:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    env = {key: value for key, value in os.environ.items() if not key.startswith("UNILABOS_")}
    env.update(PYTHONPATH=str(REPO_ROOT), PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    return subprocess.run(
        [
            sys.executable, "-m", "unilabos", "--working_dir", str(work), "--check_mode", "--skip_env_check",
            "--devices", str(pkg), "--external_devices_only",
        ],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )


def test_scan_reports_failed_files_and_repeats_them_on_cache_hits(tmp_path: Path) -> None:
    pkg = _package(tmp_path, _good_device, _broken_scan_device)
    cache: dict = {"files": {}}
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = scan_directory(pkg, executor=executor, cache=cache)
        second = scan_directory(pkg, executor=executor, cache=cache)

    for result in (first, second):
        assert set(result["devices"]) == {"check_good"}
        errors = result["_scan_errors"]
        assert [Path(item["file"]).name for item in errors] == ["broken_scan.py"]
        assert "typed" in errors[0]["error"]
    # 第二次全部命中缓存，失败的文件不会因为缓存而“消失”
    assert second["_cache_stats"]["hits"] == second["_cache_stats"]["total"]


@pytest.mark.parametrize(
    ("builders", "message"),
    [
        ((_good_device, _broken_scan_device), "broken_scan.py"),
        ((_good_device, _broken_import_device), "check_broken_import"),
    ],
    ids=["ast-scan-failure", "import-failure"],
)
def test_check_mode_fails_on_dropped_or_unimportable_drivers(tmp_path: Path, builders, message: str) -> None:
    completed = _check_mode(_package(tmp_path, *builders), tmp_path)
    output = completed.stdout + completed.stderr
    assert completed.returncode == 1, output[-4000:]
    assert "Check mode: 注册表校验失败" in output, output[-4000:]
    assert message in output


def test_check_mode_passes_a_clean_package_and_skips_backend_incompatible_imports(tmp_path: Path) -> None:
    # ROS 2 专用设备在 HostLink 校验环境里导入失败只算跳过，不算错误
    completed = _check_mode(_package(tmp_path, _good_device, _ros_only_device), tmp_path)
    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, output[-4000:]
    assert "check_ros_only" in output
    assert "Check mode: 注册表验证完成" in output


def _yaml_registry(pkg: Path) -> Path:
    """包内 YAML 注册表：一个可导入的设备、一个类路径写错的设备、一个语法损坏的文件。"""

    _write(
        pkg / "plain.py",
        """
        class PlainDevice:
            def __init__(self, **kwargs):
                pass

            def ping(self) -> bool:
                return True
        """,
    )
    devices = pkg / "registry" / "devices"
    _write(
        devices / "family" / "plain.yaml",
        """
        check_yaml.good:
          category: [check]
          class:
            module: check_pkg.plain:PlainDevice
            type: python
        check_yaml.missing-class:
          category: [check]
          class:
            module: check_pkg.plain:NoSuchClass
            type: python
        """,
    )
    return devices


def test_check_mode_fails_on_unloadable_yaml_and_keeps_the_file(tmp_path: Path) -> None:
    """YAML 设备与 @device 一样：读不了的文件、找不到的类都让 check_mode 失败；补全写回不能把
    找不到类的条目从文件里删掉。"""

    pkg = _package(tmp_path)
    devices = _yaml_registry(pkg)
    broken = devices / "broken.yaml"
    broken.write_text("check_yaml.broken: [unclosed\n", encoding="utf-8")

    completed = _check_mode(pkg, tmp_path)
    output = completed.stdout + completed.stderr
    assert completed.returncode == 1, output[-4000:]
    assert "broken.yaml" in output
    assert "check_yaml.missing-class" in output
    assert "check_yaml.missing-class" in (devices / "family" / "plain.yaml").read_text(encoding="utf-8")


def test_check_mode_passes_a_clean_yaml_package(tmp_path: Path) -> None:
    pkg = _package(tmp_path)
    devices = _yaml_registry(pkg)
    source = (devices / "family" / "plain.yaml").read_text(encoding="utf-8")
    (devices / "family" / "plain.yaml").write_text(
        source.split("check_yaml.missing-class:")[0], encoding="utf-8"
    )
    completed = _check_mode(pkg, tmp_path)
    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, output[-4000:]
    assert "Check mode: 注册表验证完成" in output
