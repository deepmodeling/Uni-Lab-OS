"""原子 Material I/O 的聚焦合同测试。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.app.scheduler.inventory.backend_api import install_backend_resource_api
from unilabos.app.scheduler.inventory.backend_contract import (
    BackendContractError,
    BackendResourceService,
)
from unilabos.app.scheduler.inventory.domain import (
    MaterialRequirement,
    MaterialSourceAdmissionRequest,
)
from unilabos.app.scheduler.inventory.material_io import (
    MaterialIOError,
    allocate_load_material_name,
    material_io_change_set,
    material_io_command_id,
    migrate_material_io_schema,
)
from unilabos.app.scheduler.inventory.reagent_contract import BackendReagentService
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.app.scheduler.resource_lock import (
    material_lock_key,
    site_lock_key,
)
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.workflow.task_scheduler_bridge import TaskSchedulerBridge


def _template(
    service: BackendResourceService,
    name: str,
    *,
    available_sites: list[dict] | None = None,
    category: list[str] | None = None,
) -> str:
    result = service.sync_resource_templates(
        [
            {
                "id": name,
                "display_name": name,
                "registry_type": "resource",
                "available_sites": available_sites or [],
                "category": category or [],
            }
        ]
    )
    return result["templates"][0]["uuid"]


def _mount(
    service: BackendResourceService,
    store: InventoryStore,
    *,
    allowed_template_uuid: str,
) -> tuple[str, str]:
    mount_template_uuid = _template(service, f"mount-{uuid4()}")
    mount = service.create_material(
        {
            "resource_template_uuid": mount_template_uuid,
            "name": f"Mount {uuid4()}",
        }
    )
    site_uuid = str(uuid4())
    now = "2026-09-18T00:00:00.000Z"
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO site(
                uuid,create_time,update_time,deleted_at,description,meta_data,
                material_uuid,name,sort_order,allowed_resource_template_uuids,
                occupied_material_uuid,position_x,position_y,position_z,
                depth,length,width
            ) VALUES (?,?,?,NULL,NULL,'{}',?,'SOURCE',0,?,NULL,0,0,0,0,0,0)
            """,
            (
                site_uuid,
                now,
                now,
                mount["uuid"],
                json.dumps([allowed_template_uuid]),
            ),
        )
    return mount["uuid"], site_uuid


def _service(tmp_path: Path) -> tuple[BackendResourceService, InventoryStore]:
    store = InventoryStore(str(tmp_path / "material-io.db"))
    return BackendResourceService(store), store


def _install_permit(
    store: InventoryStore,
    *,
    canonical_request: dict,
    lock_keys: set[str],
    operation: str,
    task_uuid: str | None = None,
) -> dict:
    """安装一份与生产 Claim/Lease 同形的完整测试 Permit。"""

    claim_uuid = str(uuid4())
    effect_uuid = str(uuid4())
    task_uuid = task_uuid or str(uuid4())
    job_uuid = str(uuid4())
    now = "2026-09-18T00:03:00.000Z"
    expected_change_set = material_io_change_set(operation, canonical_request)
    fences = {
        lock_key: ordinal
        for ordinal, lock_key in enumerate(sorted(lock_keys), start=1)
    }
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO station_execution_claim(
                claim_uuid,effect_uuid,task_uuid,job_uuid,attempt,parameter_hash,
                expected_change_set,resource_keys,state,acquired_at,committed_at,
                released_at,update_time
            ) VALUES (?,?,?,?,?,'sha256:material-io',?,?,'running',?,?,NULL,?)
            """,
            (
                claim_uuid,
                effect_uuid,
                task_uuid,
                job_uuid,
                canonical_request["attempt"],
                json.dumps(expected_change_set, sort_keys=True, separators=(",", ":")),
                json.dumps(sorted(lock_keys), separators=(",", ":")),
                now,
                now,
                now,
            ),
        )
        for lock_key, token in fences.items():
            scope = (
                "device"
                if lock_key.startswith("/devices/")
                else "material_site"
                if "/site/" in lock_key
                else "material"
            )
            connection.execute(
                """
                INSERT INTO station_execution_lock_lease(
                    claim_uuid,lock_key,scope,material_uuid,site_uuid,fencing_token,
                    state,acquired_at,released_at,update_time
                ) VALUES (?,?,?,?,?,?, 'running',?,NULL,?)
                """,
                (
                    claim_uuid,
                    lock_key,
                    scope,
                    None,
                    None,
                    token,
                    now,
                    now,
                ),
            )
    return {
        "effect_uuid": effect_uuid,
        "claim_uuid": claim_uuid,
        "task_uuid": task_uuid,
        "job_uuid": job_uuid,
        "attempt": canonical_request["attempt"],
        "parameter_hash": "sha256:material-io",
        "expected_change_set": expected_change_set,
        "fences": fences,
    }


def _release_permit(store: InventoryStore, permit: dict) -> None:
    now = "2026-09-18T00:04:00.000Z"
    with store.transaction() as connection:
        connection.execute(
            "UPDATE station_execution_lock_lease "
            "SET state='released',released_at=?,update_time=? WHERE claim_uuid=?",
            (now, now, permit["claim_uuid"]),
        )
        connection.execute(
            "UPDATE station_execution_claim "
            "SET state='released',released_at=?,update_time=? WHERE claim_uuid=?",
            (now, now, permit["claim_uuid"]),
        )


def _install_initial_site_definition(
    service: BackendResourceService,
    store: InventoryStore,
    *,
    template_uuid: str,
    child_template_uuid: str,
) -> None:
    """经公共模板同步合同安装包含默认子物料的 Site 定义。"""

    definitions = [
        {
            "schema_version": 1,
            "index": 0,
            "label": "A1",
            "visible": True,
            "position_x": 1,
            "position_y": 2,
            "position_z": 3,
            "width": 4,
            "length": 5,
            "depth": 6,
            "rotation_x": 0,
            "rotation_y": 0,
            "rotation_z": 0,
            "content_type": [],
            "allowed_resource_template_uuids": [child_template_uuid],
            "parent_link": "",
            "description": "initial",
            "meta_data": {"channel": "A"},
            "initial_material_template_uuid": child_template_uuid,
        },
        {
            "schema_version": 1,
            "index": 1,
            "label": "A2",
            "allowed_resource_template_uuids": [],
        },
    ]
    template = store.query_one(
        "SELECT name,display_name FROM resource_template WHERE uuid=?",
        (template_uuid,),
    )
    service.sync_resource_templates(
        [
            {
                "id": template["name"],
                "display_name": template["display_name"],
                "registry_type": "resource",
                "available_sites": definitions,
            }
        ]
    )


def test_load_accepts_mount_site_name_and_locks_the_resolved_uuid(
    tmp_path: Path,
) -> None:
    """动态上料传入挂载物料上的库位名时，占用和资源锁都落在该库位 UUID。"""

    service, store = _service(tmp_path)
    container_template_uuid = _template(service, "named-site-box")
    mount_uuid, mount_site_uuid = _mount(
        service, store, allowed_template_uuid=container_template_uuid
    )
    other_mount, other_site = _mount(
        service, store, allowed_template_uuid=container_template_uuid
    )
    with store.transaction() as connection:
        connection.execute(
            "UPDATE site SET name='BOX1' WHERE uuid=?",
            (mount_site_uuid,),
        )
        connection.execute(
            "UPDATE site SET name='BOX1' WHERE uuid=?",
            (other_site,),
        )
    request = {
        "template_uuid": container_template_uuid,
        "mount_material_uuid": mount_uuid,
        "site": "box1",
        "name": "Named site box",
        "workflow_id": "wf-site-name",
        "node_id": "load-by-name",
        "attempt": 1,
    }
    locks = service._material_io.resolve_resources(
        operation="load",
        mount_material_uuid=mount_uuid,
        site="box1",
    )
    lock_keys = {item["lock_key"] for item in locks}
    assert site_lock_key(mount_uuid, mount_site_uuid) in lock_keys
    assert site_lock_key(mount_uuid, "box1") not in lock_keys
    assert site_lock_key(other_mount, other_site) not in lock_keys

    canonical = {"operation": "load", **request}
    permit = _install_permit(
        store,
        canonical_request=canonical,
        operation="load",
        lock_keys=lock_keys,
    )
    loaded = service.material_io_settle_load(request, permit)
    replay = service.material_io_settle_load(request, permit)

    assert replay == loaded
    ledger = store.query_one(
        "SELECT workflow_task_uuid, workflow_node_job_uuid FROM inventory_ledger "
        "WHERE subject_type='material' AND material_uuid=? AND op_type='material.load'",
        (loaded["material_uuid"],),
    )
    assert ledger["workflow_task_uuid"] == "wf-site-name"
    assert ledger["workflow_node_job_uuid"] == permit["job_uuid"]
    listed = service.list_material_ledger(loaded["material_uuid"])
    assert listed["items"][0]["workflow_node_job_uuid"] == permit["job_uuid"]
    assert loaded["mount_site_uuid"] == mount_site_uuid
    assert store.query_one(
        "SELECT occupied_material_uuid FROM site WHERE uuid=?",
        (mount_site_uuid,),
    ) == {"occupied_material_uuid": loaded["material_uuid"]}
    assert store.query_one(
        "SELECT occupied_material_uuid FROM site WHERE uuid=?",
        (other_site,),
    ) == {"occupied_material_uuid": None}
    with pytest.raises(BackendContractError) as missing:
        service.material_io_load(
            {
                **request,
                "mount_material_uuid": other_mount,
                "site_uuid": mount_site_uuid,
                "node_id": "load-foreign-uuid",
                "name": "Foreign uuid",
            }
        )
    assert missing.value.code == 6006
    store.close()


def test_load_prefers_site_uuid_when_another_site_uses_that_name(
    tmp_path: Path,
) -> None:
    """同一挂载物料上，传入值既是某库位 UUID 又是另一库位的名字时认 UUID。"""

    service, store = _service(tmp_path)
    container_template_uuid = _template(service, "uuid-wins")
    mount_uuid, uuid_site = _mount(
        service, store, allowed_template_uuid=container_template_uuid
    )
    named_like_uuid = str(uuid4())
    now = "2026-09-18T00:00:00.000Z"
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO site(
                uuid,create_time,update_time,deleted_at,description,meta_data,
                material_uuid,name,sort_order,allowed_resource_template_uuids,
                occupied_material_uuid,position_x,position_y,position_z,
                depth,length,width
            ) VALUES (?,?,?,NULL,NULL,'{}',?,?,1,?,NULL,0,0,0,0,0,0)
            """,
            (
                named_like_uuid,
                now,
                now,
                mount_uuid,
                uuid_site,
                json.dumps([container_template_uuid]),
            ),
        )
    loaded = service.material_io_load(
        {
            "template_uuid": container_template_uuid,
            "mount_material_uuid": mount_uuid,
            "site_uuid": uuid_site,
            "name": "UUID wins",
            "workflow_id": "wf-uuid-wins",
            "node_id": "load",
            "attempt": 1,
        }
    )
    assert loaded["mount_site_uuid"] == uuid_site
    assert store.query_one(
        "SELECT occupied_material_uuid FROM site WHERE uuid=?",
        (named_like_uuid,),
    ) == {"occupied_material_uuid": None}
    name_only = str(uuid4())
    with store.transaction() as connection:
        connection.execute(
            "UPDATE site SET name=? WHERE uuid=?",
            (name_only, named_like_uuid),
        )
    with pytest.raises(BackendContractError) as missing_uuid:
        service.material_io_load(
            {
                "template_uuid": container_template_uuid,
                "mount_material_uuid": mount_uuid,
                "site_uuid": name_only,
                "name": "Name shaped like uuid",
                "workflow_id": "wf-uuid-wins",
                "node_id": "load-name-shaped-like-uuid",
                "attempt": 1,
            }
        )
    assert missing_uuid.value.code == 6006
    store.close()


