"""动态设备类型选择器的注册表与库存双权威行为合同。"""

from __future__ import annotations

from pathlib import Path

import pytest

from unilabos.app.scheduler.device_target import (
    DeviceTargetUnavailable,
    ResolvedDeviceTarget,
    make_registered_device_target_resolver,
    resolve_registered_device_target,
    resolve_registered_fixed_device_target,
)
from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.app.scheduler.service import EdgeScheduler


def test_preflight_uses_frozen_material_and_live_runtime_busy_keys() -> None:
    """固定设备预检必须读取计划 material_uuid 并合并 Runtime 当前忙碌事实。"""

    observed: list[tuple[dict[str, object], str, set[str]]] = []

    def resolve(
        selector: dict[str, object],
        action_name: str,
        busy_keys: set[str],
    ) -> ResolvedDeviceTarget:
        observed.append((dict(selector), action_name, set(busy_keys)))
        return ResolvedDeviceTarget("reactor-a", "device-material-a")

    scheduler = EdgeScheduler(
        device_target_resolver=resolve,
        external_busy_keys={"/devices/runtime-busy"},
    )
    result = scheduler.preflight_device_target(
        {
            "device_id": "reactor-a",
            "material_uuid": "device-material-a",
            "action_name": "heat",
        }
    )

    assert result == {
        "local_device_id": "reactor-a",
        "material_uuid": "device-material-a",
    }
    assert observed == [
        (
            {
                "mode": "fixed",
                "local_device_id": "reactor-a",
                "material_uuid": "device-material-a",
            },
            "heat",
            {"/devices/runtime-busy"},
        )
    ]


def test_registered_device_target_selects_first_available_matching_instance(
    tmp_path: Path,
) -> None:
    """运行时应在同类在线实例中跳过忙设备并选择稳定排序首个可用项。

    参数：``tmp_path`` 隔离库存数据库。返回：无；断言注册动作能力和库存模板必须
    同时匹配，且设备物料 UUID 的持久占用能让调度器选择第二实例。异常：数据库与
    注册表错误原样传播。
    """

    store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        backend = BackendResourceService(store)
        template = backend.sync_resource_templates(
            [
                {
                    "id": "test.reactor",
                    "display_name": "反应器",
                    "registry_type": "device",
                    "class": {},
                }
            ]
        )["templates"][0]
        first = backend.create_material(
            {
                "resource_template_uuid": template["uuid"],
                "barcode": "REACTOR-A",
                "name": "反应器 A",
            }
        )
        second = backend.create_material(
            {
                "resource_template_uuid": template["uuid"],
                "barcode": "REACTOR-B",
                "name": "反应器 B",
            }
        )
        with store.transaction() as connection:
            connection.execute(
                "UPDATE material SET type='device' WHERE uuid IN (?,?)",
                (first["uuid"], second["uuid"]),
            )
        registration = {
            "connected": True,
            "devices": [
                {
                    "local_id": "reactor-b",
                    "material_uuid": second["uuid"],
                    "actions": [{"name": "heat"}],
                },
                {
                    "local_id": "reactor-a",
                    "material_uuid": first["uuid"],
                    "actions": [{"name": "heat"}],
                },
            ],
        }

        selected = resolve_registered_device_target(
            InventoryService(store).station_resources,
            registration,
            resource_template_uuid=template["uuid"],
            action_name="heat",
            busy_keys={f"/devices/{first['uuid']}"},
        )

        assert selected.local_device_id == "reactor-b"
        assert selected.material_uuid == second["uuid"]
    finally:
        store.close()


def test_registered_device_target_fails_closed_when_edge_is_offline(
    tmp_path: Path,
) -> None:
    """设备执行进程离线时不得从陈旧注册快照选择设备。

    参数：``tmp_path`` 隔离空库存数据库。返回：无；断言稳定 ``edge_offline``
    等待码。异常：该 ``DeviceTargetUnavailable`` 是预期结果。
    """

    store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        with pytest.raises(DeviceTargetUnavailable) as caught:
            resolve_registered_device_target(
                InventoryService(store).station_resources,
                {"connected": False, "devices": []},
                resource_template_uuid=("90000000-0000-4000-8000-000000000001"),
                action_name="heat",
                busy_keys=set(),
            )
        assert caught.value.code == "edge_offline"
    finally:
        store.close()


def test_registered_device_target_reports_every_busy_candidate(
    tmp_path: Path,
) -> None:
    """同类设备全部忙时，等待事实必须列出可向用户解释的具体设备身份。"""

    store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        backend = BackendResourceService(store)
        template = backend.sync_resource_templates(
            [
                {
                    "id": "test.robot",
                    "display_name": "机械臂",
                    "registry_type": "device",
                    "class": {},
                }
            ]
        )["templates"][0]
        robot = backend.create_material(
            {
                "resource_template_uuid": template["uuid"],
                "barcode": "ROBOT-1",
                "name": "S09 机械臂",
            }
        )
        with store.transaction() as connection:
            connection.execute(
                "UPDATE material SET type='device' WHERE uuid=?",
                (robot["uuid"],),
            )

        with pytest.raises(DeviceTargetUnavailable) as caught:
            resolve_registered_device_target(
                InventoryService(store).station_resources,
                {
                    "connected": True,
                    "devices": [
                        {
                            "local_id": "robot-1",
                            "material_uuid": robot["uuid"],
                            "actions": [{"name": "transfer"}],
                        }
                    ],
                },
                resource_template_uuid=template["uuid"],
                action_name="transfer",
                busy_keys={f"/devices/{robot['uuid']}"},
            )

        assert caught.value.code == "device_busy"
        assert caught.value.resources == (
            {
                "scope": "device",
                "device_id": robot["uuid"],
                "local_device_id": "robot-1",
                "device_name": "robot-1",
            },
        )
    finally:
        store.close()


