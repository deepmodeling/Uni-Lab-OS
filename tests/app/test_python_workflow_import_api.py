"""Local 模式 Python 工作流文件导入 API 测试。"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from tests.workflow.test_authoring_engine import (
    ANALYZE_NODE_UUID,
    PREPARE_NODE_UUID,
    WORKFLOW_UUID,
    _engine,
    _source,
)
from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.domain_source_target import DomainWorkflowSourceTarget
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.source_discovery import discover_editable_sources
from unilabos.workflow.store import WorkflowStore


def _client(
    tmp_path: Any,
    *,
    compiler=None,
) -> tuple[TestClient, WorkflowService, WorkflowStore]:
    """创建模板目录、编译器和 SQLite 权威完全对齐的测试应用。

    参数：``tmp_path`` 是 pytest 提供的隔离目录。返回 HTTP 客户端、服务和存储，
    由调用方在断言后关闭服务。
    """

    selected_root = tmp_path / "domain"
    (selected_root / "demo_domain").mkdir(parents=True)
    selected_root.joinpath("package.yaml").write_text(
        "package:\n  name: demo_domain\nworkflows: []\n",
        encoding="utf-8",
    )
    plan = discover_editable_sources((selected_root,))
    runtime_store = WorkflowStore(
        tmp_path / "workflow_history.db",
        persist_workflow_definitions=False,
    )
    definitions = WorkflowStore(":memory:")
    service = WorkflowService(
        runtime_store,
        definition_store=definitions,
        compiler=compiler or _engine(),
        source_target=DomainWorkflowSourceTarget.from_discovery_plan(plan),
    )
    service.replace_discovered_source_authorizations(plan)
    return TestClient(create_workflow_app(service)), service, definitions


def _upload(
    client: TestClient,
    source: str,
    *,
    file_name: str = "sample_workflow.py",
):
    """以原始 Python 文件字节调用 Local 导入接口。

    参数：``client`` 是隔离应用的 HTTP 客户端；``source`` 是待导入源码；
    ``file_name`` 是不含路径的上传文件名。返回原始 HTTP 响应，供各测试核对
    Backend 形状 envelope 与持久事实。
    """

    return client.post(
        "/api/v1/local/workflows/import-python",
        content=source.encode("utf-8"),
        headers={
            "Content-Type": "text/x-python",
            "X-Workflow-Filename": file_name,
        },
    )


def test_import_two_material_sources_keeps_real_output_bindings(tmp_path: Any) -> None:
    from tests.workflow.test_f05_material_source_authoring_roundtrip import (
        _goal_material_engine,
        _two_material_sources,
    )
    client, service, store = _client(tmp_path, compiler=_goal_material_engine())
    try:
        response = _upload(client, _two_material_sources(), file_name="two_material_sources.py")
        assert response.status_code == 201, response.text
        graph = response.json()["data"]
        assert len(graph["nodes"]) == 2
        bindings = graph["workflow"]["meta_data"]["unilab"]["output_bindings"]
        assert set(bindings) == {"sample_1", "sample_2"}
        assert len({item["workflow_node_uuid"] for item in bindings.values()}) == 2
        stored = store.get_graph(WORKFLOW_UUID)
        assert {node["uuid"] for node in stored["nodes"]} == {node["uuid"] for node in graph["nodes"]}
        assert stored["workflow"]["meta_data"]["unilab"]["output_bindings"] == bindings
    finally:
        service.close()


def test_python_file_import_compiles_and_creates_complete_graph_atomically(
    tmp_path: Any,
) -> None:
    """有效源码应只经 AST 编译并在一次事务中创建完整工作流图。

    参数：``tmp_path`` 是本用例独占的 SQLite 与文件证据目录。返回：无。
    """

    client, service, store = _client(tmp_path)
    try:
        response = _upload(client, _source())
        assert response.status_code == 201, response.text
        payload = response.json()
        assert payload["code"] == 0, payload
        graph = payload["data"]
        assert graph["workflow"]["uuid"] == WORKFLOW_UUID
        assert graph["workflow"]["name"] == "Sample preparation"
        assert graph["workflow"]["status"] == "source"
        assert [node["uuid"] for node in graph["nodes"]] == [
            PREPARE_NODE_UUID,
            ANALYZE_NODE_UUID,
        ]
        assert store.count_rows("workflow") == 1
        assert store.count_rows("workflow_node") == 2
        provenance = graph["workflow"]["meta_data"]["unilab"]["source_bootstrap"]
        assert provenance["relative_path"] == "workflows/sample_workflow.py"
        assert provenance["source_uri"] == (
            "package://demo_domain/workflows/sample_workflow.py"
        )
        authoring = service.get_authoring(WORKFLOW_UUID)
        assert authoring["state"] == "applied"
        assert authoring["status"] == "source"
        assert authoring["workflow_revision"] == 1

        readback = client.get(f"/api/v1/workflows/{WORKFLOW_UUID}/graph")
        published = client.post(
            f"/api/v1/workflows/{WORKFLOW_UUID}/publications",
            json={"revision": 1},
        )
        assert readback.status_code == 200
        assert readback.json()["code"] == 0
        assert published.status_code == 201
        assert published.json()["data"]["node_count"] == 2
        assert published.json()["data"]["edge_count"] == 1
    finally:
        service.close()


def test_python_import_accepts_serial_material_io_with_nullable_load_material(
    tmp_path: Any,
) -> None:
    """Local 导入必须按生产模板接受 unload→load 并输出新物料。"""

    from tests.workflow.test_material_io_contract import (
        FIXED_MATERIAL_UUID,
        LOAD_UUID,
        MOUNT_UUID,
        SITE_UUID,
        UNLOAD_UUID,
        _production_material_io_engine,
    )

    source = f'''from typing import TypedDict
from lab.resources import plate_96
from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.workflow.authoring import material_io, resource_ref, workflow, workflow_output

class Result(TypedDict):
    material: ResourceSlot

@workflow(workflow_uuid="{WORKFLOW_UUID}", displayname="Tip refill", workflow_type="experiment_operation")
def refill() -> Result:
    # unilab:node_uuid={UNLOAD_UUID}
    unloaded = material_io(
        mode="unload", resource_template=None,
        mount=resource_ref("{MOUNT_UUID}"), site="{SITE_UUID}",
        material=resource_ref("{FIXED_MATERIAL_UUID}"),
        disposition="soft_delete", scope="material_with_contents",
        timeout_seconds=3600,
    )
    # unilab:node_uuid={LOAD_UUID}
    loaded = material_io(
        mode="load", resource_template=plate_96,
        mount=resource_ref("{MOUNT_UUID}"), site="{SITE_UUID}",
        material=None, disposition=None, scope=None, timeout_seconds=3600,
    )
    return workflow_output(material=loaded)
'''
    client, service, store = _client(
        tmp_path,
        compiler=_production_material_io_engine(),
    )
    try:
        response = _upload(client, source, file_name="tip_refill.py")
        assert response.status_code == 201, response.text
        graph = response.json()["data"]
        assert {node["uuid"] for node in graph["nodes"]} == {
            UNLOAD_UUID,
            LOAD_UUID,
        }
        assert graph["workflow"]["meta_data"]["unilab"]["output_bindings"][
            "material"
        ]["workflow_node_uuid"] == LOAD_UUID
        assert store.count_rows("workflow") == 1
    finally:
        service.close()


def test_python_experiment_operation_import_uses_matching_directory(
    tmp_path: Any,
) -> None:
    """证明 Python 实验操作导入写入专用目录并保持规范源码格式。

    参数：``tmp_path`` 隔离领域包、manifest 和进程内定义目录。返回：无。异常：
    类型没有参与路径选择、规范装饰器字段丢失或 manifest 登记错误时由断言暴露。
    """

    client, service, store = _client(tmp_path)
    # ``operation_source`` 只在既有装饰器中增加工作流类型；导入后仍由现有
    # AST 生成器规范化，目录分类不得另造第二套 Python 序列化格式。
    operation_source = _source().replace(
        '    displayname="Sample preparation",\n',
        '    displayname="Sample preparation",\n'
        '    workflow_type="experiment_operation",\n',
        1,
    )
    try:
        response = _upload(
            client,
            operation_source,
            file_name="sample_operation.py",
        )
        assert response.status_code == 201, response.text
        graph = response.json()["data"]
        provenance = graph["workflow"]["meta_data"]["unilab"]["source_bootstrap"]
        source_path = (
            tmp_path
            / "domain"
            / "demo_domain"
            / "experiment_operations"
            / "sample_operation.py"
        )
        assert graph["workflow"]["workflow_type"] == "experiment_operation"
        assert provenance["relative_path"] == (
            "experiment_operations/sample_operation.py"
        )
        assert provenance["source_uri"] == (
            "package://demo_domain/experiment_operations/sample_operation.py"
        )
        persisted_source = source_path.read_text(encoding="utf-8")
        assert "@workflow(" in persisted_source
        assert f'workflow_uuid="{WORKFLOW_UUID}"' in persisted_source
        assert "displayname='Sample preparation'" in persisted_source
        assert "workflow_type='experiment_operation'" in persisted_source
        assert "# unilab:node_uuid=" in persisted_source
        assert store.count_rows("workflow") == 1
    finally:
        service.close()


def test_python_file_import_rejects_invalid_source_without_partial_workflow(
    tmp_path: Any,
) -> None:
    """缺少显式工作流身份的源码必须失败且不留下空壳定义。

    参数：``tmp_path`` 是本用例独占的 SQLite 目录。返回：无。
    """

    client, service, store = _client(tmp_path)
    try:
        response = _upload(client, "raise RuntimeError('must never execute')\n")
        assert response.status_code == 200
        assert response.json()["code"] == 1000
        assert response.json()["error"]["msg"] == (
            "Python 工作流导入失败：未找到唯一且有效的工作流身份声明，请保留一个"
            " @workflow(workflow_uuid=\"工作流 UUID\") 装饰器"
        )
        assert store.count_rows("workflow") == 0
        assert store.count_rows("workflow_node") == 0
    finally:
        service.close()


def test_python_file_import_explains_source_failure_and_file_requirements(
    tmp_path: Any,
) -> None:
    """Python 导入失败时应指出源码问题或文件名要求，而不是只返回通用错误。"""

    client, service, store = _client(tmp_path)
    try:
        syntax_error = _upload(client, "def broken(:\n")
        assert syntax_error.status_code == 200
        assert syntax_error.json()["code"] == 1000
        assert "Python 工作流导入失败" in syntax_error.json()["error"]["msg"]
        assert "语法错误" in syntax_error.json()["error"]["msg"]

        invalid_name = _upload(client, _source(), file_name="nested/sample.py")
        assert invalid_name.status_code == 200
        assert invalid_name.json()["code"] == 1000
        assert "文件名必须是单个 .py 文件名" in invalid_name.json()["error"]["msg"]
        assert store.count_rows("workflow") == 0
    finally:
        service.close()


def test_python_file_import_rejects_compile_error_without_partial_workflow(
    tmp_path: Any,
) -> None:
    """有合法身份但引用未知动作的源码也不能留下工作流或节点。

    参数：``tmp_path`` 是本用例独占的 SQLite 目录。返回：无。
    """

    client, service, store = _client(tmp_path)
    invalid_source = _source().replace(
        "reactor.prepare(sample=sample, cycles=cycles)",
        "reactor.unknown(sample=sample, cycles=cycles)",
    )
    try:
        response = _upload(client, invalid_source)
        assert response.status_code == 200
        assert response.json()["code"] == 3003
        assert "Python 工作流导入失败" in response.json()["error"]["msg"]
        assert "源码编译未通过" in response.json()["error"]["msg"]
        assert "unknown" in response.json()["error"]["msg"]
        assert store.count_rows("workflow") == 0
        assert store.count_rows("workflow_node") == 0
    finally:
        service.close()


def test_python_file_import_rejects_path_and_duplicate_identity(
    tmp_path: Any,
) -> None:
    """文件名不能携带路径，同一源码身份也不能创建第二份定义。

    参数：``tmp_path`` 是本用例独占的 SQLite 目录。返回：无。
    """

    client, service, store = _client(tmp_path)
    try:
        invalid_name = _upload(client, _source(), file_name="../sample.py")
        assert invalid_name.status_code == 200
        assert invalid_name.json()["code"] == 1000
        assert store.count_rows("workflow") == 0

        first = _upload(client, _source())
        repeated = _upload(client, _source())
        assert first.status_code == 201
        assert first.json()["code"] == 0
        assert repeated.status_code == 200
        assert repeated.json()["code"] == 3003
        assert store.count_rows("workflow") == 1
        assert store.count_rows("workflow_node") == 2
    finally:
        service.close()


def test_python_file_import_rejects_duplicate_authoring_function_name(
    tmp_path: Any,
) -> None:
    """同一领域包内作者函数名重复时必须拒绝第二个工作流。"""

    client, service, store = _client(tmp_path)
    second_workflow_uuid = str(uuid4())
    second_source = _source().replace(WORKFLOW_UUID, second_workflow_uuid)
    second_source = second_source.replace(PREPARE_NODE_UUID, str(uuid4()))
    second_source = second_source.replace(ANALYZE_NODE_UUID, str(uuid4()))
    try:
        first = _upload(client, _source(), file_name="first.py")
        duplicate = _upload(client, second_source, file_name="second.py")
        assert first.status_code == 201, first.text
        assert duplicate.status_code == 200
        assert duplicate.json()["code"] == 3003
        assert store.count_rows("workflow") == 1
    finally:
        service.close()


def test_python_file_import_enforces_utf8_and_shared_body_budget(tmp_path: Any) -> None:
    """原始文件必须是 UTF-8，且继续受工作流公共 8 MiB 请求预算保护。

    参数：``tmp_path`` 是本用例独占的 SQLite 目录。返回：无。
    """

    client, service, store = _client(tmp_path)
    headers = {
        "Content-Type": "text/x-python",
        "X-Workflow-Filename": "sample.py",
    }
    try:
        invalid_utf8 = client.post(
            "/api/v1/local/workflows/import-python",
            content=b"\xff\xfe",
            headers=headers,
        )
        too_large = client.post(
            "/api/v1/local/workflows/import-python",
            content=b"#" * (8 * 1024 * 1024 + 1),
            headers=headers,
        )
        assert invalid_utf8.status_code == 200
        assert invalid_utf8.json()["code"] == 1000
        assert too_large.status_code == 200
        assert too_large.json()["code"] == 1000
        assert store.count_rows("workflow") == 0
    finally:
        service.close()


def test_python_file_import_never_executes_module_level_source(tmp_path: Any) -> None:
    """合法身份旁的模块级副作用必须作为非法静态语法拒绝且绝不执行。

    参数：``tmp_path`` 同时提供隔离数据库和副作用哨兵路径。返回：无；哨兵不存在
    证明上传源码未被 import、compile、eval 或 execute。
    """

    client, service, store = _client(tmp_path)
    side_effect_path = tmp_path / "must-not-exist.txt"
    dangerous_source = _source().replace(
        "@workflow(",
        f"__import__('pathlib').Path({str(side_effect_path)!r}).write_text('bad')\n\n"
        "@workflow(",
        1,
    )
    try:
        response = _upload(client, dangerous_source)
        assert response.status_code == 200
        assert response.json()["code"] == 3003
        assert not side_effect_path.exists()
        assert store.count_rows("workflow") == 0
        assert store.count_rows("workflow_node") == 0
    finally:
        service.close()


def test_python_file_import_rolls_back_cross_workflow_node_identity_conflict(
    tmp_path: Any,
) -> None:
    """不同工作流复用既有节点身份时必须拒绝并回滚第二个工作流主记录。

    参数：``tmp_path`` 是本用例独占的 SQLite 目录。返回：无；断言只保留首个
    工作流及其节点，证明候选身份检查和写事务没有留下第二个空壳定义。
    """

    client, service, store = _client(tmp_path)
    second_workflow_uuid = str(uuid4())
    conflicting_source = _source().replace(WORKFLOW_UUID, second_workflow_uuid)
    try:
        first = _upload(client, _source(), file_name="first.py")
        conflicting = _upload(client, conflicting_source, file_name="second.py")
        assert first.status_code == 201
        assert first.json()["code"] == 0
        assert conflicting.status_code == 200
        assert conflicting.json()["code"] == 3003
        assert store.count_rows("workflow") == 1
        assert store.count_rows("workflow_node") == 2
    finally:
        service.close()


def test_python_file_import_requires_compiler_and_filename_header(
    tmp_path: Any,
) -> None:
    """缺少模板编译器或上传文件名时必须在任何持久写入前关闭失败。

    参数：``tmp_path`` 是本用例独占的 SQLite 目录。返回：无；分别核对目录不可用
    与 HTTP 请求不完整的稳定错误 envelope。
    """

    store = WorkflowStore(tmp_path / "workflow_history.db")
    service = WorkflowService(store)
    client = TestClient(create_workflow_app(service))
    try:
        unavailable = _upload(client, _source())
        missing_name = client.post(
            "/api/v1/local/workflows/import-python",
            content=_source().encode("utf-8"),
            headers={"Content-Type": "text/x-python"},
        )
        assert unavailable.status_code == 200
        assert unavailable.json()["code"] == 5001
        assert missing_name.status_code == 200
        assert missing_name.json()["code"] == 1000
        assert missing_name.json()["error"]["msg"] == (
            "Python 工作流导入失败：请求头 X-Workflow-Filename 不能为空，必须提供"
        )
        assert store.count_rows("workflow") == 0
    finally:
        service.close()


def test_python_file_import_openapi_and_scheduler_cors_contract(tmp_path: Any) -> None:
    """OpenAPI 与独立调度入口必须共同公开浏览器可调用的上传合同。

    参数：``tmp_path`` 是本用例独占的 SQLite 目录。返回：无；OpenAPI 必须声明
    Python 原始请求体和 201，CORS 必须允许文件名请求头通过预检。
    """

    from unilabos.app.scheduler.api import create_app as create_scheduler_app

    client, service, _store = _client(tmp_path)
    try:
        operation = client.app.openapi()["paths"][
            "/api/v1/local/workflows/import-python"
        ]["post"]
        assert "201" in operation["responses"]
        assert "text/x-python" in operation["requestBody"]["content"]

        scheduler_app = create_scheduler_app()
        cors = next(
            middleware
            for middleware in scheduler_app.user_middleware
            if middleware.cls is CORSMiddleware
        )
        allowed_headers = {str(value).lower() for value in cors.kwargs["allow_headers"]}
        assert "x-workflow-filename" in allowed_headers
    finally:
        service.close()


def test_python_file_import_without_unique_domain_target_fails_closed(
    tmp_path: Any,
) -> None:
    """没有唯一领域包时导入必须失败，不能返回重启后会消失的假成功。

    参数：``tmp_path`` 提供隔离的运行事实 SQLite 目录。返回：无；接口报告来源
    目标不可用，运行库和内存定义目录均不留下工作流。
    """

    database_path = tmp_path / "workflow_history.db"
    engine = _engine()
    first_store = WorkflowStore(
        database_path,
        persist_workflow_definitions=False,
    )
    first_definitions = WorkflowStore(":memory:")
    first_service = WorkflowService(
        first_store,
        definition_store=first_definitions,
        compiler=engine,
    )
    try:
        response = _upload(
            TestClient(create_workflow_app(first_service)),
            _source(),
            file_name="restart-safe.py",
        )
        assert response.status_code == 200
        assert response.json()["code"] == 1
        assert response.json()["error"]["msg"] == (
            "Python 工作流导入失败：当前没有唯一可写的领域包，无法保存工作流源码"
        )
        assert first_definitions.count_rows("workflow") == 0
        assert first_store.count_rows("workflow") == 0
        assert first_store.count_rows("workflow_node") == 0
    finally:
        first_service.close()
