"""``material.io`` 库存命令：人工装入 / 取走 / 补量在一个事务里完成，按命令身份只写一次。

``check`` 是同一套写入的保存点试运行（不写库），抛出的异常与真正写入时相同；调度器用它在人工确认前判断
「等一等」还是「失败」。调度器闭环见 ``test_material_io_scheduler.py``。
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.server.test_scheduler_inventory_authority import _mutation, _template
from unilabos.client.materials.core import LocalMaterialsClient
from unilabos.protocol.materials import (
    MaterialIdentityWrite,
    MaterialMove,
    MaterialNodeCreate,
    MaterialTreeCreate,
    SiteCreate,
)
from unilabos.resources.objects.site import SITE_TEMPLATE_WHITELIST_KEY
from unilabos.server.backend.scheduler.resource_wait_policy import temporary_resource_condition
from unilabos.server.services.materials import MaterialsService
from unilabos.server.services.materials.contents import ContentContract
from unilabos.server.services.materials.core import MaterialConflictError, MaterialNoChangeError, MaterialNotFoundError
from unilabos.server.services.materials.material_io import MaterialIO, material_io_command_id

TASK = str(uuid4())


def _identity(template: str, name: str) -> MaterialIdentityWrite:
    return MaterialIdentityWrite(resource_id=f"{name}-{uuid4().hex[:6]}", name=f"{name}-{uuid4().hex[:6]}",
                                 template_name=template, resource_type="container", class_name="Container")


def _ensure_template(materials: MaterialsService, template_uuid: str, name: str) -> None:
    try:
        _template(materials, template_uuid, name)
    except MaterialNoChangeError:
        pass


def deck(materials: MaterialsService, *, whitelist: list[str] | None = None) -> tuple[str, dict[str, str]]:
    """一块两库位台面：A1（可带模板白名单）、B1。"""

    _ensure_template(materials, "deck-template", "deck")
    extra = {SITE_TEMPLATE_WHITELIST_KEY: whitelist} if whitelist else {}
    tree = materials.create_tree(_mutation("create_material_tree"), MaterialTreeCreate(nodes=[
        MaterialNodeCreate(client_ref="deck", identity=_identity("deck", "deck"), sites=[
            SiteCreate(template_name="slot", site_index=0, label="A1", extra=extra),
            SiteCreate(template_name="slot", site_index=1, label="B1")]),
    ])).data
    mount = tree.client_ref_map["deck"]
    return mount, {site.label: site.site_uuid for site in materials.get_material(mount).sites}


def container(materials: MaterialsService, mount: str | None = None, site: str | None = None, *,
              water_ml: float | None = None) -> str:
    """一只容器，可选放到库位上、装 ``water_ml`` 毫升水。"""

    _ensure_template(materials, "bottle-template", "bottle")
    tree = materials.create_tree(_mutation("create_material_tree"), MaterialTreeCreate(nodes=[
        MaterialNodeCreate(client_ref="bottle", identity=_identity("bottle", "bottle"))])).data
    bottle = tree.client_ref_map["bottle"]
    if site is not None:
        materials.move_material(_mutation("move_material"), MaterialMove(material_uuid=bottle, destination_site_uuid=site))
    if water_ml is not None:
        contents = ContentContract(materials, compound_source=None)
        rows = materials.connection.execute("SELECT chemical_uuid FROM chemical WHERE cas='7732-18-5'").fetchall()
        chemical = rows[0][0] if rows else contents.create_reagent_info({"name": "水", "cas": "7732-18-5"})["uuid"]
        contents.create_reagent({"material_uuid": bottle, "reagent_info_uuid": chemical, "quantity": water_ml,
                                 "quantity_unit": "mL"})
    return bottle


def _param(mode: str, mount: str, **values) -> dict:
    return {"mode": mode, "mount": {"uuid": mount}, "assignee_user_ids": [], **values}


@pytest.fixture()
def materials(tmp_path):
    service = MaterialsService(tmp_path / "materials.db")
    _ensure_template(service, "tip-box-template", "tip_box")
    yield service
    service.close()


def _command(gateway, plan: dict, job: str) -> dict:
    return gateway.inventory_command({"command_id": material_io_command_id(job), "type": "material.io",
                                      "payload": plan, "actor": "scheduler"})


def test_load_places_a_new_material_with_reagent_once(materials) -> None:
    mount, sites = deck(materials)
    gateway, job = LocalMaterialsClient(materials), str(uuid4())
    sequence = materials.latest_ledger_sequence()
    plan = gateway.check_material_io({"param": _param(
        "load", mount, site="a1", resource_template="tip_box", material_name="新盒", barcode="TB-1",
        reagent={"quantity": 20, "quantity_unit": "mL", "name": "缓冲液"}), "task_uuid": TASK, "job_uuid": job})
    assert (plan["site_uuid"], plan["site_label"], plan["template_uuid"]) == (sites["A1"], "A1", "tip-box-template")
    assert plan["lock_material_uuids"] == [mount]
    assert materials.latest_ledger_sequence() == sequence  # 试运行不写库

    first = _command(gateway, plan, job)
    assert first["status"] == "completed", first
    result = first["result"]
    new = result["material_uuid"]
    assert result["material"] == {"uuid": new} and result["name"] == "新盒"
    placed = materials.get_material(new)
    assert placed.material.barcode == "TB-1" and placed.material.template_uuid == "tip-box-template"
    assert materials.occupied_site(new).site_uuid == sites["A1"]
    assert result["reagent"]["quantity"] == 20 and result["reagent"]["name"] == "缓冲液"
    assert [(item.name, item.quantity, item.quantity_unit) for item in placed.data.substances] == [("缓冲液", 20, "mL")]

    # 同一 attempt 重放：返回首次结果，不再建第二件
    replay = _command(gateway, plan, job)
    assert replay["replayed"] is True and replay["result"]["material_uuid"] == new
    assert len([item for item in materials.list_materials() if item.material.template_name == "tip_box"]) == 1


def test_load_without_name_allocates_one_from_the_template(materials) -> None:
    mount, _sites = deck(materials)
    gateway, job = LocalMaterialsClient(materials), str(uuid4())
    plan = gateway.check_material_io({"param": _param("load", mount, site="B1", resource_template="tip-box-template"),
                                      "task_uuid": TASK, "job_uuid": job})
    result = _command(gateway, plan, job)["result"]
    assert result["name"].startswith("tip_box-")


def test_load_never_overwrites_and_respects_the_site_whitelist(materials) -> None:
    mount, sites = deck(materials, whitelist=["bottle"])
    blocker = container(materials, mount, sites["B1"])
    material_io = MaterialIO(materials)
    with pytest.raises(MaterialConflictError) as occupied:
        material_io.check(_param("load", mount, site="B1", resource_template="tip_box"), task_uuid=TASK, job_uuid="j1")
    assert temporary_resource_condition(occupied.value) == "site_occupied"  # 确认前：等库位空出来
    with pytest.raises(MaterialConflictError) as refused:
        material_io.check(_param("load", mount, site="A1", resource_template="tip_box"), task_uuid=TASK, job_uuid="j2")
    assert refused.value.conflict == "site_template" and temporary_resource_condition(refused.value) is None
    with pytest.raises(MaterialNotFoundError):
        material_io.check(_param("load", mount, site="C9", resource_template="tip_box"), task_uuid=TASK, job_uuid="j3")

    # 确认后才被占：命令被拒并带物料错误码，原物料不动、不新建
    plan = material_io.plan(_param("load", mount, site="B1", resource_template="tip_box"), task_uuid=TASK, job_uuid="j4")
    rejected = _command(LocalMaterialsClient(materials), plan, "j4")
    assert (rejected["status"], rejected["error_code"], rejected["code"]) == ("rejected", "conflict:site_occupied", 6007)
    assert materials.occupied_site(blocker).site_uuid == sites["B1"]
    assert not [item for item in materials.list_materials() if item.material.template_name == "tip_box"]
    assert _command(LocalMaterialsClient(materials), plan, "j4")["replayed"] is True  # 拒绝同样只记一次


def test_unload_detaches_or_soft_deletes(materials) -> None:
    mount, sites = deck(materials)
    empty = container(materials, mount, sites["A1"])
    full = container(materials, mount, sites["B1"], water_ml=10)
    gateway = LocalMaterialsClient(materials)

    plan = gateway.check_material_io({"param": _param("unload", mount, site="A1", material=empty, disposition="detach",
                                                      scope="material_only"), "task_uuid": TASK, "job_uuid": "u1"})
    assert plan["lock_material_uuids"] == sorted([mount, empty])
    assert _command(gateway, plan, "u1")["result"]["affected_material_uuids"] == [empty]
    detached = materials.get_material(empty)
    assert detached.material.parent_material_uuid is None and materials.occupied_site(empty) is None
    assert detached.material.lifecycle_status == "active"  # 解除挂载：物料留在库存里

    with pytest.raises(MaterialConflictError, match="material_only 要求") as caught:
        MaterialIO(materials).check(_param("unload", mount, site="B1", material=full, disposition="soft_delete",
                                           scope="material_only"), task_uuid=TASK, job_uuid="u2")
    assert temporary_resource_condition(caught.value) is None
    with pytest.raises(MaterialConflictError, match="不在库位 A1 上"):
        MaterialIO(materials).check(_param("unload", mount, site="A1", material=full, disposition="soft_delete",
                                           scope="material_with_contents"), task_uuid=TASK, job_uuid="u3")
    plan = MaterialIO(materials).plan(_param("unload", mount, site="B1", material=full, disposition="soft_delete",
                                             scope="material_with_contents"), task_uuid=TASK, job_uuid="u4")
    assert _command(gateway, plan, "u4")["status"] == "completed"
    with pytest.raises(MaterialNotFoundError):
        materials.get_material(full)
    assert materials.get_site(sites["B1"]).occupied_material_uuid is None


def test_unload_waits_while_another_task_holds_the_material(materials) -> None:
    mount, sites = deck(materials)
    bottle = container(materials, mount, sites["A1"])
    other = str(uuid4())
    with materials.write() as conn:
        conn.execute(
            "INSERT INTO material_source_binding(binding_uuid, task_uuid, node_uuid, attempt, material_uuid, "
            "template_uuid, custody_policy, status, created_at_ms) VALUES (?,?,?,?,?,?,?,?,?)",
            (str(uuid4()), other, str(uuid4()), 1, bottle, "bottle-template", "shared_source", "active", 1),
        )
    param = _param("unload", mount, site="A1", material=bottle, disposition="detach", scope="material_only")
    with pytest.raises(MaterialConflictError, match=f"任务 {other} 的来源绑定") as caught:
        MaterialIO(materials).check(param, task_uuid=TASK, job_uuid="w1")
    assert temporary_resource_condition(caught.value) == "site_claimed"
    # 同一任务自己的绑定不算占用
    MaterialIO(materials).check(param, task_uuid=other, job_uuid="w2")


def test_refill_adds_to_the_reagent_in_place(materials) -> None:
    mount, sites = deck(materials)
    bottle = container(materials, mount, sites["A1"], water_ml=20)
    gateway = LocalMaterialsClient(materials)
    plan = gateway.check_material_io({"param": _param("refill", mount, site="A1", material={"uuid": bottle},
                                                      reagent={"quantity": 500, "quantity_unit": "uL"}),
                                      "task_uuid": TASK, "job_uuid": "r1"})
    result = _command(gateway, plan, "r1")["result"]
    assert (result["quantity_added"], result["quantity"], result["quantity_unit"]) == (0.5, 20.5, "mL")
    refilled = materials.get_material(bottle)
    assert [(item.quantity, item.quantity_unit) for item in refilled.data.substances] == [(20.5, "mL")]
    assert materials.occupied_site(bottle).site_uuid == sites["A1"]  # 位置不变，不新建物料
    history = materials.connection.execute(
        "SELECT event_type, quantity_delta, workflow_node_job_uuid FROM material_content_history "
        "WHERE material_uuid=? ORDER BY recorded_at_ms, history_uuid", (bottle,)).fetchall()
    assert ("add", 0.5, "r1") in [tuple(row) for row in history]

    with pytest.raises(MaterialConflictError, match="不在库位 B1 上"):
        MaterialIO(materials).check(_param("refill", mount, site="B1", material=bottle,
                                           reagent={"quantity": 1, "quantity_unit": "mL"}), task_uuid=TASK, job_uuid="r2")
    dry = container(materials, mount, sites["B1"])
    with pytest.raises(MaterialNotFoundError, match="没有可补量的试剂"):
        MaterialIO(materials).check(_param("refill", mount, site="B1", material=dry,
                                           reagent={"quantity": 1, "quantity_unit": "mL"}), task_uuid=TASK, job_uuid="r3")


def test_http_gateway_checks_and_commits_through_the_inventory_routes(tmp_path, monkeypatch) -> None:
    from tests.server.test_v1_inventory_api import _TestClientOpener
    from unilabos.client.materials import HTTPMaterialsClient
    from unilabos.client.materials.core import MaterialsHTTPError
    from unilabos.server.api.materials import install_materials_api

    service = MaterialsService(tmp_path / "materials.db")
    _ensure_template(service, "tip-box-template", "tip_box")
    app = FastAPI()
    install_materials_api(app, service)
    try:
        with TestClient(app) as client:
            monkeypatch.setattr("unilabos.client.materials.core.urlopen", _TestClientOpener(client))
            gateway = HTTPMaterialsClient("http://testserver")
            mount, sites = deck(service)
            plan = gateway.check_material_io({"param": _param("load", mount, site="A1", resource_template="tip_box"),
                                              "task_uuid": TASK, "job_uuid": "h1"})
            assert plan["site_uuid"] == sites["A1"]
            assert _command(gateway, plan, "h1")["status"] == "completed"
            with pytest.raises(MaterialsHTTPError) as occupied:
                gateway.check_material_io({"param": _param("load", mount, site="A1", resource_template="tip_box"),
                                           "task_uuid": TASK, "job_uuid": "h2"})
            # 经 HTTP 只剩业务码：6007 库位被占，调度器同样判为临时条件
            assert occupied.value.business_code == 6007
            assert temporary_resource_condition(occupied.value) == "site_occupied"
    finally:
        service.close()
