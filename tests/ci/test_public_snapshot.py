"""发布快照和依赖清单的开源边界：排程求解器和设备驱动不公开，工站基类旧路径的转发公开。"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# 工站基类在 unilabos/workstation；设备包按旧路径导入时由这里的转发模块兼容。
PUBLIC_DEVICE_FILES = {
    "unilabos/devices/__init__.py",
    "unilabos/devices/workstation/__init__.py",
    "unilabos/devices/workstation/workstation_base.py",
}


def _source_files(directory: Path, root: Path) -> set[str]:
    """列出目录下除字节码缓存以外的文件，路径相对 ``root``。"""

    return {
        path.relative_to(root).as_posix()
        for path in directory.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }


def test_public_tree_excludes_solver_sources() -> None:
    for relative_path in (
        "unilabos/app/scheduler/time_planning.py",
        "unilabos/app/scheduler/time_scheduling.py",
        "unilabos/scheduling",
    ):
        assert not (ROOT / relative_path).exists(), f"公开源码包含排程求解器：{relative_path}"
    solver_import = re.compile(r"^\s*(?:from|import)\s+ortools\b", re.MULTILINE)
    for path in (ROOT / "unilabos").rglob("*.py"):
        assert not solver_import.search(path.read_text(encoding="utf-8")), path.relative_to(ROOT)


def test_public_install_dependencies_exclude_ortools() -> None:
    for relative_path in (
        "setup.py",
        "unilabos/utils/requirements.txt",
        "packaging/launcher/runtime-requirements.lock.txt",
        "ci/python-suite-requirements.lock",
        "tests/scheduler_core/requirements.txt",
    ):
        assert "ortools" not in (ROOT / relative_path).read_text(encoding="utf-8").lower(), relative_path


def test_public_tree_has_no_device_drivers() -> None:
    assert _source_files(ROOT / "unilabos" / "devices", ROOT) == PUBLIC_DEVICE_FILES


def test_public_build_keeps_compat_path_and_drops_private_drivers(tmp_path: Path) -> None:
    """源码目录里混有私有驱动时，构建产物也只带公开工站包和旧路径转发。"""

    shutil.copy(ROOT / "setup.py", tmp_path / "setup.py")
    shutil.copy(ROOT / "MANIFEST.in", tmp_path / "MANIFEST.in")
    (tmp_path / "skills").mkdir()
    private_files = {
        "unilabos/devices/workstation/workflow_executors.py",
        "unilabos/devices/liquid_handling/__init__.py",
        "unilabos/devices/liquid_handling/driver.py",
        "unilabos/scheduling/__init__.py",
        "unilabos/app/scheduler/time_planning.py",
    }
    public_files = {
        "unilabos/__init__.py",
        "unilabos/app/__init__.py",
        "unilabos/app/scheduler/__init__.py",
        "unilabos/workstation/__init__.py",
        "unilabos/workstation/base.py",
    }
    for relative in PUBLIC_DEVICE_FILES | private_files | public_files:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")

    subprocess.run(
        [sys.executable, "setup.py", "-q", "build_py", "--build-lib", "build"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    built = tmp_path / "build"
    assert _source_files(built / "unilabos" / "devices", built) == PUBLIC_DEVICE_FILES
    assert (built / "unilabos" / "workstation" / "base.py").is_file()
    assert not (built / "unilabos" / "scheduling").exists()
    assert not (built / "unilabos" / "app" / "scheduler" / "time_planning.py").exists()
