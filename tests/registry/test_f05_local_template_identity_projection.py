"""F05.4-C0b2 本地资源模板（ResourceTemplate）身份同步合同。"""

from __future__ import annotations

import json
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import unilabos.workflow.composition as workflow_composition
from unilabos.app.scheduler.inventory.backend_api import (
    install_backend_resource_api,
)
from unilabos.app.scheduler.inventory.backend_contract import (
    TEMPLATE_DATA_CONFLICT,
    BackendContractError,
    BackendResourceService,
)
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.registry.ast_registry_scanner import _parse_file
from unilabos.registry.registry import Registry
from unilabos.registry.template_identity import device_template_uuid
from unilabos.registry.template_projection import RegistryTemplateProjectionError
from unilabos.registry.template_snapshot import RegistryTemplateSnapshot
from unilabos.workflow.composition import (
    compose_local_workflow_template_runtime,
    get_workflow_service,
    reset_workflow_service_for_test,
)
from unilabos.workflow.published_workflow_runtime import (
    PublishedWorkflowGenerationError,
)


class _BuiltRegistry:
    """暴露由真实 AST 扫描器和注册表构建器生成的模板定义。"""

    def __init__(
        self,
        *,
        devices: list[dict[str, Any]],
        resources: list[dict[str, Any]],
    ) -> None:
        """保存一次测试注册表（Registry）定义代际。

        参数说明：``devices`` 是设备模板定义；``resources`` 是资源模板
        （ResourceTemplate）定义。返回：无；调用者只通过标准读取接口交给冻结
        快照，测试替身不直接创建前端模板或工作流数据库身份。
        """

        self._devices = devices
        self._resources = resources

    def obtain_registry_device_info(self) -> list[dict[str, Any]]:
        """返回真实构建器生成的完整设备模板定义。

        参数：无。返回：本次测试注册表中的设备定义列表；冻结快照负责后续分离。
        """

        return self._devices

    def obtain_registry_resource_info(self) -> list[dict[str, Any]]:
        """返回真实构建器生成的完整资源模板定义。

        参数：无。返回：本次测试注册表中的资源定义列表；不注入库存 UUID。
        """

        return self._resources


class _FailingInventoryStore(InventoryStore):
    """模拟库存模板同步事务明确拒绝写入。"""

    reject_template_sync = False

    @contextmanager
    def transaction(self) -> Any:
        """在模板同步开始时返回稳定后端（Backend）合同错误。

        初始化和普通读取使用真实事务，模拟模板同步时抛出冲突。
        """

        if self.reject_template_sync:
            raise BackendContractError(TEMPLATE_DATA_CONFLICT, "测试模板身份冲突")
        with super().transaction() as connection:
            yield connection


def _build_registry(tmp_path: Path) -> _BuiltRegistry:
    """从 Python 声明构建含设备动作和物料模板的真实注册表输入。

    参数说明：``tmp_path`` 是隔离的 Python 包根目录。返回：通过产品 AST 扫描器
    与注册表（Registry）构建器产生的测试注册表。异常：源码合同无法扫描或构建时原样
    抛出，让测试不能退化为手写前端模板夹具。
    """

    # ``module_path`` 是静态扫描证据文件；产品扫描器不会导入或执行该源码。
    module_path = tmp_path / "local_templates.py"
    module_path.write_text(
        '''
from typing import TypedDict
from unilabos.registry.decorators import action, device, resource
from unilabos.registry.placeholder_type import ResourceSlot

@resource(
    id="plate_96",
    category=["plate"],
    displayname="96 孔板",
    description="测试反应板物料模板。",
)
def plate_96():
    """构造测试反应板。"""
    raise NotImplementedError

class TransferResult(TypedDict):
    material: ResourceSlot

@device(
    id="pump",
    category=["pump"],
    displayname="测试泵",
    description="用于模板身份同步测试的设备。",
)
class Pump:
    @action(description="转移反应板")
    def transfer(
        self,
        plate: ResourceSlot,
    ) -> TransferResult:
        """转移需要稳定模板身份的反应板。"""
        raise NotImplementedError
''',
        encoding="utf-8",
    )
    scanned_devices, scanned_resources = _parse_file(module_path, tmp_path)
    # ``registry_builder`` 复用产品注册表构建规则，不自行拼装动作 Schema。
    registry_builder = Registry()
    built_devices = [
        {
            "id": str(definition["device_id"]),
            **registry_builder._build_device_entry_from_ast(
                str(definition["device_id"]),
                definition,
            ),
        }
        for definition in scanned_devices
    ]
    # 设备级 Handle 使用冻结快照的 Backend 字段名，覆盖内存设备详情映射。
    built_devices[0]["handles"] = [
        {
            "handler_key": "plate_in",
            "data_type": "plate",
            "label": "反应板入口",
            "io_type": "target",
            "data_key": "plate",
        }
    ]
    built_resources = [
        {
            "id": str(definition["resource_id"]),
            **registry_builder._build_resource_entry_from_ast(
                str(definition["resource_id"]),
                definition,
            ),
        }
        for definition in scanned_resources
    ]
    # ``transfer_action`` 是真实构建器生成的动作（Action）合同；这里声明合法
    # 资源模板源码别名，让复用与重启测试同时覆盖动作允许模板的稳定身份解析。
    transfer_action = built_devices[0]["class"]["action_value_mappings"]["transfer"]
    # ``action_contract`` 是第 2 版动作合同（Action Contract）的版本扩展。
    action_contract = transfer_action["schema"]["x-unilabos-action-contract"]
    action_contract["resource_template_symbols"]["goal"] = {
        "plate": ["local_templates:plate_96"]
    }
    return _BuiltRegistry(devices=built_devices, resources=built_resources)


