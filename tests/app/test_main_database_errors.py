"""数据库无法迁移时 unilab 以退出码 1 结束，只打印说明，不打印调用栈。"""

from __future__ import annotations

import pytest

from unilabos.app import main as app_main
from unilabos.server.database import DatabaseMigrationFailed, DatabaseMigrationRequired


@pytest.mark.parametrize("error", [DatabaseMigrationRequired, DatabaseMigrationFailed])
def test_database_protection_errors_exit_cleanly(monkeypatch, capsys, error) -> None:
    def refuse():
        raise error("数据库 runtime.db 与当前代码的结构不一致且无法迁移")

    monkeypatch.setattr(app_main, "_run_main", refuse)
    with pytest.raises(SystemExit) as caught:
        app_main.main()
    assert caught.value.code == 1
    output = capsys.readouterr()
    assert "无法迁移" in output.out + output.err
    assert "Traceback" not in output.out + output.err


def test_other_errors_still_propagate(monkeypatch) -> None:
    def boom():
        raise RuntimeError("unrelated")

    monkeypatch.setattr(app_main, "_run_main", boom)
    with pytest.raises(RuntimeError, match="unrelated"):
        app_main.main()
