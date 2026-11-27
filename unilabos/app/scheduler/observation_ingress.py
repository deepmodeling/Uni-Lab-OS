"""Edge 的显式采样上报；旧标量变化缓存不被推断为连续传感数据。"""


import hmac

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from unilabos.runtime.observations import Sample, Selector, StateError


class SampleReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_id: str = Field(min_length=1)
    samples: list[Sample] = Field(min_length=1, max_length=1000)


def create_observation_ingress_router(
    get_runtime, *, get_api_key=None, prefix="/device-state/observations"
) -> APIRouter:
    """挂在既有 Edge 调度 API 内，沿用该部署的访问控制边界。"""
    router = APIRouter(prefix=prefix, tags=["edge-scheduler"])

    @router.post("/report")
    def report(body: SampleReport, authorization: str | None = Header(default=None)):
        if get_api_key is not None:
            key = get_api_key()
            if not key or not hmac.compare_digest(authorization or "", "Bearer " + key):
                raise HTTPException(401, "workspace authentication required")
        runtime = get_runtime()
        if runtime is None:
            raise HTTPException(503, "device observations not enabled")
        try:
            node = runtime.resolve(Selector(value=body.device_id))
            if any(sample.material_uuid != node["uuid"] for sample in body.samples):
                raise StateError("identity_mismatch")
            # 整批校验在同一事务；旧会话、坏代际或乱序不会部分写入。
            return {"code": 0, "data": runtime.publish_batch(body.samples)}
        except StateError as error:
            raise HTTPException(
                409, {"reason": error.reason, "msg": str(error)}
            ) from error

    return router
