"""FastAPI 应用 + CRUD API + 启动入口。

用法: python -m unilabos.labware_manager.app
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from unilabos.labware_manager.models import LabwareDB, LabwareItem

_HERE = Path(__file__).resolve().parent
_DB_PATH = _HERE / "labware_db.json"

app = FastAPI(title="PRCXI 耗材管理", version="1.0")

# 静态文件 + 模板
app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")
templates = Jinja2Templates(directory=str(_HERE / "templates"))


# ---------- DB 读写 ----------

def _load_db() -> LabwareDB:
    if not _DB_PATH.exists():
        return LabwareDB()
    with open(_DB_PATH, "r", encoding="utf-8") as f:
        return LabwareDB(**json.load(f))


def _save_db(db: LabwareDB) -> None:
    with open(_DB_PATH, "w", encoding="utf-8") as f:
        json.dump(db.model_dump(), f, ensure_ascii=False, indent=2)


# ---------- 页面路由 ----------

@app.get("/", response_class=HTMLResponse)
async def index_page(request: Request):
    db = _load_db()
    # 按 type 分组
    groups = {}
    for item in db.items:
        groups.setdefault(item.type, []).append(item)
    return templates.TemplateResponse("index.html", {
        "request": request,
        "groups": groups,
        "total": len(db.items),
    })


@app.get("/labware/new", response_class=HTMLResponse)
async def new_page(request: Request, type: str = "plate"):
    return templates.TemplateResponse("edit.html", {
        "request": request,
        "item": None,
        "labware_type": type,
        "is_new": True,
    })


@app.get("/labware/{item_id}", response_class=HTMLResponse)
async def detail_page(request: Request, item_id: str):
    db = _load_db()
    item = _find_item(db, item_id)
    if not item:
        raise HTTPException(404, "耗材不存在")
    return templates.TemplateResponse("detail.html", {
        "request": request,
        "item": item,
    })


@app.get("/labware/{item_id}/edit", response_class=HTMLResponse)
async def edit_page(request: Request, item_id: str):
    db = _load_db()
    item = _find_item(db, item_id)
    if not item:
        raise HTTPException(404, "耗材不存在")
    return templates.TemplateResponse("edit.html", {
        "request": request,
        "item": item,
        "labware_type": item.type,
        "is_new": False,
    })


# ---------- API 端点 ----------

@app.get("/api/labware")
async def api_list_labware():
    db = _load_db()
    return {"items": [item.model_dump() for item in db.items]}


@app.post("/api/labware")
async def api_create_labware(request: Request):
    data = await request.json()
    db = _load_db()
    item = LabwareItem(**data)
    # 确保 id 唯一
    existing_ids = {it.id for it in db.items}
    while item.id in existing_ids:
        import uuid
        item.id = uuid.uuid4().hex[:8]
    db.items.append(item)
    _save_db(db)
    return {"status": "ok", "id": item.id}


@app.put("/api/labware/{item_id}")
async def api_update_labware(item_id: str, request: Request):
    data = await request.json()
    db = _load_db()
    for i, it in enumerate(db.items):
        if it.id == item_id or it.function_name == item_id:
            updated = LabwareItem(**{**it.model_dump(), **data, "id": it.id})
            db.items[i] = updated
            _save_db(db)
            return {"status": "ok", "id": it.id}
    raise HTTPException(404, "耗材不存在")


@app.delete("/api/labware/{item_id}")
async def api_delete_labware(item_id: str):
    db = _load_db()
    original_len = len(db.items)
    db.items = [it for it in db.items if it.id != item_id and it.function_name != item_id]
    if len(db.items) == original_len:
        raise HTTPException(404, "耗材不存在")
    _save_db(db)
    return {"status": "ok"}


@app.post("/api/generate-code")
async def api_generate_code(request: Request):
    body = await request.json() if await request.body() else {}
    test_mode = body.get("test_mode", True)
    db = _load_db()
    if not db.items:
        raise HTTPException(400, "数据库为空，请先导入")

    from unilabos.labware_manager.codegen import generate_code
    from unilabos.labware_manager.yaml_gen import generate_yaml

    py_path = generate_code(db, test_mode=test_mode)
    yaml_paths = generate_yaml(db, test_mode=test_mode)

    return {
        "status": "ok",
        "python_file": str(py_path),
        "yaml_files": [str(p) for p in yaml_paths],
        "test_mode": test_mode,
    }


@app.post("/api/import-from-code")
async def api_import_from_code():
    from unilabos.labware_manager.importer import import_from_code, save_db
    db = import_from_code()
    save_db(db)
    return {
        "status": "ok",
        "count": len(db.items),
        "items": [{"function_name": it.function_name, "type": it.type} for it in db.items],
    }


# ---------- 辅助函数 ----------

def _find_item(db: LabwareDB, item_id: str) -> Optional[LabwareItem]:
    for item in db.items:
        if item.id == item_id or item.function_name == item_id:
            return item
    return None


# ---------- 启动入口 ----------

def main():
    import uvicorn
    port = int(os.environ.get("LABWARE_PORT", "8010"))
    print(f"PRCXI 耗材管理 → http://localhost:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
