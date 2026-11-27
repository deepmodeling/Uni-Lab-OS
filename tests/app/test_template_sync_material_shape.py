"""模板同步发布 2.5D 资源模板（ResourceTemplate）外形的公共合同测试。"""

from __future__ import annotations

import gzip
import json
from typing import Any

import pytest

from unilabos.app.template_sync import (
    DEVELOPER_TOKEN_ENV,
    TemplateSyncError,
    TemplateSynchronizer,
    run_template_sync_command,
)


class _ShapeRegistry:
    """提供一个拥有 2.5D 外形的器材模板和一个无外形设备模板。"""

    def obtain_registry_device_info(self) -> list[dict[str, Any]]:
        """返回不拥有外形的设备模板。

        参数：无。返回：单个泵模板定义。异常：无。
        """

        return [
            {
                "id": "pump",
                "displayname": "注射泵",
                "registry_type": "device",
                "class": {"action_value_mappings": {}},
            }
        ]

    def obtain_registry_resource_info(self) -> list[dict[str, Any]]:
        """返回仍携带本地外形入口绑定的器材模板。

        参数：无。返回：单个离心管资源模板定义。异常：无。
        """

        return [
            {
                "id": "tube_15ml",
                "displayname": "15 mL 离心管",
                "registry_type": "resource",
                "model": {
                    "shape": {
                        "format": "unilab.shape/v1",
                        "entry": "models/shape.yml",
                    }
                },
                "class": {"action_value_mappings": {}},
            }
        ]


class _BackendResponse:
    """返回本轮两个资源模板稳定身份的 Backend 响应替身。"""

    status_code = 200

    def json(self) -> dict[str, Any]:
        """返回成功模板身份回执。

        参数：无。返回：包含设备与器材模板身份的响应对象。异常：无。
        """

        return {
            "code": 0,
            "data": {
                "templates": [
                    {"uuid": "device-template-uuid", "name": "pump"},
                    {"uuid": "resource-template-uuid", "name": "tube_15ml"},
                ]
            },
        }


class _RecordingSession:
    """记录发送给 Backend 的唯一模板同步请求。"""

    def __init__(self) -> None:
        """初始化空请求记录。

        参数：无。返回：无。异常：无。
        """

        self.calls: list[tuple[str, dict[str, Any]]] = []

    def post(self, url: str, **kwargs: Any) -> _BackendResponse:
        """记录模板同步 POST 并返回成功回执。

        参数：``url`` 是 Backend 模板接口；``kwargs`` 携带 gzip 请求体和请求头。
        返回：固定成功响应。异常：无。
        """

        self.calls.append((url, kwargs))
        return _BackendResponse()


class _WorkspaceRuntime:
    """模拟已经编译好模板外形绑定的工作区运行代。"""

    def __init__(self, shape: dict[str, Any]) -> None:
        """保存资源模板对应的完整 2.5D 外形。

        参数：``shape`` 是 PackageCatalog 同代编译结果。返回：无。异常：无。
        """

        self.material_shapes_by_template = {"tube_15ml": shape}
        self.published_registry: Any = None

    def publish(self, registry: Any) -> None:
        """记录模板同步使用了本工作区候选代。

        参数：``registry`` 是模板同步即将冻结的实时注册表（Registry）。
        返回：无。异常：无。
        """

        self.published_registry = registry


