"""实验室布局路由 ``/api/v1/lab/*``（V1 合同，统一信封）。

``GET /lab/layout`` 按 V1 返回已放置实例视图 ``{zones, placements, storage_summary}``；dev 的带修订
网格文档并入档案：``GET /lab/profile`` 的 ``grid`` 字段，``PUT /lab/profile`` 带 ``grid`` 整份替换
（revision 乐观锁，过期为 3003），``grid: null`` 重置。dev 原 ``PUT|DELETE /lab/layout`` 删除。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, FastAPI

from unilabos.server.api.envelope import RESOURCE, envelope_router
from unilabos.server.services.materials import MaterialsService
from unilabos.server.services.materials.lab import LabLayout


def create_lab_router(materials: MaterialsService, grid: Any = None) -> APIRouter:
    lab = LabLayout(materials, grid=grid)
    router = envelope_router(prefix="/api/v1/lab", tags=["lab"], profile=RESOURCE)

    @router.get("/profile")
    def get_profile() -> Any:
        return lab.get_profile()

    @router.put("/profile")
    def put_profile(body: Dict[str, Any] = Body(...)) -> Any:
        return lab.update_profile(body)

    @router.get("/layout")
    def get_layout() -> Any:
        return lab.get_layout()

    @router.get("/warehouse")
    def get_warehouse() -> Any:
        """品类库存聚合（性质 / 品类为组织依据）。"""

        return lab.warehouse()

    @router.post("/zones")
    def post_zone(body: Dict[str, Any] = Body(...)) -> Any:
        return lab.upsert_zone(body)

    @router.delete("/zones/{zone_id}")
    def delete_zone(zone_id: str) -> Any:
        return lab.delete_zone(zone_id)

    @router.post("/placements")
    def post_placement(body: Dict[str, Any] = Body(...)) -> Any:
        return lab.upsert_placement(body)

    @router.delete("/placements/{subject_id}")
    def delete_placement(subject_id: str) -> Any:
        return lab.delete_placement(subject_id)

    @router.get("/assembly/{subject_id}")
    def get_assembly(subject_id: str) -> Any:
        return lab.assembly(subject_id)

    @router.post("/demo")
    def seed_demo() -> Any:
        return {"seeded": lab.seed_demo()}

    return router


def install_lab_api(app: FastAPI, materials: MaterialsService, grid: Optional[Any] = None) -> None:
    app.include_router(create_lab_router(materials, grid))


__all__ = ["create_lab_router", "install_lab_api"]