def _build_registry_with_initial_material_sites(tmp_path: Path) -> _BuiltRegistry:
    """构建带默认子物料短名的本地注册表，用于验证二次身份解析。"""

    registry = _build_registry(tmp_path)
    resources = list(registry.obtain_registry_resource_info())
    resources.extend(
        [
            {
                "id": "szlab_pipette_tip",
                "displayname": "测试枪头",
                "registry_type": "resource",
                "class": {"module": "local_templates:szlab_pipette_tip", "type": "python"},
                "source_fqid": "local_templates:szlab_pipette_tip",
                "category": ["tip"],
                "available_sites": [],
            },
            {
                "id": "szlab_tip_box",
                "displayname": "测试枪头盒",
                "registry_type": "resource",
                "class": {"module": "local_templates:szlab_tip_box", "type": "python"},
                "source_fqid": "local_templates:szlab_tip_box",
                "category": ["tip_box"],
                "available_sites": [
                    {
                        "index": 0,
                        "label": "T01",
                        "content_type": ["szlab_pipette_tip"],
                        "meta_data": {
                            "unilab": {
                                "initial_material_template": "szlab_pipette_tip",
                            }
                        },
                    }
                ],
            },
        ]
    )
    return _BuiltRegistry(
        devices=list(registry.obtain_registry_device_info()),
        resources=resources,
    )


def _active_template_identities(store: InventoryStore) -> dict[str, str]:
    """读取库存权威中的活动资源模板业务名与 UUID。

    参数说明：``store`` 是本地库存存储。返回：业务唯一名到稳定 UUID 的映射；
    仅用于验证公开组合操作产生的权威事实，不参与产品身份解析。
    """

    # ``template_rows`` 是 inventory.db 当前全部活动资源模板事实。
    template_rows = store.query_all(
        """
        SELECT uuid, name
        FROM resource_template
        WHERE deleted_at IS NULL
        ORDER BY name
        """
    )
    return {str(row["name"]): str(row["uuid"]) for row in template_rows}


