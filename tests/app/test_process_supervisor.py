"""产品进程监督器的参数隔离与人工中断回归测试。"""

from __future__ import annotations

from typing import NoReturn

import pytest

from unilabos.app import process_supervisor


def test_child_arguments_remove_only_supervisor_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """监督器子进程参数必须保留全部普通产品参数。

    参数：``monkeypatch`` 隔离当前测试的进程参数。
    返回：无；断言两种监督器选项及其值被删除，工作区和物理图参数保持原序。
    异常：参数清理丢失普通产品输入或遗留监督器开关时断言失败。
    """

    monkeypatch.setattr(
        process_supervisor.sys,
        "argv",
        [
            "unilab",
            "--restart_mode",
            "--auto-restart-count",
            "3",
            "--workspace",
            ".",
            "--graph=graph.json",
        ],
    )

    assert process_supervisor.build_child_argv() == [
        "unilab",
        "--workspace",
        ".",
        "--graph=graph.json",
    ]


def test_interrupt_before_child_creation_exits_without_unbound_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """子进程创建完成前的人工中断不得访问未绑定进程变量。

    参数：``monkeypatch`` 注入在 ``Popen`` 内发生的 ``KeyboardInterrupt``。
    返回：无；断言监督器稳定以状态 1 退出，不泄漏 ``UnboundLocalError``。
    异常：中断被覆盖、监督器继续循环或访问未创建进程时测试失败。
    """

    def interrupt_before_process_creation(
        _command: list[str],
        *,
        env: dict[str, str],
    ) -> NoReturn:
        """模拟操作系统尚未返回子进程句柄时收到人工中断。

        参数：``_command`` 是待创建命令；``env`` 是监督器建立的子进程环境。
        返回：永不返回。
        异常：固定抛出 ``KeyboardInterrupt``，并先断言监督标志已经设置。
        """

        assert env["UNILABOS_RESTART_SUPERVISED"] == "1"
        raise KeyboardInterrupt

    monkeypatch.setattr(
        process_supervisor.subprocess,
        "Popen",
        interrupt_before_process_creation,
    )
    monkeypatch.setattr(process_supervisor.sys, "argv", ["unilab"])

    with pytest.raises(SystemExit) as exit_result:
        process_supervisor.run_as_supervisor(1)

    assert exit_result.value.code == 1
