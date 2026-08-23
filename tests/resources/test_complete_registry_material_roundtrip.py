"""完整资源注册表的创建与序列化回归检查。"""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import uuid4

from pylabrobot.resources import Resource

from unilabos.client.materials import LocalMaterialsClient
from unilabos.registry.registry import lab_registry
from unilabos.resources.resource_tracker import ResourceTreeSet
from unilabos.server.database.repositories.materials import MaterialsRepository
from unilabos.server.adapters.plr_materials import create_plr_materials
from unilabos.server.adapters.registry_materials import register_resource_definitions
from unilabos.server.protocol.common import InventoryMutation
from unilabos.server.services.materials import MaterialsService


_COMPLETE_REGISTRY_RESOURCES: dict[str, dict[str, Any]] | None = None


def _registry_resources() -> dict[str, dict[str, Any]]:
    """构建独立的完整 Registry，避免其他测试预先初始化全局单例后降级。"""

    global _COMPLETE_REGISTRY_RESOURCES
    if _COMPLETE_REGISTRY_RESOURCES is None:
        if not lab_registry._setup_called:  # noqa: SLF001 - 进程级测试边界
            lab_registry.setup(upload_registry=True, complete_registry=True)

        def has_template_root(entry: dict[str, Any]) -> bool:
            config_info = entry.get("config_info")
            return bool(
                isinstance(config_info, list)
                and config_info
                and isinstance(config_info[0], Mapping)
                and config_info[0].get("type")
            )

        if any(
            not has_template_root(entry)
            for entry in lab_registry.resource_type_registry.values()
        ):
            # Registry 是单例；pytest 中其他模块可能已按非上传模式初始化。
            # 在不重跑 setup 的情况下补齐 complete-registry 的模板快照。
            executor = ThreadPoolExecutor(
                max_workers=8, thread_name_prefix="RegistryMaterialAudit"
            )
            lab_registry._startup_executor = executor  # noqa: SLF001
            try:
                lab_registry._populate_resource_config_info(config_cache={})  # noqa: SLF001
            finally:
                executor.shutdown(wait=True)
                lab_registry._startup_executor = None  # noqa: SLF001
        missing_template_roots = [
            resource_id
            for resource_id, entry in lab_registry.resource_type_registry.items()
            if not has_template_root(entry)
        ]
        assert not missing_template_roots, (
            "complete Registry 中存在缺少根 type 的 config_info: "
            f"{missing_template_roots}"
        )
        _COMPLETE_REGISTRY_RESOURCES = lab_registry.resource_type_registry
    return _COMPLETE_REGISTRY_RESOURCES


def _construct_resource(resource_id: str, entry: dict[str, Any]) -> Resource:
    class_config = entry["class"]
    if class_config["type"] != "pylabrobot":
        raise TypeError(
            f"物料注册表只接受 type=pylabrobot，实际为 {class_config['type']!r}"
        )
    module_name, object_name = class_config["module"].rsplit(":", 1)
    factory = getattr(importlib.import_module(module_name), object_name)
    resource = factory(name=f"registry_roundtrip_{resource_id}")
    if not isinstance(resource, Resource):
        raise TypeError(
            f"{class_config['module']} 返回 {type(resource).__name__}，不是 PLR Resource"
        )
    return resource


def _first_difference(expected: Any, actual: Any, path: str = "root") -> str:
    if path.endswith(".prototype_tip.name"):
        # TipSpot.serialize() 为下一支 prototype tip 分配运行时名称；计数器属于 state，
        # 不是静态模板配置。
        return ""
    if path.endswith(".code") and "compute_" in path:
        # marshal 后重新编码的 code object 字节不保证稳定，closure 和可调用行为才是契约。
        return ""
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return "" if float(expected) == float(actual) else f"{path}: {expected!r} != {actual!r}"
    if isinstance(expected, Mapping) and isinstance(actual, Mapping):
        if expected.keys() != actual.keys():
            return f"{path}: keys {expected.keys()} != {actual.keys()}"
        for key in expected:
            difference = _first_difference(
                expected[key], actual[key], f"{path}.{key}"
            )
            if difference:
                return difference
        return ""
    if type(expected) is not type(actual):
        return (
            f"{path}: 类型 {type(expected).__name__} != "
            f"{type(actual).__name__}"
        )
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return f"{path}: 长度 {len(expected)} != {len(actual)}"
        for index, (expected_item, actual_item) in enumerate(zip(expected, actual)):
            difference = _first_difference(
                expected_item, actual_item, f"{path}[{index}]"
            )
            if difference:
                return difference
        return ""
    if expected != actual:
        return f"{path}: {expected!r} != {actual!r}"
    return ""


