"""``unilab workspace init`` 的设备包骨架合同。"""

from __future__ import annotations

import json
import keyword
import os
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest

from unilabos.app.main import main
from unilabos.package_manager import (
    WorkspaceInitError,
    compile_package_source,
    initialize_workspace,
)
from unilabos.package_manager.workspace_runtime import (
    WorkspaceSource,
    compile_workspace_startup,
)


def test_workspace_init_creates_one_loadable_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """命令应从输出目录名派生一致身份，并生成可被启动编译器读取的骨架。"""

    output = tmp_path / "sample-lab"
    monkeypatch.setattr(
        sys,
        "argv",
        ["unilab", "workspace", "init", "--output", str(output), "--json"],
    )

    main()

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "createdFiles": [
            ".gitignore",
            "DEVICE_PACKAGE_REQUIREMENTS.md",
            "README.md",
            "deployment/graphs/dry-run.json",
            "deployment/local_config.py",
            "package.yaml",
            "pyproject.toml",
            "sample_lab/__init__.py",
            "sample_lab/devices/__init__.py",
            "sample_lab/devices/demo_device.py",
            "sample_lab/experiment_operations/__init__.py",
            "sample_lab/resources/__init__.py",
            "sample_lab/workflows/__init__.py",
            "sample_lab/workflows/demo_workflow.py",
            "tests/test_workspace_contract.py",
        ],
        "distributionName": "sample-lab",
        "importPackage": "sample_lab",
        "nextCommands": [
            f"cd {output}",
            "python -m pip install -e '.[dev]'",
            "python -m pytest -q",
            "unilab package inspect --path . --out dist/inspect",
        ],
        "ok": True,
        "schemaVersion": "unilab-workspace-init/v1",
        "workspacePath": str(output),
    }
    graph = json.loads(
        output.joinpath("deployment/graphs/dry-run.json").read_text(encoding="utf-8")
    )
    assert graph["nodes"] == [
        {
            "id": "demo_device_01",
            "uuid": "579acffc-36dd-55bb-b3bf-90a1800b114e",
            "name": "示例设备",
            "class": "community.sample_lab.demo_device",
            "type": "device",
            "config": {},
            "data": {},
        }
    ]
    assert graph["links"] == []
    manifest = output.joinpath("package.yaml").read_text(encoding="utf-8")
    assert "name: sample_lab" in manifest
    assert "source: sample_lab/workflows/demo_workflow.py" in manifest
    assert output.joinpath("tests").is_dir()
    assert output.joinpath(".gitignore").read_text(encoding="utf-8") == (
        ".unilabos/\ndist/\n*.egg-info/\n__pycache__/\n.pytest_cache/\n.DS_Store\n"
    )
    readme = output.joinpath("README.md").read_text(encoding="utf-8")
    assert "## 本地检查" not in readme
    assert "unilab workspace start" not in readme
    assert "## 自带示例" in readme
    assert output.joinpath("deployment/local_config.py").read_text(
        encoding="utf-8"
    ) == (
        "class BasicConfig:\n"
        "    disable_browser = True\n"
        "    no_update_feedback = True\n"
        '    log_level = "INFO"\n'
    )
    requirements = output.joinpath("DEVICE_PACKAGE_REQUIREMENTS.md").read_text(
        encoding="utf-8"
    )
    assert "发布名称：`sample-lab`" in requirements
    assert "Python 包名：`sample_lab`" in requirements
    compile(
        output.joinpath("tests/test_workspace_contract.py").read_text(
            encoding="utf-8"
        ),
        "tests/test_workspace_contract.py",
        "exec",
    )
    compile(
        output.joinpath("sample_lab/devices/demo_device.py").read_text(
            encoding="utf-8"
        ),
        "sample_lab/devices/demo_device.py",
        "exec",
    )
    compile(
        output.joinpath("sample_lab/workflows/demo_workflow.py").read_text(
            encoding="utf-8"
        ),
        "sample_lab/workflows/demo_workflow.py",
        "exec",
    )

    source = WorkspaceSource(output)
    plan = compile_workspace_startup(source)
    assert plan.distribution_name == "sample-lab"
    assert plan.import_package == "sample_lab"
    assert plan.workflow_source_count == 1
    assert plan.default_graph == "deployment/graphs/dry-run.json"
    assert plan.default_config == "deployment/local_config.py"

    catalog = compile_package_source(source)
    assert [item.id for item in catalog.definitions.devices] == ["demo_device"]
    assert [item.id for item in catalog.definitions.workflows] == ["run_demo"]
    assert set(
        catalog.definitions.devices[0].details["registry_entry"]["class"][
            "action_value_mappings"
        ]
    ) == {"echo"}
    assert [
        dict(item)
        for item in catalog.definitions.workflows[0].details["action_references"]
    ] == [
        {
            "action_name": "echo",
            "device_symbol": "demo_device",
            "kind": "action",
        }
    ]


