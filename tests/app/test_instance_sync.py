"""设备图实例通过正式后端 API 初始化的契约测试。"""

from __future__ import annotations

import json

import pytest

from unilabos.app.instance_sync import (
    INSTANCE_TOKEN_ENV,
    InstanceSyncError,
    InstanceSynchronizer,
    run_instance_sync_command,
)
from unilabos.app.main import parse_args
from unilabos.resources.instance_identity import normalize_resource_instance_barcode


def test_resource_instance_barcode_normalizes_plr_and_fallback_shapes() -> None:
    assert normalize_resource_instance_barcode({"data": " PLR-01 "}, "sample") == "PLR-01"
    assert normalize_resource_instance_barcode(" EXPLICIT-01 ", "sample") == "EXPLICIT-01"
    assert normalize_resource_instance_barcode(None, "sample") == "UNILAB-GRAPH-sample"


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self):
        self.calls = []
        self.created = 0

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if url.endswith("/resource-templates"):
            return FakeResponse(
                {
                    "code": 0,
                    "data": {
                        "items": [
                            {
                                "uuid": "pump-template-uuid",
                                "name": "virtual_transfer_pump",
                                "resource_type": "device",
                            },
                            {
                                "uuid": "tube-template-uuid",
                                "name": "tube_15ml",
                                "resource_type": "resource",
                            },
                            {
                                "uuid": "warehouse-template-uuid",
                                "name": "sample_warehouse",
                                "resource_type": "resource",
                            },
                            {
                                "uuid": "host-template-uuid",
                                "name": "host_node",
                                "resource_type": "device",
                            },
                        ],
                        "has_more": False,
                        "page": 1,
                        "page_size": 100,
                    },
                }
            )
        if url.endswith("/materials"):
            return FakeResponse(
                {
                    "code": 0,
                    "data": {
                        "items": [],
                        "total": 0,
                        "page": 1,
                        "page_size": 100,
                    },
                }
            )
        raise AssertionError(url)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        self.created += 1
        return FakeResponse(
            {
                "code": 0,
                "data": {
                    "uuid": f"material-uuid-{self.created}",
                    **kwargs["json"],
                },
            },
            status_code=201,
        )


class ExistingSession(FakeSession):
    def __init__(self, *, relative_position=None):
        """构造包含一个既有物料及其可选云端相对位置的 HTTP 测试替身。

        Args:
            relative_position: Backend 物料图当前保存的相对位置；``None`` 表示尚未定位。

        Returns:
            无返回值；初始化请求记录和物料图快照。
        """
        super().__init__()
        # relative_position 表示 Backend 当前持久化的物料位置，用于验证设备包权威同步。
        self.relative_position = relative_position

    def put(self, url, **kwargs):
        self.calls.append(("PUT", url, kwargs))
        return FakeResponse({"code": 0, "data": {"sites": []}})

    def get(self, url, **kwargs):
        """返回既有物料列表、物料图位置或父类提供的资源模板响应。

        Args:
            url: 当前读取的 Backend API 地址。
            kwargs: 查询参数、认证头和超时配置。

        Returns:
            与请求路径匹配的 Backend-shaped 测试响应。
        """

        if url.endswith("/materials"):
            self.calls.append(("GET", url, kwargs))
            return FakeResponse(
                {
                    "code": 0,
                    "data": {
                        "items": [
                            {
                                "uuid": "existing-pump-uuid",
                                "resource_template_uuid": "pump-template-uuid",
                                "barcode": "DEV-PUMP-01",
                                "revision": 7,
                            }
                        ],
                        "total": 1,
                        "page": 1,
                        "page_size": 100,
                    },
                }
            )
        if url.endswith("/materials/graph"):
            self.calls.append(("GET", url, kwargs))
            return FakeResponse(
                {
                    "code": 0,
                    "data": {
                        "nodes": [
                            {
                                "material": {
                                    "uuid": "existing-pump-uuid",
                                    "revision": 7,
                                },
                                "relative_position": self.relative_position,
                                "sites": [],
                            }
                        ]
                    },
                }
            )
        return super().get(url, **kwargs)


class RevisionConflictSession(ExistingSession):
    """模拟设备包位置同步遇到 Backend 修订版本冲突。"""

    def put(self, url, **kwargs):
        """拒绝位置更新，同时保留请求证据供失败关闭断言。

        Args:
            url: 待调用的 Backend Edge 路径。
            kwargs: 请求头、超时和 JSON 更新命令。

        Returns:
            位置更新返回 HTTP 409；其他路径沿用既有物料测试替身。
        """

        if url.endswith("/edge/materials/existing-pump-uuid"):
            self.calls.append(("PUT", url, kwargs))
            return FakeResponse(
                {"code": 409, "error": "material revision conflict"},
                status_code=409,
            )
        return super().put(url, **kwargs)


