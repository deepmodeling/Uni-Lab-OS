"""Registry Authority API：Edge 上报 + 条目级版本管理。

``GET /api/v1/registry/digest`` 返回权威持有的条目内容哈希（生效 / 挂起），Host 据此决定
哪些条目要附完整定义。``POST /api/v1/registry/reports`` 接收 gzip JSON 上报（V1 的 ``POST /api/v1/resource-templates`` 归物料模板身份同步），两种形状：

- 按哈希增量（``RegistryReport``）：``{"entries": [{"id", "content_sha256", "payload"?}]}``，
  每个条目都带哈希，权威没有该哈希的才带 payload；结果里的 ``missing`` 列出仍缺定义的条目。
- 旧全量快照：``{"resources": [...], "workflow_templates": [...]}``，每个条目都是完整定义。

上报带 Host 的 ``machine_id`` 与当时连着的 Slave，权威据此做接受规则判定（见
``services/runtime/registry.py``）：不满足规则的变化挂起，经 ``entries/{name}/apply|dismiss``
或批量的 ``pending/apply|dismiss`` 确认或忽略。``POST /registry/slaves`` 供 Host 在 Slave 接入时
补报；``GET /registry/slaves`` / ``DELETE /registry/slaves/{machine_name}`` 查看、删除权威记着的
跨机器 Slave；``GET /registry/status`` 给出目录是否已上报与当前的规则 B 判定。

其余端点提供条目状态、版本历史、历史还原、上报批次统计，以及生效的工作流模板列表。
"""

from __future__ import annotations

import gzip
import json
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import ValidationError

from unilabos.server.api.envelope import envelope_router

from unilabos.protocol.runtime.registry import (
    RegistryDigest,
    RegistryPendingSelection,
    RegistryReport,
    RegistrySlave,
    RegistrySlaveAnnouncement,
)
from unilabos.server.services.runtime.registry import (
    RegistryAuthorityError,
    RegistryReporter,
    RegistryService,
    get_registry_service,
)

router = envelope_router(tags=["Registry"])


def _require_service() -> RegistryService:
    service = get_registry_service()
    if service is None:
        raise HTTPException(status_code=503, detail="registry service not ready")
    return service


def _ok(data: Any) -> Any:
    """成功载荷；信封由路由类统一包装（``api/envelope.py``）。"""

    return data


def _reporter(edge_uuid: str, machine_id: Any, slaves: Any) -> RegistryReporter:
    """HTTP 上报者：机器标识与 Slave 都来自请求体，缺省按跨机器处理（不是进程内上报）。"""

    parsed = []
    for raw in slaves if isinstance(slaves, list) else []:
        try:
            parsed.append(RegistrySlave.model_validate(raw).model_dump(mode="json"))
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail=f"invalid slave: {exc}") from exc
    return RegistryReporter(
        edge_uuid=edge_uuid,
        machine_id=str(machine_id or ""),
        slaves=tuple(parsed),
    )


@router.get("/api/v1/registry/digest")
def get_registry_digest() -> Any:
    """权威持有的条目内容哈希索引：Host 上报前先取，只为权威没有的哈希附完整定义。"""

    digest = _require_service().digest()
    return _ok(RegistryDigest(**digest).model_dump(mode="json"))


@router.post("/api/v1/registry/reports")
async def report_resource_templates(request: Request) -> Any:
    """Edge 全量上报（条目级替换：变了才升该条目版本，冲突挂起待确认）。"""

    service = _require_service()
    body = await request.body()
    if request.headers.get("content-encoding", "").lower() == "gzip":
        try:
            body = gzip.decompress(body)
        except OSError as exc:
            raise HTTPException(status_code=400, detail="invalid gzip body") from exc
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    edge_uuid = str(payload.get("edge_uuid") or request.headers.get("x-edge-uuid") or "")

    if "entries" in payload:
        # 按哈希增量：每个条目带 id + content_sha256，权威没有该哈希的才带 payload
        try:
            report_body = RegistryReport.model_validate(payload)
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail=f"invalid registry report: {exc}") from exc
        try:
            report = service.report_entries(
                [item.model_dump(mode="json") for item in report_body.entries],
                reporter=_reporter(
                    edge_uuid,
                    report_body.machine_id,
                    [slave.model_dump(mode="json") for slave in report_body.slaves],
                ),
            )
        except RegistryAuthorityError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _ok(report)

    if not isinstance(payload.get("resources"), list):
        raise HTTPException(status_code=400, detail="body must contain resources list")
    # 包里 @workflow 声明的工作流模板与设备 / 资源同批上报、同一套条目版本化
    workflow_templates = payload.get("workflow_templates", [])
    if not isinstance(workflow_templates, list):
        raise HTTPException(status_code=400, detail="workflow_templates must be a list")
    try:
        report = service.report(
            [*payload["resources"], *workflow_templates],
            reporter=_reporter(edge_uuid, payload.get("machine_id"), payload.get("slaves")),
        )
    except RegistryAuthorityError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _ok(report)


@router.get("/api/v1/registry/status")
def get_registry_status() -> Any:
    """目录状态：是否收到过 Host 上报、最近一次上报的统计与判定、挂起数、跨机器 Slave。"""

    return _ok(_require_service().status())


@router.post("/api/v1/registry/slaves")
def announce_registry_slaves(body: RegistrySlaveAnnouncement) -> Any:
    """Host 告知它连着的 Slave（Slave 接入时补报）；跨机器的被记下，规则 B 随之不成立。"""

    service = _require_service()
    added = service.record_slaves(
        _reporter(body.edge_uuid, body.machine_id, [slave.model_dump(mode="json") for slave in body.slaves])
    )
    return _ok({"added": added, "acceptance": service.acceptance()})


