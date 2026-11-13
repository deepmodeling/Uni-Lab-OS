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


def test_held_database_root_exits_with_its_own_code(monkeypatch, capsys, tmp_path) -> None:
    """数据库根目录被另一个实例占用：只打印谁占着，以 73 退出（看护进程据此区分普通崩溃）。"""

    from pathlib import Path

    from unilabos.app.supervisor import DATABASE_ROOT_LOCKED_EXIT_CODE
    from unilabos.server.database import DatabaseRootLocked

    root = Path(tmp_path)

    def refuse():
        raise DatabaseRootLocked(root, root / "unilab.lock", {"pid": 4321, "role": "调度权威", "started_at": "2026-11-13 08:00:00"})

    monkeypatch.setattr(app_main, "_run_main", refuse)
    with pytest.raises(SystemExit) as caught:
        app_main.main()
    assert caught.value.code == DATABASE_ROOT_LOCKED_EXIT_CODE == 73
    output = capsys.readouterr()
    text = output.out + output.err
    assert f"数据库根目录 {root} 已被另一个 Uni-Lab-OS 进程占用（pid 4321，调度权威，2026-11-13 08:00:00 启动）" in text
    assert "Traceback" not in text


def test_other_errors_still_propagate(monkeypatch) -> None:
    def boom():
        raise RuntimeError("unrelated")

    monkeypatch.setattr(app_main, "_run_main", boom)
    with pytest.raises(RuntimeError, match="unrelated"):
        app_main.main()
