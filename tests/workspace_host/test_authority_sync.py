"""Backend Authority bootstrap pagination contracts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from unilabos.workspace_host.authority_sync import BackendAuthorityBootstrapper


class _Response:
    status_code = 200

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def json(self) -> dict[str, Any]:
        return {"code": 0, "data": self._data}


class _Session:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = pages
        self.params: list[dict[str, object]] = []

    def get(
        self,
        _url: str,
        *,
        params: dict[str, object],
        timeout: float,
    ) -> _Response:
        del timeout
        self.params.append(dict(params))
        return _Response(self.pages[len(self.params) - 1])


def _bootstrapper(session: _Session) -> BackendAuthorityBootstrapper:
    return BackendAuthorityBootstrapper(
        "http://127.0.0.1:62000",
        "http://127.0.0.1:8080",
        "test-token",
        session=session,  # type: ignore[arg-type]
    )


def test_source_templates_advances_current_numbered_contract() -> None:
    session = _Session([
        {
            "items": [{"uuid": "template-1"}],
            "page": 1,
            "page_size": 100,
            "has_more": True,
        },
        {
            "items": [{"uuid": "template-2"}],
            "page": 2,
            "page_size": 100,
            "has_more": False,
        },
    ])

    assert _bootstrapper(session)._source_templates() == [
        {"uuid": "template-1"},
        {"uuid": "template-2"},
    ]
    assert session.params == [
        {"page": 1, "page_size": 100},
        {"page": 2, "page_size": 100},
    ]


def test_source_templates_advances_compact_numbered_contract() -> None:
    session = _Session([
        {"items": [{"uuid": "template-1"}], "has_more": True},
        {"items": [{"uuid": "template-2"}], "has_more": False},
    ])

    assert _bootstrapper(session)._source_templates() == [
        {"uuid": "template-1"},
        {"uuid": "template-2"},
    ]
    assert session.params == [
        {"page": 1, "page_size": 100},
        {"page": 2, "page_size": 100},
    ]


def test_source_templates_keeps_legacy_uuid_cursor_compatibility() -> None:
    session = _Session([
        {
            "items": [{"uuid": "template-1"}],
            "has_more": True,
            "next_cursor_uuid": "template-1",
        },
        {
            "items": [{"uuid": "template-2"}],
            "has_more": False,
            "next_cursor_uuid": None,
        },
    ])

    assert _bootstrapper(session)._source_templates() == [
        {"uuid": "template-1"},
        {"uuid": "template-2"},
    ]
    assert session.params == [
        {"page": 1, "page_size": 100},
        {"limit": 100, "cursor_uuid": "template-1"},
    ]


def test_bootstrap_reconciles_matching_workspace_release_materials(
    tmp_path: Path,
) -> None:
    """验证 Backend Authority 启动不会因既有发行物料跳过位置同步。

    Args:
        tmp_path: pytest 提供的隔离工作区和设备图目录。

    Returns:
        无返回值；模板未重报、设备包位置未更新或物料身份重复时断言失败。
    """

    class ReleaseSession:
        def __init__(self) -> None:
            """初始化跨 Local Backend 与目标 Backend 的 HTTP 调用记录。

            Returns:
                无返回值；``calls`` 保存模板和物料（Material）同步证据。
            """

            self.calls: list[tuple[str, str, object]] = []

        def get(self, url: str, **kwargs: object) -> _Response:
            """返回来源模板、目标模板、既有物料及其当前相对位置。

            Args:
                url: 当前读取的完整 API 地址。
                kwargs: HTTP 参数、认证头和超时配置。

            Returns:
                与请求路径匹配的 Backend-shaped 响应。

            Raises:
                AssertionError: 访问未在启动合同中声明的路径时抛出。
            """

            self.calls.append(("GET", url, kwargs))
            if url == "http://127.0.0.1:62000/api/v1/resource-templates":
                return _Response({
                    "items": [{"uuid": "source-template-uuid"}],
                    "page": 1,
                    "page_size": 100,
                    "has_more": False,
                })
            if url.endswith("/resource-templates/source-template-uuid"):
                return _Response({
                    "uuid": "source-template-uuid",
                    "name": "device.deck",
                    "display_name": "Deck",
                    "resource_type": "device",
                    "config_info": [],
                    "available_sites": [],
                    "handles": [],
                })
            if url == "http://127.0.0.1:8080/api/v1/resource-templates":
                return _Response({
                    "items": [{
                        "uuid": "target-template-uuid",
                        "name": "device.deck",
                    }],
                    "has_more": False,
                })
            if url == "http://127.0.0.1:8080/api/v1/materials":
                return _Response({
                    "items": [{
                        "uuid": "existing-material-uuid",
                        "resource_template_uuid": "target-template-uuid",
                        "barcode": "DECK-01",
                        "revision": 3,
                        "meta_data": {
                            "source_node_id": "deck",
                            "unilab_release": {
                                "source_workspace": str(tmp_path.resolve()),
                            },
                        },
                    }],
                    "total": 1,
                    "page": 1,
                    "page_size": 100,
                })
            if url == "http://127.0.0.1:8080/api/v1/materials/graph":
                return _Response({
                    "nodes": [{
                        "material": {
                            "uuid": "existing-material-uuid",
                            "revision": 3,
                        },
                        "relative_position": {
                            "position_x": 0,
                            "position_y": 0,
                            "position_z": 0,
                            "width": 100,
                            "length": 80,
                            "depth": 60,
                            "scale_x": 1,
                            "scale_y": 1,
                            "scale_z": 1,
                            "rotation_x": 0,
                            "rotation_y": 0,
                            "rotation_z": 0,
                        },
                    }],
                })
            raise AssertionError(url)

        def post(self, url: str, **kwargs: object) -> _Response:
            """接收目标 Backend 的幂等资源模板重报。

            Args:
                url: 当前写入的完整 API 地址。
                kwargs: 模板定义、认证头和超时配置。

            Returns:
                包含目标模板稳定身份的同步响应。
            """

            self.calls.append(("POST", url, kwargs))
            assert url == "http://127.0.0.1:8080/api/v1/resource-templates"
            return _Response({
                "templates": [{
                    "uuid": "target-template-uuid",
                    "name": "device.deck",
                }]
            })

        def put(self, url: str, **kwargs: object) -> _Response:
            """接收既有物料的位置更新和库位（Site）补齐命令。

            Args:
                url: 当前 Edge 物料写入地址。
                kwargs: 相对位置或库位补齐请求及认证配置。

            Returns:
                成功的 Backend-shaped 写响应。
            """

            self.calls.append(("PUT", url, kwargs))
            return _Response({"sites": []})

    graph_path = tmp_path / "graph.json"
    graph_path.write_text(json.dumps({
        "nodes": [{
            "id": "deck",
            "class": "device.deck",
            "type": "device",
            "barcode": "DECK-01",
            "position": {"x": 10, "y": 20, "z": 30},
            "config": {"size_x": 100, "size_y": 80, "size_z": 60},
        }],
    }))
    session = ReleaseSession()
    bootstrapper = BackendAuthorityBootstrapper(
        "http://127.0.0.1:62000",
        "http://127.0.0.1:8080",
        "test-token",
        session=session,  # type: ignore[arg-type]
    )

    report = bootstrapper.bootstrap(graph_path)

    assert report.template_count == 1
    assert report.created_material_count == 0
    assert report.existing_material_count == 1
    put_calls = [call for call in session.calls if call[0] == "PUT"]
    assert [call[1] for call in put_calls] == [
        "http://127.0.0.1:8080/api/v1/edge/materials/existing-material-uuid",
        (
            "http://127.0.0.1:8080/api/v1/edge/materials/"
            "existing-material-uuid/sites/from-template"
        ),
    ]
    position_request = put_calls[0][2]["json"]  # type: ignore[index]
    assert position_request["relative_position"]["position_x"] == 10
    assert position_request["expected_revision"] == 3
