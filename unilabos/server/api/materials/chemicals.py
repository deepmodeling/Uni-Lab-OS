"""``/api/v1/chemicals``：化学品定义（CAS / InChIKey 可空唯一键）的 HTTP 契约。

响应使用 envelope（``{"code": 0, "data": ...}``），业务码见 ``api/envelope.py``：1000 参数错误、
3002 不存在、3003 冲突（重复 CAS / InChIKey、版本过期）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, FastAPI, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from unilabos.server.api.envelope import envelope_error, install_envelope_validation
from unilabos.server.services.materials.chemicals import ChemicalError, ChemicalService

_CATEGORIES = {"invalid_input": "invalid", "not_found": "not_found", "conflict": "conflict"}
PhysicalState = Literal["liquid", "solid", "gas", "unknown"]


class ChemicalWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    name_en: Optional[str] = None
    aliases: List[str] = Field(default_factory=list)
    cas: Optional[str] = Field(default=None, description="CAS 登记号，校验格式与校验位；可空，未删除的行之间唯一")
    inchi_key: Optional[str] = Field(default=None, description="27 位标准 InChIKey；可空，未删除的行之间唯一")
    molecular_formula: Optional[str] = None
    smiles: Optional[str] = None
    molecular_weight: Optional[float] = Field(default=None, gt=0)
    density_g_per_ml: Optional[float] = Field(default=None, gt=0)
    physical_state: PhysicalState = "unknown"
    description: str = ""
    meta_data: Dict[str, Any] = Field(default_factory=dict)
    chemical_uuid: Optional[str] = None


class ChemicalPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    name: Optional[str] = None
    name_en: Optional[str] = None
    aliases: Optional[List[str]] = None
    cas: Optional[str] = None
    inchi_key: Optional[str] = None
    molecular_formula: Optional[str] = None
    smiles: Optional[str] = None
    molecular_weight: Optional[float] = Field(default=None, gt=0)
    density_g_per_ml: Optional[float] = Field(default=None, gt=0)
    physical_state: Optional[PhysicalState] = None
    description: Optional[str] = None
    meta_data: Optional[Dict[str, Any]] = None


def _success(data: Any = None, status: int = 200) -> JSONResponse:
    content: Dict[str, Any] = {"code": 0}
    if data is not None:
        content["data"] = data
    return JSONResponse(status_code=status, content=content)


def _error(error: ChemicalError) -> JSONResponse:
    return envelope_error(_CATEGORIES.get(error.code, "internal"), error.message)


def create_chemical_router(service: ChemicalService) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["chemicals-v1"])

    @router.get("/chemicals")
    def list_chemicals(
        q: str = "",
        cas: str = "",
        inchi_key: str = "",
        physical_state: str = "",
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=500),
    ) -> JSONResponse:
        """按名称 / 别名 / CAS / 分子式模糊搜索，或按 CAS、InChIKey 精确查找。"""

        return _success(service.list_chemicals(
            q=q, cas=cas, inchi_key=inchi_key, physical_state=physical_state,
            page=page, page_size=page_size,
        ))

    @router.post("/chemicals")
    def create_chemical(body: ChemicalWrite) -> JSONResponse:
        values = body.model_dump(exclude={"chemical_uuid"})
        try:
            return _success(service.create_chemical(values, chemical_uuid=body.chemical_uuid), status=201)
        except ChemicalError as error:
            return _error(error)

    @router.get("/chemicals/{chemical_uuid}")
    def get_chemical(chemical_uuid: str) -> JSONResponse:
        try:
            return _success(service.get_chemical(chemical_uuid))
        except ChemicalError as error:
            return _error(error)

    @router.patch("/chemicals/{chemical_uuid}")
    def update_chemical(chemical_uuid: str, body: ChemicalPatch) -> JSONResponse:
        values = body.model_dump(exclude={"expected_version"}, exclude_unset=True)
        try:
            return _success(service.update_chemical(
                chemical_uuid, values, expected_version=body.expected_version,
            ))
        except ChemicalError as error:
            return _error(error)

    @router.delete("/chemicals/{chemical_uuid}")
    def delete_chemical(chemical_uuid: str, expected_version: int = Query(..., ge=1)) -> JSONResponse:
        try:
            service.delete_chemical(chemical_uuid, expected_version=expected_version)
        except ChemicalError as error:
            return _error(error)
        return _success()

    return router


def install_chemical_api(app: FastAPI, service: ChemicalService) -> None:
    install_envelope_validation(app, ("/api/v1/chemicals",))
    app.include_router(create_chemical_router(service))


__all__ = ["ChemicalPatch", "ChemicalWrite", "create_chemical_router", "install_chemical_api"]