def test_sync_embeds_compiled_shape_in_owning_resource_template() -> None:
    """模板同步必须保留入口绑定并补齐拥有模板的完整 2.5D 外形。

    参数：无。
    返回：无；断言 Backend 收到的 gzip 模板事务只扩展目标模板的
    ``model.shape``，并保留既有 ``format``/``entry`` 兼容字段。
    异常：外形未按模板身份绑定、错误写入其他模板或旧绑定字段丢失时测试失败。
    """

    # ``compiled_shape`` 是包目录（PackageCatalog）同代校验后的完整公共外形，
    # 它属于资源模板 ``tube_15ml``，而不是设备模板 ``pump``。
    compiled_shape = {
        "schema_version": "unilab.shape/v1",
        "id": "tube",
        "bundle": "test-lab",
        "categories": ["container"],
        "categoryTokens": [],
        "priority": 0,
        "units": "mm",
        "shadow": "round",
        "sort": "center",
        "parts": [
            {
                "type": "cylinder",
                "style": "glass",
                "center": [7.5, 7.5],
                "d": 15,
                "z": [0, 120],
            }
        ],
    }
    # ``session`` 记录公共模板同步接口的唯一 gzip 请求。
    session = _RecordingSession()
    synchronizer = TemplateSynchronizer(
        "http://backend:8080",
        "developer-secret",
        session=session,
    )

    synchronizer.sync(
        _ShapeRegistry(),
        material_shapes_by_template={"tube_15ml": compiled_shape},
    )

    # ``payload`` 是 Backend 实际收到的完整模板事务，而不是内部中间对象。
    payload = json.loads(gzip.decompress(session.calls[0][1]["data"]))
    templates = {template["id"]: template for template in payload["resources"]}
    assert templates["tube_15ml"]["model"]["shape"] == {
        "format": "unilab.shape/v1",
        "entry": "models/shape.yml",
        **compiled_shape,
    }
    assert "shape" not in templates["pump"]["model"]


def test_template_sync_command_uses_workspace_shape_generation() -> None:
    """显式模板同步命令必须消费已经准备的工作区外形候选代。

    参数：无。返回：无；断言命令先发布工作区注册表代，再把同代完整 Shape
    与既有入口绑定一起发送到 Backend。异常：命令忽略工作区运行代或丢失任一
    合同部分时测试失败。
    """

    # ``compiled_shape`` 是工作区候选代保存的完整资源模板外形。
    compiled_shape = {
        "schema_version": "unilab.shape/v1",
        "id": "tube",
        "bundle": "test-lab",
        "categories": ["container"],
        "categoryTokens": [],
        "priority": 0,
        "units": "mm",
        "shadow": "round",
        "sort": "center",
        "parts": [{"type": "box", "style": "glass"}],
    }
    # ``runtime`` 与 ``registry`` 分别表示准备完成的工作区代和模板同步活动注册表。
    runtime = _WorkspaceRuntime(compiled_shape)
    registry = _ShapeRegistry()
    session = _RecordingSession()

    def build_registry(**_kwargs: Any) -> _ShapeRegistry:
        """返回本次同步唯一的测试注册表（Registry）。

        参数：接收模板同步构建器透传但本测试无需使用的关键字参数。
        返回：预先创建且可供工作区运行代记录发布行为的注册表。
        异常：无。
        """

        return registry

    run_template_sync_command(
        {},
        backend_address="http://backend:8080",
        environment={DEVELOPER_TOKEN_ENV: "developer-secret"},
        registry_builder=build_registry,
        session=session,
        workspace_runtime=runtime,
    )

    payload = json.loads(gzip.decompress(session.calls[0][1]["data"]))
    templates = {template["id"]: template for template in payload["resources"]}
    assert runtime.published_registry is registry
    assert templates["tube_15ml"]["model"]["shape"] == {
        "format": "unilab.shape/v1",
        "entry": "models/shape.yml",
        **compiled_shape,
    }


def test_sync_rejects_shape_without_owning_template_before_http() -> None:
    """找不到外形所属模板时整次同步必须在 HTTP 前关闭式失败。

    参数：无。返回：无；断言错误保留缺失模板身份且没有发送任何 Backend 请求。
    异常：实现静默丢弃外形或发送部分模板事务时测试失败。
    """

    # ``orphan_shape`` 模拟候选代与 Registry 模板全集不一致的损坏绑定。
    orphan_shape = {
        "schema_version": "unilab.shape/v1",
        "id": "orphan",
        "parts": [{"type": "box", "style": "body"}],
    }
    session = _RecordingSession()
    synchronizer = TemplateSynchronizer(
        "http://backend:8080",
        "developer-secret",
        session=session,
    )

    with pytest.raises(TemplateSyncError, match="missing-template"):
        synchronizer.sync(
            _ShapeRegistry(),
            material_shapes_by_template={"missing-template": orphan_shape},
        )

    assert session.calls == []
