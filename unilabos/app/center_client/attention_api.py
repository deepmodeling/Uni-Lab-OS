"""中心事项当前摘要；沿用工站操作台同源访问边界。"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable

from fastapi import APIRouter, HTTPException


def attention_summary(snapshot: dict[str, Any]) -> dict[str, Any]:
    """只裁剪物料/交接的诊断正文，保留全部事项及原处置身份。"""
    result = deepcopy(snapshot)
    for item in result["items"]["exceptions"]:
        kind = item.get("decision_kind")
        if kind not in {"handoff_reconciliation", "material_export"}:
            continue
        metadata = item["meta_data"]
        summary = {}
        if "last_error" in metadata:
            summary["last_error"] = metadata["last_error"].encode("utf-8")[:500].decode("utf-8", errors="ignore")
        if kind == "handoff_reconciliation":
            progress = metadata["progress"]
            summary["progress"] = {key: progress[key] for key in ("handoff_uuid", "revision", "role", "state") if key in progress}
        elif "execution_status" in metadata:
            summary["execution_status"] = metadata["execution_status"]
        item["meta_data"] = summary
    return result


def create_router(get_runtime: Callable[[], Any]) -> APIRouter:
    router = APIRouter(prefix="/api/v1/center-attentions", tags=["Center attention"])

    @router.get("", summary="查询中心当前事项摘要")
    def current() -> dict[str, Any]:
        value = get_runtime()
        if value is None:
            raise HTTPException(503, "中心运行时未就绪")
        # 与轮询及会话保存共用锁，身份和事项必须来自同一份当前会话。
        with value._poll_lock:
            session = value.client.session
            if session is None:
                raise HTTPException(503, "中心控制会话未就绪")
            return {"code": 0, "data": {
                "station_uuid": value.config.station_uuid,
                "instance_uuid": value.config.instance_uuid,
                "process_uuid": getattr(session, "process_uuid", None) or value.client.process_uuid,
                "session_uuid": session.session_uuid,
                "session_epoch": session.session_epoch,
                "snapshot": attention_summary(value.center_attention_snapshot()),
            }}

    return router
