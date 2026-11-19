"""``material.io`` 库存命令：人工装入 / 取走 / 补量在一个事务里完成，按命令身份只写一次。

``check`` 是同一套写入的保存点试运行（不写库），抛出的异常与真正写入时相同；调度器用它在人工确认前判断
「等一等」还是「失败」。调度器闭环见 ``test_material_io_scheduler.py``。
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.plr_fixtures import TIP_RACK_MODULE, registry_entry
from tests.server.test_scheduler_inventory_authority import _mutation, _template
from unilabos.client.materials.core import LocalMaterialsClient
from unilabos.protocol.materials import (
    MaterialIdentityWrite,
    MaterialMove,
    MaterialNodeCreate,
    MaterialPatch,
    MaterialTreeCreate,
    SiteCreate,
)
from unilabos.registry.registry import lab_registry
from unilabos.resources.adapters.registry_materials import register_resource_definitions
from unilabos.resources.objects.site import SITE_TEMPLATE_WHITELIST_KEY
from unilabos.server.scheduler.resource_wait_policy import temporary_resource_condition
from unilabos.server.services.materials import MaterialsService
from unilabos.server.services.materials.contents import ContentContract
from unilabos.server.services.materials.core import (
    MaterialConflictError,
    MaterialNoChangeError,
    MaterialNotFoundError,
    MaterialValidationError,
)
from unilabos.server.services.materials.material_io import MaterialIO, material_io_command_id

TASK = str(uuid4())
#: 注册表里的 300 uL 枪头盒资源条目 id；注册表同步出的资源模板同名
TIP_RACK = "test_tip_rack_300ul"
#: 注册表里的单槽储液槽（只有一个孔，建树快）：调度器闭环测试只需要一个能实例化的模板
RESERVOIR = "test_reservoir_290ml"
RESERVOIR_MODULE = "pylabrobot.resources.opentrons.reservoirs:agilent_1_reservoir_290ml"


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


def device_deck(materials: MaterialsService, device_id: str = "liquid_handler") -> tuple[str, dict[str, str]]:
    """设备物料下挂一块两库位台面（A1、B1）：台面的所属设备是 ``device_id``。"""

    _ensure_template(materials, "deck-template", "deck")
    tree = materials.create_tree(_mutation("create_material_tree"), MaterialTreeCreate(nodes=[
        MaterialNodeCreate(client_ref="device", identity=MaterialIdentityWrite(
            resource_id=device_id, name=device_id, template_name="liquid_handler_device",
            resource_type="device", class_name="LiquidHandler")),
        MaterialNodeCreate(client_ref="deck", parent_client_ref="device", identity=_identity("deck", "deck"), sites=[
            SiteCreate(template_name="slot", site_index=0, label="A1"),
            SiteCreate(template_name="slot", site_index=1, label="B1")]),
    ])).data
    mount = tree.client_ref_map["deck"]
    return mount, {site.label: site.site_uuid for site in materials.get_material(mount).sites}


def registry_template(materials: MaterialsService, monkeypatch, entry_id: str = TIP_RACK,
                      module: str = TIP_RACK_MODULE) -> str:
    """注册表里有这个资源类，并同步成同名资源模板（与 Host 上报后的权威相同：模板名 = 资源条目 id）；返回模板名。"""

    monkeypatch.setitem(lab_registry.resource_type_registry, entry_id, registry_entry(module))
    register_resource_definitions([{"id": entry_id, **registry_entry(module)}], LocalMaterialsClient(materials))
    return entry_id


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


def test_plan_and_load_use_display_names(materials) -> None:
    """计划里给人看的挂载物料名取 display_name；装入的 material_name 落到 display_name。"""

    mount, _sites = deck(materials)
    materials.patch_material(_mutation("patch_material"), mount, MaterialPatch(display_name="主台面"))
    gateway, job = LocalMaterialsClient(materials), str(uuid4())
    plan = gateway.check_material_io({"param": _param(
        "load", mount, site="A1", resource_template="tip_box", material_name="新盒"), "task_uuid": TASK, "job_uuid": job})
    assert plan["mount_name"] == "主台面"
    result = _command(gateway, plan, job)["result"]
    assert result["display_name"] == "新盒"
    assert materials.get_material(result["material_uuid"]).material.display_name == "新盒"


def test_load_without_name_allocates_one_from_the_template(materials) -> None:
    mount, _sites = deck(materials)
    gateway, job = LocalMaterialsClient(materials), str(uuid4())
    plan = gateway.check_material_io({"param": _param("load", mount, site="B1", resource_template="tip-box-template"),
                                      "task_uuid": TASK, "job_uuid": job})
    result = _command(gateway, plan, job)["result"]
    assert result["name"].startswith("tip_box-")


def test_load_instantiates_the_registry_class_with_its_default_children(materials, monkeypatch) -> None:
    """模板对应注册表资源类：与 ``instantiate_material`` 同一条路建出整棵 PLR 树（枪头盒 + 96 个孔位 + 96 支枪头），
    根节点是装入步骤的身份；同一个命令身份重放不再建第二份。普通货架上也一样（能实例化就实例化）。"""

    from unilabos.resources.adapters.plr_materials import material_tree_to_resource_tree

    template = registry_template(materials, monkeypatch)
    mount, sites = deck(materials)
    gateway, job = LocalMaterialsClient(materials), str(uuid4())
    plan = gateway.check_material_io({"param": _param(
        "load", mount, site="A1", resource_template=template, material_name="新吸头盒", barcode="TR-1"),
        "task_uuid": TASK, "job_uuid": job})
    count = len(materials.list_materials())
    first = _command(gateway, plan, job)
    assert first["status"] == "completed", first
    new = first["result"]["material_uuid"]
    tree = materials.get_tree(new)
    root = next(node.material for node in tree.nodes if node.material.material_uuid == new)
    # name 是物料 UUID，人看的标签在 display_name；PLR 键（config.name）是按标签实例化出的 PLR 名
    assert (root.name, root.display_name, root.barcode) == (new, "新吸头盒", "TR-1")
    assert root.config["name"] == "新吸头盒"
    assert (root.template_name, root.template_uuid) == (template, plan["template_uuid"])
    assert root.meta_data["material_io"] == {"task_uuid": TASK, "job_uuid": job}
    assert root.config["type"] == root.class_name and root.resource_type == "tip_rack"
    spots = [node.material for node in tree.nodes if node.material.parent_material_uuid == new]
    tips = [node.material for node in tree.nodes if node.material.resource_type == "tip"]
    assert (len(spots), len(tips), len(tree.nodes)) == (96, 96, 1 + 96 + 96)
    assert {item.resource_type for item in spots} == {"tip_spot"}
    assert all(not node.material.barcode for node in tree.nodes if node.material.material_uuid != new)
    assert materials.occupied_site(new).site_uuid == sites["A1"]
    # 设备台面拉到的就是这棵树：能反序列化成带枪头的 PLR 枪头盒
    [rack] = material_tree_to_resource_tree(tree).to_plr_resources()
    assert rack.name == "新吸头盒" and rack.unilabos_uuid == new
    assert len(rack.children) == 96 and all(spot.has_tip() for spot in rack.children)

    replay = _command(gateway, plan, job)
    assert replay["replayed"] is True and replay["result"]["material_uuid"] == new
    assert len(materials.list_materials()) == count + 1 + 96 + 96

    # 名字没被根物料占用、但 resource_id 已被别的物料占用：根换一个不重名的 ID，子物料的路径前缀跟着换
    materials.create_tree(_mutation("create_material_tree"), MaterialTreeCreate(nodes=[MaterialNodeCreate(
        client_ref="other", identity=MaterialIdentityWrite(resource_id="第二盒", name="另一件", template_name="bottle",
                                                            resource_type="container", class_name="Container"))]))
    second = MaterialIO(materials).plan(_param("load", mount, site="B1", resource_template=template,
                                               material_name="第二盒"), task_uuid=TASK, job_uuid="second")
    result = _command(gateway, second, "second")["result"]
    nodes = [node.material for node in materials.get_tree(result["material_uuid"]).nodes]
    root_id = next(item.resource_id for item in nodes if item.material_uuid == result["material_uuid"])
    assert root_id.startswith("第二盒-")
    assert all(item.resource_id.startswith(f"{root_id}/") for item in nodes if item.material_uuid != result["material_uuid"])


def test_load_onto_a_shelf_keeps_identity_only_for_a_template_without_a_registry_class(materials) -> None:
    """普通货架（没有所属设备）+ 不能实例化的模板：照旧只按模板写一件物料的身份。"""

    mount, sites = deck(materials)
    gateway = LocalMaterialsClient(materials)
    plan = MaterialIO(materials).plan(_param("load", mount, site="A1", resource_template="tip_box"),
                                      task_uuid=TASK, job_uuid="shelf")
    new = _command(gateway, plan, "shelf")["result"]["material_uuid"]
    [node] = materials.get_tree(new).nodes
    assert (node.material.template_name, node.material.class_name) == ("tip_box", "Container")
    # 只写身份：没有 PLR 结构（config 只有权威给新根补的 PLR 键 = resource_id）
    assert node.material.config == {"name": node.material.resource_id} and node.material.name == new
    assert materials.occupied_site(new).site_uuid == sites["A1"]


def test_a_device_mount_rejects_a_template_that_cannot_be_instantiated(materials, monkeypatch) -> None:
    """挂载物料属于设备、模板不能实例化成 PLR 资源：计划 / 试运行就拒绝并说明原因（写库后台面同步必然失败）；
    冻结计划之后才变得不能实例化的，写库时同样拒绝，什么都不写。"""

    mount, _sites = device_deck(materials)
    material_io = MaterialIO(materials)
    sequence = materials.latest_ledger_sequence()
    for call in (material_io.plan, material_io.check):
        with pytest.raises(MaterialValidationError, match="属于设备 liquid_handler") as caught:
            call(_param("load", mount, site="A1", resource_template="tip_box"), task_uuid=TASK, job_uuid="d1")
        assert caught.value.subject == "template" and "registry 资源类不可实例化: 'tip_box'" in str(caught.value)
        assert temporary_resource_condition(caught.value) is None  # 合同错误：节点失败，不排队
    assert materials.latest_ledger_sequence() == sequence

    template = registry_template(materials, monkeypatch)
    plan = material_io.check(_param("load", mount, site="A1", resource_template=template), task_uuid=TASK, job_uuid="d2")
    assert plan["template_name"] == template
    monkeypatch.delitem(lab_registry.resource_type_registry, template)  # 确认期间注册表里没了这个资源类
    sequence = materials.latest_ledger_sequence()
    rejected = _command(LocalMaterialsClient(materials), plan, "d2")
    assert (rejected["status"], rejected["error_code"]) == ("rejected", "invalid_material:template")
    assert "属于设备 liquid_handler" in rejected["error"]
    assert materials.latest_ledger_sequence() == sequence


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
            # 设备挂载点 + 不能实例化的模板：试运行路由按参数不合法拒绝，调度器判为失败而不是排队
            device_mount, _device_sites = device_deck(service)
            with pytest.raises(MaterialsHTTPError) as invalid:
                gateway.check_material_io({"param": _param("load", device_mount, site="A1", resource_template="tip_box"),
                                           "task_uuid": TASK, "job_uuid": "h3"})
            assert invalid.value.business_code == 1000 and "属于设备 liquid_handler" in str(invalid.value)
            assert temporary_resource_condition(invalid.value) is None
    finally:
        service.close()
