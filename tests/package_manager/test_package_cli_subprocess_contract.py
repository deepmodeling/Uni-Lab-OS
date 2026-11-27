"""公共软件包命令行（Package CLI）的无环境启动合同。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from tests.package_manager.test_package_dependency_lock import _write_package

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _run_public_package_command(
    cwd: Path,
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    """在隔离目录用真实子进程调用公共软件包命令行（Package CLI）。

    参数：``cwd`` 是不得被产品启动副作用污染的当前目录；``arguments`` 是
    ``unilab`` 公共入口中 ``package`` 后的完整参数。
    返回：包含退出码、标准输出和标准错误的已完成子进程结果。
    异常：命令超过三十秒时抛出 ``TimeoutExpired``；它通常表示错误进入了交互式
    配置或设备启动路径。子进程标准输入固定为关闭，禁止测试隐式回答首次启动问题。
    """

    environment = os.environ.copy()
    # ``python_path`` 只授权当前候选仓库代码，避免测试命中环境中另一版 UniLab-OS。
    python_path = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = os.pathsep.join(
        value for value in (str(REPOSITORY_ROOT), python_path) if value
    )
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "unilabos.app.main",
            "package",
            *arguments,
        ],
        cwd=cwd,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _assert_no_product_bootstrap_artifacts(cwd: Path, output: str) -> None:
    """断言一次包命令没有进入产品工作目录或交互配置启动路径。

    参数：``cwd`` 是子进程当前目录；``output`` 是标准输出与标准错误的合并文本。
    返回：无；验证没有生成 ``unilabos_data``、``local_config.py``，也没有打印
    环境检查、工作目录创建或首次启动提示。
    异常：任何产品启动副作用都会触发断言失败。
    """

    assert not cwd.joinpath("unilabos_data").exists()
    assert not cwd.joinpath("local_config.py").exists()
    assert "第一次使用" not in output
    assert "环境检查" not in output
    assert "当前工作目录" not in output


def test_package_inspect_runs_from_clean_cwd_without_product_bootstrap(
    tmp_path: Path,
) -> None:
    """真实 ``package inspect`` 必须在干净 cwd 无输入完成包目录编译。

    参数：``tmp_path`` 提供相互隔离的当前目录、待检查包和产物目录。
    返回：无；断言命令成功生成同源包目录（PackageCatalog）且没有产品启动副作用。
    异常：若命令读取本地配置、检查环境、创建工作目录或等待输入，子进程失败。
    """

    clean_cwd = tmp_path / "clean-cwd"
    package_root = tmp_path / "inspect-package"
    output_root = tmp_path / "artifacts"
    clean_cwd.mkdir()
    _write_package(
        package_root,
        distribution_name="inspect-lab",
        package_name="inspect_lab",
        device_ids=("reader",),
    )

    result = _run_public_package_command(
        clean_cwd,
        "inspect",
        "--path",
        str(package_root),
        "--out",
        str(output_root),
    )
    command_output = result.stdout + result.stderr

    assert result.returncode == 0, command_output
    assert output_root.joinpath("package.catalog.json").is_file()
    _assert_no_product_bootstrap_artifacts(clean_cwd, command_output)


def test_package_build_runs_from_clean_cwd_and_publishes_an_audited_wheel(
    tmp_path: Path,
) -> None:
    """真实 ``package build`` 在产品启动前完成标准 wheel 自审计。

    参数：``tmp_path`` 提供干净当前目录、可构建包和产物目录。
    返回：无；断言公共入口生成 wheel、目录投影且没有产品启动副作用。
    异常：若命令缺失、进入 ROS/配置 bootstrap 或审计失败，测试失败。
    """

    # ``clean_cwd`` 是不得被产品运行态污染的公共命令当前目录。
    clean_cwd = tmp_path / "clean-cwd"
    # ``package_root`` 与 ``output_root`` 分别是作者源码和发布产物边界。
    package_root = tmp_path / "build-package"
    output_root = tmp_path / "artifacts"
    clean_cwd.mkdir()
    _write_package(
        package_root,
        distribution_name="build-lab",
        package_name="build_lab",
        device_ids=("reader",),
    )
    package_root.joinpath("pyproject.toml").write_text(
        "[build-system]\n"
        'requires = ["setuptools>=68", "wheel"]\n'
        'build-backend = "setuptools.build_meta"\n\n'
        "[project]\n"
        'name = "build-lab"\n'
        'version = "1.0.0"\n\n'
        "[tool.setuptools.packages.find]\n"
        'include = ["build_lab*"]\n',
        encoding="utf-8",
    )

    # ``result`` 是真实 public CLI 进程的完成状态与输出。
    result = _run_public_package_command(
        clean_cwd,
        "build",
        "--path",
        str(package_root),
        "--out",
        str(output_root),
    )
    command_output = result.stdout + result.stderr

    assert result.returncode == 0, command_output
    assert len(tuple(output_root.glob("*.whl"))) == 1
    assert output_root.joinpath("package.catalog.json").is_file()
    assert output_root.joinpath("package_info.json").is_file()
    assert output_root.joinpath("resources.json").is_file()
    _assert_no_product_bootstrap_artifacts(clean_cwd, command_output)


def test_package_dependency_commands_use_early_local_dispatch_without_product_bootstrap(
    tmp_path: Path,
) -> None:
    """真实 add/update/remove 必须共享无环境的本地依赖管理分派。

    参数：``tmp_path`` 提供作为当前目录的主工作区和显式外部包。
    返回：无；断言三个动作依次成功并且始终没有产品配置、ROS 或设备启动副作用。
    异常：若任一动作进入通用产品启动路径、需要交互输入或没有发布锁，测试失败。
    """

    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external"
    _write_package(
        workspace_root,
        distribution_name="workspace-lab",
        package_name="workspace_lab",
    )
    _write_package(
        external_root,
        distribution_name="external-lab",
        package_name="external_lab",
        device_ids=("reader",),
    )

    invocations = (
        ("add", "../external"),
        ("update", "external-lab"),
        ("remove", "community.external_lab"),
    )
    for arguments in invocations:
        result = _run_public_package_command(workspace_root, *arguments)
        command_output = result.stdout + result.stderr
        assert result.returncode == 0, command_output
        _assert_no_product_bootstrap_artifacts(workspace_root, command_output)
