"""运行文件的读取、预览及显式清理接口。"""

from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from unilabos.utils.run_artifacts import RunArtifactStore

router = APIRouter(prefix="/run-artifacts", tags=["run-artifacts"])


@router.get("")
def list_artifacts(task_uuid: str):
    try:
        items = RunArtifactStore.configured().list(task_uuid)
        return {
            "code": 0,
            "data": {"items": items, "size_bytes": sum(i["size_bytes"] for i in items if not i["deleted_at"])},
        }
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.get("/{identifier}/content/{name}")
def content(identifier: str, name: str):
    try:
        metadata, body = RunArtifactStore.configured().read(identifier)
        if name != metadata["name"]:
            raise FileNotFoundError()
        disposition = "inline" if metadata["media_type"] == "application/pdf" else "attachment"
        return Response(
            body,
            media_type=metadata["media_type"],
            headers={
                "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(name, safe='')}",
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
            },
        )
    except (ValueError, FileNotFoundError) as error:
        raise HTTPException(404, "文件不存在、已清理或校验失败") from error


@router.delete("/{identifier}")
def delete_artifact(identifier: str):
    from unilabos.workflow.composition import get_workflow_service
    from unilabos.workflow.service import WorkflowError

    store = RunArtifactStore.configured()
    try:
        metadata = store.metadata(identifier)
    except (ValueError, FileNotFoundError) as error:
        raise HTTPException(404, "文件不存在") from error
    service = get_workflow_service()
    if service is None:
        raise HTTPException(409, "任务状态不可用，不能清理文件")
    try:
        eligibility = service.get_workflow_task_delete_eligibility(metadata["task_uuid"])
    except WorkflowError as error:
        raise HTTPException(409, "无法核验任务是否已结束，不能清理文件") from error
    if not eligibility["can_delete"]:
        raise HTTPException(409, "任务仍可能执行或恢复，不能清理文件")
    return {"code": 0, "data": store.delete(identifier)}