@router.get("/api/v1/registry/slaves")
def list_registry_remote_slaves() -> Any:
    """权威记着的跨机器 Slave（规则 B 因它们不成立）。"""

    service = _require_service()
    return _ok({"slaves": service.list_remote_slaves(), "acceptance": service.acceptance()})


@router.delete("/api/v1/registry/slaves/{machine_name}")
def forget_registry_remote_slave(machine_name: str) -> Any:
    """删除一条跨机器 Slave 记录（确认不再使用）；该 Slave 再次接入会被重新记下。"""

    try:
        return _ok(_require_service().forget_remote_slave(machine_name))
    except RegistryAuthorityError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/v1/registry/pending/apply")
def apply_registry_pending(body: RegistryPendingSelection) -> Any:
    """批量接受挂起的变化；``names`` 为空时接受全部。"""

    return _ok(_require_service().apply_pending_many(body.names))


@router.post("/api/v1/registry/pending/dismiss")
def dismiss_registry_pending(body: RegistryPendingSelection) -> Any:
    """批量忽略挂起的变化；``names`` 为空时忽略全部。"""

    return _ok(_require_service().dismiss_pending_many(body.names))


@router.get("/api/v1/registry/workflow-templates")
def list_registry_workflow_templates() -> Any:
    """生效的工作流模板（设备包 ``@workflow``）：前端模板面板与脚本实例化的数据源。"""

    return _ok({"templates": _require_service().list_workflow_templates()})


@router.get("/api/v1/registry/entries")
def list_registry_entries(
    status: str = Query(default=""),
) -> Any:
    """条目状态列表；``status`` 过滤 active/pending/removed/unusable。"""

    return _ok({"entries": _require_service().list_entries(status=status)})


@router.get("/api/v1/registry/pending-impacts")
def list_registry_pending_impacts() -> Any:
    """挂起条目影响面：冲突明细 + 受影响 workflow 节点（画布徽标数据源）。"""

    return _ok({"impacts": _require_service().pending_impacts()})


@router.get("/api/v1/registry/entries/{name}")
def get_registry_entry(name: str) -> Any:
    """条目详情：状态 + 生效 payload + 挂起 payload 与冲突明细。"""

    try:
        return _ok(_require_service().entry_detail(name))
    except RegistryAuthorityError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/v1/registry/entries/{name}/versions")
def list_registry_entry_versions(name: str) -> Any:
    try:
        return _ok({"versions": _require_service().entry_versions(name)})
    except RegistryAuthorityError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/v1/registry/entries/{name}/versions/{version}")
def get_registry_entry_version(name: str, version: int) -> Any:
    try:
        return _ok(_require_service().get_entry_version(name, version))
    except RegistryAuthorityError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/v1/registry/entries/{name}/apply")
def apply_registry_entry(name: str) -> Any:
    """接受挂起的变化：挂起版本切换为生效版本，待确认移除则软移除。"""

    try:
        return _ok(_require_service().apply_pending(name))
    except RegistryAuthorityError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/v1/registry/entries/{name}/dismiss")
def dismiss_registry_entry(name: str) -> Any:
    """忽略挂起的变化（历史保留，生效版本不动；Host 再报同一内容会再次挂起）。"""

    try:
        return _ok(_require_service().dismiss_pending(name))
    except RegistryAuthorityError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/v1/registry/entries/{name}/restore/{version}")
def restore_registry_entry(name: str, version: int) -> Any:
    """把历史版本内容还原为新的生效版本（条目版本号继续自增）。"""

    try:
        return _ok(_require_service().restore(name, version))
    except RegistryAuthorityError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/v1/registry/reports")
def list_registry_reports(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> Any:
    """上报批次统计（新增/更新/挂起/移除/复活/不可用计数与明细）。"""

    reports, total = _require_service().list_reports(page=page, page_size=page_size)
    return _ok({"reports": reports, "total": total, "page": page, "page_size": page_size})


# ── V1 设备目录（权威合成：物料设备根 + 生效注册表 + Host endpoint 能力） ──────────


def _devices():
    from unilabos.server.services.runtime.devices import current_device_directory

    return current_device_directory()


@router.get("/api/v1/devices")
def list_devices() -> Any:
    """V1：与 Go Backend ``GET /devices`` 同形的设备读模型（binding / material / 在线 / 动作模板 UUID）。"""

    return _devices().backend_devices()


@router.get("/api/v1/authoring/device-catalog")
def get_authoring_device_catalog() -> Any:
    """V1：创作工具使用的富设备目录（动作输入输出 Schema、状态 Schema、在线与忙闲）。"""

    return _devices().authoring_catalog()


@router.get("/api/v1/online-devices")
def list_online_devices() -> Any:
    """V1：在线设备（Host endpoint 在线且含该设备路由或能力）。"""

    return _devices().online_devices()


@router.get("/api/v1/actions")
def list_all_actions() -> Any:
    """V1：所有设备的可用动作与忙闲。"""

    return _devices().all_actions()


@router.get("/api/v1/devices/{device_id}/actions")
def list_device_actions(device_id: str) -> Any:
    """V1：单个设备的动作列表；``device_id`` 为设备 ID（物料 resource_id）或设备物料 UUID。"""

    return _devices().device_actions(device_id)


@router.get("/api/v1/devices/{device_id}/actions/{action_name}/schema")
def get_device_action_schema(device_id: str, action_name: str) -> Any:
    """V1：动作参数 Schema / 默认值 / 类型（注册表声明优先，回退 Host 能力描述）。"""

    return _devices().action_schema(device_id, action_name)


def install_registry_api(app: FastAPI) -> None:
    app.include_router(router)


__all__ = ["install_registry_api", "router"]
