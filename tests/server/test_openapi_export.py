"""离线 OpenAPI 导出：两种角色的路由全集 + x-openlab-role 标注，供前端协议包对账。"""

from __future__ import annotations

import json

from unilabos.server import openapi_export

HTTP_METHODS = {"get", "post", "put", "patch", "delete"}


def _operations(document: dict) -> dict[str, dict]:
    return {
        f"{method.upper()} {path}": operation
        for path, item in document["paths"].items()
        for method, operation in item.items()
        if method in HTTP_METHODS
    }


def test_export_contains_both_roles_and_annotates_every_operation() -> None:
    document = openapi_export.export_openapi()
    operations = _operations(document)

    # 全集：Host 专属、backend 专属、两者共有 都在
    assert "POST /api/v1/driver-packages/install" in operations
    assert "GET /api/v1/device-processes" in operations
    assert "GET /api/v1/status-incidents" in operations
    assert "GET /api/v1/registry/entries" in operations
    assert "GET /api/v1/health" in operations
    assert "GET /api/v1/ping" in operations
    assert "GET /api/v1/scheduler/drain" in operations
    assert "POST /api/v1/scheduler/drain" in operations
    assert "POST /api/v1/scheduler/drain/resume" in operations
    assert "GET /api/v1/inventory/instances" in operations
    assert "GET /api/v1/materials/{material_uuid}" in operations
    assert "GET /api/v1/materials/instances" not in operations
    assert "GET /api/v1/workflow-tasks/{task_uuid}/node-runs" in operations
    assert "GET /api/v1/events" in operations  # SSE 在 OpenAPI 里是 GET

    roles = {key: operation["x-openlab-role"] for key, operation in operations.items()}
    assert set(roles.values()) <= {"host", "backend", "any"}
    assert roles["POST /api/v1/driver-packages/install"] == "host"
    assert roles["GET /api/v1/error-decisions"] == "host"
    assert roles["POST /api/v1/materials/notify-device"] == "host"
    # registry 与 workflow 一样跟随调度权威归属：默认 Host 与 --role backend 都挂
    assert roles["GET /api/v1/registry/entries"] == "any"
    assert roles["GET /api/v1/health"] == "any"
    assert roles["GET /api/v1/workflows"] == "any"

    meta = document["info"]["x-openlab-protocol"]
    assert meta["api_version"] == "v1" and meta["api_prefix"] == "/api/v1"
    assert meta["sse"] == ["/api/v1/events", "/api/v1/materials/events"]
    assert all(path.startswith("/api/v1/") for path in document["paths"])
    # 统一信封：响应模型是 Envelope[SchedulerDrainStatus]，data 字段引用原 DTO
    assert operations["GET /api/v1/scheduler/drain"]["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/Envelope_SchedulerDrainStatus_"
    }
    schemas = document["components"]["schemas"]
    drain_envelope = schemas["Envelope_SchedulerDrainStatus_"]
    assert set(drain_envelope["properties"]) == {"code", "data"}
    assert drain_envelope["properties"]["data"] == {"$ref": "#/components/schemas/SchedulerDrainStatus"}
    # 探针不包信封：/health 仍直接引用 HealthStatus
    assert operations["GET /api/v1/health"]["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/HealthStatus"
    }
    assert {"HealthStatus", "ReadinessStatus", "RestartRequest", "RestartStatus", "ResetRequest", "ResetStatus"} <= schemas.keys()


def test_export_is_deterministic_and_json_serializable() -> None:
    first = json.dumps(openapi_export.export_openapi(), sort_keys=False)
    second = json.dumps(openapi_export.export_openapi(), sort_keys=False)
    assert first == second
    assert list(json.loads(first)["paths"]) == sorted(json.loads(first)["paths"])


def test_cli_writes_file(tmp_path) -> None:
    output = tmp_path / "openapi.json"
    assert openapi_export.main(["--output", str(output)]) == 0
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["info"]["version"]
    assert _operations(document)