def test_workspace_init_uses_explicit_python_package_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--name`` 接受发布名，并以人类可读结果给出下一步。"""

    output = tmp_path / "hardware-package"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "unilab",
            "workspace",
            "init",
            "--output",
            str(output),
            "--name",
            "sample-lab",
        ],
    )

    main()

    project = output.joinpath("pyproject.toml").read_text(encoding="utf-8")
    assert 'name = "sample-lab"' in project
    assert 'include = ["sample_lab*"]' in project
    assert output.joinpath("sample_lab/__init__.py").is_file()
    rendered = capsys.readouterr().out
    assert f"工作区已创建：{output}" in rendered
    assert "先填写：DEVICE_PACKAGE_REQUIREMENTS.md" in rendered
    assert "python -m pytest -q" in rendered
    assert not rendered.lstrip().startswith("{")


def test_workspace_init_refuses_to_modify_an_existing_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """目标无论是否为空都不得覆盖，已有内容必须原样保留。"""

    output = tmp_path / "existing-lab"
    output.mkdir()
    sentinel = output / "owned-by-user.txt"
    sentinel.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["unilab", "workspace", "init", "--output", str(output), "--json"],
    )

    with pytest.raises(SystemExit) as caught:
        main()

    assert caught.value.code == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "workspace_exists"
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert list(output.iterdir()) == [sentinel]


@pytest.mark.parametrize(
    "package_name",
    ["Bad-Name", "9lab", "a/b", "", "lab-", "lab.", "lab_", *keyword.kwlist],
)
def test_workspace_init_rejects_invalid_python_package_name_before_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    package_name: str,
) -> None:
    """非法包名必须在创建目标目录前关闭式失败。"""

    output = tmp_path / "new-package"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "unilab",
            "workspace",
            "init",
            "--output",
            str(output),
            "--name",
            package_name,
            "--json",
        ],
    )

    with pytest.raises(SystemExit) as caught:
        main()

    assert caught.value.code == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == (
        "invalid_package_name"
    )
    assert not output.exists()


def test_workspace_init_rejects_keyword_derived_from_directory(tmp_path: Path) -> None:
    """默认从目录派生的包名同样不能生成 Python 关键字导入语句。"""

    output = tmp_path / "CLASS"
    with pytest.raises(WorkspaceInitError) as caught:
        initialize_workspace(output)

    assert caught.value.code == "invalid_package_name"
    assert not output.exists()


@pytest.mark.parametrize("package_name", ["sample__lab", "sample-._lab", "sample.lab"])
def test_workspace_init_normalizes_names_like_the_catalog(
    tmp_path: Path,
    package_name: str,
) -> None:
    """连续分隔符必须与包目录的规范身份一致，否则生成后无法被加载。"""

    output = initialize_workspace(tmp_path / package_name)
    plan = compile_workspace_startup(WorkspaceSource(output.root))
    catalog = compile_package_source(WorkspaceSource(output.root))

    assert (
        output.import_package
        == plan.import_package
        == catalog.import_package
        == "sample_lab"
    )
    assert output.distribution_name == plan.distribution_name == "sample-lab"


def test_generated_workspace_passes_tests_inspect_and_build(tmp_path: Path) -> None:
    """真实 CLI 生成的项目应通过自带测试、目录检查和包含启动文件的构建。"""

    repository = Path(__file__).resolve().parents[2]
    output = tmp_path / "generated-lab"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join((str(output), str(repository)))
    environment["PIP_NO_INDEX"] = "1"
    # 生成包的自带测试不依赖宿主里的 ROS / xdist 插件。
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    environment.pop("PYTEST_XDIST_WORKER", None)
    environment.pop("PYTEST_XDIST_TESTRUNUID", None)

    def run(*arguments: str, cwd: Path) -> str:
        result = subprocess.run(
            [sys.executable, "-m", *arguments],
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    payload = json.loads(
        run(
            "unilabos", "workspace", "init", "--output", str(output), "--json",
            cwd=tmp_path,
        )
    )
    assert len(payload["createdFiles"]) == 15
    assert "2 passed" in run("pytest", "-q", "tests", cwd=output)
    run(
        "unilabos", "package", "inspect", "--path", ".", "--out", "dist/inspect",
        cwd=output,
    )
    run(
        "unilabos", "package", "build", "--path", ".", "--out", "dist/build",
        cwd=output,
    )

    assert output.joinpath("dist/inspect/package.catalog.json").is_file()
    (wheel,) = output.joinpath("dist/build").glob("*.whl")
    with ZipFile(wheel) as archive:
        assert {
            "generated_lab/devices/demo_device.py",
            "generated_lab/workflows/demo_workflow.py",
            "generated_lab/_generated/package.catalog.json",
            "generated_lab/_generated/package.yaml",
            "generated_lab/_generated/pyproject.toml",
            "generated_lab/_generated/workspace/deployment/graphs/dry-run.json",
            "generated_lab/_generated/workspace/deployment/local_config.py",
        } <= set(archive.namelist())
    assert not output.joinpath(".unilabos").exists()
    assert not tmp_path.joinpath("unilabos_data").exists()


def test_workspace_init_refuses_a_dangling_symlink_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """悬空链接也属于已有路径，命令不得沿链接在其他位置创建内容。"""

    linked_target = tmp_path / "outside-target"
    output = tmp_path / "linked-lab"
    output.symlink_to(linked_target, target_is_directory=True)
    monkeypatch.setattr(
        sys,
        "argv",
        ["unilab", "workspace", "init", "--output", str(output), "--json"],
    )

    with pytest.raises(SystemExit) as caught:
        main()

    assert caught.value.code == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "workspace_exists"
    assert output.is_symlink()
    assert not linked_target.exists()


def test_workspace_init_removes_its_partial_target_after_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """写入失败时只清理本次新建目录，不留下可被误用的半成品。"""

    output = tmp_path / "partial-lab"
    original_write_text = Path.write_text

    def fail_on_manifest(
        path: Path,
        content: str,
        *,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> int:
        if path.name == "package.yaml":
            raise OSError("simulated write failure")
        return original_write_text(
            path,
            content,
            encoding=encoding,
            errors=errors,
            newline=newline,
        )

    monkeypatch.setattr(Path, "write_text", fail_on_manifest)

    with pytest.raises(WorkspaceInitError) as caught:
        initialize_workspace(output)

    assert caught.value.code == "workspace_init_failed"
    assert not output.exists()
