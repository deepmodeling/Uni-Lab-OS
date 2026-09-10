"""Small persistent reagent API for the legacy local Web runtime.

The current console uses the Backend-shaped ``/reagent-infos`` and
``/reagents`` contract.  Older Edge runtimes expose materials but do not yet
ship the reagent tables, so this adapter keeps the contract available locally
without changing the scheduler or inventory authority.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _envelope(data: Any = None, *, status_code: int = 200) -> JSONResponse:
    body: dict[str, Any] = {"code": 0}
    if data is not None:
        body["data"] = data
    return JSONResponse(status_code=status_code, content=body)


def _error(message: str, *, status_code: int = 400) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"code": 1, "error": {"msg": message}})


def _json_object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


class ReagentCompatStore:
    """SQLite-backed catalog and container inventory used by the compatibility router."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS reagent_infos (
                    uuid TEXT PRIMARY KEY, name TEXT NOT NULL, name_en TEXT,
                    aliases_json TEXT NOT NULL, cas TEXT, molecular_formula TEXT,
                    smiles TEXT, inchi_key TEXT, molecular_weight REAL,
                    density_g_per_ml REAL, physical_state TEXT NOT NULL,
                    description TEXT, meta_data_json TEXT NOT NULL,
                    update_time TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reagents (
                    uuid TEXT PRIMARY KEY, material_uuid TEXT NOT NULL UNIQUE,
                    reagent_info_uuid TEXT NOT NULL, quantity REAL NOT NULL,
                    quantity_unit TEXT NOT NULL, concentration_value REAL,
                    concentration_unit TEXT, description TEXT,
                    meta_data_json TEXT NOT NULL, revision INTEGER NOT NULL,
                    update_time TEXT NOT NULL
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def list_infos(self, page: int, page_size: int) -> dict[str, Any]:
        with self._connect() as conn:
            total = int(conn.execute("SELECT COUNT(*) FROM reagent_infos").fetchone()[0])
            offset = max(page - 1, 0) * page_size if page_size else 0
            limit = page_size or max(total, 1)
            rows = conn.execute(
                "SELECT * FROM reagent_infos ORDER BY update_time DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return {"items": [self._info(row) for row in rows], "total": total, "has_more": offset + len(rows) < total}

    def list_reagents(self, page: int, page_size: int) -> dict[str, Any]:
        with self._connect() as conn:
            total = int(conn.execute("SELECT COUNT(*) FROM reagents").fetchone()[0])
            offset = max(page - 1, 0) * page_size if page_size else 0
            limit = page_size or max(total, 1)
            rows = conn.execute(
                """SELECT r.*, i.name, i.cas, i.molecular_formula, i.physical_state,
                   i.density_g_per_ml FROM reagents r JOIN reagent_infos i
                   ON i.uuid = r.reagent_info_uuid ORDER BY r.update_time DESC
                   LIMIT ? OFFSET ?""",
                (limit, offset),
            ).fetchall()
        return {"items": [self._reagent(row) for row in rows], "total": total, "has_more": offset + len(rows) < total}

    @staticmethod
    def _info(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["aliases"] = json.loads(result.pop("aliases_json") or "[]")
        result["meta_data"] = json.loads(result.pop("meta_data_json") or "{}")
        return result

    @staticmethod
    def _reagent(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["meta_data"] = json.loads(result.pop("meta_data_json") or "{}")
        result["active_workflow_reserved_quantity"] = 0
        result["container_name"] = result.get("material_uuid")
        return result

    def create_info(self, values: dict[str, Any]) -> dict[str, Any]:
        identity = str(uuid4())
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO reagent_infos
                (uuid,name,name_en,aliases_json,cas,molecular_formula,smiles,inchi_key,
                 molecular_weight,density_g_per_ml,physical_state,description,meta_data_json,update_time)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (identity, str(values.get("name", "")).strip(), values.get("name_en"),
                 json.dumps(values.get("aliases") or [], ensure_ascii=False), values.get("cas") or None,
                 values.get("molecular_formula"), values.get("smiles"), values.get("inchi_key"),
                 values.get("molecular_weight"), values.get("density_g_per_ml"), values.get("physical_state") or "unknown",
                 values.get("description"), json.dumps(_json_object(values.get("meta_data")), ensure_ascii=False), now),
            )
        return self.get_info(identity) or {}

    def get_info(self, identity: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM reagent_infos WHERE uuid = ?", (identity,)).fetchone()
        return self._info(row) if row else None

    def delete_info(self, identity: str) -> bool:
        with self._connect() as conn:
            if conn.execute("SELECT 1 FROM reagents WHERE reagent_info_uuid = ? LIMIT 1", (identity,)).fetchone():
                return False
            return conn.execute("DELETE FROM reagent_infos WHERE uuid = ?", (identity,)).rowcount > 0

    def create_reagent(self, values: dict[str, Any]) -> dict[str, Any]:
        info_uuid = str(values.get("reagent_info_uuid") or "")
        material_uuid = str(values.get("material_uuid") or "")
        if not material_uuid or not self.get_info(info_uuid):
            raise ValueError("material_uuid 和 reagent_info_uuid 必须引用已存在记录")
        identity, now = str(uuid4()), _now()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO reagents
                (uuid,material_uuid,reagent_info_uuid,quantity,quantity_unit,concentration_value,
                 concentration_unit,description,meta_data_json,revision,update_time)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (identity, material_uuid, info_uuid, float(values.get("quantity", 0)),
                 str(values.get("quantity_unit") or "mL"), values.get("concentration_value"),
                 values.get("concentration_unit"), values.get("description"),
                 json.dumps(_json_object(values.get("meta_data")), ensure_ascii=False), 1, now),
            )
        return next(item for item in self.list_reagents(1, 0)["items"] if item["uuid"] == identity)

    def update_reagent(self, identity: str, values: dict[str, Any]) -> dict[str, Any] | None:
        now = _now()
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM reagents WHERE uuid = ?", (identity,)).fetchone()
            if not row:
                return None
            expected = values.get("expected_revision")
            if expected is not None and int(expected) != int(row["revision"]):
                raise ValueError("试剂记录版本冲突，请刷新后重试")
            conn.execute(
                """UPDATE reagents SET quantity=?, quantity_unit=?, concentration_value=?,
                   concentration_unit=?, description=?, meta_data_json=?, revision=revision+1,
                   update_time=? WHERE uuid=?""",
                (float(values.get("quantity", row["quantity"])), values.get("quantity_unit") or row["quantity_unit"],
                 values.get("concentration_value"), values.get("concentration_unit"), values.get("description"),
                 json.dumps(_json_object(values.get("meta_data")), ensure_ascii=False), now, identity),
            )
        return next(item for item in self.list_reagents(1, 0)["items"] if item["uuid"] == identity)

    def delete_reagent(self, identity: str) -> bool:
        with self._connect() as conn:
            return conn.execute("DELETE FROM reagents WHERE uuid = ?", (identity,)).rowcount > 0


def create_reagent_compat_router(store: ReagentCompatStore) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["reagents"])

    @router.get("/compounds/{cas}")
    def lookup_compound(cas: str) -> JSONResponse:
        return _envelope({"cas": cas, "status": "not_found", "message": "本地模式未配置化合物查询服务"})

    @router.get("/reagent-infos")
    def list_infos(page: int = Query(1), page_size: int = Query(100)) -> JSONResponse:
        return _envelope(store.list_infos(page, page_size))

    @router.post("/reagent-infos")
    async def create_info(request: Request) -> JSONResponse:
        values = await request.json()
        name = str(values.get("name") or "").strip()
        if not name:
            return _error("name 不能为空")
        return _envelope(store.create_info(values), status_code=201)

    @router.delete("/reagent-infos/{identity}")
    def delete_info(identity: str) -> JSONResponse:
        if not store.delete_info(identity):
            return _error("试剂目录不存在或已被库存引用", status_code=409)
        return _envelope()

    @router.get("/reagents")
    def list_reagents(page: int = Query(1), page_size: int = Query(100)) -> JSONResponse:
        return _envelope(store.list_reagents(page, page_size))

    @router.post("/reagents")
    async def create_reagent(request: Request) -> JSONResponse:
        try:
            return _envelope(store.create_reagent(await request.json()), status_code=201)
        except (ValueError, sqlite3.IntegrityError) as exc:
            return _error(str(exc))

    @router.put("/reagents/{identity}")
    async def update_reagent(identity: str, request: Request) -> JSONResponse:
        try:
            value = store.update_reagent(identity, await request.json())
        except ValueError as exc:
            return _error(str(exc), status_code=409)
        return _envelope(value) if value else _error("试剂记录不存在", status_code=404)

    @router.delete("/reagents/{identity}")
    def delete_reagent(identity: str) -> JSONResponse:
        return _envelope() if store.delete_reagent(identity) else _error("试剂记录不存在", status_code=404)

    @router.get("/materials/{material_uuid}/reagent-history")
    def reagent_history(material_uuid: str) -> JSONResponse:
        return _envelope({"items": [], "total": 0, "has_more": False})

    return router


__all__ = ["ReagentCompatStore", "create_reagent_compat_router"]