def test_fixed_device_target_uses_same_online_health_and_capability_gate(
    tmp_path: Path,
) -> None:
    """固定设备不能绕过动态设备使用的注册、健康和动作能力门禁。"""

    store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        backend = BackendResourceService(store)
        template = backend.sync_resource_templates(
            [{"id": "test.fixed", "display_name": "固定设备", "registry_type": "device", "class": {}}]
        )["templates"][0]
        device = backend.create_material(
            {
                "resource_template_uuid": template["uuid"],
                "barcode": "FIXED-1",
                "name": "固定设备 1",
            }
        )
        with store.transaction() as connection:
            connection.execute(
                "UPDATE material SET type='device' WHERE uuid=?", (device["uuid"],)
            )
        registration = {
            "connected": True,
            "devices": [
                {
                    "local_id": "fixed-1",
                    "material_uuid": device["uuid"],
                    "actions": [{"name": "heat"}],
                    "online": False,
                }
            ],
        }
        resolver = make_registered_device_target_resolver(
            InventoryService(store).station_resources,
            lambda: registration,
        )

        with pytest.raises(DeviceTargetUnavailable) as caught:
            resolver(
                {
                    "mode": "fixed",
                    "local_device_id": "fixed-1",
                    "material_uuid": device["uuid"],
                },
                "heat",
                set(),
            )
        assert caught.value.code == "device_offline"

        registration["devices"][0]["online"] = True
        registration["devices"][0]["unknown_command_ids"] = ["command-1"]
        with pytest.raises(DeviceTargetUnavailable) as caught:
            resolver(
                {
                    "mode": "fixed",
                    "local_device_id": "fixed-1",
                    "material_uuid": device["uuid"],
                },
                "heat",
                set(),
            )
        assert caught.value.code == "device_requires_reconciliation"
        assert caught.value.resources[0]["device_name"] == "fixed-1"
        assert caught.value.resources[0]["wait_code"] == (
            "device_requires_reconciliation"
        )
    finally:
        store.close()


def test_fixed_device_target_accepts_execution_plan_material_uuid_alias(
    tmp_path: Path,
) -> None:
    """执行计划把物料 UUID 作为 device_id 时，仍解析为注册中的本地设备 ID。"""

    store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        backend = BackendResourceService(store)
        template = backend.sync_resource_templates(
            [{"id": "test.plan-device", "display_name": "计划设备", "registry_type": "device", "class": {}}]
        )["templates"][0]
        device = backend.create_material(
            {
                "resource_template_uuid": template["uuid"],
                "barcode": "PLAN-DEVICE-1",
                "name": "计划设备 1",
            }
        )
        with store.transaction() as connection:
            connection.execute(
                "UPDATE material SET type='device' WHERE uuid=?", (device["uuid"],)
            )

        selected = resolve_registered_fixed_device_target(
            {
                "connected": True,
                "devices": [
                    {
                        "local_id": "plan-device-1",
                        "material_uuid": device["uuid"],
                        "actions": [{"name": "heat"}],
                    }
                ],
            },
            local_device_id=device["uuid"],
            material_uuid=device["uuid"],
            action_name="heat",
            busy_keys=set(),
        )

        assert selected == ResolvedDeviceTarget("plan-device-1", device["uuid"])
    finally:
        store.close()


def test_dynamic_device_wait_reports_named_unavailable_candidates(
    tmp_path: Path,
) -> None:
    """动态选择全部不可用时必须返回每台匹配设备的名称和具体原因。"""

    store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        backend = BackendResourceService(store)
        template = backend.sync_resource_templates(
            [
                {
                    "id": "test.named-device",
                    "display_name": "命名设备",
                    "registry_type": "device",
                    "class": {},
                }
            ]
        )["templates"][0]
        device = backend.create_material(
            {
                "resource_template_uuid": template["uuid"],
                "barcode": "NAMED-1",
                "name": "S08 样品瓶开关盖",
            }
        )
        with store.transaction() as connection:
            connection.execute(
                "UPDATE material SET type='device' WHERE uuid=?",
                (device["uuid"],),
            )

        with pytest.raises(DeviceTargetUnavailable) as caught:
            resolve_registered_device_target(
                InventoryService(store).station_resources,
                {
                    "connected": True,
                    "devices": [
                        {
                            "local_id": "capper-1",
                            "name": "S08 样品瓶开关盖",
                            "material_uuid": device["uuid"],
                            "actions": [{"name": "open"}],
                            "online": False,
                        }
                    ],
                },
                resource_template_uuid=template["uuid"],
                action_name="open",
                busy_keys=set(),
            )

        assert caught.value.code == "device_offline"
        assert caught.value.resources == (
            {
                "scope": "device",
                "device_id": device["uuid"],
                "local_device_id": "capper-1",
                "device_name": "S08 样品瓶开关盖",
                "wait_code": "device_offline",
                "wait_message": "目标设备当前离线",
            },
        )
    finally:
        store.close()
