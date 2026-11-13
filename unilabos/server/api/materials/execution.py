"""材料执行协议入口；人工释放由调度权威复验当前 attempt 后调用。"""

from fastapi import HTTPException

from unilabos.server.api.envelope import RESOURCE, envelope_router
from unilabos.protocol.material_execution import (
    MaterialExecutionPrepare,
    MaterialExecutionReceipt,
    MaterialExecutionRelease,
)


def create_material_execution_router(service):
    from .core import _call

    router = envelope_router(prefix="/executions", profile=RESOURCE)

    @router.get("")
    def list_executions():
        return _call(service.executions.list)

    @router.post("/prepare")
    def prepare(value: MaterialExecutionPrepare):
        return _call(service.executions.prepare, value)

    @router.post("/validate")
    def validate(value: MaterialExecutionReceipt):
        return _call(service.executions.validate, value)

    @router.post("/settle")
    def settle(value: MaterialExecutionReceipt):
        return _call(service.executions.settle, value)

    @router.get("/{job_uuid}")
    def get_execution(job_uuid: str):
        value = _call(service.executions.get, job_uuid)
        if value is None:
            raise HTTPException(status_code=404, detail="物理动作不存在")
        return value

    @router.post("/{job_uuid}/release")
    def release(job_uuid: str, value: MaterialExecutionRelease):
        return _call(service.executions.release, job_uuid, value)

    return router