def test_instance_sync_creates_device_and_instrument_through_material_api() -> None:
    """验证实例同步创建物料、补齐父级，并兼容没有几何字段的旧设备图。

    测试不接收参数且无返回值；请求顺序、父级、身份、Host Node 默认位置或旧图省略规则
    任一变化时断言失败。
    """
    graph = {
        "nodes": [
            {
                "id": "pump_01",
                "name": "模拟注射泵",
                "type": "device",
                "class": "community.example.virtual_transfer_pump",
                "barcode": "DEV-PUMP-01",
                "config": {"port": "MOCK"},
                "data": {"status": "Idle"},
            },
            {
                "id": "tube_01",
                "name": "15 mL 离心管",
                "type": "resource",
                "class": "tube_15ml",
                "barcode": "INS-TUBE-01",
                "config": {},
                "data": {},
            },
        ],
        "links": [],
    }
    session = FakeSession()
    synchronizer = InstanceSynchronizer(
        "http://backend:8080/api/v1",
        "operator-secret",
        session=session,
    )

    report = synchronizer.sync_graph(graph)

    assert report.created_count == 3
    assert report.existing_count == 0
    assert report.material_uuids == {
        "host_node": "material-uuid-1",
        "pump_01": "material-uuid-2",
        "tube_01": "material-uuid-3",
    }
    get_calls = [call for call in session.calls if call[0] == "GET"]
    assert [call[1] for call in get_calls] == [
        "http://backend:8080/api/v1/resource-templates",
        "http://backend:8080/api/v1/materials",
    ]
    assert get_calls[1][2]["params"]["with_children"] == "true"
    post_calls = [call for call in session.calls if call[0] == "POST"]
    assert [call[2]["json"]["barcode"] for call in post_calls] == [
        "UNILAB-GRAPH-host_node",
        "DEV-PUMP-01",
        "INS-TUBE-01",
    ]
    assert post_calls[0][2]["json"]["relative_position"] == {
        "position_x": 0.0,
        "position_y": 0.0,
        "position_z": 0.0,
        "width": 1.0,
        "length": 1.0,
        "depth": 1.0,
        "scale_x": 1.0,
        "scale_y": 1.0,
        "scale_z": 1.0,
        "rotation_x": 0.0,
        "rotation_y": 0.0,
        "rotation_z": 0.0,
    }
    assert post_calls[1][2]["json"] == {
        "resource_template_uuid": "pump-template-uuid",
        "barcode": "DEV-PUMP-01",
        "name": "模拟注射泵",
        "config": {"port": "MOCK"},
        "meta_data": {
            "edge_local_id": "pump_01",
            "edge_resource_type": "device",
            "initial_state": {"status": "Idle"},
        },
    }
    assert post_calls[2][2]["json"]["parent_uuid"] == "material-uuid-1"
    assert all(
        call[2]["headers"]["Authorization"] == "Bearer operator-secret"
        for call in session.calls
    )


def test_instance_sync_sends_graph_geometry_as_material_relative_position() -> None:
    """验证实例同步把设备图位置、尺寸和旋转映射到云端物料相对位置创建合同。

    测试不接收额外参数且无返回值；任一轴映射、尺寸语义或单位缩放缺失时断言失败。
    """
    # graph 只包含一个带完整几何定义的设备，不需要补充场景 Host Node。
    graph = {
        "nodes": [
            {
                "id": "pump_01",
                "name": "模拟注射泵",
                "type": "device",
                "class": "community.example.virtual_transfer_pump",
                "barcode": "DEV-PUMP-01",
                "position": {"x": 10, "y": 20.5, "z": 30},
                "config": {
                    "size_x": 100,
                    "size_y": 80,
                    "size_z": 60,
                    "rotation": {"x": 1, "y": 2, "z": 90},
                },
                "data": {},
            }
        ],
        "links": [],
    }
    # session 记录正式创建请求，使测试能直接核对跨仓库 HTTP 合同。
    session = FakeSession()
    synchronizer = InstanceSynchronizer(
        "http://backend:8080/api/v1",
        "operator-secret",
        session=session,
    )

    synchronizer.sync_graph(graph)

    # pump_request 是目标设备对应的 POST /materials 请求，不包含自动补齐的 HostNode。
    pump_request = next(
        call[2]["json"]
        for call in session.calls
        if call[0] == "POST" and call[2]["json"]["barcode"] == "DEV-PUMP-01"
    )
    assert pump_request["relative_position"] == {
        "position_x": 10.0,
        "position_y": 20.5,
        "position_z": 30.0,
        "width": 100.0,
        "length": 80.0,
        "depth": 60.0,
        "scale_x": 1.0,
        "scale_y": 1.0,
        "scale_z": 1.0,
        "rotation_x": 1.0,
        "rotation_y": 2.0,
        "rotation_z": 90.0,
    }


