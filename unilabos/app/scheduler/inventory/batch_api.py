"""物料与试剂批量创建共用的逐行校验及结果响应。"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Sequence, Type

from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from unilabos.app.scheduler.inventory.backend_contract import (
    INVALID_PARAMETER,
    BackendContractError,
)
from unilabos.app.scheduler.inventory.backend_response import success

MAX_BATCH_ROWS = 500


def batch_error_response(
    message: str,
    *,
    details: Dict[str, Any],
    code: int = INVALID_PARAMETER,
) -> JSONResponse:
    """构造带行号明细的批量导入错误信封。"""

    return JSONResponse(
        status_code=200,
        content={
            "code": code,
            "error": {"msg": message, "details": details},
        },
    )


def _validation_errors(error: ValidationError) -> List[Dict[str, str]]:
    """将 Pydantic 错误压缩成稳定的字段/消息结构。"""

    return [
        {
            "field": ".".join(str(part) for part in detail.get("loc", ())) or "row",
            "message": str(detail.get("msg") or "字段无效"),
        }
        for detail in error.errors()
    ]


def _validated_rows(
    rows: Sequence[Dict[str, Any]],
    model: Type[BaseModel],
) -> tuple[List[tuple[int, Dict[str, Any]]], List[Dict[str, Any]]]:
    """逐行执行 DTO 校验，返回可写行与行级错误。"""

    valid: List[tuple[int, Dict[str, Any]]] = []
    errors: List[Dict[str, Any]] = []
    for row_number, row in enumerate(rows, 1):
        try:
            parsed = model.model_validate(row)
        except ValidationError as error:
            errors.append({"row": row_number, "errors": _validation_errors(error)})
            continue
        valid.append((row_number, parsed.model_dump(mode="json")))
    return valid, errors


def create_batch_response(
    rows: Sequence[Dict[str, Any]],
    *,
    model: Type[BaseModel],
    batch_create: Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]],
    single_create: Callable[[Dict[str, Any]], Dict[str, Any]],
    atomic: bool,
) -> JSONResponse:
    """统一执行 JSON/文件批量导入并返回行级结果。"""

    valid, errors = _validated_rows(rows, model)
    total = len(rows)
    if atomic and errors:
        return batch_error_response(
            "批量导入校验失败，未写入任何数据",
            details={"total": total, "created": 0, "failed": len(errors), "errors": errors},
        )
    if not valid:
        return batch_error_response(
            "批量导入没有可写入的数据",
            details={"total": total, "created": 0, "failed": len(errors), "errors": errors},
        )

    created: List[Dict[str, Any]] = []
    if atomic:
        try:
            created = batch_create([payload for _, payload in valid])
        except BackendContractError as error:
            batch_error = {
                "row": None,
                "errors": [{"message": error.message}],
            }
            return batch_error_response(
                error.message,
                details={
                    "total": total,
                    "created": 0,
                    "failed": total,
                    "errors": [*errors, batch_error],
                },
                code=error.code,
            )
    else:
        for row_number, payload in valid:
            try:
                created.append(single_create(payload))
            except BackendContractError as error:
                errors.append({"row": row_number, "errors": [{"message": error.message}]})
    result = {
        "total": total,
        "created": len(created),
        "failed": len(errors),
        "atomic": atomic,
        "items": created,
        "errors": errors,
    }
    return success(result)