def test_load_atomically_creates_sites_initial_material_and_replays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, store = _service(tmp_path)
    child_template_uuid = _template(service, "tip")
    container_template_uuid = _template(service, "tip-rack")
    _install_initial_site_definition(
        service,
        store,
        template_uuid=container_template_uuid,
        child_template_uuid=child_template_uuid,
    )
    mount_uuid, mount_site_uuid = _mount(
        service, store, allowed_template_uuid=container_template_uuid
    )
    request = {
        "template_uuid": container_template_uuid,
        "mount_material_uuid": mount_uuid,
        "site_uuid": mount_site_uuid,
        "name": "Rack 01",
        "workflow_id": "wf-refill",
        "node_id": "load-source",
        "attempt": 1,
    }
    statements: list[str] = []
    execute = store._conn.execute

    def record_execute(sql: str, parameters: tuple = ()) -> Any:
        """记录业务 SQL，仍由真实数据库完成事务和读写。"""
        statements.append(sql)
        return execute(sql, parameters)

    with monkeypatch.context() as patch:
        patch.setattr(store._conn, "execute", record_execute)
        first = service.material_io_load(request)
        replay = service.material_io_load(request)
    assert replay == first
    assert store.query_one(
        "SELECT COUNT(*) AS count FROM processed_command "
        "WHERE command_id LIKE 'material_io:%'"
    ) == {"count": 1}
    command = store.query_one(
        "SELECT command_id,status,result_json FROM processed_command "
        "WHERE command_id=?",
        (material_io_command_id("wf-refill", "load-source", 1),),
    )
    assert command is not None
    assert command["status"] == "completed"
    envelope = json.loads(command["result_json"])
    assert envelope["kind"] == "material_io"
    assert envelope["operation"] == "load"
    assert envelope["effect_uuid"] == first["effect_uuid"]
    assert envelope["result"] == first
    assert not store._conn.table_exists("material_io_effect")
    root = store.query_one(
        "SELECT parent_uuid,resource_template_uuid FROM material WHERE uuid=?",
        (first["material_uuid"],),
    )
    assert root == {
        "parent_uuid": mount_uuid,
        "resource_template_uuid": container_template_uuid,
    }
    sites = store.query_all(
        "SELECT name,occupied_material_uuid FROM site "
        "WHERE material_uuid=? AND deleted_at IS NULL ORDER BY sort_order",
        (first["material_uuid"],),
    )
    assert [site["name"] for site in sites] == ["A1", "A2"]
    assert sites[0]["occupied_material_uuid"] == first["initial_material_uuids"][0]
    assert sites[1]["occupied_material_uuid"] is None
    child = store.query_one(
        "SELECT parent_uuid,resource_template_uuid FROM material WHERE uuid=?",
        (first["initial_material_uuids"][0],),
    )
    assert child == {
        "parent_uuid": first["material_uuid"],
        "resource_template_uuid": child_template_uuid,
    }
    assert store.query_one(
        "SELECT occupied_material_uuid FROM site WHERE uuid=?", (mount_site_uuid,)
    ) == {"occupied_material_uuid": first["material_uuid"]}
    # Claim 门禁自身会读取入口持续占用；Material I/O 不写批次和预留账。
    # 物料台账只追加 inventory_ledger，不改返回值。
    forbidden = (
        "inventory_lot",
        "inventory_reservation",
        "workflow_quantity_reservation",
        "material_ledger_entry",
        "warehouse",
    )
    ledger = store.query_all(
        "SELECT op_type, quantity_delta, delta_json FROM inventory_ledger "
        "WHERE subject_type='material' AND aggregate_id IN (?, ?) ORDER BY ledger_id",
        (first["material_uuid"], first["initial_material_uuids"][0]),
    )
    assert [row["op_type"] for row in ledger] == ["material.load", "material.load"]
    assert [row["quantity_delta"] for row in ledger] == [1, 1]
    assert '"name": "Rack 01"' in ledger[0]["delta_json"]
    assert '"name": "Rack 01 / A1"' in ledger[1]["delta_json"]
    assert not [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        and any(name in statement.lower() for name in forbidden)
    ]

    with pytest.raises(BackendContractError) as conflict:
        service.material_io_load({**request, "name": "Different rack"})
    assert conflict.value.code == 4002
    store.close()


def test_load_without_name_uses_local_timestamp_and_replays(tmp_path: Path) -> None:
    service, store = _service(tmp_path)
    child_template_uuid = _template(service, "tip")
    container_template_uuid = _template(service, "tip_box")
    _install_initial_site_definition(
        service,
        store,
        template_uuid=container_template_uuid,
        child_template_uuid=child_template_uuid,
    )
    mount_uuid, mount_site_uuid = _mount(
        service, store, allowed_template_uuid=container_template_uuid
    )
    second_mount, second_site = _mount(
        service, store, allowed_template_uuid=container_template_uuid
    )
    request = {
        "template_uuid": container_template_uuid,
        "mount_material_uuid": mount_uuid,
        "site_uuid": mount_site_uuid,
        "workflow_id": "wf-refill",
        "node_id": "load-auto",
        "attempt": 1,
    }

    first = service.material_io_load(request)
    replay = service.material_io_load(request)
    second = service.material_io_load({
        **request,
        "mount_material_uuid": second_mount,
        "site_uuid": second_site,
        "node_id": "load-auto-2",
    })

    assert replay == first
    assert first["name"].startswith("tip_box-")
    assert len(first["name"]) == len("tip_box-") + len("20260923-160305")
    child = store.query_one(
        "SELECT name FROM material WHERE uuid=?",
        (first["initial_material_uuids"][0],),
    )
    assert child["name"] == f"{first['name']} / A1"
    assert second["name"] != first["name"]
    assert second["name"].startswith("tip_box-")
    store.close()


def test_timestamp_name_adds_milliseconds_when_the_second_is_taken() -> None:
    from datetime import datetime

    moment = datetime(2026, 9, 23, 16, 3, 5, 120000)
    taken = {"tip_box-20260923-160305"}
    chosen = allocate_load_material_name(
        prefix="tip_box",
        now=moment,
        taken=taken.__contains__,
    )
    assert chosen == "tip_box-20260923-160305-120"


def test_load_writes_barcode_and_rejects_active_duplicate(tmp_path: Path) -> None:
    service, store = _service(tmp_path)
    container_template_uuid = _template(service, "tip-rack-barcode")
    first_mount, first_site = _mount(
        service, store, allowed_template_uuid=container_template_uuid
    )
    second_mount, second_site = _mount(
        service, store, allowed_template_uuid=container_template_uuid
    )
    request = {
        "template_uuid": container_template_uuid,
        "mount_material_uuid": first_mount,
        "site_uuid": first_site,
        "name": "tip",
        "barcode": "TIP-BOX-01",
        "workflow_id": "wf-barcode",
        "node_id": "load-barcode",
        "attempt": 1,
    }

    loaded = service.material_io_load(request)

    row = store.query_one(
        "SELECT barcode,name FROM material WHERE uuid=?",
        (loaded["material_uuid"],),
    )
    assert row == {"barcode": "TIP-BOX-01", "name": "tip"}
    with pytest.raises(BackendContractError) as conflict:
        service.material_io_load(
            {
                **request,
                "mount_material_uuid": second_mount,
                "site_uuid": second_site,
                "name": "tip-again",
                "barcode": "tip-box-01",
                "node_id": "load-barcode-duplicate",
            }
        )
    assert conflict.value.code == 6010
    store.close()


