"""微后端代码只能从 ``unilabos.server`` 命名空间导入。"""

from __future__ import annotations

from pathlib import Path

import unilabos


def test_microbackend_namespaces_live_under_server() -> None:
    package_root = Path(unilabos.__file__).resolve().parent

    for removed in ("workflow", "scheduler", "storage"):
        assert not (package_root / removed).exists()
    assert not (package_root / "app" / "scheduler").exists()

    server_root = package_root / "server"
    # Workflow 归属 runtime 库域：契约、表模型和服务分别位于对应的
    # protocol、tables 与 services/runtime 路径。
    assert not (server_root / "workflow").exists()
    assert (package_root / "protocol" / "utils" / "workflow_validation.py").is_file()

    # protocol/tables/services/api/client 五层按四个数据库域组织；
    # 多模块域使用子包，telemetry 与 history 使用单文件。
    protocol_root = package_root / "protocol"
    tables_root = server_root / "database" / "tables"
    services_root = server_root / "services"
    api_root = server_root / "api"
    client_root = package_root / "client"

    # protocol：runtime 子包统一 runtime.v1（data=数据/执行边界 /
    # control=业务控制面 / workflow / registry），
    # materials/telemetry/history 单文件；校验与编解码辅助统一在 utils
    for name in ("data.py", "control.py", "workflow.py", "registry.py"):
        assert (protocol_root / "runtime" / name).is_file()
    assert not (protocol_root / "runtime" / "backend_control.py").exists()
    for name in ("materials.py", "telemetry.py", "history.py"):
        assert (protocol_root / name).is_file()
    for name in ("workflow_validation.py", "json_codec.py"):
        assert (protocol_root / "utils" / name).is_file()
    # Domain protocols must not reintroduce top-level compatibility modules.
    for stray in (
        "workflow.py",
        "registry.py",
        "control.py",
        "common.py",
        "virtual_environment.py",
        "json_codec.py",
        "workflow_validation.py",
    ):
        assert not (protocol_root / stray).exists()

    for layer_dir in (tables_root, services_root, api_root, client_root):
        assert (layer_dir / "runtime" / "__init__.py").is_file()
        for name in ("telemetry.py", "history.py"):
            assert (layer_dir / name).is_file()
        for stray in (
            "workflow",
            "workflow.py",
            "registry.py",
            "graph.py",
            "material_snapshot.py",
            "virtual_environment.py",
            "edge_control.py",
            "backend.py",
        ):
            assert not (layer_dir / stray).exists()
    # Service 通过 SqliteDomain 或域内 store 基座直接持有连接。
    assert not (server_root / "database" / "repositories").exists()
    # tables 的 materials 域是单文件，services/api/client 三层是子包
    assert (tables_root / "materials.py").is_file()
    for layer_dir in (services_root, api_root, client_root):
        assert (layer_dir / "materials" / "__init__.py").is_file()
        assert (layer_dir / "materials" / "core.py").is_file()
        assert (layer_dir / "materials" / "graph.py").is_file()
    for name in ("data.py", "workflow.py", "registry.py"):
        assert (tables_root / "runtime" / name).is_file()

    # workflow 领域服务是 runtime 域内的子包（store=存储基座）；client 无 registry client
    assert (services_root / "runtime" / "workflow" / "service.py").is_file()
    assert (services_root / "runtime" / "workflow" / "store.py").is_file()
    assert (services_root / "runtime" / "workflow" / "errors.py").is_file()
    assert (services_root / "runtime" / "workflow" / "upload.py").is_file()
    assert (services_root / "runtime" / "registry.py").is_file()
    assert (services_root / "materials" / "store.py").is_file()
    assert (services_root / "materials" / "snapshot.py").is_file()
    assert (api_root / "runtime" / "workflow.py").is_file()
    assert (api_root / "runtime" / "registry.py").is_file()
    # runtime.v1 控制面（Backend↔Edge）与 protocol/runtime/control 对齐；
    # 诊断/干预路由（health / status-incidents / restart）也归 runtime 域
    assert (api_root / "runtime" / "control.py").is_file()
    assert (api_root / "runtime" / "data.py").is_file()
    assert not (api_root / "runtime" / "backend_control.py").exists()
    assert (api_root / "runtime" / "diagnostics.py").is_file()
    assert (client_root / "runtime" / "workflow.py").is_file()
    assert not (client_root / "runtime" / "registry.py").exists()
    # client 与 protocol 同范式带 utils 子包收纳横切辅助（封套解析 / CLI 输出）
    assert (client_root / "utils" / "envelope.py").is_file()
    assert (client_root / "utils" / "output.py").is_file()
    assert not (client_root / "envelope.py").exists()
    assert not (client_root / "output.py").exists()
    # DDL 与表模型同文件，当前 schema 采用 checksum 重建策略。
    assert not (server_root / "database" / "migrations").exists()
    assert not (server_root / "scheduler").exists()
    for module_name in (
        "composition.py",
        "coordinator.py",
        "execution.py",
        "execution_queue.py",
        "incidents.py",
        "inventory.py",
        "telemetry.py",
    ):
        assert (server_root / "backend" / module_name).is_file()
    assert (server_root / "backend" / "scheduler" / "service.py").is_file()
    assert (
        server_root / "backend" / "scheduler" / "dag" / "executor.py"
    ).is_file()
    assert (server_root / "composition.py").is_file()
    assert not list((server_root / "storage").glob("*.py"))

    # 契约层独立于 server；物料适配器归 resources，传输实现归 backend。
    assert (package_root / "protocol" / "base.py").is_file()
    assert (package_root / "resources" / "adapters" / "plr_materials.py").is_file()
    assert not (package_root / "adapters").exists()
    assert not (server_root / "protocol").exists()
    assert not (server_root / "adapters").exists()
    backend_root = package_root / "backend"
    assert (backend_root / "hostlink" / "network.py").is_file()
    assert (backend_root / "ros2" / "base_device_node.py").is_file()
    # Host 与 Workstation 编排按 backend 各一份（ros2/presets 与 hostlink），
    # 共享逻辑在 runtime（HostAdapterBase / workstation_protocol）；
    # backend 根目录不提供合一编排层；downlink 是归属 hostlink 且供 ROS2
    # 复用的函数集。
    assert (backend_root / "ros2" / "presets" / "host_node.py").is_file()
    assert (backend_root / "ros2" / "presets" / "workstation.py").is_file()
    assert (backend_root / "hostlink" / "host_node.py").is_file()
    assert (backend_root / "hostlink" / "workstation.py").is_file()
    assert (backend_root / "hostlink" / "downlink.py").is_file()
    assert (backend_root / "runtime" / "host_adapter.py").is_file()
    assert (backend_root / "runtime" / "workstation_protocol.py").is_file()
    assert not (backend_root / "presets").exists()
    assert not (backend_root / "ros2" / "hostlink_bridge.py").exists()
    assert not (backend_root / "hostlink" / "execution_adapter.py").exists()
    assert (backend_root / "dora" / "runtime.py").is_file()
    # 设备执行内核与传输实现同属 backend 大类
    assert (backend_root / "runtime" / "node.py").is_file()
    assert (backend_root / "runtime" / "exception.py").is_file()
    assert not (backend_root / "device_runtime").exists()
    for forbidden_top_level in ("ros", "hostlink", "dora", "device_runtime", "runtime"):
        assert not (package_root / forbidden_top_level).exists()
