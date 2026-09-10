"""边缘端（Edge）设备注册表（Registry）到后端（Backend）模板协议的契约测试。"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import pytest

from unilabos.app.main import parse_args
from unilabos.app.register import register_devices_and_resources
from unilabos.app.template_sync import (
    DEVELOPER_TOKEN_ENV,
    TemplateSyncError,
    TemplateSynchronizer,
    run_template_sync_command,
)
from unilabos.registry.template_projection import RegistryTemplateProjection
from unilabos.registry.template_snapshot import RegistryTemplateSnapshot
from unilabos.workflow.store import WorkflowStore

RESOURCE_TEMPLATE_UUID = "10000000-0000-4000-8000-000000000001"
TUBE_RESOURCE_TEMPLATE_UUID = "10000000-0000-4000-8000-000000000002"


def _local_resource_template_identity(resource_name: str) -> str:
    """按设备注册表（Registry）业务唯一名称返回本地资源模板 UUID。

    参数说明：``resource_name`` 是设备或器材的业务唯一索引。返回：测试本地
    模板数据库中既有的稳定 UUID；未知名称返回空串供投影关闭式失败。
    """

    # ``identities`` 同时覆盖动作所有者和物料资源模板，模拟完整本地身份索引。
    identities = {
        "pump": RESOURCE_TEMPLATE_UUID,
        "tube_15ml": TUBE_RESOURCE_TEMPLATE_UUID,
    }
    return identities.get(resource_name, "")


class FakeRegistry:
    """提供模板同步测试使用的冻结设备注册表（Registry）定义。"""

    def obtain_registry_device_info(self) -> list[dict[str, Any]]:
        """返回一个带第 2 版动作合同（Action Contract）的设备定义。

        参数：无。返回：包含泵、动作 Schema 和连接点（Handle）的完整设备定义集；
        测试字段均为分离的新容器，调用方可以安全规范化。
        """

        return [
            {
                "id": "pump",
                "displayname": "注射泵",
                "registry_type": "device",
                "file_path": "/private/pump.py",
                "class": {
                    "module": "drivers.pump:Pump",
                    "type": "python",
                    "status_types": {"status": "String"},
                    "action_value_mappings": {
                        "transfer": {
                            "contract_kind": "typed",
                            "displayname": "输送",
                            "description": "把物料输送到目标库位",
                            "type": "UniLabJsonCommand",
                            "goal": {
                                "unilabos_device_id": "unilabos_device_id",
                                "volume": "volume",
                            },
                            "goal_default": {
                                "unilabos_device_id": "",
                                "volume": 1.0,
                            },
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "goal": {
                                        "type": "object",
                                        "properties": {
                                            "unilabos_device_id": {
                                                "type": "string",
                                                "default": "",
                                            },
                                            "volume": {"type": "number"},
                                        },
                                        "required": ["unilabos_device_id", "volume"],
                                    }
                                },
                                "x-unilabos-action-contract": {
                                    "version": 2,
                                    "input_order": ["volume"],
                                    "output_order": [],
                                    "resource_template_symbols": {
                                        "goal": {},
                                        "result": {},
                                    },
                                },
                            },
                            "handles": {
                                "input": [
                                    {
                                        "handler_key": "volume",
                                        "label": "体积",
                                        "data_type": "number",
                                        "data_source": "param",
                                        "data_key": "volume",
                                        "io_type": "target",
                                    }
                                ],
                                "output": [],
                            },
                        }
                    },
                },
                "handles": [],
                "category": ["pump"],
                "init_param_schema": {
                    "config": {
                        "type": "object",
                        "properties": {"port": {"type": "string"}},
                    }
                },
            }
        ]

    def obtain_registry_resource_info(self) -> list[dict[str, Any]]:
        """返回带源码全限定身份的器材模板。

        参数：无。返回：后端（Backend）同批身份映射所需的资源模板
        （ResourceTemplate）定义集；``source_fqid`` 不会在设备注册表（Registry）
        快照规范化时丢失。
        """

        return [
            {
                "id": "tube_15ml",
                "source_fqid": "resources.tube:Tube15mL",
                "displayname": "15 mL 离心管",
                "registry_type": "resource",
                "class": {
                    "module": "resources.tube:Tube15mL",
                    "type": "pylabrobot",
                },
                "handles": [],
                "category": ["container"],
            }
        ]


class DuplicateDeviceRegistry(FakeRegistry):
    """返回两个相同设备业务名，用于验证完整快照唯一性错误。"""

    def obtain_registry_device_info(self) -> list[dict[str, Any]]:
        """复制同一设备定义，制造重复活动业务名。

        参数：无。返回：含两个相同业务身份的设备定义集，供快照关闭式失败测试。
        """

        devices = super().obtain_registry_device_info()
        return [devices[0], dict(devices[0])]


class FakeResponse:
    """记录后端（Backend）模板同步响应的最小替身。"""

    def __init__(
        self,
        status_code: int = 200,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """保存 HTTP 状态、响应对象和对应文本。

        参数说明：``status_code`` 是 HTTP 状态码，``payload`` 是可选后端
        （Backend）响应对象。返回：无；缺省响应包含两项稳定模板身份回执。
        """

        self.status_code = status_code
        self._payload = payload or {
            "code": 0,
            "data": {
                "templates": [
                    {"uuid": "device-template-uuid", "name": "pump"},
                    {"uuid": "resource-template-uuid", "name": "tube_15ml"},
                ]
            },
        }
        self.text = json.dumps(self._payload, ensure_ascii=False)

    def json(self) -> dict[str, Any]:
        """返回分离边界内保存的响应对象。

        参数：无。返回：构造时保存的后端（Backend）JSON 响应对象。
        """

        return self._payload


class FakeSession:
    """记录模板同步 HTTP 调用而不访问网络的会话替身。"""

    def __init__(self, response: FakeResponse | None = None) -> None:
        """保存固定响应并初始化请求记录。

        参数说明：``response`` 是每次 POST 返回的固定响应。返回：无；
        ``calls`` 保存 URL 与关键字参数，供测试解释完整上传事务。
        """

        self.response = response or FakeResponse()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        """记录一次 POST 并返回固定响应。

        参数说明：``url`` 是后端（Backend）模板接口地址，``kwargs`` 是压缩载荷、
        请求头等调用参数。返回：构造时保存的 ``FakeResponse``。
        """

        self.calls.append((url, kwargs))
        return self.response


def test_sync_merges_device_and_resource_templates_into_one_transaction() -> None:
    """同一设备注册表快照必须一次上传设备、器材和派生连接点。

    参数：无。返回：无；关键变量分别代表 HTTP 会话、同步器、同步回执和解压后
    的后端（Backend）请求载荷，断言第 2 版动作合同（Action Contract）只上传
    一次完整定义。
    """

    # ``session`` 记录唯一 HTTP 请求；``synchronizer`` 执行不可变快照同步。
    session = FakeSession()
    synchronizer = TemplateSynchronizer(
        "http://backend:8080",
        "developer-secret",
        session=session,
    )

    # ``report`` 是后端（Backend）返回的稳定资源模板身份回执。
    report = synchronizer.sync(FakeRegistry())

    assert report.device_count == 1
    assert report.resource_count == 1
    assert report.template_uuids == {
        "pump": "device-template-uuid",
        "tube_15ml": "resource-template-uuid",
    }
    assert len(session.calls) == 1
    url, request = session.calls[0]
    assert url == "http://backend:8080/api/v1/resource-templates"
    assert request["headers"]["Authorization"] == "Bearer developer-secret"
    assert request["headers"]["Content-Encoding"] == "gzip"
    # ``payload`` 是后端（Backend）实际收到的完整模板定义事务。
    payload = json.loads(gzip.decompress(request["data"]))
    assert [resource["id"] for resource in payload["resources"]] == [
        "pump",
        "tube_15ml",
    ]
    device, resource = payload["resources"]
    assert device["display_name"] == "注射泵"
    assert (
        device["class"]["action_value_mappings"]["transfer"]["display_name"] == "输送"
    )
    action = device["class"]["action_value_mappings"]["transfer"]
    assert "unilabos_device_id" not in action["goal"]
    assert "unilabos_device_id" not in action["goal_default"]
    assert (
        "unilabos_device_id" not in action["schema"]["properties"]["goal"]["properties"]
    )
    assert action["schema"]["properties"]["goal"]["required"] == ["volume"]
    assert action["handles"] == {
        "input": [
            {
                "label": "volume",
                "data_key": "volume",
                "data_type": "number",
                "data_source": "goal",
                "handler_key": "volume",
            }
        ],
        "output": [],
    }
    assert device["init_param_schema"] == {
        "config": {"properties": {"port": {"type": "string"}}}
    }
    assert "file_path" not in device
    assert "status_types" not in device["class"]
    assert resource["display_name"] == "15 mL 离心管"
    assert resource["registry_type"] == "resource"
    assert resource["source_fqid"] == "resources.tube:Tube15mL"


def test_sync_rejects_backend_business_error() -> None:
    """后端（Backend）业务错误必须映射为模板同步领域错误。

    参数：无。返回：无；``session`` 固定返回业务错误，``synchronizer`` 是被测
    同步器，断言异常保留稳定错误码。
    """

    session = FakeSession(
        FakeResponse(
            payload={
                "code": 5003,
                "error": {"msg": "template definition invalid"},
            }
        )
    )
    synchronizer = TemplateSynchronizer(
        "http://backend:8080/api/v1",
        "developer-secret",
        session=session,
    )

    with pytest.raises(TemplateSyncError, match="5003"):
        synchronizer.sync(FakeRegistry())


def test_template_sync_command_builds_complete_registry_without_starting_edge() -> None:
    """同步命令应构建设备注册表（Registry）而不启动边缘端（Edge）。

    参数：无。返回：无；``parsed`` 是命令参数，``builder_calls`` 记录构建参数，
    ``report`` 是同步回执，断言命令只执行模板同步职责。
    """

    parsed = vars(
        parse_args().parse_args(
            [
                "--addr",
                "http://backend:8080/api/v1",
                "--registry_path",
                "/registry-a",
                "--devices",
                "/drivers-a",
                "template-sync",
            ]
        )
    )
    builder_calls = []

    def registry_builder(**kwargs: Any) -> FakeRegistry:
        """记录设备注册表（Registry）构建参数并返回固定定义。

        参数说明：``kwargs`` 是同步命令传入的目录与构建开关。返回：新的
        ``FakeRegistry``；不启动边缘端（Edge）或访问设备。
        """

        builder_calls.append(kwargs)
        return FakeRegistry()

    session = FakeSession()
    report = run_template_sync_command(
        parsed,
        backend_address=parsed["addr"],
        environment={DEVELOPER_TOKEN_ENV: "developer-secret"},
        registry_builder=registry_builder,
        session=session,
    )

    assert report.device_count == 1
    assert builder_calls == [
        {
            "registry_paths": ["/registry-a"],
            "devices_dirs": ["/drivers-a"],
            "upload_registry": False,
            "complete_registry": False,
            "external_only": False,
        }
    ]


def test_legacy_startup_registration_is_read_only() -> None:
    """旧启动注册入口必须拒绝隐式写入后端（Backend）。

    参数：无。返回：无；断言调用方被引导到显式模板同步命令。
    """

    with pytest.raises(RuntimeError, match="template-sync"):
        register_devices_and_resources(FakeRegistry())


def test_local_projection_and_template_sync_share_one_registry_snapshot(
    tmp_path: Path,
) -> None:
    """本地模板投影和后端同步必须消费同一不可变设备注册表快照。

    参数说明：``tmp_path`` 隔离本地工作流数据库；测试比较两条消费路径中的动作
    业务名和后端（Backend）规范的 ``goal`` 参数模式，禁止二次设备注册表
    （Registry）遍历产生漂移；返回：无；后端上传仍保留完整第 2 版动作合同
    （Action Contract）作为唯一编译输入。
    """

    registry_snapshot = RegistryTemplateSnapshot.from_registry(FakeRegistry())
    projection = RegistryTemplateProjection(
        WorkflowStore(tmp_path / "workflow_history.db"),
        authority_id="local",
        resource_template_identity_resolver=_local_resource_template_identity,
    )
    local_action = projection.refresh(registry_snapshot).require_action(
        "drivers.pump:Pump",
        "transfer",
    )

    session = FakeSession()
    synchronizer = TemplateSynchronizer(
        "http://backend:8080",
        "developer-secret",
        session=session,
    )
    synchronizer.sync(registry_snapshot)
    payload = json.loads(gzip.decompress(session.calls[0][1]["data"]))
    synchronized_action = payload["resources"][0]["class"]["action_value_mappings"][
        "transfer"
    ]

    # ``synchronized_goal_schema`` 是完整上传合同中供节点参数校验使用的 goal 子模式。
    synchronized_goal_schema = synchronized_action["schema"]["properties"]["goal"]
    # ``local_goal_schema`` 从后端（Backend）文本字段解码，仅在测试中比较两条
    # 投影路径共享的 JSON Schema 语义；公共工作流节点模板仍保持字符串线合同。
    local_goal_schema = json.loads(local_action.detached_template()["schema"])
    assert synchronized_goal_schema == local_goal_schema
    assert synchronized_action["schema"]["x-unilabos-action-contract"]["version"] == 2
    assert synchronized_action["display_name"] == local_action.template["display_name"]
    projection.close()


def test_template_sync_maps_registry_snapshot_error_to_sync_domain_error() -> None:
    """设备注册表（Registry）快照错误不得泄露模块外异常类型。

    参数：无。返回：无；``synchronizer`` 是被测同步器，重复设备业务身份必须
    映射为 ``TemplateSyncError``。
    """

    synchronizer = TemplateSynchronizer(
        "http://backend:8080",
        "developer-secret",
        session=FakeSession(),
    )

    with pytest.raises(TemplateSyncError, match="重复"):
        synchronizer.sync(DuplicateDeviceRegistry())