def test_instance_sync_reads_position_and_rotation_from_pose() -> None:
    """验证旧图缺少顶层 position 时从 pose 读取坐标和旋转。
    测试不接收参数且无返回值；pose 兼容路径或尺寸映射改变时断言失败。
    """
    graph = {
        "nodes": [
            {
                "id": "pump_01",
                "name": "模拟注射泵",
                "type": "device",
                "class": "community.example.virtual_transfer_pump",
                "barcode": "DEV-PUMP-01",
                "pose": {
                    "position": {"x": -10, "y": 0, "z": 5},
                    "rotation": {"x": -1, "y": 0, "z": 45},
                },
                "config": {"size_x": 100, "size_y": 80, "size_z": 60},
            }
        ]
    }
    # session 保存 Backend 物料创建入参，供兼容路径断言使用。
    session = FakeSession()
    synchronizer = InstanceSynchronizer(
        "http://backend:8080/api/v1",
        "operator-secret",
        session=session,
    )

    synchronizer.sync_graph(graph)

    request = next(call[2]["json"] for call in session.calls if call[0] == "POST")
    assert request["relative_position"] == {
        "position_x": -10.0,
        "position_y": 0.0,
        "position_z": 5.0,
        "width": 100.0,
        "length": 80.0,
        "depth": 60.0,
        "scale_x": 1.0,
        "scale_y": 1.0,
        "scale_z": 1.0,
        "rotation_x": -1.0,
        "rotation_y": 0.0,
        "rotation_z": 45.0,
    }


@pytest.mark.parametrize(
    ("geometry", "expected_error"),
    [
        ({"position": {"x": float("nan")}}, "position.x must be finite"),
        ({"position": {"x": True}}, "position.x must be a number"),
        ({"config": {"size_x": -1}}, "config.size_x must be at least 0"),
        ({"pose": []}, "pose must be an object"),
    ],
)
def test_instance_sync_rejects_invalid_graph_geometry(
    geometry: dict[str, object], expected_error: str
) -> None:
    """验证非法几何在任何模板查询或物料写入前失败关闭。

    Args:
        geometry: 覆盖基础设备节点的非法几何片段。
        expected_error: 预期稳定错误消息片段。

    Returns:
        无返回值；未抛出同步错误、错误消息漂移或发生 HTTP 调用时断言失败。
    """
    node = {
        "id": "pump_01",
        "name": "模拟注射泵",
        "type": "device",
        "class": "community.example.virtual_transfer_pump",
        "barcode": "DEV-PUMP-01",
        **geometry,
    }
    # session 用于证明失败发生在外部 HTTP 副作用之前。
    session = FakeSession()
    synchronizer = InstanceSynchronizer(
        "http://backend:8080/api/v1",
        "operator-secret",
        session=session,
    )

    with pytest.raises(InstanceSyncError, match=expected_error):
        synchronizer.sync_graph({"nodes": [node]})

    assert session.calls == []


