"""实验室布局（区域 / 围墙像素格）：runtime.db 单行文档、revision 乐观锁、不变量校验、HTTP 路由。"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tests.api_envelope import error_of, ok_data
from unilabos.protocol.runtime.lab import LabLayoutWrite, LabZone
from unilabos.server.api import install_server_apis
from unilabos.server.api.runtime.lab import install_lab_api
from unilabos.server.composition import ServerServices
from unilabos.server.database import ServerDatabasePaths
from unilabos.server.services.runtime import RuntimeService
from unilabos.server.services.runtime.lab import DEFAULT_CELL_SIZE, LabLayoutConflict, LabLayoutService


@pytest.fixture()
def service(tmp_path):
    runtime = RuntimeService(tmp_path / "runtime.db")
    try:
        yield LabLayoutService(runtime)
    finally:
        runtime.close()


def _write(revision: int, **overrides) -> LabLayoutWrite:
    payload = {
        "revision": revision,
        "cell_size": 100,
        "zones": [LabZone(id="prep", name="样品制备区", color="#2E5BFF", cells=["0,0", "1,0"])],
        "walls": ["5,5", "5,6"],
    }
    payload.update(overrides)
    return LabLayoutWrite.model_validate(payload)


def test_default_then_save_then_conflict(service: LabLayoutService) -> None:
    empty = service.get_layout()
    assert empty.revision == 0 and empty.zones == [] and empty.walls == []
    assert empty.cell_size == DEFAULT_CELL_SIZE

    saved = service.save_layout(_write(0))
    assert saved.revision == 1
    assert saved.zones[0].color == "#2e5bff"  # 颜色归一为小写
    assert saved.walls == ["5,5", "5,6"]
    assert saved.created_at_ms > 0 and saved.updated_at_ms >= saved.created_at_ms

    # 旧 revision 写入 → 冲突，权威内容不变
    with pytest.raises(LabLayoutConflict) as conflict:
        service.save_layout(_write(0, walls=[]))
    assert conflict.value.current == 1
    assert service.get_layout().walls == ["5,5", "5,6"]

    again = service.save_layout(_write(1, walls=["9,9"], cell_size=50))
    assert again.revision == 2 and again.walls == ["9,9"] and again.cell_size == 50

    assert service.reset_layout() is True
    assert service.get_layout().revision == 0
    assert service.reset_layout() is False


def test_write_model_enforces_invariants() -> None:
    with pytest.raises(ValidationError, match="同时属于"):
        _write(0, zones=[LabZone(id="a", name="A", cells=["0,0"]), LabZone(id="b", name="B", cells=["0,0"])])
    with pytest.raises(ValidationError, match="既是围墙"):
        _write(0, walls=["0,0"])
    with pytest.raises(ValidationError, match="id 重复"):
        _write(0, zones=[LabZone(id="a", name="A"), LabZone(id="a", name="B")], walls=[])
    with pytest.raises(ValidationError, match="非法格子键"):
        _write(0, walls=["x,1"])
    with pytest.raises(ValidationError, match="rrggbb"):
        LabZone(id="a", name="A", color="blue")
    # 重复格子键去重而不是报错
    assert LabZone(id="a", name="A", cells=["1,1", "1,1", "-2,3"]).cells == ["1,1", "-2,3"]


def test_router_roundtrip(service: LabLayoutService) -> None:
    app = FastAPI()
    install_lab_api(app, service)
    client = TestClient(app)

    initial = ok_data(client.get("/api/v1/lab/layout"))
    assert initial["revision"] == 0 and initial["layout_key"] == "default"

    body = {"revision": 0, "cell_size": 100, "zones": [{"id": "z1", "name": "洗涤区", "color": "#0e9f6e", "cells": ["2,2"]}], "walls": ["0,0"]}
    saved = client.put("/api/v1/lab/layout", json=body)
    assert saved.status_code == 200, saved.text
    assert ok_data(saved)["revision"] == 1

    stale = client.put("/api/v1/lab/layout", json=body)
    assert "revision 1" in error_of(stale, 3003)["msg"]

    invalid = client.put("/api/v1/lab/layout", json={**body, "revision": 1, "walls": ["2,2"]})
    error_of(invalid, 1000)

    # 重置是无数据命令：HTTP 200 + {"code": 0}
    assert client.delete("/api/v1/lab/layout").json() == {"code": 0}
    assert ok_data(client.get("/api/v1/lab/layout"))["revision"] == 0


def test_server_services_mount_lab_layout(tmp_path) -> None:
    services = ServerServices.open(ServerDatabasePaths.resolve(tmp_path))
    app = FastAPI()
    install_server_apis(app, services)
    try:
        assert "/api/v1/lab/layout" in app.openapi()["paths"]
        with TestClient(app) as client:
            assert ok_data(client.get("/api/v1/lab/layout"))["revision"] == 0
    finally:
        services.close()
