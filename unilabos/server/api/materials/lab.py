"""实验室视图路由 ``/api/v1/lab/*``（V1 合同，统一信封）：``warehouse``、``assembly/{id}``、``demo``。

只读写物料权威（materials.db）。V1 的档案 / 布局 / 分区 / 摆放（``/lab/profile``、``/lab/layout``、
``/lab/zones``、``/lab/placements``）及其表按负责人 2026-11-17 的决定已删除。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, FastAPI

from unilabos.server.api.envelope import RESOURCE, envelope_router
from unilabos.server.services.materials import MaterialsService
from unilabos.server.services.materials.lab import LabViews


def create_lab_router(materials: MaterialsService) -> APIRouter:
    lab = LabViews(materials)
    router = envelope_router(prefix="/api/v1/lab", tags=["lab"], profile=RESOURCE)

    @router.get("/warehouse")
    def get_warehouse() -> Any:
        """品类库存聚合（性质 / 品类为组织依据）。"""

        return lab.warehouse()

    @router.get("/assembly/{subject_id}")
    def get_assembly(subject_id: str) -> Any:
        return lab.assembly(subject_id)

    @router.post("/demo")
    def seed_demo() -> Any:
        return {"seeded": lab.seed_demo()}

    return router


def install_lab_api(app: FastAPI, materials: MaterialsService) -> None:
    app.include_router(create_lab_router(materials))


__all__ = ["create_lab_router", "install_lab_api"]