def _template_storage_facts(
    store: InventoryStore,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """读取完整模板事实与聚合版本，用于证明失败前后零新增、零更新。

    参数说明：``store`` 是真实本地库存存储。返回：按稳定身份排序的资源模板
    （ResourceTemplate）行和模板库存聚合行；调用者只比较公开组合操作前后的
    权威事实，不把数据库旁路用作产品身份解析。
    """

    # 两组事实同时覆盖模板字段更新和聚合版本递增，避免只检查活动行数漏报更新。
    template_rows = [
        dict(row)
        for row in store.query_all("SELECT * FROM resource_template ORDER BY uuid")
    ]
    inventory_rows = [
        dict(row)
        for row in store.query_all(
            "SELECT uuid AS resource_template_uuid, aggregate_version "
            "FROM resource_template ORDER BY uuid"
        )
    ]
    return template_rows, inventory_rows


def test_local_composition_creates_missing_inventory_template_identities(
    tmp_path: Path,
) -> None:
    """本地组合必须先创建缺失身份，再发布可查询的工作流模板投影。

    参数说明：``tmp_path`` 隔离库存与工作流数据库。返回：无；断言只有物料
    资源模板（ResourceTemplate）进入 inventory.db，设备与动作留在内存目录，
    动作所有者与物料占位符（ResourceSlot）仍引用稳定 UUID。
    """

    reset_workflow_service_for_test()
    # ``inventory_store`` 是本地库存模板身份权威；启动前没有任何模板行。
    inventory_store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        # ``registry`` 是合法资源模板与动作源码别名共享的冻结前注册表代际。
        registry = _build_registry(tmp_path)
        # ``projection`` 是公开组合成功发布的工作流模板投影。
        _service, projection = compose_local_workflow_template_runtime(
            tmp_path,
            inventory_store=inventory_store,
            registry=registry,
        )

        # ``template_identities`` 只包含仍由 SQLite 承担的物料模板身份。
        template_identities = _active_template_identities(inventory_store)
        # ``action`` 是从同代投影查询到的转移动作（Action）模板及连接点集合。
        action = projection.snapshot().require_action(
            "local_templates:Pump",
            "transfer",
        )
        # ``plate_input`` 是声明合法反应板源码别名的动作物料输入连接点（Handle）。
        plate_input = next(
            handle
            for handle in action.handles
            if handle["io_type"] == "target" and handle["handle_key"] == "plate"
        )

        assert set(template_identities) == {"plate_96"}
        assert action.template["resource_template_uuid"] == device_template_uuid("pump")
        assert plate_input["meta_data"]["unilab"][
            "allowed_resource_template_uuids"
        ] == (template_identities["plate_96"],)
        assert (
            projection.snapshot().require_resource_template_uuid(
                "local_templates:plate_96"
            )
            == template_identities["plate_96"]
        )
        # 对外资源模板 API 仍能读取设备详情，但 SQLite 中没有设备模板行。
        resource_service = BackendResourceService(inventory_store)
        device_uuid = device_template_uuid("pump")
        device_detail = resource_service.get_resource_template(device_uuid)
        assert device_detail["name"] == "pump"
        assert device_detail["handles"] == [
            {
                "uuid": device_detail["handles"][0]["uuid"],
                "create_time": "1970-01-01T00:00:00Z",
                "update_time": "1970-01-01T00:00:00Z",
                "description": "",
                "meta_data": {},
                "resource_template_uuid": device_uuid,
                "name": "plate_in",
                "display_name": "反应板入口",
                "type": "plate",
                "io_type": "target",
                "key": "plate",
            }
        ]
        assert {
            item["name"]
            for item in resource_service.list_resource_templates(
                page=1,
                page_size=100,
                keyword="",
                resource_type="device",
            )["items"]
        } == {"pump"}
        assert (
            inventory_store.query_one(
                "SELECT COUNT(*) AS count FROM resource_template "
                "WHERE resource_type='device'"
            )["count"]
            == 0
        )
        # 原有物料实例写接口仍接受对外设备模板 UUID；校验来源已切到内存目录。
        device_material = resource_service.create_material(
            {
                "resource_template_uuid": device_uuid,
                "name": "测试泵实例",
                "barcode": "PUMP-001",
            }
        )
        assert device_material["resource_template_uuid"] == device_uuid
        graph_device = next(
            item
            for item in resource_service.material_graph()["nodes"]
            if item["material"]["uuid"] == device_material["uuid"]
        )
        assert graph_device["material"]["type"] == "device"
        assert graph_device["resource_template"]["name"] == "pump"
        # 同步当前领域包设备定义仍幂等成功，但不会重新写入 SQLite。
        device_receipt = resource_service.sync_resource_templates(
            RegistryTemplateSnapshot.from_registry(registry).detached_devices()
        )
        assert device_receipt == {
            "templates": [{"uuid": device_uuid, "name": "pump"}]
        }
        assert inventory_store.query_all(
            "SELECT uuid FROM resource_template WHERE resource_type='device' "
            "AND deleted_at IS NULL"
        ) == []
        app = FastAPI()
        install_backend_resource_api(app, resource_service)
        update_response = TestClient(app).put(
            f"/api/v1/resource-templates/{device_uuid}",
            json={"display_name": "接口不得覆盖的设备名"},
        )
        assert update_response.json()["code"] == TEMPLATE_DATA_CONFLICT
        assert resource_service.get_resource_template(device_uuid)[
            "display_name"
        ] == "测试泵"
    finally:
        reset_workflow_service_for_test()
        inventory_store.close()


def test_local_composition_resolves_initial_material_template_hint(
    tmp_path: Path,
) -> None:
    """本地同步必须把库位默认子物料短名落成稳定 UUID。"""

    reset_workflow_service_for_test()
    inventory_store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        registry = _build_registry_with_initial_material_sites(tmp_path)
        compose_local_workflow_template_runtime(
            tmp_path,
            inventory_store=inventory_store,
            registry=registry,
        )
        identities = _active_template_identities(inventory_store)
        row = inventory_store.query_one(
            "SELECT available_sites FROM resource_template "
            "WHERE name='szlab_tip_box' AND deleted_at IS NULL"
        )
        sites = json.loads(str(row["available_sites"]))
        assert sites[0]["initial_material_template_uuid"] == identities[
            "szlab_pipette_tip"
        ]
        assert identities["szlab_pipette_tip"] in sites[0][
            "allowed_resource_template_uuids"
        ]
    finally:
        reset_workflow_service_for_test()
        inventory_store.close()


def test_local_composition_preserves_business_identities_for_shared_implementations(
    tmp_path: Path,
) -> None:
    """多个模板复用同一 Python 实现类时仍须按业务 ID 稳定投影。

    参数说明：``tmp_path`` 隔离库存与工作流数据库。返回：无；测试通过公开本地
    工作流模板组合接缝证明，两个设备模板和两个遗留资源模板即使分别复用同一
    ``class.module``，也会获得互不相同的资源模板（ResourceTemplate）UUID；
    同代显式 ``source_fqid`` 及动作合同（Action Contract）合法资源别名仍解析到
    原资源模板 UUID。任一实现类复用被误判为源码声明冲突时测试失败。
    """

    reset_workflow_service_for_test()
    # ``inventory_store`` 是本轮业务 ID 到稳定资源模板 UUID 的唯一库存权威。
    inventory_store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        # ``registry`` 保留原合法强类型动作与显式物料资源源码身份，并增加历史
        # YAML 注册表中常见的“多个业务模板复用一个实现类”形状。
        registry = _build_registry(tmp_path)
        # ``shared_device_module`` 是两个设备业务模板共同使用的驱动实现类身份；
        # 它不是任一设备资源模板的业务唯一身份。
        shared_device_module = "lab.devices.shared:SharedDevice"
        # ``shared_resource_module`` 是两个遗留资源业务模板共同使用的容器实现类；
        # 它没有显式源码声明身份，因而不能被猜成唯一物料资源符号。
        shared_resource_module = "lab.resources.shared:SharedContainer"
        registry._devices.extend(
            [
                {
                    "id": business_id,
                    "displayname": business_id,
                    "class": {
                        "module": shared_device_module,
                        "type": "python",
                        "action_value_mappings": {},
                    },
                }
                for business_id in ("shared_device_a", "shared_device_b")
            ]
        )
        registry._resources.extend(
            [
                {
                    "id": business_id,
                    "displayname": business_id,
                    "class": {
                        "module": shared_resource_module,
                        "type": "python",
                        "action_value_mappings": {},
                    },
                }
                for business_id in ("shared_resource_a", "shared_resource_b")
            ]
        )

        # ``projection`` 是同一注册表代际成功同步后发布的可信模板投影。
        _service, projection = compose_local_workflow_template_runtime(
            tmp_path,
            inventory_store=inventory_store,
            registry=registry,
        )
        # ``template_identities`` 按注册表业务 ID 读取活动资源模板稳定 UUID。
        template_identities = _active_template_identities(inventory_store)
        # ``plate_input`` 是合法显式资源源码身份驱动的动作物料输入连接点。
        plate_input = next(
            handle
            for handle in projection.snapshot()
            .require_action("local_templates:Pump", "transfer")
            .handles
            if handle["io_type"] == "target" and handle["handle_key"] == "plate"
        )

        assert set(template_identities) == {
            "plate_96",
            "shared_resource_a",
            "shared_resource_b",
        }
        assert device_template_uuid("shared_device_a") != device_template_uuid(
            "shared_device_b"
        )
        assert (
            template_identities["shared_resource_a"]
            != template_identities["shared_resource_b"]
        )
        assert (
            projection.snapshot().require_resource_template_uuid(
                "local_templates:plate_96"
            )
            == template_identities["plate_96"]
        )
        assert plate_input["meta_data"]["unilab"][
            "allowed_resource_template_uuids"
        ] == (template_identities["plate_96"],)
    finally:
        reset_workflow_service_for_test()
        inventory_store.close()


def test_local_composition_reuses_existing_business_identity_uuid(
    tmp_path: Path,
) -> None:
    """已有活动业务唯一名必须复用 UUID，而不能产生第二模板身份。

    参数说明：``tmp_path`` 隔离数据库。返回：无；断言既有同步结果经本地组合
    再次同步后完全不变，并被模板投影（Template Projection）直接引用。
    """

    reset_workflow_service_for_test()
    # ``inventory_store`` 是预置同步与公开组合共同使用的真实库存权威。
    inventory_store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        # ``registry`` 是同一合法设备/物料模板定义代际。
        registry = _build_registry(tmp_path)
        # ``frozen_registry`` 是预置和组合必须共同遵守的同一规范定义代际。
        frozen_registry = RegistryTemplateSnapshot.from_registry(registry)
        # ``first_result`` 是公开组合前第一次同步的稳定身份回执（Receipt）。
        first_result = BackendResourceService(inventory_store).sync_resource_templates(
            frozen_registry.detached_definitions()
        )
        # ``expected_identities`` 只保留组合后仍由 SQLite 持有的物料模板身份。
        expected_identities = {
            str(item["name"]): str(item["uuid"])
            for item in first_result["templates"]
            if item["name"] == "plate_96"
        }

        # ``projection`` 必须引用预置回执中的同一活动资源模板身份。
        _service, projection = compose_local_workflow_template_runtime(
            tmp_path,
            inventory_store=inventory_store,
            registry=frozen_registry,
        )
        # ``action`` 是复用既有设备模板 UUID 的转移动作投影。
        action = projection.snapshot().require_action(
            "local_templates:Pump",
            "transfer",
        )
        # ``plate_input`` 必须复用回执中的同一反应板资源模板 UUID。
        plate_input = next(
            handle
            for handle in action.handles
            if handle["io_type"] == "target" and handle["handle_key"] == "plate"
        )

        assert _active_template_identities(inventory_store) == expected_identities
        assert action.template["resource_template_uuid"] == device_template_uuid("pump")
        assert plate_input["meta_data"]["unilab"][
            "allowed_resource_template_uuids"
        ] == (expected_identities["plate_96"],)
    finally:
        reset_workflow_service_for_test()
        inventory_store.close()


def test_local_composition_restart_keeps_template_identity_stable(
    tmp_path: Path,
) -> None:
    """重复组合与进程重启不得让库存模板 UUID 漂移。

    参数说明：``tmp_path`` 保留同一 inventory.db 与 workflow_history.db。返回：
    无；断言关闭并重新打开两个本地存储后，业务名映射和动作所有者身份均稳定。
    """

    reset_workflow_service_for_test()
    # ``inventory_path`` 是两个进程组合周期共同使用的持久库存数据库路径。
    inventory_path = tmp_path / "inventory.db"
    # ``first_store`` 持有首次组合周期的库存权威连接。
    first_store = InventoryStore(str(inventory_path))
    # ``registry`` 在两个周期保持同一资源模板及合法动作源码别名代际。
    registry = _build_registry(tmp_path)
    try:
        # ``first_projection`` 是首次组合发布的模板投影。
        _first_service, first_projection = compose_local_workflow_template_runtime(
            tmp_path,
            inventory_store=first_store,
            registry=registry,
        )
        # ``first_identities`` 冻结首次组合提交的活动业务 ID/UUID 映射。
        first_identities = _active_template_identities(first_store)
        # ``first_owner_uuid`` 是首次转移动作引用的设备资源模板稳定身份。
        first_owner_uuid = (
            first_projection.snapshot()
            .require_action(
                "local_templates:Pump",
                "transfer",
            )
            .template["resource_template_uuid"]
        )
    finally:
        reset_workflow_service_for_test()
        first_store.close()

    # ``restarted_store`` 模拟进程重启后重新打开同一库存数据库。
    restarted_store = InventoryStore(str(inventory_path))
    try:
        # ``second_projection`` 是重启后从同一注册表代际重新发布的模板投影。
        _second_service, second_projection = compose_local_workflow_template_runtime(
            tmp_path,
            inventory_store=restarted_store,
            registry=registry,
        )
        # ``restarted_action`` 必须同时保持设备所有者和反应板允许集稳定。
        restarted_action = second_projection.snapshot().require_action(
            "local_templates:Pump",
            "transfer",
        )
        # ``restarted_plate_input`` 是重启后再次从合法源码别名解析的物料输入。
        restarted_plate_input = next(
            handle
            for handle in restarted_action.handles
            if handle["io_type"] == "target" and handle["handle_key"] == "plate"
        )

        assert _active_template_identities(restarted_store) == first_identities
        assert restarted_action.template["resource_template_uuid"] == first_owner_uuid
        assert restarted_plate_input["meta_data"]["unilab"][
            "allowed_resource_template_uuids"
        ] == (first_identities["plate_96"],)
    finally:
        reset_workflow_service_for_test()
        restarted_store.close()


def test_runtime_device_catalog_rejects_sqlite_device_template_writes(
    tmp_path: Path,
) -> None:
    """运行时设备目录启用后不得再通过 Backend 合同写 SQLite 设备模板。

    参数：``tmp_path`` 隔离库存与工作流数据库。返回：无；断言即使设备名不在
    当前领域包中，设备模板写入也会关闭式失败，而物料模板既有行保持不变。
    """

    reset_workflow_service_for_test()
    inventory_store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        registry = _build_registry(tmp_path)
        compose_local_workflow_template_runtime(
            tmp_path,
            inventory_store=inventory_store,
            registry=registry,
        )
        persisted_before = _active_template_identities(inventory_store)

        with pytest.raises(
            BackendContractError,
            match="runtime package catalog",
        ) as error:
            BackendResourceService(inventory_store).sync_resource_templates(
                [
                    {
                        "id": "external_pump",
                        "display_name": "接口创建的泵",
                        "registry_type": "device",
                    }
                ]
            )

        assert error.value.code == TEMPLATE_DATA_CONFLICT
        assert _active_template_identities(inventory_store) == persisted_before
        assert inventory_store.query_all(
            "SELECT uuid FROM resource_template WHERE resource_type='device'"
        ) == []
    finally:
        reset_workflow_service_for_test()
        inventory_store.close()


def test_local_composition_rejects_conflicting_resource_source_aliases(
    tmp_path: Path,
) -> None:
    """同一源码别名指向多个资源模板 UUID 时必须关闭式失败。

    参数说明：``tmp_path`` 隔离数据库。返回：无；断言冲突不能选择任一物料模板
    （ResourceTemplate），也不能发布半成品工作流权威（Workflow Authority）。
    """

    reset_workflow_service_for_test()
    # ``inventory_store`` 持有冲突探针前后需要逐字段比较的库存权威事实。
    inventory_store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        # ``registry`` 初始为合法注册表代际，随后仅引入源码别名冲突。
        registry = _build_registry(tmp_path)
        # ``stable_snapshot`` 先建立合法同代事实；冲突组合不得更新既有模板版本。
        stable_snapshot = RegistryTemplateSnapshot.from_registry(registry)
        BackendResourceService(inventory_store).sync_resource_templates(
            stable_snapshot.detached_definitions()
        )
        # ``facts_before_conflict`` 冻结全部模板行与聚合版本作为原子性基线。
        facts_before_conflict = _template_storage_facts(inventory_store)
        # ``original_resource`` 是合法反应板资源模板定义的可变测试副本。
        original_resource = registry.obtain_registry_resource_info()[0]
        # ``conflicting_resource`` 是绑定相同源码别名的第二业务模板定义。
        conflicting_resource = {
            **original_resource,
            "id": "plate_384",
            "displayname": "384 孔板",
        }
        # 两个不同业务模板故意声明同一源码身份，不能静默选择先出现者。
        original_resource["source_fqid"] = "lab.resources:shared_plate"
        conflicting_resource["source_fqid"] = "lab.resources:shared_plate"
        registry._resources = [original_resource, conflicting_resource]

        with pytest.raises(RegistryTemplateProjectionError, match="源码身份"):
            compose_local_workflow_template_runtime(
                tmp_path,
                inventory_store=inventory_store,
                registry=registry,
            )
        assert get_workflow_service() is None
        assert _template_storage_facts(inventory_store) == facts_before_conflict
    finally:
        reset_workflow_service_for_test()
        inventory_store.close()


def test_local_composition_rejects_unresolvable_alias_before_inventory_write(
    tmp_path: Path,
) -> None:
    """不可解析源码别名必须在任何库存模板写事务之前关闭式失败。

    参数说明：``tmp_path`` 隔离真实库存数据库。返回：无；断言非法
    ``source_fqid`` 和类模块别名不能留下资源模板（ResourceTemplate）或模板库存
    聚合事实，也不能发布半成品工作流权威（Workflow Authority）。
    """

    reset_workflow_service_for_test()
    # ``inventory_store`` 用真实事务证明非法模板别名不会产生任何写入。
    inventory_store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        # ``facts_before_invalid_alias`` 包含迁移生成的软删除兼容占位事实；失败后也不得更新。
        facts_before_invalid_alias = _template_storage_facts(inventory_store)
        # ``registry`` 是将被注入非法 Python 源码身份的注册表代际。
        registry = _build_registry(tmp_path)
        # ``invalid_resource`` 是同时破坏两个源码身份字段的物料模板定义。
        invalid_resource = registry.obtain_registry_resource_info()[0]
        # 两个字段共同构成无效 Python 源码别名，不能延迟到库存提交后才校验。
        invalid_resource["source_fqid"] = "not a python source identity"
        invalid_resource["class"]["module"] = "not a python source identity"
        registry._resources = [invalid_resource]

        with pytest.raises(RegistryTemplateProjectionError, match="源码身份"):
            compose_local_workflow_template_runtime(
                tmp_path,
                inventory_store=inventory_store,
                registry=registry,
            )

        assert get_workflow_service() is None
        assert _template_storage_facts(inventory_store) == facts_before_invalid_alias
    finally:
        reset_workflow_service_for_test()
        inventory_store.close()


def test_local_composition_rejects_unknown_action_alias_without_inventory_write(
    tmp_path: Path,
) -> None:
    """未知动作源码别名必须在库存同步前失败且保持全部模板事实不变。

    参数说明：``tmp_path`` 隔离真实 ``inventory.db`` 与工作流数据库。返回：无；
    测试先通过注册表快照同步接缝建立合法资源模板（ResourceTemplate）回执，再经
    公开工作流运行时组合（``compose_local_workflow_template_runtime``）提交包含
    未知源码别名的动作合同（Action Contract）。断言失败前后的活动/历史模板行、
    UUID、``deleted_at``、聚合版本与既有回执身份完全一致，并且不发布工作流权威
    （Workflow Authority）。
    """

    reset_workflow_service_for_test()
    # ``inventory_store`` 是本轮唯一库存权威，用真实 SQLite 事务暴露部分写入。
    inventory_store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        # ``registry`` 是由真实 AST 扫描器和注册表（Registry）构建器产生的代际。
        registry = _build_registry(tmp_path)
        # ``stable_snapshot`` 是失败探针前已成功提交的同一规范模板代际。
        stable_snapshot = RegistryTemplateSnapshot.from_registry(registry)
        # ``stable_receipt`` 冻结失败前活动业务 ID 到 UUID 的同步回执（Receipt）。
        stable_receipt = BackendResourceService(
            inventory_store
        ).sync_resource_templates(stable_snapshot.detached_definitions())
        # ``stable_receipt_identities`` 是回执承诺的活动模板稳定身份全集。
        stable_receipt_identities = {
            str(identity["name"]): str(identity["uuid"])
            for identity in stable_receipt["templates"]
        }
        # ``facts_before_unknown_alias`` 包含活动/历史行、软删除时间与聚合版本。
        facts_before_unknown_alias = _template_storage_facts(inventory_store)
        # ``transfer_action`` 是公开组合将消费的真实已构建动作定义。
        transfer_action = registry.obtain_registry_device_info()[0]["class"][
            "action_value_mappings"
        ]["transfer"]
        # ``action_contract`` 是故意改为未知资源模板源码别名的动作合同版本扩展。
        action_contract = transfer_action["schema"]["x-unilabos-action-contract"]
        action_contract["resource_template_symbols"]["goal"] = {
            "plate": ["lab.resources:plate_96"]
        }

        with pytest.raises(RegistryTemplateProjectionError, match="源码身份"):
            compose_local_workflow_template_runtime(
                tmp_path,
                inventory_store=inventory_store,
                registry=registry,
            )

        assert get_workflow_service() is None
        assert _active_template_identities(inventory_store) == (
            stable_receipt_identities
        )
        assert _template_storage_facts(inventory_store) == (facts_before_unknown_alias)
    finally:
        reset_workflow_service_for_test()
        inventory_store.close()


def test_local_composition_rejects_shared_class_action_alias_before_inventory_write(
    tmp_path: Path,
) -> None:
    """Action（动作）不得通过共享实现类猜测物料资源模板。

    参数说明：``tmp_path`` 隔离真实库存（Inventory）与工作流数据库。返回：无；
    断言显式资源模板和遗留资源模板共享 ``class.module`` 时，引用该歧义实现类的
    动作合同（Action Contract）在库存同步前关闭式失败，且不留下任何资源模板
    （ResourceTemplate）或模板库存聚合的部分写入。
    """

    reset_workflow_service_for_test()
    # ``inventory_store`` 是用于证明预检失败前后零部分写入的本地库存权威。
    inventory_store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        # ``facts_before_ambiguous_alias`` 是组合开始前完整模板与聚合事实基线。
        facts_before_ambiguous_alias = _template_storage_facts(inventory_store)
        # ``registry`` 提供一个真实强类型动作和可修改的资源模板定义代际。
        registry = _build_registry(tmp_path)
        # ``shared_class`` 是显式与遗留资源模板共同复用的实现类身份。
        shared_class = "lab.resources.shared:Container"
        # ``explicit_resource`` 保留作者显式声明的稳定来源身份。
        explicit_resource = registry.obtain_registry_resource_info()[0]
        explicit_resource["class"]["module"] = shared_class
        explicit_resource["source_fqid"] = "lab.resources:explicit_plate"
        # ``legacy_resource`` 只有业务 ID 与共享实现类，不声明稳定来源身份。
        legacy_resource = deepcopy(explicit_resource)
        legacy_resource["id"] = "legacy_plate"
        legacy_resource["displayname"] = "遗留反应板"
        legacy_resource.pop("source_fqid", None)
        registry._resources = [explicit_resource, legacy_resource]
        # ``action_contract`` 故意引用无法证明唯一所有者的实现类兼容别名。
        transfer_action = registry.obtain_registry_device_info()[0]["class"][
            "action_value_mappings"
        ]["transfer"]
        action_contract = transfer_action["schema"]["x-unilabos-action-contract"]
        action_contract["resource_template_symbols"]["goal"] = {"plate": [shared_class]}

        with pytest.raises(RegistryTemplateProjectionError, match="源码身份"):
            compose_local_workflow_template_runtime(
                tmp_path,
                inventory_store=inventory_store,
                registry=registry,
            )

        assert get_workflow_service() is None
        assert _template_storage_facts(inventory_store) == (
            facts_before_ambiguous_alias
        )
    finally:
        reset_workflow_service_for_test()
        inventory_store.close()


def test_local_composition_fails_closed_when_inventory_sync_is_rejected(
    tmp_path: Path,
) -> None:
    """本地库存拒绝资源模板身份同步时，模板运行时必须关闭式失败。

    参数说明：``tmp_path`` 隔离数据库。返回：无；断言同步错误被转换为模板投影
    （Template Projection）领域错误，且不发布半装配工作流权威（Workflow
    Authority）。
    """

    reset_workflow_service_for_test()
    # ``inventory_store`` 固定抛出同步冲突，验证组合根的失败关闭转换。
    inventory_store = _FailingInventoryStore(str(tmp_path / "inventory.db"))
    inventory_store.reject_template_sync = True
    try:
        with pytest.raises(RegistryTemplateProjectionError, match="同步失败"):
            compose_local_workflow_template_runtime(
                tmp_path,
                inventory_store=inventory_store,
                registry=_build_registry(tmp_path),
            )
        assert get_workflow_service() is None
    finally:
        reset_workflow_service_for_test()
        inventory_store.close()


def test_workflow_catalog_failure_does_not_publish_or_migrate_device_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """领域工作流目录失败时不得发布设备目录或迁移旧设备模板。

    参数：临时目录隔离数据库，``monkeypatch`` 在设备/动作预检通过后注入领域
    工作流代际失败。返回：无；断言旧设备模板仍是活动持久事实，运行时设备目录
    仍未安装，证明三个目录共同成功前没有半代设备定义对外可见。
    """

    reset_workflow_service_for_test()
    inventory_store = InventoryStore(str(tmp_path / "inventory.db"))
    registry = _build_registry(tmp_path)
    try:
        # 模拟升级前仍在 SQLite 的设备模板；成功启动才允许迁移并软删除。
        BackendResourceService(inventory_store).sync_resource_templates(
            RegistryTemplateSnapshot.from_registry(registry).detached_definitions()
        )

        def fail_generation(**_arguments: Any) -> None:
            raise PublishedWorkflowGenerationError("测试领域工作流目录无效")

        monkeypatch.setattr(
            workflow_composition,
            "build_published_workflow_generation",
            fail_generation,
        )
        with pytest.raises(
            RegistryTemplateProjectionError,
            match="测试领域工作流目录无效",
        ):
            compose_local_workflow_template_runtime(
                tmp_path,
                inventory_store=inventory_store,
                registry=registry,
            )

        assert inventory_store.runtime_device_template_catalog is None
        assert inventory_store.query_one(
            "SELECT COUNT(*) AS count FROM resource_template "
            "WHERE resource_type='device' AND deleted_at IS NULL"
        )["count"] == 1
        legacy_device = inventory_store.query_one(
            "SELECT uuid FROM resource_template "
            "WHERE name='pump' AND resource_type='device' AND deleted_at IS NULL"
        )
        unknown_device = deepcopy(
            registry.obtain_registry_device_info()[0]
        )
        unknown_device["id"] = "untrusted_device_after_failed_startup"
        with pytest.raises(BackendContractError) as rejected:
            BackendResourceService(inventory_store).sync_resource_templates(
                [unknown_device]
            )
        assert rejected.value.code == TEMPLATE_DATA_CONFLICT
        assert inventory_store.query_one(
            "SELECT COUNT(*) AS count FROM resource_template "
            "WHERE resource_type='device' AND deleted_at IS NULL"
        )["count"] == 1
        with pytest.raises(BackendContractError) as name_collision:
            BackendResourceService(inventory_store).sync_resource_templates(
                [
                    {
                        "id": "pump",
                        "display_name": "伪装成物料模板的同名设备",
                        "registry_type": "resource",
                    }
                ]
            )
        assert name_collision.value.code == TEMPLATE_DATA_CONFLICT
        with pytest.raises(BackendContractError) as delete_rejected:
            BackendResourceService(inventory_store).delete_resource_template(
                str(legacy_device["uuid"])
            )
        assert delete_rejected.value.code == TEMPLATE_DATA_CONFLICT
        assert get_workflow_service() is None
    finally:
        reset_workflow_service_for_test()
        inventory_store.close()


def test_runtime_catalog_activation_rejects_preexisting_material_name_collision(
    tmp_path: Path,
) -> None:
    """启动前已存在的同名物料模板不得与内存设备目录形成双权威。

    参数：``tmp_path`` 隔离库存与工作流数据库。返回：无；先用旧入口持久化名为
    ``pump`` 的物料模板，再加载同名设备包，断言目录激活关闭式失败且原事实未被
    改写、运行时目录和工作流权威均未发布。
    """

    reset_workflow_service_for_test()
    inventory_store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        receipt = BackendResourceService(inventory_store).sync_resource_templates(
            [
                {
                    "id": "pump",
                    "display_name": "旧版同名物料模板",
                    "registry_type": "resource",
                }
            ]
        )
        with pytest.raises(
            RegistryTemplateProjectionError,
            match="已被活动物料模板占用",
        ):
            compose_local_workflow_template_runtime(
                tmp_path,
                inventory_store=inventory_store,
                registry=_build_registry(tmp_path),
            )

        assert inventory_store.runtime_device_template_catalog is None
        assert inventory_store.query_one(
            "SELECT uuid,resource_type FROM resource_template "
            "WHERE name='pump' AND deleted_at IS NULL"
        ) == {
            "uuid": receipt["templates"][0]["uuid"],
            "resource_type": "resource",
        }
        assert get_workflow_service() is None
    finally:
        reset_workflow_service_for_test()
        inventory_store.close()
