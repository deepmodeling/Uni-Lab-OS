"""materials.v1 SSE 失效通知端点冒烟测试。

只验证协议形状：Last-Event-ID 续传时按 ledger sequence 重放
``materials.changed`` 事件；正文仅含失效定位字段，不承载业务数据。
TestClient 无法安全关闭无限事件流，这里直接调用端点函数迭代 body。
"""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest
from fastapi import APIRouter, HTTPException

from unilabos.client.materials import bind_payload
from unilabos.server.api.materials import create_materials_router
from unilabos.protocol.materials import InventoryMutation
from unilabos.protocol.materials import ResourceTemplateWrite
from unilabos.server.services.materials import MaterialsService


class _FakeRequest:
    """有限轮次后报告断开，保证无限流可退出。"""

    def __init__(self, alive_rounds: int = 5) -> None:
        self._rounds = alive_rounds

    async def is_disconnected(self) -> bool:
        self._rounds -= 1
        return self._rounds < 0


def _mutation(operation: str) -> InventoryMutation:
    return InventoryMutation(
        command_uuid=str(uuid4()), effect_key=operation, operation=operation
    )


def _events_endpoint(router: APIRouter):
    # include_router 可能保留子路由包装，直接从 materials router 取端点。
    route = next(
        r
        for r in router.routes
        if str(getattr(r, "path", "")).endswith("/events")
    )
    return route.endpoint


def _build_router(tmp_path) -> tuple[APIRouter, MaterialsService]:
    service = MaterialsService(tmp_path / "materials.db")
    return create_materials_router(service), service


async def _collect_sse(router: APIRouter, last_event_id: str) -> list[str]:
    response = await _events_endpoint(router)(
        request=_FakeRequest(), last_event_id=last_event_id
    )
    assert response.media_type == "text/event-stream"
    chunks: list[str] = []
    iterator = response.body_iterator
    try:
        async for chunk in iterator:
            text = chunk if isinstance(chunk, str) else chunk.decode("utf-8")
            chunks.append(text)
            if "data:" in text or len(chunks) > 20:
                break
    finally:
        await iterator.aclose()
    return chunks


def test_materials_events_sse_replays_from_cursor(tmp_path) -> None:
    router, service = _build_router(tmp_path)
    template = ResourceTemplateWrite(
        template_uuid="beaker-template",
        name="beaker",
        display_name="Beaker",
        resource_type="container",
        class_name="RegularContainer",
    )
    mutation = bind_payload(_mutation("put_template"), template)
    try:
        result = service.put_template(mutation, template)
        assert result.data.template_uuid

        # Last-Event-ID: 0 → 从账本头部续传，应立即重放刚写入的变更
        joined = "".join(asyncio.run(_collect_sse(router, "0")))
    finally:
        service.close()

    assert "event: materials.changed" in joined
    data_line = next(
        line for line in joined.splitlines() if line.startswith("data:")
    )
    payload = json.loads(data_line.split(":", 1)[1])
    # 失效通知只带定位字段；正文由订阅方重新经 HTTP 读取
    assert payload["sequence"] >= 1
    assert payload["aggregate_uuid"]
    assert payload["operation"]
    assert set(payload) == {"sequence", "operation", "aggregate_type", "aggregate_uuid"}


def test_materials_events_rejects_invalid_cursor(tmp_path) -> None:
    router, service = _build_router(tmp_path)
    try:
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(
                _events_endpoint(router)(
                    request=_FakeRequest(), last_event_id="not-a-number"
                )
            )
        assert exc_info.value.status_code == 422
    finally:
        service.close()