def test_load_creates_initial_material_from_template_name_hint(tmp_path: Path) -> None:
    """本地模板只写短名时，装载仍须按 hint 生成占用的默认子物料。"""

    service, store = _service(tmp_path)
    child_template_uuid = _template(service, "szlab_pipette_tip")
    container_template_uuid = _template(
        service,
        "szlab_tip_box",
        available_sites=[
            {
                "index": 0,
                "label": "T01",
                "content_type": ["szlab_pipette_tip"],
                "meta_data": {
                    "unilab": {"initial_material_template": "szlab_pipette_tip"}
                },
            }
        ],
    )
    mount_uuid, mount_site_uuid = _mount(
        service, store, allowed_template_uuid=container_template_uuid
    )

    loaded = service.material_io_load(
        {
            "template_uuid": container_template_uuid,
            "mount_material_uuid": mount_uuid,
            "site_uuid": mount_site_uuid,
            "name": "Hint rack",
            "workflow_id": "wf-hint",
            "node_id": "load-source",
            "attempt": 1,
        }
    )

    assert loaded["initial_material_uuids"]
    child = store.query_one(
        "SELECT parent_uuid,resource_template_uuid FROM material WHERE uuid=?",
        (loaded["initial_material_uuids"][0],),
    )
    assert child == {
        "parent_uuid": loaded["material_uuid"],
        "resource_template_uuid": child_template_uuid,
    }
    occupied = store.query_one(
        "SELECT occupied_material_uuid FROM site "
        "WHERE material_uuid=? AND name='T01' AND deleted_at IS NULL",
        (loaded["material_uuid"],),
    )
    assert occupied == {"occupied_material_uuid": loaded["initial_material_uuids"][0]}
    store.close()


def test_load_rolls_back_when_initial_template_is_missing(tmp_path: Path) -> None:
    service, store = _service(tmp_path)
    container_template_uuid = _template(service, "invalid-rack")
    missing_template_uuid = str(uuid4())
    _install_initial_site_definition(
        service,
        store,
        template_uuid=container_template_uuid,
        child_template_uuid=missing_template_uuid,
    )
    mount_uuid, mount_site_uuid = _mount(
        service, store, allowed_template_uuid=container_template_uuid
    )

    with pytest.raises(BackendContractError) as missing:
        service.material_io_load(
            {
                "template_uuid": container_template_uuid,
                "mount_material_uuid": mount_uuid,
                "site_uuid": mount_site_uuid,
                "name": "Must roll back",
                "workflow_id": "wf",
                "node_id": "load",
                "attempt": 1,
            }
        )

    assert missing.value.code == 6001
    assert store.query_one(
        "SELECT COUNT(*) AS count FROM material WHERE name='Must roll back'"
    ) == {"count": 0}
    assert store.query_one(
        "SELECT COUNT(*) AS count FROM processed_command "
        "WHERE command_id LIKE 'material_io:%'"
    ) == {"count": 0}
    assert store.query_one(
        "SELECT occupied_material_uuid FROM site WHERE uuid=?", (mount_site_uuid,)
    ) == {"occupied_material_uuid": None}
    store.close()


def test_load_clears_stale_occupancy_left_by_soft_deleted_material(
    tmp_path: Path,
) -> None:
    service, store = _service(tmp_path)
    container_template_uuid = _template(service, "stale-container")
    mount_uuid, mount_site_uuid = _mount(
        service, store, allowed_template_uuid=container_template_uuid
    )
    ghost = service.create_material(
        {
            "resource_template_uuid": container_template_uuid,
            "name": "ghost leftover box",
        }
    )
    ghost_uuid = ghost["uuid"]
    now = "2026-09-20T09:22:51.000Z"
    with store.transaction() as connection:
        connection.execute(
            "UPDATE material SET deleted_at=?,parent_uuid=NULL WHERE uuid=?",
            (now, ghost_uuid),
        )
        connection.execute(
            "UPDATE site SET occupied_material_uuid=? WHERE uuid=?",
            (ghost_uuid, mount_site_uuid),
        )

    loaded = service.material_io_load(
        {
            "template_uuid": container_template_uuid,
            "mount_material_uuid": mount_uuid,
            "site_uuid": mount_site_uuid,
            "name": "After stale occupancy",
            "workflow_id": "wf-stale",
            "node_id": "load",
            "attempt": 1,
        }
    )
    assert loaded["material_uuid"]
    assert store.query_one(
        "SELECT occupied_material_uuid FROM site WHERE uuid=?", (mount_site_uuid,)
    ) == {"occupied_material_uuid": loaded["material_uuid"]}
    store.close()


def test_unload_rejects_declared_site_that_does_not_hold_the_material(
    tmp_path: Path,
) -> None:
    service, store = _service(tmp_path)
    container_template_uuid = _template(service, "located-container")
    mount_uuid, mount_site_uuid = _mount(
        service, store, allowed_template_uuid=container_template_uuid
    )
    other_mount, other_site = _mount(
        service, store, allowed_template_uuid=container_template_uuid
    )
    loaded = service.material_io_load(
        {
            "template_uuid": container_template_uuid,
            "mount_material_uuid": mount_uuid,
            "site_uuid": mount_site_uuid,
            "name": "Cup on first site",
            "workflow_id": "wf-location",
            "node_id": "load",
            "attempt": 1,
        }
    )
    material_uuid = loaded["material_uuid"]
    matched = service._material_io.resolve_resources(
        operation="unload",
        mount_material_uuid=mount_uuid,
        site_uuid=mount_site_uuid,
        material_uuid=material_uuid,
        scope="material_only",
    )
    assert any(item["site_uuid"] == mount_site_uuid for item in matched)
    named = service._material_io.resolve_resources(
        operation="unload",
        mount_material_uuid=mount_uuid,
        site="SOURCE",
        site_uuid=mount_site_uuid,
        material_uuid=material_uuid,
        scope="material_only",
    )
    assert any(item["site_uuid"] == mount_site_uuid for item in named)
    with pytest.raises(MaterialIOError, match="site 名称与物料实际库位不一致"):
        service._material_io.resolve_resources(
            operation="unload",
            mount_material_uuid=mount_uuid,
            site="BEAKER1",
            site_uuid=mount_site_uuid,
            material_uuid=material_uuid,
            scope="material_only",
        )
    with pytest.raises(MaterialIOError, match="not occupying the given site"):
        service._material_io.resolve_resources(
            operation="unload",
            mount_material_uuid=mount_uuid,
            site_uuid=other_site,
            material_uuid=material_uuid,
            scope="material_only",
        )
    with pytest.raises(MaterialIOError, match="not mounted on the given mount"):
        service._material_io.resolve_resources(
            operation="unload",
            mount_material_uuid=other_mount,
            site_uuid=mount_site_uuid,
            material_uuid=material_uuid,
            scope="material_only",
        )
    still_there = store.query_one(
        "SELECT parent_uuid, (SELECT occupied_material_uuid FROM site WHERE uuid=?) AS occupied "
        "FROM material WHERE uuid=?",
        (mount_site_uuid, material_uuid),
    )
    assert still_there["parent_uuid"] == mount_uuid
    assert still_there["occupied"] == material_uuid
    store.close()


