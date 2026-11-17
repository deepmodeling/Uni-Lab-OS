"""V1.0 容器内容物路由：试剂信息、化合物查询、试剂、样品、当前内容物及其历史。

路径与 DTO 以 V1（``reagent_api.py`` / ``content_api.py``）为准，统一信封（资源口径）；
``/materials/{material_uuid}/...`` 子路由与资源合同一样挂在 dev 字面量路由之后。
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import PurePath
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, FastAPI, File, Query, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from unilabos.server.api.envelope import INVALID_PARAMETER, RESOURCE, ApiError, envelope_router
from unilabos.server.services.materials import MaterialsService
from unilabos.server.services.materials.contents import ContentContract, batch_create

_MAX_IMPORT_ROWS = 500
_MAX_IMPORT_FILE_BYTES = 5 * 1024 * 1024
_REAGENT_SUBJECTS = ("reagent",)
_SUBSTANCE_SUBJECTS = ("sample", "current_substance")


class V1Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ReagentInfoCreateRequest(V1Model):
    cas: str = ""
    name: str
    name_en: Optional[str] = None
    aliases: List[str] = Field(default_factory=list)
    molecular_formula: Optional[str] = None
    smiles: Optional[str] = None
    inchi_key: Optional[str] = None
    molecular_weight: Optional[float] = None
    density_g_per_ml: Optional[float] = None
    physical_state: str = "unknown"
    description: Optional[str] = None
    meta_data: Dict[str, Any] = Field(default_factory=dict)


class ReagentInfoUpdateRequest(V1Model):
    cas: Optional[str] = None
    name: Optional[str] = None
    name_en: Optional[str] = None
    aliases: Optional[List[str]] = None
    molecular_formula: Optional[str] = None
    smiles: Optional[str] = None
    inchi_key: Optional[str] = None
    molecular_weight: Optional[float] = None
    density_g_per_ml: Optional[float] = None
    physical_state: Optional[str] = None
    description: Optional[str] = None
    meta_data: Optional[Dict[str, Any]] = None


class ReagentCreateRequest(V1Model):
    material_uuid: str
    reagent_info_uuid: Optional[str] = None
    cas: str = ""
    physical_state: str = "unknown"
    density_g_per_ml: Optional[float] = None
    concentration_value: Optional[float] = None
    concentration_unit: Optional[str] = None
    quantity: float
    quantity_unit: str
    source: Optional[str] = None
    observed_at: Optional[str] = None
    description: Optional[str] = None
    meta_data: Dict[str, Any] = Field(default_factory=dict)
    container_capacity: Optional[Dict[str, Any]] = None
    expected_material_revision: Optional[int] = None


class ReagentUpdateRequest(V1Model):
    concentration_value: Optional[float] = None
    concentration_unit: Optional[str] = None
    quantity: float
    quantity_unit: str
    source: Optional[str] = None
    observed_at: Optional[str] = None
    expected_revision: Optional[int] = None
    description: Optional[str] = None
    meta_data: Dict[str, Any] = Field(default_factory=dict)
    container_capacity: Optional[Dict[str, Any]] = None
    expected_material_revision: Optional[int] = None


class BatchRequest(V1Model):
    # 先保留原始行，逐行校验：atomic=false 时只拒绝出错的行
    items: List[Dict[str, Any]] = Field(min_length=1, max_length=_MAX_IMPORT_ROWS)
    atomic: bool = True


class SampleRequest(V1Model):
    material_uuid: str
    code: str
    name: str
    quantity: float
    quantity_unit: str
    description: Optional[str] = None
    meta_data: Dict[str, Any] = Field(default_factory=dict)


class CurrentSubstanceComponentRequest(V1Model):
    reagent_uuid: str
    quantity: float
    quantity_unit: str


class CurrentSubstanceRequest(V1Model):
    material_uuid: str
    name: Optional[str] = None
    components: List[CurrentSubstanceComponentRequest] = Field(default_factory=list)
    quantity: float
    quantity_unit: str
    physical_state: str
    observed_at: Optional[str] = None
    expected_revision: Optional[int] = None
    description: Optional[str] = None
    meta_data: Dict[str, Any] = Field(default_factory=dict)


def _validated_rows(rows: List[Dict[str, Any]], model: type[BaseModel]) -> List[Dict[str, Any]]:
    result = []
    for index, row in enumerate(rows, start=1):
        try:
            result.append(model.model_validate(row).model_dump(mode="json", exclude_unset=False))
        except Exception as error:  # noqa: BLE001 - 行级校验错误带行号回报
            raise ApiError(INVALID_PARAMETER, f"第 {index} 行不合法：{error}",
                           details={"row": index}) from None
    return result


def _parse_cell(key: str, value: Any) -> Any:
    if value is None:
        return {"aliases": [], "meta_data": {}, "cas": ""}.get(key)
    text = str(value).strip()
    if text == "":
        return {"aliases": [], "meta_data": {}, "cas": ""}.get(key)
    if key in {"aliases", "meta_data", "container_capacity"}:
        if key == "aliases" and not text.startswith("["):
            return [item.strip() for item in text.split(";") if item.strip()]
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            raise ApiError(INVALID_PARAMETER, f"{key} 不是有效 JSON") from None
    return text


async def _read_import_rows(file: UploadFile, allowed: set[str]) -> List[Dict[str, Any]]:
    """JSON（数组或 ``{items}``）/ CSV / TSV 导入文件 → 行字典。"""

    content = await file.read(_MAX_IMPORT_FILE_BYTES + 1)
    if len(content) > _MAX_IMPORT_FILE_BYTES:
        raise ApiError(INVALID_PARAMETER, "导入文件超过 5 MB")
    suffix = PurePath(file.filename or "").suffix.lower()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ApiError(INVALID_PARAMETER, "导入文件必须是 UTF-8 编码") from None
    if suffix == ".json":
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            raise ApiError(INVALID_PARAMETER, "导入文件不是有效 JSON") from None
        rows = payload.get("items") if isinstance(payload, dict) else payload
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise ApiError(INVALID_PARAMETER, "JSON 导入文件必须是对象数组")
    elif suffix in {".csv", ".tsv", ".txt"}:
        reader = csv.DictReader(io.StringIO(text), delimiter="\t" if suffix == ".tsv" else ",")
        rows = []
        for number, raw in enumerate(reader, start=2):
            unknown = sorted(set(raw) - allowed - {None})
            if unknown:
                raise ApiError(INVALID_PARAMETER, f"第 {number} 行包含未知字段：{', '.join(unknown)}")
            rows.append({key: _parse_cell(key, value) for key, value in raw.items() if key is not None})
    else:
        raise ApiError(INVALID_PARAMETER, "只支持 JSON、CSV 或 TSV 导入文件")
    if not rows:
        raise ApiError(INVALID_PARAMETER, "导入文件没有数据行")
    if len(rows) > _MAX_IMPORT_ROWS:
        raise ApiError(INVALID_PARAMETER, f"一次最多导入 {_MAX_IMPORT_ROWS} 行")
    return rows


def create_v1_content_router(service: MaterialsService, *, compound_source: Any = None,
                             use_configured_compound_source: bool = True) -> APIRouter:
    kwargs: dict[str, Any] = {} if use_configured_compound_source and compound_source is None else {"compound_source": compound_source}
    contract = ContentContract(service, **kwargs)
    router = envelope_router(prefix="/api/v1", tags=["content-contract"], profile=RESOURCE)
    info_fields = set(ReagentInfoCreateRequest.model_fields)
    reagent_fields = set(ReagentCreateRequest.model_fields)

    # ── 化合物 / 试剂信息 ─────────────────────────────────────

    @router.get("/compounds/{cas}")
    def lookup_compound(cas: str) -> Any:
        return contract.lookup_compound(cas)

    @router.post("/reagent-infos", status_code=201)
    def create_reagent_info(body: ReagentInfoCreateRequest) -> Any:
        return contract.create_reagent_info(body.model_dump(mode="json"))

    @router.get("/reagent-infos")
    def list_reagent_infos(page: int = Query(default=0), page_size: int = Query(default=0), name: str = Query(default=""),
                           cas: str = Query(default=""), physical_state: str = Query(default="")) -> Any:
        return contract.list_reagent_infos(page=page, page_size=page_size, name=name, cas=cas, physical_state=physical_state)

    @router.post("/reagent-infos/batch")
    def create_reagent_infos_batch(body: BatchRequest) -> Any:
        rows = _validated_rows(body.items, ReagentInfoCreateRequest)
        return batch_create(rows, atomic=body.atomic, batch=contract.create_reagent_infos, single=contract.create_reagent_info)

    @router.post("/reagent-infos/import")
    async def import_reagent_infos(file: UploadFile = File(...), atomic: bool = Query(default=True)) -> Any:
        rows = _validated_rows(await _read_import_rows(file, info_fields), ReagentInfoCreateRequest)
        return batch_create(rows, atomic=atomic, batch=contract.create_reagent_infos, single=contract.create_reagent_info)

    @router.get("/reagent-infos/{reagent_info_uuid}")
    def get_reagent_info(reagent_info_uuid: str) -> Any:
        return contract.get_reagent_info(reagent_info_uuid)

    @router.get("/reagent-infos/{reagent_info_uuid}/structure-3d")
    def get_reagent_info_structure(reagent_info_uuid: str) -> Any:
        return contract.get_reagent_info_structure(reagent_info_uuid)

    @router.put("/reagent-infos/{reagent_info_uuid}")
    def update_reagent_info(reagent_info_uuid: str, body: ReagentInfoUpdateRequest) -> Any:
        return contract.update_reagent_info(reagent_info_uuid, body.model_dump(mode="json", exclude_unset=True))

    @router.delete("/reagent-infos/{reagent_info_uuid}")
    def delete_reagent_info(reagent_info_uuid: str) -> None:
        contract.delete_reagent_info(reagent_info_uuid)

    # ── 试剂 ──────────────────────────────────────────────────

    @router.post("/reagents", status_code=201)
    def create_reagent(body: ReagentCreateRequest) -> Any:
        return contract.create_reagent(body.model_dump(mode="json"))

    @router.post("/reagents/batch")
    def create_reagents_batch(body: BatchRequest) -> Any:
        rows = _validated_rows(body.items, ReagentCreateRequest)
        return batch_create(rows, atomic=body.atomic, batch=contract.create_reagents, single=contract.create_reagent)

    @router.post("/reagents/import")
    async def import_reagents(file: UploadFile = File(...), atomic: bool = Query(default=True)) -> Any:
        rows = _validated_rows(await _read_import_rows(file, reagent_fields), ReagentCreateRequest)
        return batch_create(rows, atomic=atomic, batch=contract.create_reagents, single=contract.create_reagent)

    @router.get("/reagents")
    def list_reagents(page: int = Query(default=0), page_size: int = Query(default=0),
                      material_uuid: Optional[str] = Query(default=None), reagent_info_uuid: Optional[str] = Query(default=None),
                      keyword: str = Query(default=""), cas: str = Query(default=""), barcode: str = Query(default="")) -> Any:
        return contract.list_reagents(page=page, page_size=page_size, material_uuid=material_uuid or "",
                                      reagent_info_uuid=reagent_info_uuid or "", keyword=keyword, cas=cas, barcode=barcode)

    @router.get("/reagents/{reagent_uuid}")
    def get_reagent(reagent_uuid: str) -> Any:
        return contract.get_reagent(reagent_uuid)

    @router.put("/reagents/{reagent_uuid}")
    def update_reagent(reagent_uuid: str, body: ReagentUpdateRequest) -> Any:
        return contract.update_reagent(reagent_uuid, body.model_dump(mode="json", exclude_unset=True))

    @router.delete("/reagents/{reagent_uuid}")
    def delete_reagent(reagent_uuid: str) -> None:
        contract.delete_reagent(reagent_uuid)

    @router.get("/reagent-history/{history_uuid}")
    def get_reagent_history(history_uuid: str) -> Any:
        return contract.get_history(history_uuid, _REAGENT_SUBJECTS)

    @router.get("/materials/{material_uuid}/reagent-history")
    def list_reagent_history(material_uuid: str, page: int = Query(default=0), page_size: int = Query(default=0)) -> Any:
        return contract.list_history(material_uuid, _REAGENT_SUBJECTS, page=page, page_size=page_size)

    # ── 样品 ──────────────────────────────────────────────────

    @router.post("/samples", status_code=201)
    def create_sample(body: SampleRequest) -> Any:
        return contract.create_sample(body.model_dump(mode="json"))

    @router.get("/samples")
    def list_samples(page: int = Query(default=0), page_size: int = Query(default=0),
                     material_uuid: Optional[str] = Query(default=None), code: str = Query(default=""),
                     name: str = Query(default=""), keyword: str = Query(default=""), barcode: str = Query(default="")) -> Any:
        return contract.list_samples(page=page, page_size=page_size, material_uuid=material_uuid or "", code=code,
                                     name=name, keyword=keyword, barcode=barcode)

    @router.get("/samples/{sample_uuid}")
    def get_sample(sample_uuid: str) -> Any:
        return contract.get_sample(sample_uuid)

    @router.put("/samples/{sample_uuid}")
    def update_sample(sample_uuid: str, body: SampleRequest) -> Any:
        return contract.update_sample(sample_uuid, body.model_dump(mode="json"))

    @router.delete("/samples/{sample_uuid}")
    def delete_sample(sample_uuid: str) -> None:
        contract.delete_sample(sample_uuid)

    # ── 当前内容物 ────────────────────────────────────────────

    @router.post("/current-substances", status_code=201)
    def create_current_substance(body: CurrentSubstanceRequest) -> Any:
        return contract.create_current_substance(body.model_dump(mode="json"))

    @router.get("/current-substances")
    def list_current_substances(page: int = Query(default=0), page_size: int = Query(default=0),
                                material_uuid: Optional[str] = Query(default=None)) -> Any:
        return contract.list_current_substances(page=page, page_size=page_size, material_uuid=material_uuid or "")

    @router.get("/current-substances/{substance_uuid}")
    def get_current_substance(substance_uuid: str) -> Any:
        return contract.get_current_substance(substance_uuid)

    @router.put("/current-substances/{substance_uuid}")
    def update_current_substance(substance_uuid: str, body: CurrentSubstanceRequest) -> Any:
        return contract.update_current_substance(substance_uuid, body.model_dump(mode="json"))

    @router.delete("/current-substances/{substance_uuid}")
    def delete_current_substance(substance_uuid: str) -> None:
        contract.delete_current_substance(substance_uuid)

    @router.get("/substance-history/{history_uuid}")
    def get_substance_history(history_uuid: str) -> Any:
        return contract.get_history(history_uuid, _SUBSTANCE_SUBJECTS)

    @router.get("/materials/{material_uuid}/current-substance")
    def get_current_substance_by_material(material_uuid: str) -> Any:
        return contract.get_current_substance_by_material(material_uuid)

    @router.get("/materials/{material_uuid}/substance-history")
    def list_substance_history(material_uuid: str, page: int = Query(default=0), page_size: int = Query(default=0)) -> Any:
        return contract.list_history(material_uuid, _SUBSTANCE_SUBJECTS, page=page, page_size=page_size)

    return router


def install_v1_content_api(app: FastAPI, service: MaterialsService, **kwargs: Any) -> None:
    app.include_router(create_v1_content_router(service, **kwargs))


__all__ = ["create_v1_content_router", "install_v1_content_api"]
