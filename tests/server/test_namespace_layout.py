"""微后端代码只能从 ``unilabos.server`` 命名空间导入。"""

from __future__ import annotations

from pathlib import Path

import unilabos


def test_microbackend_namespaces_live_under_server() -> None:
    package_root = Path(unilabos.__file__).resolve().parent

    for removed in ("workflow", "scheduler", "storage"):
        assert not (package_root / removed).exists()
    assert not (package_root / "app" / "scheduler").exists()

    server_root = package_root / "server"
    assert (server_root / "workflow" / "upload.py").is_file()
    assert (server_root / "workflow" / "api.py").is_file()
    assert (server_root / "scheduler" / "workflow_execution.py").is_file()
    assert (server_root / "scheduler" / "dag" / "dag_executor.py").is_file()
    assert (server_root / "composition.py").is_file()
    assert not list((server_root / "storage").glob("*.py"))