def test_unload_rejects_material_only_then_soft_deletes_tree_leaf_first(
    tmp_path: Path,
) -> None:
    service, store = _service(tmp_path)
    child_template_uuid = _template(service, "consumable")
    container_template_uuid = _template(service, "container")
    _install_initial_site_definition(
        service,
        store,
        template_uuid=container_template_uuid,
        child_template_uuid=child_template_uuid,
    )
    mount_uuid, mount_site_uuid = _mount(
        service, store, allowed_template_uuid=container_template_uuid
    )
    loaded = service.material_io_load(
        {
            "template_uuid": container_template_uuid,
            "mount_material_uuid": mount_uuid,
            "site_uuid": mount_site_uuid,
            "name": "Loaded container",
            "workflow_id": "wf",
            "node_id": "load",
            "attempt": 1,
        }
    )
    root_uuid = loaded["material_uuid"]
    child_uuid = loaded["initial_material_uuids"][0]

    with pytest.raises(BackendContractError) as not_empty:
        service.material_io_unload(
            {
                "material_uuid": root_uuid,
                "disposition": "soft_delete",
                "scope": "material_only",
                "workflow_id": "wf",
                "node_id": "unload",
                "attempt": 1,
            }
        )
    assert not_empty.value.code == 2005

    now = "2026-09-18T00:01:00.000Z"
    sample_uuid = str(uuid4())
    position_uuid = str(uuid4())
    history_uuid = str(uuid4())
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO sample(
                uuid,create_time,update_time,deleted_at,description,meta_data,
                material_uuid,code,name,quantity,quantity_unit
            ) VALUES (?,?,?,NULL,NULL,'{}',?,?,'tip sample',1,'count')
            """,
            (sample_uuid, now, now, child_uuid, f"S-{uuid4()}"),
        )
        connection.execute(
            """
            UPDATE material SET pose_uuid=?, position_x=0, position_y=0, position_z=0,
                depth=0, length=0, width=0, scale_x=1, scale_y=1, scale_z=1,
                rotation_x=0, rotation_y=0, rotation_z=0
            WHERE uuid=?
            """,
            (position_uuid, root_uuid),
        )
        connection.execute(
            """
            INSERT INTO material_state_history(
                uuid,create_time,update_time,deleted_at,description,meta_data,
                material_uuid,status,state_data,source,observed_at
            ) VALUES (?,?,?,NULL,NULL,'{}',?,'loaded','{}','test',?)
            """,
            (history_uuid, now, now, root_uuid, now),
        )

    request = {
        "material_uuid": root_uuid,
        "disposition": "soft_delete",
        "scope": "material_with_contents",
        "workflow_id": "wf",
        "node_id": "unload",
        "attempt": 2,
    }
    result = service.material_io_unload(request)

    assert result["affected_material_uuids"][-1] == root_uuid
    assert result["affected_material_uuids"][0] == child_uuid
    assert service.material_io_unload(request) == result
    assert all(
        row["deleted_at"] is not None
        for row in store.query_all(
            "SELECT deleted_at FROM material WHERE uuid IN (?,?)",
            (root_uuid, child_uuid),
        )
    )
    assert store.query_one(
        "SELECT deleted_at FROM sample WHERE uuid=?", (sample_uuid,)
    )["deleted_at"]
    assert store.query_one(
        "SELECT position_x FROM material WHERE uuid=?", (root_uuid,)
    )["position_x"] is None
    assert store.query_one(
        "SELECT deleted_at,status FROM material_state_history WHERE uuid=?",
        (history_uuid,),
    ) == {"deleted_at": None, "status": "loaded"}
    assert all(
        row["deleted_at"] is not None and row["occupied_material_uuid"] is None
        for row in store.query_all(
            "SELECT deleted_at,occupied_material_uuid FROM site WHERE material_uuid=?",
            (root_uuid,),
        )
    )
    assert store.query_one(
        "SELECT occupied_material_uuid FROM site WHERE uuid=?", (mount_site_uuid,)
    ) == {"occupied_material_uuid": None}
    assert store.query_one(
        "SELECT COUNT(*) AS count FROM processed_command "
        "WHERE command_id LIKE 'material_io:%'"
    ) == {"count": 2}
    store.close()


def test_detach_keeps_empty_material_and_clears_parent_and_site(tmp_path: Path) -> None:
    service, store = _service(tmp_path)
    container_template_uuid = _template(service, "empty-container")
    mount_uuid, mount_site_uuid = _mount(
        service, store, allowed_template_uuid=container_template_uuid
    )
    loaded = service.material_io_load(
        {
            "template_uuid": container_template_uuid,
            "mount_material_uuid": mount_uuid,
            "site_uuid": mount_site_uuid,
            "name": "Empty",
            "workflow_id": "wf-detach",
            "node_id": "load",
            "attempt": 1,
        }
    )

    result = service.material_io_unload(
        {
            "material_uuid": loaded["material_uuid"],
            "disposition": "detach",
            "scope": "material_only",
            "workflow_id": "wf-detach",
            "node_id": "unload",
            "attempt": 1,
        }
    )

    assert result["affected_material_uuids"] == [loaded["material_uuid"]]
    assert store.query_one(
        "SELECT parent_uuid,deleted_at FROM material WHERE uuid=?",
        (loaded["material_uuid"],),
    ) == {"parent_uuid": None, "deleted_at": None}
    assert store.query_one(
        "SELECT occupied_material_uuid FROM site WHERE uuid=?", (mount_site_uuid,)
    ) == {"occupied_material_uuid": None}
    store.close()


def test_backend_material_io_route_and_claim_guard(tmp_path: Path) -> None:
    service, store = _service(tmp_path)
    container_template_uuid = _template(service, "claimed-container")
    mount_uuid, mount_site_uuid = _mount(
        service, store, allowed_template_uuid=container_template_uuid
    )
    now = "2026-09-18T00:02:00.000Z"
    claim_uuid = str(uuid4())
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO station_execution_claim(
                claim_uuid,effect_uuid,task_uuid,job_uuid,attempt,parameter_hash,
                expected_change_set,resource_keys,state,acquired_at,committed_at,
                released_at,update_time
            ) VALUES (?,?,?,?,1,'hash','{}','[]','running',?,NULL,NULL,?)
            """,
            (claim_uuid, str(uuid4()), str(uuid4()), str(uuid4()), now, now),
        )
        connection.execute(
            """
            INSERT INTO station_execution_lock_lease(
                claim_uuid,lock_key,scope,material_uuid,site_uuid,fencing_token,
                state,acquired_at,released_at,update_time
            ) VALUES (?,?,'material',?,NULL,1,'running',?,NULL,?)
            """,
            (
                claim_uuid,
                f"material/{mount_uuid}/exclusive",
                mount_uuid,
                now,
                now,
            ),
        )
    app = FastAPI()
    install_backend_resource_api(app, service)
    client = TestClient(app)
    payload = {
        "resource_template_uuid": container_template_uuid,
        "name": "Claimed",
        "site_placement": {"action": "place", "site_uuid": mount_site_uuid},
    }

    blocked = client.post("/api/v1/materials", json=payload)

    assert blocked.status_code == 200
    assert blocked.json()["code"] == 6011
    assert store.query_one(
        "SELECT COUNT(*) AS count FROM material WHERE name='Claimed'"
    ) == {"count": 0}

    with store.transaction() as connection:
        connection.execute(
            "UPDATE station_execution_lock_lease "
            "SET state='released',released_at=?,update_time=? WHERE claim_uuid=?",
            (now, now, claim_uuid),
        )
        connection.execute(
            "UPDATE station_execution_claim "
            "SET state='released',released_at=?,update_time=? WHERE claim_uuid=?",
            (now, now, claim_uuid),
        )
    created = client.post("/api/v1/materials", json=payload)
    assert created.status_code == 201
    assert created.json()["code"] == 0
    store.close()


def test_approved_permit_settles_material_site_and_reagent_then_replays(
    tmp_path: Path,
) -> None:
    service, store = _service(tmp_path)
    container_template_uuid = _template(service, "settled-container")
    mount_uuid, mount_site_uuid = _mount(
        service, store, allowed_template_uuid=container_template_uuid
    )
    load_values = {
        "template_uuid": container_template_uuid,
        "mount_material_uuid": mount_uuid,
        "site_uuid": mount_site_uuid,
        "name": "Permit container",
        "workflow_id": "wf-permit",
        "node_id": "load",
        "attempt": 1,
    }
    canonical_load = {"operation": "load", **load_values}
    load_permit = _install_permit(
        store,
        canonical_request=canonical_load,
        operation="load",
        lock_keys={
            material_lock_key(mount_uuid),
            site_lock_key(mount_uuid, mount_site_uuid),
        },
    )

    with pytest.raises(BackendContractError) as public_blocked:
        service.material_io_load(load_values)
    assert public_blocked.value.code == 6011

    stale_permit = {
        **load_permit,
        "fences": {
            **load_permit["fences"],
            site_lock_key(mount_uuid, mount_site_uuid): 999,
        },
    }
    with pytest.raises(BackendContractError) as stale:
        service.material_io_settle_load(load_values, stale_permit)
    assert stale.value.code == 4002
    assert store.query_one(
        "SELECT COUNT(*) AS count FROM material WHERE name='Permit container'"
    ) == {"count": 0}

    loaded = service.material_io_settle_load(load_values, load_permit)
    assert loaded["effect_uuid"] == load_permit["effect_uuid"]
    assert store.query_one(
        "SELECT occupied_material_uuid FROM site WHERE uuid=?", (mount_site_uuid,)
    ) == {"occupied_material_uuid": loaded["material_uuid"]}
    _release_permit(store, load_permit)
    assert service.material_io_settle_load(load_values, load_permit) == loaded

    reagent_info_uuid = str(uuid4())
    reagent_uuid = str(uuid4())
    now = "2026-09-18T00:05:00.000Z"
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO reagent_info(
                uuid,create_time,update_time,deleted_at,description,meta_data,
                cas,name,name_en,aliases,molecular_formula,smiles,inchi_key,
                molecular_weight,density_g_per_ml,physical_state
            ) VALUES (?,?,?,NULL,NULL,'{}',NULL,'permit reagent',NULL,'[]',
                      NULL,NULL,NULL,NULL,NULL,'unknown')
            """,
            (reagent_info_uuid, now, now),
        )
        connection.execute(
            """
            INSERT INTO reagent(
                uuid,create_time,update_time,deleted_at,description,meta_data,
                material_uuid,reagent_info_uuid,concentration_value,
                concentration_unit,quantity,quantity_unit,revision,physical_state,
                density_g_per_ml,density_source
            ) VALUES (?,?,?,NULL,NULL,'{}',?,?,NULL,NULL,1,'ml',1,
                      'liquid',NULL,NULL)
            """,
            (
                reagent_uuid,
                now,
                now,
                loaded["material_uuid"],
                reagent_info_uuid,
            ),
        )
    unload_values = {
        "material_uuid": loaded["material_uuid"],
        "disposition": "soft_delete",
        "scope": "material_with_contents",
        "workflow_id": "wf-permit",
        "node_id": "unload",
        "attempt": 1,
    }
    canonical_unload = {"operation": "unload", **unload_values}
    unload_permit = _install_permit(
        store,
        canonical_request=canonical_unload,
        operation="unload",
        lock_keys={
            material_lock_key(loaded["material_uuid"]),
            material_lock_key(mount_uuid),
            site_lock_key(mount_uuid, mount_site_uuid),
        },
    )

    with pytest.raises(BackendContractError) as unload_public_blocked:
        service.material_io_unload(unload_values)
    assert unload_public_blocked.value.code == 6011

    unloaded = service.material_io_settle_unload(unload_values, unload_permit)
    assert unloaded["effect_uuid"] == unload_permit["effect_uuid"]
    assert store.query_one(
        "SELECT deleted_at FROM reagent WHERE uuid=?", (reagent_uuid,)
    )["deleted_at"]
    assert store.query_one(
        "SELECT deleted_at FROM material WHERE uuid=?", (loaded["material_uuid"],)
    )["deleted_at"]
    _release_permit(store, unload_permit)
    assert service.material_io_settle_unload(unload_values, unload_permit) == unloaded
    store.close()


def test_legacy_effect_rows_fold_into_processed_command(tmp_path: Path) -> None:
    """旧 material_io_effect 行必须折进 processed_command 后删除原表。"""

    store = InventoryStore(str(tmp_path / "material-io-fold.db"))
    effect_uuid = str(uuid4())
    request = {
        "operation": "load",
        "workflow_id": "wf-legacy",
        "node_id": "load",
        "attempt": 1,
    }
    result = {
        "effect_uuid": effect_uuid,
        "operation": "load",
        "request_hash": "legacy-hash",
        "material_uuid": "material-legacy",
    }
    with store.transaction() as connection:
        connection.executescript(
            """
            CREATE TABLE material_io_effect (
                effect_uuid TEXT PRIMARY KEY NOT NULL,
                create_time DATETIME NOT NULL,
                workflow_id TEXT NOT NULL,
                node_id TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                operation TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                request_json TEXT NOT NULL,
                result_json TEXT NOT NULL
            );
            """
        )
        connection.execute(
            """
            INSERT INTO material_io_effect(
                effect_uuid,create_time,workflow_id,node_id,attempt,operation,
                request_hash,request_json,result_json
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                effect_uuid,
                "2026-09-18T00:00:00.000Z",
                "wf-legacy",
                "load",
                1,
                "load",
                "legacy-hash",
                json.dumps(request, ensure_ascii=False),
                json.dumps(result, ensure_ascii=False),
            ),
        )
        migrate_material_io_schema(connection)
    command = store.query_one(
        "SELECT status,result_json FROM processed_command WHERE command_id=?",
        (material_io_command_id("wf-legacy", "load", 1),),
    )
    assert command is not None
    assert command["status"] == "completed"
    envelope = json.loads(command["result_json"])
    assert envelope["kind"] == "material_io"
    assert envelope["effect_uuid"] == effect_uuid
    assert envelope["result"] == result
    assert not store._conn.table_exists("material_io_effect")
    store.close()


