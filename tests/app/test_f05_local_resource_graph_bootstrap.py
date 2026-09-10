"""本地资源树进入公共物料图（MaterialGraph）的启动合同测试。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from unilabos.app.main import should_bootstrap_local_resource_graph
from unilabos.app.scheduler import integration
from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
from unilabos.app.scheduler.inventory.resource_graph_bootstrap import (
    ResourceGraphBootstrapError,
    bootstrap_local_resource_graph,
)
from unilabos.app.scheduler.inventory.resource_reference import (
    build_inventory_resource_reference_resolver,
)
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.registry.template_snapshot import RegistryTemplateSnapshot

MOUNT_MATERIAL_UUID = "97539b08-24de-5003-8b2e-9eb6e983c68a"
FIRST_SITE_UUID = "1962ab7c-b006-5e44-a1bd-9b1fde81d529"


class _Registry:
    """提供启动投影所需的最小设备注册表（Registry）快照。

    参数：构造时无参数。返回：两个读取方法分别返回设备与器材模板定义。
    异常：无；测试只提供一个身份唯一的设备资源模板（ResourceTemplate）。
    """

    def obtain_registry_device_info(self) -> list[dict[str, Any]]:
        """返回固定设备资源模板定义。

        参数：无。返回：包含稳定业务 ID 与 Python 源码身份的单元素列表。
        异常：无；``m2b_mount`` 是资源图设备类和资源模板的共同业务身份。
        """

        return [
            {
                "id": "m2b_mount",
                "display_name": "M2B Mount",
                "type": "device",
                "class": {
                    "module": "m2b_native_e2e.mount:M2BMount",
                    "type": "M2BMount",
                    "action_value_mappings": {},
                },
                "handles": [],
                "category": ["stacker"],
                "config_info": [],
                "scene": [],
                "device_params": {},
            }
        ]

    def obtain_registry_resource_info(self) -> list[dict[str, Any]]:
        """返回空器材资源模板集合。

        参数：无。返回：空列表。异常：无；本夹具只验证设备物料与库位（Site）。
        """

        return []


class _SharedImplementationRegistry(_Registry):
    """提供两个合法复用同一 Python 实现类的设备资源模板。"""

    def obtain_registry_device_info(self) -> list[dict[str, Any]]:
        """返回业务 ID 不同但实现类相同的两个设备模板。

        参数：无。返回：主设备和次设备定义；二者共享实现类身份，但资源图只按
        唯一业务 ID 引用主设备。异常：无；每次调用都返回无共享引用的新字典。
        """

        primary = super().obtain_registry_device_info()[0]
        secondary = {
            **primary,
            "id": "m2b_mount_secondary",
            "display_name": "M2B Mount Secondary",
            "class": dict(primary["class"]),
        }
        return [primary, secondary]


class _RegistryWithHostExecutor(_Registry):
    """在资源图设备之外发布 OS 内建 Host 平台执行器模板。"""

    def obtain_registry_device_info(self) -> list[dict[str, Any]]:
        """返回资源树设备与内建 Host 平台执行器资源模板。

        参数：无。返回：保留原设备并追加 ``host_node`` 模板的新列表。异常：无；
        Host 不出现在资源树快照中，用于验证启动投影补齐实际设备物料身份。
        """

        return [
            *super().obtain_registry_device_info(),
            {
                "id": "host_node",
                "display_name": "Host Node",
                "type": "device",
                "class": {
                    "module": "unilabos.ros.nodes.presets.host_node:HostNode",
                    "type": "HostNode",
                    "action_value_mappings": {},
                },
                "handles": [],
                "category": ["platform-executor"],
                "config_info": [],
                "scene": [],
                "device_params": {},
            },
        ]


class _RegistryWithChangedAction(_Registry):
    """仅修改设备动作合同、不修改库存资源模板的注册表。"""

    def obtain_registry_device_info(self) -> list[dict[str, Any]]:
        """返回带新动作字段的同一设备资源模板。"""

        device = super().obtain_registry_device_info()[0]
        device["class"]["action_value_mappings"] = {
            "pick": {
                "type": "UniLabJsonCommand",
                "goal": {"resource": {}},
                "result": {"resource": {}},
            }
        }
        return [device]


class _RegistryWithConfigSite(_Registry):
    """提供配置式库位（Site）占用所需的父子设备资源模板。"""

    def obtain_registry_device_info(self) -> list[dict[str, Any]]:
        """返回挂载设备与其库位中子设备的唯一资源模板定义。"""
        child = {
            **super().obtain_registry_device_info()[0],
            "id": "community.m2b_native_e2e.m2b_child",
            "display_name": "M2B Child",
            "class": {
                "module": "m2b_native_e2e.child:M2BChild",
                "type": "M2BChild",
                "action_value_mappings": {},
            },
        }
        return [*super().obtain_registry_device_info(), child]


class _ConfigSiteResourceTree:
    """暴露由父物料 ``config.sites`` 声明的单个已占用库位。"""

    def dump(self) -> list[list[dict[str, Any]]]:
        """返回父子物料与配置式库位声明，不伪造显式库位节点。"""
        owner_runtime_uuid = "64000000-0000-4000-8000-0000000002c0"
        child_runtime_uuid = "64000000-0000-4000-8000-0000000002c1"
        return [[
            {
                "id": "m2b_mount",
                "uuid": owner_runtime_uuid,
                "name": "Stacker A",
                "parent_uuid": None,
                "type": "device",
                "class": "m2b_mount",
                "pose": _pose(0, 0, 0, 360, 300, 720),
                "config": {
                    "sites": [{
                        "label": "Slot 1",
                        "position": {"x": 10, "y": 20, "z": 30},
                        "size": {"width": 100, "height": 90, "depth": 80},
                        "content_type": ["m2b_child"],
                        "occupied_by": "m2b_child",
                    }]
                },
                "data": {},
            },
            {
                "id": "m2b_child",
                "uuid": child_runtime_uuid,
                "name": "Child A",
                "parent_uuid": owner_runtime_uuid,
                "type": "device",
                "class": "community.m2b_native_e2e.m2b_child",
                "pose": _pose(10, 20, 30, 100, 90, 80),
                "config": {},
                "data": {},
            },
        ]]


class _ResourceTree:
    """以产品 ``ResourceTreeSet.dump`` 形状暴露固定资源树。

    参数：``site_parent_uuid`` 可覆盖库位（Site）的父运行时 UUID；
    ``mount_name`` 可改变资源图内容以制造指纹冲突；``mount_scale`` 模拟旧资源
    跟踪器（ResourceTracker）的零值缩放。返回：``dump`` 提供一棵树。异常：无；
    非法父引用与缩放由被测深模块关闭式处理。
    """

    def __init__(
        self,
        *,
        site_parent_uuid: str = "64000000-0000-4000-8000-0000000002b0",
        mount_name: str = "Stacker A",
        mount_scale: float = 1,
    ) -> None:
        """保存测试资源树的可变输入。

        参数：三个参数分别表示库位父身份、设备展示名与设备三轴缩放值。返回：
        无。异常：无；``site_parent_uuid`` 是运行时父引用，不是正式物料 UUID。
        """

        self._site_parent_uuid = site_parent_uuid
        self._mount_name = mount_name
        self._mount_scale = mount_scale

    def dump(self) -> list[list[dict[str, Any]]]:
        """返回设备物料和两个有序库位（Site）的序列化树。

        参数：无。返回：与 ``ResourceTreeSet.dump`` 相同的嵌套列表。
        异常：无；运行时 UUID 只用于关系解析，正式身份由资源图来源生成。
        """

        # ``runtime_mount_uuid`` 是资源树内部关系身份，不得成为库存权威物料身份。
        runtime_mount_uuid = "64000000-0000-4000-8000-0000000002b0"
        # ``mount_pose`` 允许精确复现 ResourceTracker 未显式配置时的三轴零缩放。
        mount_pose = _pose(0, 0, 0, 360, 300, 720)
        mount_pose["scale"] = {
            "x": self._mount_scale,
            "y": self._mount_scale,
            "z": self._mount_scale,
        }
        return [
            [
                {
                    "id": "m2b_mount",
                    "uuid": runtime_mount_uuid,
                    "name": self._mount_name,
                    "description": "",
                    "parent_uuid": None,
                    "type": "device",
                    "class": "m2b_mount",
                    "pose": mount_pose,
                    "config": {"category": "stacker"},
                    "data": {},
                    "barcode": "",
                },
                {
                    "id": "slot_a",
                    "uuid": "64000000-0000-4000-8000-0000000002b1",
                    "name": "Slot 1",
                    "description": "",
                    "parent_uuid": self._site_parent_uuid,
                    "type": "well",
                    "class": "",
                    "pose": _pose(0, 0, 40, 100, 100, 24),
                    "config": {"category": "well"},
                    "data": {},
                    "barcode": "",
                },
                {
                    "id": "slot_b",
                    "uuid": "64000000-0000-4000-8000-0000000002b2",
                    "name": "Slot 2",
                    "description": "",
                    "parent_uuid": runtime_mount_uuid,
                    "type": "well",
                    "class": "",
                    "pose": _pose(120, 0, 40, 100, 100, 24),
                    "config": {"category": "well"},
                    "data": {},
                    "barcode": "",
                },
            ]
        ]


def _pose(
    x: float,
    y: float,
    z: float,
    width: float,
    height: float,
    depth: float,
) -> dict[str, Any]:
    """构造资源树位置与尺寸。

    参数：前三项是位置，后三项是宽、高、深。返回：产品 ``pose`` 字典。
    异常：无；数值均是已知有限测试向量。
    """

    return {
        "position": {"x": x, "y": y, "z": z},
        "size": {"width": width, "height": height, "depth": depth},
        "scale": {"x": 1, "y": 1, "z": 1},
        "rotation": {"x": 0, "y": 0, "z": 0},
    }


def _bootstrap(
    store: InventoryStore,
    tree: _ResourceTree,
    *,
    registry: _Registry | None = None,
    material_rendering_by_template: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """通过正式深模块接口执行一次启动投影。

    参数：``store`` 是本地库存权威，``tree`` 是资源树集合替身；``registry``
    可注入含共享实现类的注册表（Registry），缺省使用单模板注册表；
    ``material_rendering_by_template`` 是已编译的公共模型快照。返回：导入回执。
    异常：资源图非法或与既有权威冲突时传播
    ``ResourceGraphBootstrapError``。
    """

    # ``registry_snapshot`` 是模板同步与资源投影共同消费的单代注册表事实。
    registry_snapshot = RegistryTemplateSnapshot.from_registry(registry or _Registry())
    return bootstrap_local_resource_graph(
        store=store,
        resource_tree_set=tree,
        registry_snapshot=registry_snapshot,
        source_id="/workspace/m2b-native-workspace/graph.json",
        material_rendering_by_template=material_rendering_by_template,
    )


def test_first_bootstrap_exposes_stable_device_material_and_ordered_sites() -> None:
    """首次启动必须通过公共接口发布稳定设备物料和业务顺序库位。

    参数：无。返回：无。断言：UUID5 固定向量、父物料与 ``sort_order`` 不漂移。
    """

    store = InventoryStore(":memory:")
    try:
        receipt = _bootstrap(store, _ResourceTree())
        graph = BackendResourceService(store).material_graph()
    finally:
        store.close()

    assert receipt["status"] == "imported"
    assert [node["material"]["uuid"] for node in graph["nodes"]] == [
        MOUNT_MATERIAL_UUID
    ]
    sites = graph["nodes"][0]["sites"]
    assert [site["uuid"] for site in sites] == [
        FIRST_SITE_UUID,
        "56dfa4a8-06b8-5750-bff9-b2290766a57d",
    ]
    assert [site["sort_order"] for site in sites] == [0, 1]
    assert all(site["material_uuid"] == MOUNT_MATERIAL_UUID for site in sites)


def test_bootstrap_projects_public_shape_kind_and_model_url() -> None:
    """物料读模型必须携带外形类型与 OS 公开模型 URL。

    参数：无。返回：无。断言：资源树类别进入 ``rendering.kind``，
    工作区编译结果进入 ``rendering.model``，且路径不使用
    ``local_bridge``。异常：任一投影丢失时测试失败。
    """

    # ``public_model`` 是浏览器只能通过 OS HTTP 读取的模型快照。
    public_model = {
        "path": "/api/v1/material-models/szlab/device.xacro",
        "format": "xacro",
        "meshDir": "/api/v1/material-models/szlab/models",
        "macro": "m2b_mount",
    }
    store = InventoryStore(":memory:")
    try:
        _bootstrap(
            store,
            _ResourceTree(),
            material_rendering_by_template={"m2b_mount": public_model},
        )
        graph = BackendResourceService(store).material_graph()
    finally:
        store.close()

    rendering = graph["nodes"][0]["material"]["config"]["rendering"]
    assert rendering == {"kind": "stacker", "model": public_model}
    assert "local_bridge" not in rendering["model"]["path"]


def test_config_sites_project_ordered_occupied_inventory_sites() -> None:
    """配置式库位声明必须进入库存权威并保留占用物料身份。"""
    store = InventoryStore(":memory:")
    try:
        receipt = _bootstrap(
            store,
            _ConfigSiteResourceTree(),
            registry=_RegistryWithConfigSite(),
        )
        graph = BackendResourceService(store).material_graph()
    finally:
        store.close()

    owner = next(node for node in graph["nodes"] if node["material"]["name"] == "Stacker A")
    child = next(node for node in graph["nodes"] if node["material"]["name"] == "Child A")
    assert receipt["site_count"] == 1
    assert owner["sites"][0]["name"] == "Slot 1"
    assert owner["sites"][0]["occupied_material_uuid"] == child["material"]["uuid"]
    assert child["current_site_uuid"] == owner["sites"][0]["uuid"]


def test_shared_implementation_class_keeps_unique_business_aliases() -> None:
    """共享 Python 实现类不得阻止按唯一业务 ID 投影本地资源图。

    参数：无。返回：无。断言：两个资源模板（ResourceTemplate）合法复用同一
    实现类时，模糊类别名不进入解析表，但资源图中的 ``m2b_mount`` 业务 ID 仍
    唯一解析并提交；这复现 SZLab 多设备复用 ``MoveitInterface`` 的启动形状。
    """

    store = InventoryStore(":memory:")
    try:
        receipt = _bootstrap(
            store,
            _ResourceTree(),
            registry=_SharedImplementationRegistry(),
        )
        graph = BackendResourceService(store).material_graph()
    finally:
        store.close()

    assert receipt["status"] == "imported"
    assert [node["material"]["uuid"] for node in graph["nodes"]] == [
        MOUNT_MATERIAL_UUID
    ]


def test_bootstrap_persists_implicit_host_executor_material_identity() -> None:
    """内建 Host 平台执行器必须在同一启动事务获得实际设备物料身份。

    参数：无。返回：无。断言：注册表（Registry）发布 ``host_node`` 但资源树
    未显式包含它时，库存权威（Inventory Authority）仍持久化唯一实际设备物料
    （Material），业务 ID 解析器可返回规范物料与资源模板 UUID；不创建工作流
    任务（WorkflowTask）或执行动作。
    """

    store = InventoryStore(":memory:")
    try:
        _bootstrap(
            store,
            _ResourceTree(),
            registry=_RegistryWithHostExecutor(),
        )
        # ``resolved_host`` 是工作流创作与执行器绑定共用的最小库存身份回执。
        resolved_host = build_inventory_resource_reference_resolver(store)("host_node")
        material_count = store.query_one("SELECT COUNT(*) AS count FROM material")
        host_row = store.query_one(
            "SELECT name,meta_data FROM material WHERE uuid=?",
            (resolved_host["uuid"],) if resolved_host is not None else ("",),
        )
    finally:
        store.close()

    assert resolved_host is not None
    assert material_count == {"count": 2}
    assert host_row is not None and host_row["name"] == "Host Node"
    assert '"source_node_id":"host_node"' in host_row["meta_data"]


def test_legacy_zero_scale_is_normalized_to_identity_scale() -> None:
    """旧资源跟踪器的零缩放应按“未指定”规范化为单位缩放。

    参数：无。返回：无。断言：SZLab 形状的 ``scale=(0,0,0)`` 不阻止首次投影，
    且 SQLite 中仍满足 Backend 形状 ``scale_* > 0`` 约束并保存三轴单位缩放；
    负缩放与非有限值不在本兼容规则内。
    """

    store = InventoryStore(":memory:")
    try:
        receipt = _bootstrap(store, _ResourceTree(mount_scale=0))
        position = store.query_one(
            "SELECT scale_x,scale_y,scale_z FROM relative_position "
            "WHERE material_uuid=?",
            (MOUNT_MATERIAL_UUID,),
        )
    finally:
        store.close()

    assert receipt["status"] == "imported"
    assert position == {"scale_x": 1.0, "scale_y": 1.0, "scale_z": 1.0}


def test_restart_with_same_source_and_fingerprint_is_idempotent(tmp_path: Path) -> None:
    """同一资源图跨进程重启只返回幂等回执。

    参数：``tmp_path`` 隔离 SQLite 文件。返回：无。断言：正式身份和行数不重复。
    """

    database_path = tmp_path / "inventory.db"
    first = InventoryStore(str(database_path))
    try:
        assert _bootstrap(first, _ResourceTree())["status"] == "imported"
    finally:
        first.close()
    reopened = InventoryStore(str(database_path))
    try:
        receipt = _bootstrap(reopened, _ResourceTree())
        material_count = reopened.query_one("SELECT COUNT(*) AS count FROM material")
        site_count = reopened.query_one("SELECT COUNT(*) AS count FROM site")
    finally:
        reopened.close()

    assert receipt["status"] == "unchanged"
    assert material_count == {"count": 1}
    assert site_count == {"count": 2}


def test_registry_action_change_does_not_invalidate_identical_inventory_graph(
    tmp_path: Path,
) -> None:
    """设备动作合同变化不应伪装成库存资源图变化。

    参数：``tmp_path`` 隔离 SQLite 文件。返回无；断言实际物料、
    库位（Site）和位置投影相同时，新注册表代际仍为幂等启动。
    """

    database_path = tmp_path / "inventory.db"
    first = InventoryStore(str(database_path))
    try:
        assert _bootstrap(first, _ResourceTree())[
            "status"
        ] == "imported"
    finally:
        first.close()
    reopened = InventoryStore(str(database_path))
    try:
        receipt = _bootstrap(
            reopened,
            _ResourceTree(),
            registry=_RegistryWithChangedAction(),
        )
    finally:
        reopened.close()

    assert receipt["status"] == "unchanged"


def test_changed_fingerprint_fails_closed_without_overwriting_public_graph() -> None:
    """既有权威遇到同来源不同指纹时必须关闭式失败且保持原图。

    参数：无。返回：无。断言：冲突不会改名、追加物料或改变库位（Site）。
    """

    store = InventoryStore(":memory:")
    try:
        _bootstrap(store, _ResourceTree())
        before = BackendResourceService(store).material_graph()
        with pytest.raises(ResourceGraphBootstrapError, match="指纹|fingerprint"):
            _bootstrap(store, _ResourceTree(mount_name="Changed"))
        after = BackendResourceService(store).material_graph()
    finally:
        store.close()

    assert after == before


def test_dangling_site_parent_rolls_back_all_projection_rows() -> None:
    """悬空库位父引用必须在任何投影行提交前失败。

    参数：无。返回：无。断言：物料、库位和启动指纹全部保持空集合。
    """

    store = InventoryStore(":memory:")
    try:
        with pytest.raises(ResourceGraphBootstrapError, match="父|owner|parent"):
            _bootstrap(store, _ResourceTree(site_parent_uuid="missing-runtime-parent"))
        material_count = store.query_one("SELECT COUNT(*) AS count FROM material")
        site_count = store.query_one("SELECT COUNT(*) AS count FROM site")
        bootstrap_meta = store.query_all(
            "SELECT * FROM lab_meta WHERE meta_key LIKE 'resource_graph_bootstrap_%'"
        )
    finally:
        store.close()

    assert material_count == {"count": 0}
    assert site_count == {"count": 0}
    assert bootstrap_meta == []


def test_bootstrap_gate_follows_os_host_authority() -> None:
    """启动投影只由 OS 主机的本地后端权威决定。

    参数：无。返回：无；断言主机固定建立本地库存（Inventory）与资源图，从节点
    不建立第二份权威。异常：权威边界退化为可选远端数据源时测试失败。
    """

    assert should_bootstrap_local_resource_graph(is_host_mode=True)
    assert not should_bootstrap_local_resource_graph(is_host_mode=False)


def test_inventory_composition_bootstraps_before_legacy_cloud_upload(
    tmp_path: Path,
) -> None:
    """库存组合根必须先建立公共物料图且不依赖旧云端上传成功。

    参数：``tmp_path`` 提供隔离的库存 SQLite。返回：无。断言：正式
    ``setup_edge_inventory`` 接线公开稳定设备物料；随后模拟旧云端资源树上传失败，
    本地公共物料图（MaterialGraph）仍保持可查询。异常：上传失败只属于旧桥接路径，
    不得回滚已经提交的本地库存权威（Inventory Authority）事实。
    """

    class _FailingLegacyCloudBridge:
        """模拟拒绝旧资源树上传的云端桥接器。

        参数：无。返回：无。异常：``resource_tree_add`` 固定抛出网络错误。
        """

        def resource_tree_add(self, _resource_tree: object) -> None:
            """拒绝一次旧资源树上传。

            参数：``_resource_tree`` 是不参与断言的旧上传负载。返回：无。
            异常：固定抛出 ``RuntimeError``，模拟云端不可达。
            """

            raise RuntimeError("legacy cloud unavailable")

    integration.reset_for_test()
    try:
        # ``inventory_service`` 是主机组合根创建的唯一嵌入式库存服务。
        inventory_service = integration.setup_edge_inventory(
            str(tmp_path / "inventory.db"),
            resource_tree_set=_ResourceTree(),
            registry_snapshot=RegistryTemplateSnapshot.from_registry(_Registry()),
            resource_graph_source_id="/workspace/m2b-native-workspace/graph.json",
        )
        before_upload = BackendResourceService(inventory_service.store).material_graph()
        with pytest.raises(RuntimeError, match="legacy cloud unavailable"):
            _FailingLegacyCloudBridge().resource_tree_add(_ResourceTree())
        after_upload = BackendResourceService(inventory_service.store).material_graph()
    finally:
        integration.reset_for_test()

    assert [node["material"]["uuid"] for node in before_upload["nodes"]] == [
        MOUNT_MATERIAL_UUID
    ]
    assert after_upload == before_upload