def test_complete_registry_resources_can_be_created_and_roundtripped():
    """所有已发布物料模板都必须支持默认创建和两层序列化往返。"""

    entries = _registry_resources()
    assert entries, "完整资源注册表为空"

    failures: list[str] = []
    for resource_id, entry in sorted(entries.items()):
        try:
            resource = _construct_resource(resource_id, entry)
        except Exception as exc:
            failures.append(f"{resource_id} [create]: {type(exc).__name__}: {exc}")
            continue

        try:
            serialized = resource.serialize()
            serialized_state = resource.serialize_all_state()
            restored = Resource.deserialize(serialized, allow_marshal=True)
            restored.load_all_state(serialized_state)
            direct_serialized = restored.serialize()
            if direct_serialized != serialized:
                difference = _first_difference(serialized, direct_serialized)
                if difference:
                    raise AssertionError(difference)
            restored_state = restored.serialize_all_state()
            if restored_state != serialized_state:
                difference = _first_difference(serialized_state, restored_state)
                if difference:
                    raise AssertionError(f"state: {difference}")
        except Exception as exc:
            failures.append(
                f"{resource_id} [plr-roundtrip]: {type(exc).__name__}: {exc}"
            )
            continue

        try:
            tree = ResourceTreeSet.from_plr_resources(
                [resource], known_random_uuid=True
            )
            loaded = ResourceTreeSet.load(tree.dump())
            restored_resources = loaded.to_plr_resources(skip_devices=False)
            if len(restored_resources) != 1:
                raise AssertionError(
                    f"ResourceTreeSet 往返得到 {len(restored_resources)} 个根资源"
                )
            tracker_serialized = restored_resources[0].serialize()
            if tracker_serialized != serialized:
                difference = _first_difference(serialized, tracker_serialized)
                if difference:
                    raise AssertionError(difference)
            tracker_state = restored_resources[0].serialize_all_state()
            if tracker_state != serialized_state:
                difference = _first_difference(serialized_state, tracker_state)
                if difference:
                    raise AssertionError(f"state: {difference}")
        except Exception as exc:
            failures.append(
                f"{resource_id} [treeset-roundtrip]: "
                f"{type(exc).__name__}: {exc}"
            )

    assert not failures, "\n\n".join(failures)


def test_complete_registry_resources_can_be_created_by_materials_authority(
    tmp_path,
):
    """Registry 模板和实际 PLR 树必须能经微后端创建并取回权威 UUID。"""

    entries = _registry_resources()
    service = MaterialsService(MaterialsRepository(tmp_path / "materials.db"))
    client = LocalMaterialsClient(service)
    failures: list[str] = []
    try:
        definitions = [
            {"id": resource_id, **entry}
            for resource_id, entry in sorted(entries.items())
        ]
        report = register_resource_definitions(definitions, client)
        assert report.resource_count == len(entries)

        for resource_id, entry in sorted(entries.items()):
            try:
                resource = _construct_resource(resource_id, entry)
                serialized = resource.serialize()
                serialized_state = resource.serialize_all_state()
                created = create_plr_materials(
                    client,
                    InventoryMutation(
                        command_uuid=str(uuid4()),
                        effect_key=f"create:{resource_id}",
                        operation="create_material_tree",
                    ),
                    [resource],
                )
                authoritative = created.resources[0]
                if not getattr(authoritative, "unilabos_uuid", ""):
                    raise AssertionError("微后端回执根物料缺少权威 UUID")
                difference = _first_difference(
                    serialized, authoritative.serialize()
                )
                if difference:
                    raise AssertionError(difference)
                state_difference = _first_difference(
                    serialized_state, authoritative.serialize_all_state()
                )
                if state_difference:
                    raise AssertionError(f"state: {state_difference}")
            except Exception as exc:
                failures.append(
                    f"{resource_id}: {type(exc).__name__}: {exc}"
                )
    finally:
        service.repository.close()

    assert not failures, "\n\n".join(failures)
