"""tests/server 共用夹具。"""

from __future__ import annotations

import pytest


@pytest.fixture(params=["linux_lease", "portable"])
def source_write_backend(request, monkeypatch) -> str:
    """让作者源码回写测试在两种后端下各跑一遍。

    ``linux_lease`` 需要 Linux file lease 与 dir_fd 原语，当前平台没有时跳过；
    ``portable`` 是 macOS / Windows 的默认后端，在 Linux 上同样可以强制运行。
    """

    from unilabos.server.services.runtime.workflow import service as workflow_service

    backend = request.param
    if backend == workflow_service._SOURCE_WRITE_LEASE and not workflow_service._linux_lease_write_supported():
        pytest.skip("当前平台没有 Linux file lease / dir_fd 原语")
    monkeypatch.setattr(workflow_service, "_SOURCE_WRITE_BACKEND", backend)
    return backend
