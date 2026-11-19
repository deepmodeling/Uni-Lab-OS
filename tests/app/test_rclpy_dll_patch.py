import builtins
import json
import os
from pathlib import Path

import pytest

from unilabos.app import utils


def _write_mutex(prefix: Path, distro: str, version: str) -> None:
    metadata_dir = prefix / "conda-meta"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "name": "ros2-distro-mutex",
        "version": version,
        "build": f"{distro}_18",
        "channel": f"robostack-{distro}",
    }
    (metadata_dir / f"ros2-distro-mutex-{version}-{distro}_18.json").write_text(
        json.dumps(metadata), encoding="utf-8"
    )


def test_detect_ros_distro_prefers_mutex_metadata(tmp_path, monkeypatch) -> None:
    _write_mutex(tmp_path, "humble", "0.9.0")
    monkeypatch.setenv("ROS_DISTRO", "jazzy")

    assert utils._detect_conda_ros_distro(str(tmp_path)) == "humble"


def test_detect_ros_distro_rejects_conflicting_mutexes(tmp_path, monkeypatch) -> None:
    _write_mutex(tmp_path, "humble", "0.9.0")
    _write_mutex(tmp_path, "jazzy", "0.15.0")
    monkeypatch.setenv("ROS_DISTRO", "humble")

    assert utils._detect_conda_ros_distro(str(tmp_path)) is None


def test_apply_patch_does_not_modify_hardlinked_package_cache(tmp_path) -> None:
    cache_file = tmp_path / "package-cache.py"
    environment_file = tmp_path / "environment.py"
    cache_file.write_text("ORIGINAL = True\n", encoding="utf-8")
    try:
        os.link(cache_file, environment_file)
    except OSError as error:
        pytest.skip(f"hard links are unavailable: {error}")

    assert utils._apply_dll_patch(str(environment_file), str(tmp_path / "Library" / "bin"))
    assert cache_file.read_text(encoding="utf-8") == "ORIGINAL = True\n"
    assert utils._PATCH_MARKER in environment_file.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("distro", "mutex_version"),
    [("humble", "0.9.0"), ("jazzy", "0.15.0")],
)
def test_supported_distros_apply_same_patch_after_dll_load_failure(
    tmp_path, monkeypatch, distro: str, mutex_version: str
) -> None:
    _write_mutex(tmp_path, distro, mutex_version)
    lib_bin = tmp_path / "Library" / "bin"
    site_packages = tmp_path / "Lib" / "site-packages"
    rclpy_dir = site_packages / "rclpy"
    rclpy_impl = rclpy_dir / "impl" / "implementation_singleton.py"
    rpyutils_dll = site_packages / "rpyutils" / "add_dll_directories.py"
    lib_bin.mkdir(parents=True)
    rclpy_impl.parent.mkdir(parents=True)
    rpyutils_dll.parent.mkdir(parents=True)
    rclpy_impl.write_text("ORIGINAL_RCLPY = True\n", encoding="utf-8")
    rpyutils_dll.write_text("ORIGINAL_RPYUTILS = True\n", encoding="utf-8")
    (rclpy_dir / "_rclpy_pybind11.cp314-win_amd64.pyd").write_bytes(b"")

    original_import = builtins.__import__

    def fail_rclpy_import(name, *args, **kwargs):
        if name == "rclpy":
            raise ImportError("DLL load failed while importing _rclpy_pybind11")
        return original_import(name, *args, **kwargs)

    class RestartRequested(Exception):
        pass

    patched_files = []

    def request_restart(files):
        patched_files.extend(files)
        raise RestartRequested

    monkeypatch.setattr(utils.sys, "platform", "win32")
    monkeypatch.setenv("CONDA_PREFIX", str(tmp_path))
    monkeypatch.setattr(builtins, "__import__", fail_rclpy_import)
    monkeypatch.setattr(utils, "_print_restart_banner", request_restart)

    with pytest.raises(RestartRequested):
        utils.patch_rclpy_dll_windows()

    assert patched_files == [str(rclpy_impl), str(rpyutils_dll)]
    assert utils._PATCH_MARKER in rclpy_impl.read_text(encoding="utf-8")
    assert utils._PATCH_MARKER in rpyutils_dll.read_text(encoding="utf-8")