def test_load_can_omit_or_create_or_reuse_reagent_identity(tmp_path: Path) -> None:
    service, store = _service(tmp_path)
    bottle_uuid = _template(service, "bottle", category=["container"])
    reagents = BackendReagentService(store)
    existing = reagents.create_reagent_info(
        {"name": "乙醇", "cas": "64-17-5", "physical_state": "liquid"}
    )
    empty_mount, empty_site = _mount(service, store, allowed_template_uuid=bottle_uuid)
    reuse_mount, reuse_site = _mount(service, store, allowed_template_uuid=bottle_uuid)
    create_mount, create_site = _mount(service, store, allowed_template_uuid=bottle_uuid)
    cas_mount, cas_site = _mount(service, store, allowed_template_uuid=bottle_uuid)

    omitted = service.material_io_load(
        {
            "template_uuid": bottle_uuid,
            "mount_material_uuid": empty_mount,
            "site_uuid": empty_site,
            "name": "Empty bottle",
            "workflow_id": "wf-reagent",
            "node_id": "omit",
            "attempt": 1,
        }
    )
    assert "reagent" not in omitted
    assert store.query_one(
        "SELECT COUNT(*) AS count FROM reagent WHERE material_uuid=?",
        (omitted["material_uuid"],),
    ) == {"count": 0}

    reused = service.material_io_load(
        {
            "template_uuid": bottle_uuid,
            "mount_material_uuid": reuse_mount,
            "site_uuid": reuse_site,
            "name": "Reuse ethanol",
            "workflow_id": "wf-reagent",
            "node_id": "reuse",
            "attempt": 1,
            "reagent": {
                "reagent_info_uuid": existing["uuid"],
                "quantity": 25,
                "quantity_unit": "mL",
            },
        }
    )
    assert reused["reagent_info_created"] is False
    assert reused["reagent_info"]["uuid"] == existing["uuid"]
    assert reused["reagent"]["quantity"] == 25
    assert reused["reagent"]["quantity_unit"] == "mL"
    assert store.query_one(
        "SELECT reagent_info_uuid,quantity FROM reagent WHERE uuid=?",
        (reused["reagent"]["uuid"],),
    ) == {"reagent_info_uuid": existing["uuid"], "quantity": 25}

    created = service.material_io_load(
        {
            "template_uuid": bottle_uuid,
            "mount_material_uuid": create_mount,
            "site_uuid": create_site,
            "name": "New acetone",
            "workflow_id": "wf-reagent",
            "node_id": "create",
            "attempt": 1,
            "reagent": {
                "name": "丙酮",
                "cas": "67-64-1",
                "quantity": 10,
                "quantity_unit": "mL",
                "physical_state": "liquid",
            },
        }
    )
    assert created["reagent_info_created"] is True
    assert created["reagent_info"]["name"] == "丙酮"
    assert created["reagent_info"]["cas"] == "67-64-1"
    assert created["reagent_info"]["uuid"] != existing["uuid"]
    assert store.query_one(
        "SELECT name FROM reagent_info WHERE uuid=?",
        (created["reagent_info"]["uuid"],),
    ) == {"name": "丙酮"}

    cas_reused = service.material_io_load(
        {
            "template_uuid": bottle_uuid,
            "mount_material_uuid": cas_mount,
            "site_uuid": cas_site,
            "name": "CAS ethanol",
            "workflow_id": "wf-reagent",
            "node_id": "cas",
            "attempt": 1,
            "reagent": {
                "name": "乙醇瓶",
                "cas": "64-17-5",
                "quantity": 5,
                "quantity_unit": "mL",
            },
        }
    )
    assert cas_reused["reagent_info_created"] is False
    assert cas_reused["reagent_info"]["uuid"] == existing["uuid"]
    replay = service.material_io_load(
        {
            "template_uuid": bottle_uuid,
            "mount_material_uuid": reuse_mount,
            "site_uuid": reuse_site,
            "name": "Reuse ethanol",
            "workflow_id": "wf-reagent",
            "node_id": "reuse",
            "attempt": 1,
            "reagent": {
                "reagent_info_uuid": existing["uuid"],
                "quantity": 25,
                "quantity_unit": "mL",
            },
        }
    )
    assert replay == reused
    with pytest.raises(BackendContractError) as conflict:
        service.material_io_load(
            {
                "template_uuid": bottle_uuid,
                "mount_material_uuid": reuse_mount,
                "site_uuid": reuse_site,
                "name": "Reuse ethanol",
                "workflow_id": "wf-reagent",
                "node_id": "reuse",
                "attempt": 1,
                "reagent": {
                    "reagent_info_uuid": existing["uuid"],
                    "quantity": 40,
                    "quantity_unit": "mL",
                },
            }
        )
    assert conflict.value.code == 4002
    store.close()


def test_load_reagent_rolls_back_when_template_is_not_container(tmp_path: Path) -> None:
    service, store = _service(tmp_path)
    rack_uuid = _template(service, "tip-rack")
    mount_uuid, mount_site_uuid = _mount(service, store, allowed_template_uuid=rack_uuid)
    request = {
        "template_uuid": rack_uuid,
        "mount_material_uuid": mount_uuid,
        "site_uuid": mount_site_uuid,
        "name": "Not a bottle",
        "workflow_id": "wf-not-container",
        "node_id": "load",
        "attempt": 1,
        "reagent": {"name": "乙醇", "quantity": 10, "quantity_unit": "mL"},
    }

    with pytest.raises(BackendContractError) as error:
        service.material_io_load(request)
    assert error.value.code == 1000
    assert store.query_one(
        "SELECT COUNT(*) AS count FROM material WHERE name='Not a bottle'"
    ) == {"count": 0}
    assert store.query_one(
        "SELECT COUNT(*) AS count FROM reagent_info WHERE name='乙醇'"
    ) == {"count": 0}
    assert store.query_one(
        "SELECT COUNT(*) AS count FROM processed_command "
        "WHERE command_id LIKE 'material_io:%'"
    ) == {"count": 0}
    store.close()


def test_load_reagent_missing_cas_identity_does_not_create_material(tmp_path: Path) -> None:
    service, store = _service(tmp_path)
    bottle_uuid = _template(service, "cas-bottle", category=["container"])
    mount_uuid, mount_site_uuid = _mount(service, store, allowed_template_uuid=bottle_uuid)

    with pytest.raises(BackendContractError) as error:
        service.material_io_load(
            {
                "template_uuid": bottle_uuid,
                "mount_material_uuid": mount_uuid,
                "site_uuid": mount_site_uuid,
                "name": "Missing CAS",
                "workflow_id": "wf-missing-cas",
                "node_id": "load",
                "attempt": 1,
                "reagent": {"cas": "64-17-5", "quantity": 10, "quantity_unit": "mL"},
            }
        )
    assert error.value.code == 4001
    assert store.query_one(
        "SELECT COUNT(*) AS count FROM material WHERE name='Missing CAS'"
    ) == {"count": 0}
    store.close()


