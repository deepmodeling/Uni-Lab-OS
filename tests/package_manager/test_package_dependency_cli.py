"""软件包命令行（Package CLI）显式依赖管理合同。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.package_manager.test_package_dependency_lock import _write_package
from unilabos.package_manager.workspace_runtime import compile_package_source


@pytest.mark.parametrize(
    ("arguments", "action", "identity", "source"),
    (
        (("add", "../external"), "add", "", "../external"),
        (("update", "external-lab"), "update", "external-lab", ""),
        (("remove", "community.external_lab"), "remove", "community.external_lab", ""),
    ),
)
def test_public_cli_parses_dependency_actions_with_current_workspace_default(
    monkeypatch: pytest.MonkeyPatch,
    arguments: tuple[str, ...],
    action: str,
    identity: str,
    source: str,
) -> None:
    """公开 ``unilab`` 入口解析 add/update/remove 且 ``--workspace`` 缺值为当前目录。

    参数：``monkeypatch`` 隔离进程参数；``arguments`` 是子动作参数；``action``、
    ``identity`` 与 ``source`` 是本例预期的稳定解析结果。
    返回：无；断言 package_manager 拥有的解析接缝投影一致字段。
    异常：命令未注册或 ``--workspace`` 仍要求路径时 argparse 抛出 ``SystemExit``。
    """

    from unilabos.app.main import parse_args

    monkeypatch.setattr(
        sys,
        "argv",
        ["unilab", "package", *arguments, "--workspace"],
    )
    # ``parsed`` 是公共命令行交给软件包依赖管理深模块的唯一参数投影。
    parsed = parse_args().parse_args()

    assert parsed.package_action == action
    assert parsed.workspace == "."
    assert getattr(parsed, "dependency_identity", "") == identity
    assert getattr(parsed, "dependency_source", "") == source


def test_package_command_add_update_remove_runs_without_ambient_install(
    tmp_path: Path,
) -> None:
    """命令分派通过显式工作区来源完成增改删，不调用遗留 pip install。

    参数：``tmp_path`` 提供主工作区和外部软件包。
    返回：无；断言三个命令依次发布、更新和清空同一锁。
    异常：若命令仍分派到 ambient 安装器、绕过锁或不能更新则测试失败。
    """

    from unilabos.package_manager import load_locked_package_catalogs
    from unilabos.package_manager.cli import cmd_package

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

    cmd_package(
        {
            "package_action": "add",
            "dependency_source": "../external",
            "workspace": str(workspace_root),
        }
    )
    _write_package(
        external_root,
        distribution_name="external-lab",
        package_name="external_lab",
        device_ids=("reader", "incubator"),
    )
    cmd_package(
        {
            "package_action": "update",
            "dependency_identity": "external-lab",
            "dependency_source": "",
            "workspace": str(workspace_root),
        }
    )
    assert tuple(
        item.id
        for item in load_locked_package_catalogs(
            workspace_root,
            compile_catalog=compile_package_source,
        )[0].definitions.devices
    ) == ("incubator", "reader")

    cmd_package(
        {
            "package_action": "remove",
            "dependency_identity": "external-lab",
            "workspace": str(workspace_root),
        }
    )
    assert (
        load_locked_package_catalogs(
            workspace_root,
            compile_catalog=compile_package_source,
        )
        == ()
    )


def test_package_subcommand_preserves_explicit_top_level_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """子命令未重复传参时不得覆盖顶层显式工作区路径。

    参数：``tmp_path`` 提供预期工作区路径；``monkeypatch`` 隔离进程参数。
    返回：无；断言顶层路径穿过 package_manager 注册的子解析器保持不变。
    异常：若子解析器默认值错误覆盖显式路径，断言失败。
    """

    from unilabos.app.main import parse_args

    workspace_root = tmp_path / "workspace"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "unilab",
            "--workspace",
            str(workspace_root),
            "package",
            "add",
            "../external",
        ],
    )

    parsed = parse_args().parse_args()

    assert parsed.workspace == str(workspace_root)


def test_package_inspect_parser_keeps_public_argument_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """解析职责迁入 package_manager 后仍保留既有 inspect 参数字段。

    参数：``monkeypatch`` 隔离公共命令行参数。
    返回：无；断言路径、命名空间和产物目录继续使用原字段名。
    异常：若分层目录迁移误改公开命令形状，argparse 或断言失败。
    """

    from unilabos.app.main import parse_args

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "unilab",
            "package",
            "inspect",
            "--path",
            "/tmp/example-package",
            "--namespace",
            "community.example",
            "--out",
            "/tmp/dist",
        ],
    )

    parsed = parse_args().parse_args()

    assert parsed.package_action == "inspect"
    assert parsed.package_path == "/tmp/example-package"
    assert parsed.namespace == "community.example"
    assert parsed.out == "/tmp/dist"


def test_package_upload_parser_rejects_external_download_url_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """公共上传命令不得接受绕过本次 wheel 直传的 URL。

    参数：``monkeypatch`` 隔离公共命令行参数。
    返回：无；断言 argparse 关闭式拒绝历史 ``--download-url``。
    异常：预期 ``SystemExit``；若参数仍被接受则测试失败。
    """

    from unilabos.app.main import parse_args

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "unilab",
            "package",
            "upload",
            "--path",
            "/tmp/example-package",
            "--download-url",
            "https://packages.example/bypass.whl",
        ],
    )

    with pytest.raises(SystemExit):
        parse_args().parse_args()


@pytest.mark.parametrize(
    "package_action",
    ("inspect", "build", "upload", "add", "update", "remove"),
)
def test_package_commands_skip_workspace_product_runtime_preparation(
    package_action: str,
) -> None:
    """软件包管理命令不得在修改依赖锁前创建产品运行时代。

    参数：``package_action`` 覆盖全部查询、上传和显式依赖变更动作。
    返回：无；断言公共主流程关闭工作区产品准备，而普通常驻启动仍然开启。
    异常：命令会预读物理图（Graph）、旧依赖锁或安装监视线程时测试失败。
    """

    from unilabos.app.main import should_prepare_workspace_product_runtime

    assert not should_prepare_workspace_product_runtime(
        {
            "command": "package",
            "package_action": package_action,
            "workspace": ".",
        }
    )
    assert should_prepare_workspace_product_runtime({"command": None, "workspace": "."})