def test_instance_sync_updates_existing_material_geometry_from_device_graph() -> None:
    """验证设备图声明的位置会更新云端已存在物料。

    测试不接收参数且无返回值；设备包位置与云端不一致时，OS 必须通过 Edge
    写接口携带乐观并发版本和幂等身份更新，再补齐资源模板声明的库位（Site）。
    """
    graph = {
        "nodes": [
            {
                "id": "pump_01",
                "name": "模拟注射泵",
                "type": "device",
                "class": "community.example.virtual_transfer_pump",
                "barcode": "DEV-PUMP-01",
                "position": {"x": 10, "y": 20, "z": 30},
                "config": {"size_x": 100, "size_y": 80, "size_z": 60},
            }
        ]
    }
    # cloud_position 是 Backend 当前持久位置，刻意与设备图声明不同。
    cloud_position = {
        "position_x": 1.0,
        "position_y": 2.0,
        "position_z": 3.0,
        "width": 4.0,
        "length": 5.0,
        "depth": 6.0,
        "scale_x": 1.0,
        "scale_y": 1.0,
        "scale_z": 1.0,
        "rotation_x": 0.0,
        "rotation_y": 0.0,
        "rotation_z": 0.0,
    }
    # session 提供一个已存在物料，用于验证设备包权威的位置更新边界。
    session = ExistingSession(relative_position=cloud_position)
    synchronizer = InstanceSynchronizer(
        "http://backend:8080/api/v1",
        "operator-secret",
        session=session,
    )

    report = synchronizer.sync_graph(graph)

    assert report.created_count == 0
    assert report.existing_count == 1
    assert report.material_uuids == {"pump_01": "existing-pump-uuid"}
    put_calls = [call for call in session.calls if call[0] == "PUT"]
    assert [call[1] for call in put_calls] == [
        "http://backend:8080/api/v1/edge/materials/existing-pump-uuid",
        (
            "http://backend:8080/api/v1/edge/materials/"
            "existing-pump-uuid/sites/from-template"
        ),
    ]
    # position_request 是携带设备包声明、并发版本和审计来源的 Edge 更新命令。
    position_request = put_calls[0][2]["json"]
    assert position_request["relative_position"] == {
        "position_x": 10.0,
        "position_y": 20.0,
        "position_z": 30.0,
        "width": 100.0,
        "length": 80.0,
        "depth": 60.0,
        "scale_x": 1.0,
        "scale_y": 1.0,
        "scale_z": 1.0,
        "rotation_x": 0.0,
        "rotation_y": 0.0,
        "rotation_z": 0.0,
    }
    assert position_request["expected_revision"] == 7
    assert position_request["idempotency_key"].startswith(
        "instance-position/existing-pump-uuid/7/"
    )
    assert position_request["extension"] == {
        "source": "device_package_graph",
        "edge_local_id": "pump_01",
    }
    assert put_calls[1][2]["json"] == {}


def test_instance_sync_skips_unchanged_existing_material_geometry() -> None:
    """验证设备包位置与云端一致时不产生重复位置写入。

    测试不接收参数且无返回值；相同几何仍补齐库位（Site），但不创建位置台账、
    不增加物料修订版本，也不创建重复物料。
    """
    # device_position 同时作为设备图声明和 Backend 当前持久位置。
    device_position = {
        "position_x": 10.0,
        "position_y": 20.0,
        "position_z": 30.0,
        "width": 100.0,
        "length": 80.0,
        "depth": 60.0,
        "scale_x": 1.0,
        "scale_y": 1.0,
        "scale_z": 1.0,
        "rotation_x": 0.0,
        "rotation_y": 0.0,
        "rotation_z": 0.0,
    }
    graph = {
        "nodes": [
            {
                "id": "pump_01",
                "name": "模拟注射泵",
                "type": "device",
                "class": "community.example.virtual_transfer_pump",
                "barcode": "DEV-PUMP-01",
                "position": {"x": 10, "y": 20, "z": 30},
                "config": {"size_x": 100, "size_y": 80, "size_z": 60},
            }
        ]
    }
    session = ExistingSession(relative_position=device_position)
    synchronizer = InstanceSynchronizer(
        "http://backend:8080/api/v1",
        "operator-secret",
        session=session,
    )

    report = synchronizer.sync_graph(graph)

    assert report.created_count == 0
    assert report.existing_count == 1
    put_calls = [call for call in session.calls if call[0] == "PUT"]
    assert [call[1] for call in put_calls] == [
        (
            "http://backend:8080/api/v1/edge/materials/"
            "existing-pump-uuid/sites/from-template"
        )
    ]


def test_instance_sync_fails_closed_on_material_revision_conflict() -> None:
    """验证并发修订冲突不会继续补库位或盲目覆盖云端物料。

    测试不接收参数且无返回值；Backend 在位置读取后发生并发写入时，OS 必须
    保留同一设备包命令身份并失败关闭，等待下一次完整同步重新读取权威事实。
    """
    graph = {
        "nodes": [
            {
                "id": "pump_01",
                "name": "模拟注射泵",
                "type": "device",
                "class": "community.example.virtual_transfer_pump",
                "barcode": "DEV-PUMP-01",
                "position": {"x": 10, "y": 20, "z": 30},
                "config": {"size_x": 100, "size_y": 80, "size_z": 60},
            }
        ]
    }
    # session 返回未定位的云端物料，并在 Edge 更新时模拟 revision 冲突。
    session = RevisionConflictSession(relative_position=None)
    synchronizer = InstanceSynchronizer(
        "http://backend:8080/api/v1",
        "operator-secret",
        session=session,
    )

    with pytest.raises(InstanceSyncError, match="HTTP 409"):
        synchronizer.sync_graph(graph)

    put_calls = [call for call in session.calls if call[0] == "PUT"]
    assert [call[1] for call in put_calls] == [
        "http://backend:8080/api/v1/edge/materials/existing-pump-uuid"
    ]


