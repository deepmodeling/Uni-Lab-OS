"""V1 工站调用 ``POST /api/v1/station/workflow-invocations``：不鉴权（工站密钥已取消）、两种调用形状、
幂等重放与同键冲突、发布修订指纹。"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from tests.api_envelope import error_of
from tests.server.v1_lifecycle_support import Stack
from unilabos.config.config import BasicConfig
from unilabos.server.api.runtime.workflow import create_workflow_app

STATION = "/api/v1/station/workflow-invocations"


@pytest.fixture
def stack(monkeypatch, tmp_path):
    monkeypatch.setattr(BasicConfig, "working_dir", str(tmp_path))
    value = Stack(with_scheduler=False)
    yield value
    value.close()


def _legacy(workflow_name: str, **extra):
    return {"backend_task_uuid": str(uuid4()), "invocation_key": "inv-1", "workflow_name": workflow_name,
            "input": {}, **extra}


def test_station_entry_needs_no_credential_and_ignores_stale_ones(stack: Stack, tmp_path) -> None:
    """V1 工站密钥已取消（2026-11-17）：不带凭据、带旧的 Bearer 密钥或 X-UniLab-Token 都照常受理。"""

    stack.create_workflow(["device-a"])
    stale_key_file = tmp_path / "station_api_key"  # 旧部署留下的密钥文件不再被读取
    stale_key_file.write_text("old-secret\n", encoding="utf-8")
    for index, headers in enumerate(
        ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "Bearer old-secret"}, {"X-UniLab-Token": "anything"})
    ):
        response = stack.client.post(STATION, json=_legacy("生命周期测试", invocation_key=f"inv-{index}"), headers=headers)
        assert response.status_code == 201 and response.json()["code"] == 0, response.text
    # 非法请求体照常按参数错误回答，不再先得到 401
    error_of(stack.client.post(STATION, json={"nope": 1}), 1000)


def test_full_app_middlewares_let_station_calls_through(stack: Stack) -> None:
    """管理 API 应用的中间件栈里没有鉴权：工站入口经 CORS 中间件后照常受理。"""

    from unilabos.server.api.app import ConfiguredCORSMiddleware

    stack.create_workflow(["device-a"])
    app = create_workflow_app(stack.service)
    app.add_middleware(ConfiguredCORSMiddleware)
    with TestClient(app) as client:
        response = client.post(STATION, json=_legacy("生命周期测试"), headers={"Origin": "https://fe.example"})
        assert response.status_code == 201 and response.json()["code"] == 0, response.text


def test_legacy_name_invocation_is_idempotent_per_invocation_key(stack: Stack) -> None:
    workflow = stack.create_workflow(["device-a"])
    body = _legacy("生命周期测试", priority=2.5, description="来自 Backend", meta_data={"plan": "p-1"})
    first = stack.client.post(STATION, json=body)
    assert first.status_code == 201, first.text
    task = first.json()["data"]
    assert task["workflow_uuid"] == workflow["uuid"] and task["priority"] == "high"
    station = task["meta_data"]["station_invocation"]
    assert station["backend_task_uuid"] == body["backend_task_uuid"] and station["invocation_key"] == "inv-1"
    assert station["priority"] == 2.5 and task["meta_data"]["plan"] == "p-1"
    replay = stack.client.post(STATION, json=body)
    assert replay.status_code == 201 and replay.json()["data"]["uuid"] == task["uuid"]
    # 同一调用键、不同请求内容：冲突，不建第二个任务
    error_of(stack.client.post(STATION, json={**body, "description": "另一请求"}), 3003)
    other = stack.client.post(STATION, json={**body, "invocation_key": "inv-2"}).json()["data"]
    assert other["uuid"] != task["uuid"]
    # 名称不存在 3002；名称不唯一 3003
    error_of(stack.client.post(STATION, json=_legacy("不存在的工作流")), 3002)
    stack.create_workflow(["device-b"])
    error_of(stack.client.post(STATION, json=_legacy("生命周期测试")), 3003)


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
    created = stack.client.post(STATION, json=body)
    assert created.status_code == 201, created.text
    station = created.json()["data"]["meta_data"]["station_invocation"]
    assert station["revision_fingerprint"] == fingerprint and station["deadline"] == "2026-11-12T08:00:00Z"
    error_of(stack.client.post(STATION, json={**body, "invocation_key": "inv-d",
                                              "revision_fingerprint": "sha256:" + "0" * 64}), 3003)
    # 两套身份混用、缺字段：参数错误
    error_of(stack.client.post(STATION, json={**body, "workflow_name": "生命周期测试"}), 1000)
    error_of(stack.client.post(STATION, json={"invocation_key": "x", "workflow_name": "生命周期测试"}), 1000)
