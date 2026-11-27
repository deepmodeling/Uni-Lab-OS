"""F05.4-C0b2 活动资源模板（ResourceTemplate）身份复用合同。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
from unilabos.app.scheduler.inventory.store import InventoryStore


def _resource_definition() -> dict[str, Any]:
    """返回可重复同步的最小设备资源模板定义。

    参数：无。返回：业务唯一名为 ``pump``、源码身份可解析的资源模板
    （ResourceTemplate）定义；每次调用返回独立对象，避免测试共享可变状态。
    """

    return {
        "id": "pump",
        "display_name": "测试泵",
        "registry_type": "device",
        "class": {"module": "lab.devices:Pump", "type": "python"},
    }


def _synchronize_pump(service: BackendResourceService) -> str:
    """同步测试泵并返回本次活动资源模板 UUID。

    参数说明：``service`` 是本地后端形态资源服务。返回：同步回执中 ``pump`` 的
    稳定身份；回执缺失会让测试直接失败，不自行查询或猜测 UUID。
    """

    # ``synchronization`` 是一次完整模板同步事务的身份回执。
    synchronization = service.sync_resource_templates([_resource_definition()])
    return str(synchronization["templates"][0]["uuid"])


def test_soft_deleted_template_is_reintroduced_with_new_uuid(tmp_path: Path) -> None:
    """只有软删除历史时重新引入业务名必须创建新 UUID。

    参数说明：``tmp_path`` 隔离真实库存数据库。返回：无；断言软删除历史身份
    保持历史状态，新活动资源模板（ResourceTemplate）获得不同 UUID，绝不复活
    旧身份。
    """

    # ``inventory_store`` 持有软删除历史与重新引入身份的真实库存权威。
    inventory_store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        # ``service`` 是所有模板生命周期写入共用的后端（Backend）形态资源接口。
        service = BackendResourceService(inventory_store)
        # 两个 UUID 分别表示已软删除历史和重新引入后的活动模板身份。
        deleted_template_uuid = _synchronize_pump(service)
        service.delete_resource_template(deleted_template_uuid)
        active_template_uuid = _synchronize_pump(service)
        # ``template_rows`` 同时保留软删除历史与唯一活动模板的持久事实。
        template_rows = inventory_store.query_all(
            "SELECT uuid, deleted_at FROM resource_template WHERE name=? ORDER BY uuid",
            ("pump",),
        )

        assert active_template_uuid != deleted_template_uuid
        assert len(template_rows) == 2
        assert {
            str(row["uuid"]): row["deleted_at"] is None for row in template_rows
        } == {
            deleted_template_uuid: False,
            active_template_uuid: True,
        }
    finally:
        inventory_store.close()


def test_active_template_wins_over_same_name_soft_deleted_history(
    tmp_path: Path,
) -> None:
    """活动与软删除历史同名时后续同步必须确定复用活动 UUID。

    参数说明：``tmp_path`` 隔离真实库存数据库。返回：无；断言第三次同步只更新
    当前活动资源模板（ResourceTemplate），不按历史行顺序选择或复活旧 UUID。
    """

    # ``inventory_store`` 持有同名历史与活动模板并存的真实库存权威。
    inventory_store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        # ``service`` 负责三个连续同步周期中的身份选择与回执（Receipt）。
        service = BackendResourceService(inventory_store)
        # ``historical_template_uuid`` 是软删除历史；``active_template_uuid`` 是唯一活动身份。
        historical_template_uuid = _synchronize_pump(service)
        service.delete_resource_template(historical_template_uuid)
        active_template_uuid = _synchronize_pump(service)

        # ``reused_template_uuid`` 是第三次同步必须返回的唯一活动模板 UUID。
        reused_template_uuid = _synchronize_pump(service)
        # ``historical_row`` 证明旧身份仍保持软删除，未被同步过程复活。
        historical_row = inventory_store.query_one(
            "SELECT deleted_at FROM resource_template WHERE uuid=?",
            (historical_template_uuid,),
        )

        assert reused_template_uuid == active_template_uuid
        assert historical_row is not None
        assert historical_row["deleted_at"] is not None
    finally:
        inventory_store.close()


def test_resource_template_detail_preserves_package_source_identity(
    tmp_path: Path,
) -> None:
    """Template detail keeps source identity outside the Backend list summary."""

    inventory_store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        service = BackendResourceService(inventory_store)
        definition = _resource_definition()
        definition["source_uri"] = "package://catalog_lab/definitions.py"
        template_uuid = str(
            service.sync_resource_templates([definition])["templates"][0]["uuid"]
        )

        summary = service.list_resource_templates(
            page=1,
            page_size=20,
            keyword="",
            resource_type="",
        )["items"][0]
        detail = service.get_resource_template(template_uuid)
        assert "source_uri" not in summary
        assert detail["meta_data"]["unilab"]["source_uri"] == (
            "package://catalog_lab/definitions.py"
        )
    finally:
        inventory_store.close()