def test_create_material_expands_template_sites_and_initial_contents(
    tmp_path: Path,
) -> None:
    service, store = _service(tmp_path)
    child_template_uuid = _template(service, "tip")
    container_template_uuid = _template(service, "tip-rack")
    _install_initial_site_definition(
        service,
        store,
        template_uuid=container_template_uuid,
        child_template_uuid=child_template_uuid,
    )
    mount_uuid, mount_site_uuid = _mount(
        service, store, allowed_template_uuid=container_template_uuid
    )
    app = FastAPI()
    install_backend_resource_api(app, service)
    created = TestClient(app).post(
        "/api/v1/materials",
        json={
            "resource_template_uuid": container_template_uuid,
            "name": "Rack 01",
            "site_placement": {"action": "place", "site_uuid": mount_site_uuid},
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()["data"]
    assert len(body["site_uuids"]) == 2
    assert len(body["initial_material_uuids"]) == 1
    sites = store.query_all(
        "SELECT name,occupied_material_uuid FROM site "
        "WHERE material_uuid=? AND deleted_at IS NULL ORDER BY sort_order",
        (body["uuid"],),
    )
    assert [site["name"] for site in sites] == ["A1", "A2"]
    assert sites[0]["occupied_material_uuid"] == body["initial_material_uuids"][0]
    assert sites[1]["occupied_material_uuid"] is None
    assert store.query_one(
        "SELECT parent_uuid,resource_template_uuid FROM material WHERE uuid=?",
        (body["initial_material_uuids"][0],),
    ) == {
        "parent_uuid": body["uuid"],
        "resource_template_uuid": child_template_uuid,
    }
    assert store.query_one(
        "SELECT occupied_material_uuid FROM site WHERE uuid=?",
        (mount_site_uuid,),
    ) == {"occupied_material_uuid": body["uuid"]}

    blocked = TestClient(app).delete(f"/api/v1/materials/{body['uuid']}")
    assert blocked.status_code == 200
    assert blocked.json()["code"] == 2005
    deleted = TestClient(app).delete(
        f"/api/v1/materials/{body['uuid']}?scope=material_with_contents"
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["code"] == 0
    assert all(
        row["deleted_at"] is not None
        for row in store.query_all(
            "SELECT deleted_at FROM material WHERE uuid IN (?,?)",
            (body["uuid"], body["initial_material_uuids"][0]),
        )
    )
    store.close()


def _load_refill_bottle(
    service: BackendResourceService,
    store: InventoryStore,
    *,
    node_id: str = "load",
) -> dict[str, Any]:
    bottle_uuid = _template(service, f"refill-bottle-{uuid4()}", category=["container"])
    mount_uuid, site_uuid = _mount(service, store, allowed_template_uuid=bottle_uuid)
    loaded = service.material_io_load(
        {
            "template_uuid": bottle_uuid,
            "mount_material_uuid": mount_uuid,
            "site_uuid": site_uuid,
            "name": f"Working bottle {uuid4()}",
            "workflow_id": "wf-refill",
            "node_id": node_id,
            "attempt": 1,
            "reagent": {
                "name": "乙醇",
                "cas": "64-17-5",
                "quantity": 10,
                "quantity_unit": "mL",
                "container_capacity": {"max_volume_ul": 100000},
            },
        }
    )
    return {
        "loaded": loaded,
        "mount_uuid": mount_uuid,
        "site_uuid": site_uuid,
        "material_uuid": loaded["material_uuid"],
    }


def test_refill_site_name_matches_uuid_and_rejects_a_different_name(
    tmp_path: Path,
) -> None:
    service, store = _service(tmp_path)
    fixture = _load_refill_bottle(service, store, node_id="refill-site")
    matched = service._material_io.resolve_resources(
        operation="refill",
        material_uuid=fixture["material_uuid"],
        mount_material_uuid=fixture["mount_uuid"],
        site="SOURCE",
        site_uuid=fixture["site_uuid"],
    )
    assert any(item["site_uuid"] == fixture["site_uuid"] for item in matched)
    name_only = service._material_io.resolve_resources(
        operation="refill",
        material_uuid=fixture["material_uuid"],
        mount_material_uuid=fixture["mount_uuid"],
        site="source",
    )
    assert any(item["site_uuid"] == fixture["site_uuid"] for item in name_only)
    omitted = service._material_io.resolve_resources(
        operation="refill",
        material_uuid=fixture["material_uuid"],
        mount_material_uuid=fixture["mount_uuid"],
    )
    assert any(item["site_uuid"] == fixture["site_uuid"] for item in omitted)
    with pytest.raises(MaterialIOError, match="site 名称与物料实际库位不一致"):
        service._material_io.resolve_resources(
            operation="refill",
            material_uuid=fixture["material_uuid"],
            mount_material_uuid=fixture["mount_uuid"],
            site="BEAKER1",
            site_uuid=fixture["site_uuid"],
        )
    store.close()


def _refill_job(fixture: dict[str, Any], *, workflow_id: str, node_id: str) -> dict[str, Any]:
    return {
        "workflow_task_uuid": workflow_id,
        "workflow_node_uuid": node_id,
        "attempt": 1,
        "param": {
            "mode": "refill",
            "material": {"uuid": fixture["material_uuid"]},
            "mount": {"uuid": fixture["mount_uuid"]},
            "reagent": {"quantity": 10, "quantity_unit": "mL"},
            "timeout_seconds": 60,
        },
    }


def test_workflow_refill_settles_site_name_without_treating_it_as_uuid(
    tmp_path: Path,
) -> None:
    """文档 A1/A2/A3：名称、名称加 UUID 都能补液；只传 UUID 仍成功。"""

    service, store = _service(tmp_path)
    fixtures = [
        _load_refill_bottle(service, store, node_id=f"load-{name}")
        for name in ("a1", "a2", "a3")
    ]
    extras = (
        {"site": "SOURCE"},
        {"site": "SOURCE", "site_uuid": fixtures[1]["site_uuid"]},
        {"site": None, "site_uuid": fixtures[2]["site_uuid"]},
    )
    for fixture, extra, name in zip(fixtures, extras, ("a1", "a2", "a3"), strict=True):
        job = _refill_job(fixture, workflow_id=f"wf-{name}", node_id=f"refill-{name}")
        job["param"].update(extra)
        _operation, values = TaskSchedulerBridge._material_io_request_values(
            job, job["param"]
        )
        if name == "a1":
            assert values["site"] == "SOURCE"
            assert "site_uuid" not in values
        elif name == "a2":
            assert values["site"] == "SOURCE"
            assert values["site_uuid"] == fixture["site_uuid"]
        else:
            assert "site" not in values
            assert values["site_uuid"] == fixture["site_uuid"]
        settled = service.material_io_refill(values)
        assert settled["quantity"] == 20
        assert store.query_one(
            "SELECT occupied_material_uuid FROM site WHERE uuid=?",
            (fixture["site_uuid"],),
        )["occupied_material_uuid"] == fixture["material_uuid"]
    store.close()


def test_resolve_resources_error_is_handled_by_the_same_try() -> None:
    """库位冲突从 resolve_resources 抛出，必须由包住这次调用的 try 接住。"""

    import ast

    source_path = Path(__file__).resolve().parents[2] / "unilabos/app/scheduler/service.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "resolve_resources"
    ]
    assert len(calls) == 1
    owner = calls[0]
    while owner is not None and not isinstance(owner, ast.Try):
        owner = parents.get(owner)
    assert isinstance(owner, ast.Try)
    handled: list[str] = []
    for handler in owner.handlers:
        caught = handler.type
        if isinstance(caught, ast.Name):
            handled.append(caught.id)
        elif isinstance(caught, ast.Tuple):
            handled.extend(item.id for item in caught.elts if isinstance(item, ast.Name))
    assert "MaterialIOError" in handled


def test_conflicting_site_name_is_reported_on_the_pending_job(tmp_path: Path) -> None:
    """文档 B1/B2：名称与 UUID 冲突时，准备失败写到作业，不再只留后台日志。"""

    service, store = _service(tmp_path)
    bottle = _load_refill_bottle(service, store, node_id="load-b2")
    job = _refill_job(bottle, workflow_id="wf-b2", node_id="refill-b2")
    job["param"]["site"] = "BEAKER1"
    job["param"]["site_uuid"] = bottle["site_uuid"]
    _operation, values = TaskSchedulerBridge._material_io_request_values(
        job, job["param"]
    )
    assert values["site"] == "BEAKER1"
    assert values["site_uuid"] == bottle["site_uuid"]
    with pytest.raises(MaterialIOError, match="site 名称与物料实际库位不一致") as caught:
        service._material_io.resolve_resources(
            operation="refill",
            material_uuid=values["material_uuid"],
            mount_material_uuid=values["mount_material_uuid"],
            site=values["site"],
            site_uuid=values["site_uuid"],
        )
    reported: dict[str, Any] = {}

    class Control:
        def report_error(self, job_id, *, phase, message, error_code=None):
            reported.update(
                job_id=job_id,
                phase=phase,
                message=message,
                error_code=error_code,
            )

    class Run:
        def hold_error(self, node_id):
            self.held = node_id

    class Node:
        job_id = "job-b2"
        id = "node-b2"

    scheduler = EdgeScheduler.__new__(EdgeScheduler)
    scheduler._station_control = Control()
    run = Run()
    try:
        raise caught.value
    except MaterialIOError:
        scheduler._fail_prepared_node(run, Node())
    assert reported == {
        "job_id": "job-b2",
        "phase": "prepare",
        "message": "site 名称与物料实际库位不一致",
        "error_code": "invalid_parameter",
    }
    assert run.held == "node-b2"
    assert store.query_one(
        "SELECT quantity FROM reagent WHERE uuid=?",
        (bottle["loaded"]["reagent"]["uuid"],),
    )["quantity"] == 10
    store.close()


def test_refill_adds_quantity_without_occupancy_change_and_replays(
    tmp_path: Path,
) -> None:
    service, store = _service(tmp_path)
    fixture = _load_refill_bottle(service, store)
    request = {
        "material_uuid": fixture["material_uuid"],
        "mount_material_uuid": fixture["mount_uuid"],
        "site_uuid": fixture["site_uuid"],
        "workflow_id": "wf-refill",
        "node_id": "refill",
        "attempt": 1,
        "reagent": {"quantity": 15, "quantity_unit": "mL"},
    }

    first = service.material_io_refill(request)
    replay = service.material_io_refill(request)

    assert first["operation"] == "refill"
    assert first["quantity_added"] == 15
    assert first["quantity"] == 25
    assert first["reagent"]["uuid"] == fixture["loaded"]["reagent"]["uuid"]
    assert first["reagent_info"]["cas"] == "64-17-5"
    assert replay == first
    assert store.query_one(
        "SELECT quantity FROM reagent WHERE uuid=?",
        (first["reagent"]["uuid"],),
    ) == {"quantity": 25}
    assert store.query_one(
        "SELECT occupied_material_uuid FROM site WHERE uuid=?",
        (fixture["site_uuid"],),
    ) == {"occupied_material_uuid": fixture["material_uuid"]}
    assert store.query_one(
        "SELECT COUNT(*) AS count FROM processed_command "
        "WHERE command_id LIKE 'material_io:%'"
    )["count"] >= 2
    with pytest.raises(BackendContractError) as conflict:
        service.material_io_refill({**request, "reagent": {"quantity": 8, "quantity_unit": "mL"}})
    assert conflict.value.code == 4002
    store.close()


def test_refill_rejects_identity_fields_missing_reagent_and_unit_mismatch(
    tmp_path: Path,
) -> None:
    service, store = _service(tmp_path)
    fixture = _load_refill_bottle(service, store)
    base = {
        "material_uuid": fixture["material_uuid"],
        "mount_material_uuid": fixture["mount_uuid"],
        "workflow_id": "wf-refill-invalid",
        "node_id": "refill",
        "attempt": 1,
    }
    with pytest.raises(BackendContractError) as identity:
        service.material_io_refill(
            {
                **base,
                "reagent": {
                    "name": "乙醇",
                    "quantity": 5,
                    "quantity_unit": "mL",
                },
            }
        )
    assert identity.value.code == 1000

    empty_template = _template(
        service, f"empty-bottle-{uuid4()}", category=["container"]
    )
    empty_mount, empty_site = _mount(
        service, store, allowed_template_uuid=empty_template
    )
    empty = service.material_io_load(
        {
            "template_uuid": empty_template,
            "mount_material_uuid": empty_mount,
            "site_uuid": empty_site,
            "name": "Empty refill target",
            "workflow_id": "wf-refill-empty",
            "node_id": "load",
            "attempt": 1,
        }
    )
    with pytest.raises(BackendContractError) as missing:
        service.material_io_refill(
            {
                "material_uuid": empty["material_uuid"],
                "mount_material_uuid": empty_mount,
                "workflow_id": "wf-refill-empty",
                "node_id": "refill",
                "attempt": 1,
                "reagent": {"quantity": 5, "quantity_unit": "mL"},
            }
        )
    assert missing.value.code == 4001

    with pytest.raises(BackendContractError) as unit:
        service.material_io_refill(
            {**base, "reagent": {"quantity": 5, "quantity_unit": "g"}}
        )
    assert unit.value.code == 1000
    store.close()


def test_approved_permit_settles_refill_then_replays(tmp_path: Path) -> None:
    service, store = _service(tmp_path)
    fixture = _load_refill_bottle(service, store, node_id="permit-load")
    refill_values = {
        "material_uuid": fixture["material_uuid"],
        "mount_material_uuid": fixture["mount_uuid"],
        "site_uuid": fixture["site_uuid"],
        "workflow_id": "wf-permit-refill",
        "node_id": "refill",
        "attempt": 1,
        "reagent": {"quantity": 8, "quantity_unit": "mL"},
    }
    permit = _install_permit(
        store,
        canonical_request={"operation": "refill", **refill_values},
        operation="refill",
        lock_keys={
            material_lock_key(fixture["material_uuid"]),
            material_lock_key(fixture["mount_uuid"]),
            site_lock_key(fixture["mount_uuid"], fixture["site_uuid"]),
        },
    )

    with pytest.raises(BackendContractError) as public_blocked:
        service.material_io_refill(refill_values)
    assert public_blocked.value.code == 6011

    settled = service.material_io_settle_refill(refill_values, permit)
    assert settled["effect_uuid"] == permit["effect_uuid"]
    assert settled["quantity"] == 18
    assert store.query_one(
        "SELECT occupied_material_uuid FROM site WHERE uuid=?",
        (fixture["site_uuid"],),
    ) == {"occupied_material_uuid": fixture["material_uuid"]}
    _release_permit(store, permit)
    assert service.material_io_settle_refill(refill_values, permit) == settled
    store.close()


def test_task_release_keeps_soft_deleted_box_deleted(tmp_path: Path) -> None:
    store = InventoryStore(str(tmp_path / "inventory.db"))
    backend = BackendResourceService(store)
    inventory = InventoryService(store)
    task = str(uuid4())

    def template(name: str, sites: list[dict] | None = None) -> str:
        return backend.sync_resource_templates(
            [{"id": name, "registry_type": "resource", "available_sites": sites or []}]
        )["templates"][0]["uuid"]

    child_template = template("repro.tip")
    box_template = template(
        "repro.box",
        [
            {
                "index": 0,
                "label": "TIP1",
                "allowed_resource_template_uuids": [child_template],
                "initial_material_template_uuid": child_template,
            }
        ],
    )
    mount_template = template(
        "repro.mount",
        [
            {
                "index": 0,
                "label": "WORK",
                "allowed_resource_template_uuids": [box_template],
            }
        ],
    )
    mount = backend.create_material({"resource_template_uuid": mount_template, "name": "工作位"})
    box = backend.create_material(
        {
            "resource_template_uuid": box_template,
            "name": "整盒待卸",
            "site_placement": {"action": "place", "site_uuid": mount["site_uuids"][0]},
        }
    )
    child = box["initial_material_uuids"][0]
    inventory.admit_task_materials(
        task,
        [
            MaterialSourceAdmissionRequest(
                node_id="box-source",
                resource_template_uuid=box_template,
                custody_policy="task_exclusive",
                requirement=MaterialRequirement(
                    template_id=box_template,
                    mount_uuid=mount["uuid"],
                    search_descendants=True,
                ),
            )
        ],
        [],
    )
    values = {"material_uuid":box["uuid"],"disposition":"soft_delete","scope":"material_with_contents",
              "workflow_id":task,"node_id":"unload","attempt":1}
    with pytest.raises(BackendContractError):
        backend.material_io_unload(values)
    _settle_owned_unload(backend,store,values,task)
    inventory.release_workflow(task)
    rows = store.query_all(
        "SELECT uuid, deleted_at FROM material WHERE uuid IN (?, ?) ORDER BY uuid",
        (box["uuid"], child),
    )
    assert all(row["deleted_at"] is not None for row in rows)
    box_status = store.query_one(
        "SELECT status FROM material_instance WHERE edge_uuid=?",
        (box["uuid"],),
    )
    assert box_status["status"] == "discarded"
    store.close()


def _whole_box_fixture(tmp_path: Path, tips: int = 2) -> dict[str, Any]:
    """工作位上一盒带逐支子 TIP 的整盒，供软删除/共享绑定回归复用。"""

    store = InventoryStore(str(tmp_path / "inventory.db"))
    backend = BackendResourceService(store)
    inventory = InventoryService(store)

    def template(name: str, sites: list[dict] | None = None) -> str:
        return backend.sync_resource_templates(
            [{"id": name, "registry_type": "resource", "available_sites": sites or []}]
        )["templates"][0]["uuid"]

    tip_template = template("retire.tip")
    box_template = template(
        "retire.box",
        [
            {
                "index": index,
                "label": f"TIP{index + 1}",
                "allowed_resource_template_uuids": [tip_template],
                "initial_material_template_uuid": tip_template,
            }
            for index in range(tips)
        ],
    )
    mount_template = template(
        "retire.mount",
        [{"index": 0, "label": "WORK", "allowed_resource_template_uuids": [box_template]}],
    )
    mount = backend.create_material({"resource_template_uuid": mount_template, "name": "工作位"})
    box = backend.create_material(
        {
            "resource_template_uuid": box_template,
            "name": "整盒",
            "site_placement": {"action": "place", "site_uuid": mount["site_uuids"][0]},
        }
    )
    return {
        "store": store,
        "backend": backend,
        "inventory": inventory,
        "tip_template": tip_template,
        "box_template": box_template,
        "mount": mount["uuid"],
        "box": box["uuid"],
        "tips": list(box["initial_material_uuids"]),
    }


def _source(node_id: str, template_uuid: str, mount_uuid: str, custody: str):
    return MaterialSourceAdmissionRequest(
        node_id=node_id,
        resource_template_uuid=template_uuid,
        custody_policy=custody,
        requirement=MaterialRequirement(
            template_id=template_uuid, mount_uuid=mount_uuid, search_descendants=True
        ),
    )


def _unload_values(material_uuid: str, workflow_id: str) -> dict[str, Any]:
    return {
        "material_uuid": material_uuid,
        "disposition": "soft_delete",
        "scope": "material_with_contents",
        "workflow_id": workflow_id,
        "node_id": "unload",
        "attempt": 1,
    }


def _settle_owned_unload(backend, store, values, task_uuid):
    resources=backend._material_io.resolve_resources(operation="unload",material_uuid=values["material_uuid"],
        scope=values["scope"],disposition=values["disposition"])
    permit=_install_permit(store,canonical_request={"operation":"unload",**values},operation="unload",
        lock_keys={resource["lock_key"] for resource in resources},task_uuid=task_uuid)
    result=backend.material_io_settle_unload(values,permit)
    _release_permit(store,permit)
    return result


@pytest.mark.parametrize("binding", ["none", "box_and_unused_tip", "box_and_used_tip"])
@pytest.mark.parametrize("trigger", ["io_unload", "public_delete"])
def test_whole_box_soft_delete_retires_inventory_status(
    tmp_path: Path, binding: str, trigger: str
) -> None:
    """整盒软删除把盒与全部子 TIP 收敛为 discarded；任务释放不复活，公开在库列表不再列出。"""

    from unilabos.app.scheduler.inventory.api import create_router
    from unilabos.app.scheduler.inventory.domain import InsufficientStock
    from unilabos.app.scheduler.inventory.material_usage import mark_material_used

    fixture = _whole_box_fixture(tmp_path)
    store, backend, inventory = fixture["store"], fixture["backend"], fixture["inventory"]
    box, tips = fixture["box"], fixture["tips"]
    identities = (box, *tips)
    task = str(uuid4())
    try:
        if binding != "none":
            inventory.admit_task_materials(
                task,
                [_source("box-source", fixture["box_template"], fixture["mount"], "task_exclusive")],
                [],
            )
            inventory.admit_task_materials(
                task,
                [_source("tip-source", fixture["tip_template"], box, "task_exclusive")],
                [],
            )
        if binding == "box_and_used_tip":
            mark_material_used(
                inventory, task_uuid=task, job_uuid=str(uuid4()), material={"uuid": tips[0]}
            )
        if trigger == "io_unload":
            if binding == "none":
                backend.material_io_unload(_unload_values(box, task))
            else:
                with pytest.raises(BackendContractError):
                    backend.material_io_unload(_unload_values(box, task))
                _settle_owned_unload(backend,store,_unload_values(box,task),task)
        else:
            if binding != "none":
                with pytest.raises(BackendContractError):
                    backend.delete_material(box, scope="material_with_contents")
                inventory.release_workflow(task)
            backend.delete_material(box, scope="material_with_contents")

        placeholders = ",".join("?" for _ in identities)

        def statuses() -> dict[str, str]:
            return {
                row["edge_uuid"]: row["status"]
                for row in store.query_all(
                    "SELECT edge_uuid,status FROM material_instance "
                    f"WHERE edge_uuid IN ({placeholders})",
                    identities,
                )
            }

        # 已使用的 TIP 保留 used（整盒卸料既有合同），其余一律收敛为 discarded
        expected = {identity: "discarded" for identity in identities}
        if binding == "box_and_used_tip":
            expected[tips[0]] = "used"
        assert statuses() == expected
        inventory.release_workflow(task)
        assert statuses() == expected
        assert all(
            row["deleted_at"] is not None
            for row in store.query_all(
                f"SELECT deleted_at FROM material WHERE uuid IN ({placeholders})", identities
            )
        )
        # 终态没有出边：后续标记已使用被拒绝，且不会经兼容视图触发器清空 deleted_at
        with pytest.raises(Exception):
            inventory.mark_instance_used(tips[-1])
        assert store.query_one("SELECT deleted_at FROM material WHERE uuid=?", (tips[-1],))[
            "deleted_at"
        ] is not None
        app = FastAPI()
        app.include_router(create_router(inventory))
        listed = TestClient(app).get(
            "/api/v1/inventory/instances", params={"status": "warehouse"}
        ).json()["instances"]
        assert not {row["edge_uuid"] for row in listed} & set(identities)
        other = str(uuid4())
        with pytest.raises(InsufficientStock):
            inventory.admit_task_materials(
                other,
                [_source("again-tip", fixture["tip_template"], fixture["mount"], "task_exclusive")],
                [],
            )
    finally:
        store.close()


@pytest.mark.parametrize("bound", ["box", "tip"])
def test_unload_and_whole_tree_delete_wait_for_other_task_shared_binding(
    tmp_path: Path, bound: str
) -> None:
    """技术方案 §7.9：其他任务以共享来源绑定盒（或盒内 TIP）时，卸盒与公开整树删除都按活动占用拒绝。"""

    fixture = _whole_box_fixture(tmp_path)
    store, backend, inventory = fixture["store"], fixture["backend"], fixture["inventory"]
    box = fixture["box"]
    holder, remover = str(uuid4()), str(uuid4())
    if bound == "box":
        source = _source("shared-box", fixture["box_template"], fixture["mount"], "shared_source")
    else:
        source = _source("shared-tip", fixture["tip_template"], box, "shared_source")
    inventory.admit_task_materials(holder, [source], [])
    app = FastAPI()
    install_backend_resource_api(app, backend)
    client = TestClient(app)
    try:
        with pytest.raises(BackendContractError) as unload_blocked:
            backend.material_io_unload(_unload_values(box, remover))
        assert unload_blocked.value.code == 6011
        assert "共享来源" in unload_blocked.value.message
        deleted = client.delete(
            f"/api/v1/materials/{box}", params={"scope": "material_with_contents"}
        )
        assert deleted.json()["code"] == 6011
        # 被拒绝时没有任何物理副作用：盒、子 TIP、库位占用、库存状态都不变，也未记效果
        assert store.query_one("SELECT deleted_at FROM material WHERE uuid=?", (box,))[
            "deleted_at"
        ] is None
        assert store.query_one(
            "SELECT occupied_material_uuid FROM site WHERE material_uuid=?", (fixture["mount"],)
        )["occupied_material_uuid"] == box
        assert all(
            row["deleted_at"] is None
            for row in store.query_all("SELECT deleted_at FROM site WHERE material_uuid=?", (box,))
        )
        assert store.query_one(
            "SELECT COUNT(*) AS n FROM processed_command WHERE command_id=?",
            (material_io_command_id(remover, "unload", 1),),
        )["n"] == 0

        # 持有方任务结束释放共享绑定后，同一请求即可完成
        inventory.release_workflow(holder)
        result = backend.material_io_unload(_unload_values(box, remover))
        assert set(result["affected_material_uuids"]) == {box, *fixture["tips"]}
    finally:
        store.close()


def test_own_task_shared_binding_does_not_block_unload(tmp_path: Path) -> None:
    """同一任务（父流程共享绑定、子流程卸同一任务的盒）自己的共享绑定不阻塞卸盒。"""

    fixture = _whole_box_fixture(tmp_path)
    store, backend, inventory = fixture["store"], fixture["backend"], fixture["inventory"]
    task = str(uuid4())
    inventory.admit_task_materials(
        task,
        [_source("shared-box", fixture["box_template"], fixture["mount"], "shared_source")],
        [],
    )
    try:
        backend.material_io_unload(_unload_values(fixture["box"], task))
        assert store.query_one(
            "SELECT deleted_at FROM material WHERE uuid=?", (fixture["box"],)
        )["deleted_at"] is not None
        inventory.release_workflow(task)
        assert store.query_one(
            "SELECT status FROM material_instance WHERE edge_uuid=?", (fixture["box"],)
        )["status"] == "discarded"
    finally:
        store.close()


def test_soft_deleted_material_cannot_transition_back_to_active_status(tmp_path: Path) -> None:
    """旧库中「已软删除但状态仍是 warehouse」的行：上台/标记已使用都被拒绝，不经视图触发器复活。"""

    from unilabos.app.scheduler.inventory.domain import CommandRejected

    fixture = _whole_box_fixture(tmp_path, tips=1)
    store, inventory = fixture["store"], fixture["inventory"]
    tip = fixture["tips"][0]
    try:
        # 模拟修复前的整盒软删除：只写 deleted_at，库存状态停在 warehouse
        with store.transaction() as connection:
            connection.execute(
                "UPDATE material SET deleted_at='2026-09-26T00:00:00.000Z' WHERE uuid=?",
                (tip,),
            )
        for operation in (
            lambda: inventory.deploy_instance(tip),
            lambda: inventory.mark_instance_used(tip),
        ):
            with pytest.raises(CommandRejected):
                operation()
            row = store.query_one(
                "SELECT m.deleted_at, i.status FROM material m "
                "JOIN material_instance i ON i.edge_uuid=m.uuid WHERE m.uuid=?",
                (tip,),
            )
            assert row["deleted_at"] is not None and row["status"] == "warehouse"
        # 终态迁移仍允许：报废把遗留行收敛到 discarded 且保持删除
        inventory.discard_instance(tip)
        row = store.query_one(
            "SELECT m.deleted_at, i.status FROM material m "
            "JOIN material_instance i ON i.edge_uuid=m.uuid WHERE m.uuid=?",
            (tip,),
        )
        assert row["deleted_at"] is not None and row["status"] == "discarded"
    finally:
        store.close()
