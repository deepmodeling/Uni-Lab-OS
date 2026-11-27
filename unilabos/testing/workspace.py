"""领域包软件验收的公共装配入口；复用真实库存与工作流编译，不启动设备。"""

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any


@contextmanager
def software_workspace(package_catalog: Any, path: Path, *, device_ids: set[str],
                       resource_nodes: Sequence[Mapping[str, Any]], source_id: str) -> Iterator[SimpleNamespace]:
    """构建隔离工作区并拥有其存储生命周期。调用方提供领域定义和图，不能指定 SQL。

    返回的 store 仅用于跨仓集成测试核查；生产设备必须使用 runtime 公共接口。
    """
    import hashlib

    import unilabos
    from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
    from unilabos.app.scheduler.inventory.resource_graph_bootstrap import bootstrap_local_resource_graph
    from unilabos.app.scheduler.inventory.resource_reference import build_inventory_resource_reference_resolver
    from unilabos.app.scheduler.inventory.store import InventoryStore
    from unilabos.package_manager.package_catalog.compilers.python.registry import compile_registry_definitions
    from unilabos.persistence.config import DatabaseSettings, use_database_settings
    from unilabos.registry.template_projection import RegistryTemplateProjection
    from unilabos.registry.template_snapshot import RegistryTemplateSnapshot
    from unilabos.resources.resource_tracker import ResourceTreeSet
    from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine

    os_root = Path(unilabos.__file__).resolve().parents[1]
    host_path = os_root / "unilabos/ros/nodes/presets/host_node.py"
    host, _ = compile_registry_definitions(
        workspace_root=os_root,
        namespace="builtin",
        python_files=(host_path,),
        content_hashes={host_path.relative_to(os_root).as_posix(): hashlib.sha256(host_path.read_bytes()).hexdigest()},
    )

    def entries(definitions):
        return [
            dict(
                definition.to_dict()["details"]["registry_entry"],
                id=definition.id,
                source_fqid=f"{definition.module}:{definition.symbol}",
            )
            for definition in definitions
        ]

    devices = entries([*host, *[d for d in package_catalog.definitions.devices if d.id in device_ids]])
    resources = entries(package_catalog.definitions.resources)
    snapshot = RegistryTemplateSnapshot.from_registry(
        SimpleNamespace(
            obtain_registry_device_info=lambda: devices,
            obtain_registry_resource_info=lambda: resources,
        )
    )
    # 测试目录必须实际拥有其数据库，不能继承调用进程的生产 PostgreSQL 配置。
    path.mkdir(parents=True, exist_ok=True)
    with use_database_settings(DatabaseSettings("sqlite")):
        store = InventoryStore(str(path / "inventory.db"))
    try:
        backend = BackendResourceService(store)
        identities = {
            item["name"]: item["uuid"]
            for item in backend.sync_resource_templates(
                snapshot.detached_definitions(),
            )["templates"]
        }
        bootstrap_local_resource_graph(
            store=store,
            resource_tree_set=ResourceTreeSet.from_raw_dict_list([dict(node) for node in resource_nodes]),
            registry_snapshot=snapshot,
            source_id=source_id,
        )
        resolver = build_inventory_resource_reference_resolver(store)
        projection = RegistryTemplateProjection.in_memory(
            authority_id="local",
            resource_template_identity_resolver=identities.__getitem__,
        )
        compiler = WorkflowAuthoringEngine(catalog=projection.refresh(snapshot), resource_reference_resolver=resolver)
        yield SimpleNamespace(store=store, backend=backend, compiler=compiler,
                              identities=identities, resolver=resolver, path=path)
    finally:
        store.close()
