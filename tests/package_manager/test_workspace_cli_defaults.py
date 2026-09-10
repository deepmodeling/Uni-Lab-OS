"""公共工作区（Workspace）命令行缺省路径合同。"""

from __future__ import annotations

import sys

import pytest


def test_workspace_flag_without_path_defaults_to_current_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """证明 ``--workspace`` 省略路径时选择当前目录。

    参数：``monkeypatch`` 隔离当前进程的公共命令行参数。
    返回：无；断言解析结果使用 ``.`` 作为工作区根候选，再由工作区来源执行
    规范路径与安全校验。
    异常：若公共解析器仍要求显式路径，argparse 会抛出 ``SystemExit`` 并使测试失败。
    """

    from unilabos.app.main import parse_args

    monkeypatch.setattr(
        sys,
        "argv",
        ["unilab", "--workspace"],
    )

    # ``parsed_arguments`` 是公共命令行（CLI）交给工作区启动模块的唯一参数投影。
    parsed_arguments = parse_args().parse_args()

    assert parsed_arguments.workspace == "."


def test_omitted_workspace_flag_keeps_legacy_startup_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """证明未提供 ``--workspace`` 时不会隐式启用工作区运行模式。

    参数：``monkeypatch`` 隔离当前进程的公共命令行参数。
    返回：无；断言缺省值仍是 ``None``，保留既有图和设备目录启动合同。
    异常：公共解析器拒绝既有参数时，argparse 会抛出 ``SystemExit`` 并使测试失败。
    """

    from unilabos.app.main import parse_args

    monkeypatch.setattr(sys, "argv", ["unilab"])

    # ``parsed_arguments`` 表示没有显式工作区权威的遗留启动选择。
    parsed_arguments = parse_args().parse_args()

    assert parsed_arguments.workspace is None
