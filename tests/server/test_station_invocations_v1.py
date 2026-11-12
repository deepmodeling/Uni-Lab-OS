"""V1 工站调用 ``POST /api/v1/station/workflow-invocations``：工站密钥鉴权（中间件 + 路由）、两种调用形状、
幂等重放与同键冲突、发布修订指纹。"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from tests.api_envelope import error_of
from tests.server.v1_lifecycle_support import Stack
from unilabos.config.config import BasicConfig
from unilabos.server.api.app import ConfiguredCORSMiddleware, ControlTokenMiddleware
from unilabos.server.api.runtime.workflow import create_workflow_app

STATION = "/api/v1/station/workflow-invocations"
KEY = "station-secret"
AUTH = {"Authorization": f"Bearer {KEY}"}


@pytest.fixture
def stack(monkeypatch, tmp_path):
    monkeypatch.setattr(BasicConfig, "station_api_key", KEY)
    monkeypatch.setattr(BasicConfig, "working_dir", str(tmp_path))
    monkeypatch.delenv("UNILABOS_BASICCONFIG_STATION_API_KEY", raising=False)
    monkeypatch.delenv("UNILABOS_EDGECONTROLCONFIG_API_KEY", raising=False)
    value = Stack(with_scheduler=False)
    yield value
    value.close()


def _legacy(workflow_name: str, **extra):
    return {"backend_task_uuid": str(uuid4()), "invocation_key": "inv-1", "workflow_name": workflow_name,
            "input": {}, **extra}


def test_station_key_is_required_and_control_token_does_not_replace_it(stack: Stack, monkeypatch) -> None:
    stack.create_workflow(["device-a"])
    body = _legacy("生命周期测试")
    for headers in ({}, {"Authorization": "Bearer wrong"}, {"X-UniLab-Token": "anything"}, {"Authorization": KEY}):
        response = stack.client.post(STATION, json=body, headers=headers)
        assert response.status_code == 401 and response.json()["code"] == 1002
    # 无密钥时非法请求体也先得到 401（依赖先于请求体校验）
    assert stack.client.post(STATION, json={"nope": 1}).status_code == 401
    # 未配置工站密钥：任何 Bearer 都不放行（V1 同口径）
    monkeypatch.setattr(BasicConfig, "station_api_key", "")
    assert stack.client.post(STATION, json=body, headers={"Authorization": "Bearer "}).status_code == 401
    # 工作目录里的密钥文件作为配置缺省
    with open(f"{BasicConfig.working_dir}/station_api_key", "w", encoding="utf-8") as handle:
        handle.write(KEY + "\n")
    assert stack.client.post(STATION, json=body, headers=AUTH).status_code == 201


def test_legacy_name_invocation_is_idempotent_per_invocation_key(stack: Stack) -> None:
    workflow = stack.create_workflow(["device-a"])
    body = _legacy("生命周期测试", priority=2.5, description="来自 Backend", meta_data={"plan": "p-1"})
    first = stack.client.post(STATION, json=body, headers=AUTH)
    assert first.status_code == 201, first.text
    task = first.json()["data"]
    assert task["workflow_uuid"] == workflow["uuid"] and task["priority"] == "high"
    station = task["meta_data"]["station_invocation"]
    assert station["backend_task_uuid"] == body["backend_task_uuid"] and station["invocation_key"] == "inv-1"
    assert station["priority"] == 2.5 and task["meta_data"]["plan"] == "p-1"
    replay = stack.client.post(STATION, json=body, headers=AUTH)
    assert replay.status_code == 201 and replay.json()["data"]["uuid"] == task["uuid"]
    # 同一调用键、不同请求内容：冲突，不建第二个任务
    error_of(stack.client.post(STATION, json={**body, "description": "另一请求"}, headers=AUTH), 3003)
    other = stack.client.post(STATION, json={**body, "invocation_key": "inv-2"}, headers=AUTH).json()["data"]
    assert other["uuid"] != task["uuid"]
    # 名称不存在 3002；名称不唯一 3003
    error_of(stack.client.post(STATION, json=_legacy("不存在的工作流"), headers=AUTH), 3002)
    stack.create_workflow(["device-b"])
    error_of(stack.client.post(STATION, json=_legacy("生命周期测试"), headers=AUTH), 3003)


def test_canonical_invocation_pins_the_published_revision(stack: Stack) -> None:
    workflow = stack.create_workflow(["device-a"])
    current = stack.client.get(f"/api/v1/workflows/{workflow['uuid']}").json()["data"]
    published = stack.client.post(f"/api/v1/workflows/{workflow['uuid']}/publications",
                                  json={"revision": current["revision"]}).json()
    assert published["code"] == 0, published
    fingerprint = published["data"]["revision_fingerprint"]
    body = {"task_uuid": str(uuid4()), "invocation_key": "inv-c", "workflow_id": workflow["uuid"],
            "revision_fingerprint": fingerprint, "normalized_input": {},
            "deadline": "2026-11-12T16:00:00+08:00"}
    created = stack.client.post(STATION, json=body, headers=AUTH)
    assert created.status_code == 201, created.text
    station = created.json()["data"]["meta_data"]["station_invocation"]
    assert station["revision_fingerprint"] == fingerprint and station["deadline"] == "2026-11-12T08:00:00Z"
    error_of(stack.client.post(STATION, json={**body, "invocation_key": "inv-d",
                                              "revision_fingerprint": "sha256:" + "0" * 64}, headers=AUTH), 3003)
    # 两套身份混用、缺字段：参数错误
    error_of(stack.client.post(STATION, json={**body, "workflow_name": "生命周期测试"}, headers=AUTH), 1000)
    error_of(stack.client.post(STATION, json={"invocation_key": "x", "workflow_name": "生命周期测试"},
                               headers=AUTH), 1000)


def test_control_token_middleware_lets_valid_station_key_through_only(stack: Stack, monkeypatch) -> None:
    stack.create_workflow(["device-a"])  # 夹具经无中间件的应用建工作流
    monkeypatch.setattr(BasicConfig, "control_token", "control-secret")
    app = create_workflow_app(stack.service)
    app.add_middleware(ControlTokenMiddleware)
    app.add_middleware(ConfiguredCORSMiddleware)
    with TestClient(app) as client:
        body = _legacy("生命周期测试")
        assert client.post(STATION, json=body, headers=AUTH).status_code == 201
        for headers in ({}, {"X-UniLab-Token": "control-secret"}, {"Authorization": "Bearer control-secret"}):
            rejected = client.post(STATION, json=body, headers=headers)
            assert rejected.status_code == 401 and rejected.json()["code"] == 1002
        # 工站密钥不能代替控制面 token 做其他写操作
        other = client.post("/api/v1/workflows", json={"name": "x"}, headers=AUTH)
        assert other.status_code == 401