def test_instance_sync_command_reads_graph_without_starting_edge(tmp_path):
    graph_path = tmp_path / "devices.json"
    graph_path.write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "id": "pump_01",
                        "name": "模拟注射泵",
                        "type": "device",
                        "class": "virtual_transfer_pump",
                        "barcode": "DEV-PUMP-01",
                    }
                ],
                "links": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    parsed = vars(
        parse_args().parse_args(
            [
                "--graph",
                str(graph_path),
                "--addr",
                "http://backend:8080/api/v1",
                "instance-sync",
            ]
        )
    )

    report = run_instance_sync_command(
        parsed,
        backend_address=parsed["addr"],
        environment={INSTANCE_TOKEN_ENV: "operator-secret"},
        session=FakeSession(),
    )

    assert report.created_count == 1
    assert report.material_uuids == {"pump_01": "material-uuid-1"}


def test_instance_sync_normalizes_barcode_less_graph_resources():
    graph = {
        "nodes": [
            {
                "id": "sample_warehouse_01",
                "name": "样品仓",
                "type": "warehouse",
                "class": "community.example.sample_warehouse",
                "parent": "external_deck",
            }
        ]
    }
    session = FakeSession()
    synchronizer = InstanceSynchronizer(
        "http://backend:8080/api/v1",
        "operator-secret",
        session=session,
    )

    report = synchronizer.sync_graph(graph)

    assert report.created_count == 2
    assert report.material_uuids == {
        "host_node": "material-uuid-1",
        "sample_warehouse_01": "material-uuid-2",
    }
    requests = [call[2]["json"] for call in session.calls if call[0] == "POST"]
    request = requests[1]
    assert request["barcode"] == "UNILAB-GRAPH-sample_warehouse_01"
    assert request["parent_uuid"] == "material-uuid-1"
    assert request["meta_data"]["edge_resource_type"] == "resource"


def test_read_only_check_blocks_edge_until_instances_exist():
    graph = {
        "nodes": [
            {
                "id": "pump_01",
                "name": "模拟注射泵",
                "type": "device",
                "class": "virtual_transfer_pump",
                "barcode": "DEV-PUMP-01",
            }
        ]
    }
    session = ExistingSession()
    synchronizer = InstanceSynchronizer(
        "http://backend:8080/api/v1",
        "",
        session=session,
    )

    report = synchronizer.check_graph(graph)

    assert report.created_count == 0
    assert report.existing_count == 1
    assert report.material_uuids == {"pump_01": "existing-pump-uuid"}
    assert not any(call[0] == "POST" for call in session.calls)
    assert all("Authorization" not in call[2]["headers"] for call in session.calls)


def test_instance_sync_reconciles_template_sites_for_existing_material():
    """验证未声明位置的既有物料只补齐资源模板库位（Site）。

    测试不接收参数且无返回值；OS 不得读取物料图或清空云端位置，只调用幂等
    库位补齐接口并复用既有物料（Material）身份。
    """

    graph = {
        "nodes": [
            {
                "id": "pump_01",
                "name": "模拟注射泵",
                "type": "device",
                "class": "virtual_transfer_pump",
                "barcode": "DEV-PUMP-01",
            }
        ]
    }
    session = ExistingSession()
    synchronizer = InstanceSynchronizer(
        "http://backend:8080/api/v1",
        "operator-secret",
        session=session,
    )

    report = synchronizer.sync_graph(graph)

    assert report.existing_count == 1
    put_calls = [call for call in session.calls if call[0] == "PUT"]
    assert [call[1] for call in put_calls] == [
        "http://backend:8080/api/v1/edge/materials/existing-pump-uuid/sites/from-template"
    ]
    assert put_calls[0][2]["json"] == {}
    assert not any(
        call[0] == "GET" and call[1].endswith("/materials/graph")
        for call in session.calls
    )
