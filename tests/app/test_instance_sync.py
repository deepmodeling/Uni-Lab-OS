"""设备图实例通过正式后端 API 初始化的契约测试。"""

from __future__ import annotations

import json

from unilabos.app.instance_sync import (
    INSTANCE_TOKEN_ENV,
    InstanceSynchronizer,
    run_instance_sync_command,
)
from unilabos.app.main import parse_args


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
                        ],
                        "has_more": False,
                        "next_cursor_uuid": None,
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
    def get(self, url, **kwargs):
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
                            }
                        ],
                        "total": 1,
                        "page": 1,
                        "page_size": 100,
                    },
                }
            )
        return super().get(url, **kwargs)


def test_instance_sync_creates_device_and_instrument_through_material_api():
    graph = {
        "nodes": [
            {
                "id": "pump_01",
                "name": "模拟注射泵",
                "type": "device",
                "class": "virtual_transfer_pump",
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

    assert report.created_count == 2
    assert report.existing_count == 0
    assert report.material_uuids == {
        "pump_01": "material-uuid-1",
        "tube_01": "material-uuid-2",
    }
    get_calls = [call for call in session.calls if call[0] == "GET"]
    assert [call[1] for call in get_calls] == [
        "http://backend:8080/api/v1/resource-templates",
        "http://backend:8080/api/v1/materials",
    ]
    post_calls = [call for call in session.calls if call[0] == "POST"]
    assert [call[2]["json"]["barcode"] for call in post_calls] == [
        "DEV-PUMP-01",
        "INS-TUBE-01",
    ]
    assert post_calls[0][2]["json"] == {
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
    assert all(
        call[2]["headers"]["Authorization"] == "Bearer operator-secret"
        for call in session.calls
    )


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
