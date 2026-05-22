"""
统一注册表系统

合并了原 Registry (YAML 加载) 和 DecoratorRegistry (装饰器/AST 扫描) 的功能，
提供单一入口来构建、验证和查询设备/资源注册表。
"""

import copy
import importlib
import inspect
import io
import os
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import yaml
from unilabos_msgs.action import EmptyIn, ResourceCreateFromOuter, ResourceCreateFromOuterEasy
from unilabos_msgs.msg import Resource

from unilabos.config.config import BasicConfig
from unilabos.registry.decorators import (
    get_device_meta,
    get_action_meta,
    get_resource_meta,
    has_action_decorator,
    get_all_registered_devices,
    get_all_registered_resources,
    is_not_action,
    is_always_free,
    get_topic_config,
    NodeType,
    normalize_enum_value,
)
from unilabos.registry.utils import (
    ROSMsgNotFound,
    parse_docstring,
    get_json_schema_type,
    parse_type_node,
    type_node_to_schema,
    resolve_type_object,
    type_to_schema,
    detect_slot_type,
    detect_placeholder_keys,
    normalize_ast_handles,
    normalize_ast_action_handles,
    wrap_action_schema,
    preserve_field_descriptions,
    resolve_method_params_via_import,
    SIMPLE_TYPE_MAP,
)
from unilabos.resources.graphio import resource_plr_to_ulab, tree_to_list
from unilabos.resources.resource_tracker import ResourceTreeSet
from unilabos.ros.msgs.message_converter import (
    msg_converter_manager,
    ros_action_to_json_schema,
    String,
    ros_message_to_json_schema,
)
from unilabos.utils import logger
from unilabos.utils.decorator import singleton
from unilabos.utils.cls_creator import import_class
from unilabos.utils.import_manager import get_enhanced_class_info
from unilabos.utils.type_check import NoAliasDumper
from msgcenterpy.instances.json_schema_instance import JSONSchemaMessageInstance
from msgcenterpy.instances.ros2_instance import ROS2MessageInstance

_module_hash_cache: Dict[str, Optional[str]] = {}


@singleton
class Registry:
    """
    统一注册表。

    核心流程:
      1. AST 静态扫描 @device/@resource 装饰器 (快速, 无需 import)
      2. 加载 YAML 注册表 (兼容旧格式)
      3. 设置 host_node 内置设备
      4. verify & resolve (实际 import 验证 + 类型解析)
    """

    def __init__(self, registry_paths=None):
        import ctypes

        try:
            # noinspection PyUnusedImports
            import unilabos_msgs
        except ImportError:
            logger.error("[UniLab Registry] unilabos_msgs模块未找到，请确保已根据官方文档安装unilabos_msgs包。")
            sys.exit(1)
        try:
            ctypes.CDLL(str(Path(unilabos_msgs.__file__).parent / "unilabos_msgs_s__rosidl_typesupport_c.pyd"))
        except OSError:
            pass

        self.registry_paths = [Path(__file__).absolute().parent]
        if registry_paths:
            self.registry_paths.extend(registry_paths)
            logger.debug(f"[UniLab Registry] registry_paths: {self.registry_paths}")

        self.device_type_registry: Dict[str, Any] = {}
        self.resource_type_registry: Dict[str, Any] = {}
        self._type_resolve_cache: Dict[str, Any] = {}

        self._setup_called = False
        self._startup_executor: Optional[ThreadPoolExecutor] = None

    # ------------------------------------------------------------------
    # 统一入口
    # ------------------------------------------------------------------

    def setup(self, devices_dirs=None, upload_registry=False, complete_registry=False, external_only=False):
        """统一构建注册表入口。"""
        if self._setup_called:
            logger.critical("[UniLab Registry] setup方法已被调用过，不允许多次调用")
            return

        self._startup_executor = ThreadPoolExecutor(
            max_workers=8, thread_name_prefix="RegistryStartup"
        )

        # 1. AST 静态扫描 (快速, 无需 import)
        self._run_ast_scan(devices_dirs, upload_registry=upload_registry, external_only=external_only)

        # 2. Host node 内置设备
        self._setup_host_node()

        # 3. YAML 注册表加载 (兼容旧格式) — external_only 模式下跳过
        if external_only:
            logger.info("[UniLab Registry] external_only 模式: 跳过 YAML 注册表加载")
        else:
            self.registry_paths = [Path(path).absolute() for path in self.registry_paths]
            for i, path in enumerate(self.registry_paths):
                sys_path = path.parent
                logger.trace(f"[UniLab Registry] Path {i+1}/{len(self.registry_paths)}: {sys_path}")
                sys.path.append(str(sys_path))
                self.load_device_types(path, complete_registry=complete_registry)
                if BasicConfig.enable_resource_load:
                    self.load_resource_types(path, upload_registry, complete_registry=complete_registry)
                else:
                    logger.warning(
                        "[UniLab Registry] 资源加载已禁用 (enable_resource_load=False)，跳过资源注册表加载"
                    )
        self._startup_executor.shutdown(wait=True)
        self._startup_executor = None
        self._setup_called = True
        logger.trace(f"[UniLab Registry] ----------Setup Complete----------")

    # ------------------------------------------------------------------
    # Host node 设置
    # ------------------------------------------------------------------

    def _setup_host_node(self):
        """设置 host_node 内置设备 — 基于 _run_ast_scan 已扫描的结果进行覆写。"""
        # 从 AST 扫描结果中取出 host_node 的 action_value_mappings
        ast_entry = self.device_type_registry.get("host_node", {})
        ast_actions = ast_entry.get("class", {}).get("action_value_mappings", {})

        # 取出 AST 生成的 action entries, 补充特定覆写
        test_latency_action = ast_actions.get("auto-test_latency", {})
        test_resource_action = ast_actions.get("auto-test_resource", {})
        manual_confirm_action = ast_actions.get("manual_confirm", {})
        test_resource_action["handles"] = {
            "input": [
                {
                    "handler_key": "input_resources",
                    "data_type": "resource",
                    "label": "InputResources",
                    "data_source": "handle",
                    "data_key": "resources",
                },
            ]
        }

        create_resource_action = ast_actions.get("auto-create_resource", {})
        raw_create_resource_schema = ros_action_to_json_schema(
            ResourceCreateFromOuterEasy, "用于创建或更新物料资源，每次传入一个物料信息。"
        )
        raw_create_resource_schema["properties"]["result"] = create_resource_action["schema"]["properties"]["result"]

        # 覆写: 保留硬编码的 ROS2 action + AST 生成的 auto-method
        self.device_type_registry["host_node"] = {
            "class": {
                "module": "unilabos.ros.nodes.presets.host_node:HostNode",
                "status_types": {},
                "action_value_mappings": {
                    "create_resource": {
                        "type": ResourceCreateFromOuterEasy,
                        "goal": {
                            "res_id": "res_id",
                            "class_name": "class_name",
                            "parent": "parent",
                            "device_id": "device_id",
                            "bind_locations": "bind_locations",
                            "liquid_input_slot": "liquid_input_slot[]",
                            "liquid_type": "liquid_type[]",
                            "liquid_volume": "liquid_volume[]",
                            "slot_on_deck": "slot_on_deck",
                        },
                        "feedback": {},
                        "result": {"success": "success"},
                        "schema": raw_create_resource_schema,
                        "goal_default": ROS2MessageInstance(ResourceCreateFromOuterEasy.Goal()).get_python_dict(),
                        "handles": {
                            "output": [
                                {
                                    "handler_key": "labware",
                                    "data_type": "resource",
                                    "label": "Labware",
                                    "data_source": "executor",
                                    "data_key": "created_resource_tree.@flatten",
                                },
                                {
                                    "handler_key": "liquid_slots",
                                    "data_type": "resource",
                                    "label": "LiquidSlots",
                                    "data_source": "executor",
                                    "data_key": "liquid_input_resource_tree.@flatten",
                                },
                                {
                                    "handler_key": "materials",
                                    "data_type": "resource",
                                    "label": "AllMaterials",
                                    "data_source": "executor",
                                    "data_key": "[created_resource_tree,liquid_input_resource_tree].@flatten.@flatten",
                                },
                            ]
                        },
                        "placeholder_keys": {
                            "res_id": "unilabos_resources",
                            "device_id": "unilabos_devices",
                            "parent": "unilabos_nodes",
                            "class_name": "unilabos_class",
                        },
                        "always_free": True,
                        "feedback_interval": 300.0,
                    },
                    "test_latency": test_latency_action,
                    "auto-test_resource": test_resource_action,
                    "manual_confirm": manual_confirm_action,
                },
                "init_params": {},
            },
            "version": "1.0.0",
            "category": [],
            "config_info": [],
            "icon": "icon_device.webp",
            "registry_type": "device",
            "description": "Host Node",
            "handles": [],
            "init_param_schema": {},
            "file_path": "/",
        }
        self._add_builtin_actions(self.device_type_registry["host_node"], "host_node")

    # ------------------------------------------------------------------
    # AST 静态扫描
    # ------------------------------------------------------------------

    def _run_ast_scan(self, devices_dirs=None, upload_registry=False, external_only=False):
        """
        执行 AST 静态扫描，从 Python 代码中提取 @device / @resource 装饰器元数据。
        无需 import 任何驱动模块，速度极快。

        所有缓存（AST 扫描 / build 结果 / config_info）统一存放在
        registry_cache.pkl 一个文件中，删除即可完全重置。
        """
        import time as _time
        from unilabos.registry.ast_registry_scanner import _CACHE_VERSION as AST_SCAN_CACHE_VERSION
        from unilabos.registry.ast_registry_scanner import scan_directory

        scan_t0 = _time.perf_counter()

        # 确保 executor 存在
        own_executor = False
        if self._startup_executor is None:
            self._startup_executor = ThreadPoolExecutor(
                max_workers=8, thread_name_prefix="RegistryStartup"
            )
            own_executor = True

        # ---- 统一缓存：一个 pkl 包含所有数据 ----
        unified_cache = self._load_config_cache()
        ast_cache = unified_cache.setdefault("_ast_scan", {"files": {}})
        if ast_cache.get("version") != AST_SCAN_CACHE_VERSION:
            ast_cache = {"version": AST_SCAN_CACHE_VERSION, "files": {}}
            unified_cache["_ast_scan"] = ast_cache
            unified_cache.pop("_build_results", None)

        # 默认：扫描 unilabos 包所在的父目录
        pkg_root = Path(__file__).resolve().parent.parent          # .../unilabos
        python_path = pkg_root.parent                              # .../Uni-Lab-OS
        scan_root = pkg_root                                       # 扫描 unilabos/ 整个包

        # 额外的 --devices 目录：把它们的父目录加入 sys.path
        extra_dirs: list[Path] = []
        if devices_dirs:
            for d in devices_dirs:
                d_path = Path(d).resolve()
                if not d_path.is_dir():
                    logger.warning(f"[UniLab Registry] --devices 路径不存在或不是目录: {d_path}")
                    continue
                parent_dir = str(d_path.parent)
                if parent_dir not in sys.path:
                    sys.path.insert(0, parent_dir)
                    logger.info(f"[UniLab Registry] 添加 Python 路径: {parent_dir}")
                extra_dirs.append(d_path)

        # 主扫描
        if external_only:
            core_files = [
                pkg_root / "ros" / "nodes" / "presets" / "host_node.py",
                pkg_root / "resources" / "container.py",
            ]
            scan_result = scan_directory(
                scan_root, python_path=python_path, executor=self._startup_executor,
                cache=ast_cache, include_files=core_files,
            )
            logger.info(
                f"[UniLab Registry] external_only 模式: 仅扫描核心文件 "
                f"({', '.join(f.name for f in core_files)})"
            )
        else:
            exclude_files = {"lab_resources.py"} if not BasicConfig.extra_resource else None
            scan_result = scan_directory(
                scan_root, python_path=python_path, executor=self._startup_executor,
                exclude_files=exclude_files, cache=ast_cache,
            )
            if exclude_files:
                logger.info(
                    f"[UniLab Registry] 排除扫描文件: {exclude_files} "
                    f"(可通过 --extra_resource 启用加载)"
                )

        # 合并缓存统计
        total_stats = scan_result.pop("_cache_stats", {"hits": 0, "misses": 0, "total": 0})

        # 额外目录逐个扫描并合并
        for d_path in extra_dirs:
            extra_result = scan_directory(
                d_path, python_path=str(d_path.parent), executor=self._startup_executor,
                cache=ast_cache,
            )
            extra_stats = extra_result.pop("_cache_stats", {"hits": 0, "misses": 0, "total": 0})
            total_stats["hits"] += extra_stats["hits"]
            total_stats["misses"] += extra_stats["misses"]
            total_stats["total"] += extra_stats["total"]

            for did, dmeta in extra_result.get("devices", {}).items():
                if did in scan_result.get("devices", {}):
                    existing = scan_result["devices"][did].get("file_path", "?")
                    new_file = dmeta.get("file_path", "?")
                    raise ValueError(
                        f"@device id 重复: '{did}' 同时出现在 {existing} 和 {new_file}"
                    )
                scan_result.setdefault("devices", {})[did] = dmeta
            for rid, rmeta in extra_result.get("resources", {}).items():
                if rid in scan_result.get("resources", {}):
                    existing = scan_result["resources"][rid].get("file_path", "?")
                    new_file = rmeta.get("file_path", "?")
                    raise ValueError(
                        f"@resource id 重复: '{rid}' 同时出现在 {existing} 和 {new_file}"
                    )
                scan_result.setdefault("resources", {})[rid] = rmeta

        # 缓存命中统计
        if total_stats["total"] > 0:
            logger.info(
                f"[UniLab Registry] AST 缓存统计: "
                f"{total_stats['hits']}/{total_stats['total']} 命中, "
                f"{total_stats['misses']} 重新解析"
            )

        ast_devices = scan_result.get("devices", {})
        ast_resources = scan_result.get("resources", {})

        # build 结果缓存：当所有 AST 文件命中时跳过 _build_*_entry_from_ast
        all_ast_hit = total_stats["misses"] == 0 and total_stats["total"] > 0
        cached_build = unified_cache.get("_build_results") if all_ast_hit else None

        if cached_build:
            cached_devices = cached_build.get("devices", {})
            cached_resources = cached_build.get("resources", {})
            if set(cached_devices) == set(ast_devices) and set(cached_resources) == set(ast_resources):
                self.device_type_registry.update(cached_devices)
                self.resource_type_registry.update(cached_resources)
                logger.info(
                    f"[UniLab Registry] build 缓存命中: 跳过 {len(cached_devices)} 设备 + "
                    f"{len(cached_resources)} 资源的 entry 构建"
                )
            else:
                cached_build = None

        if not cached_build:
            build_t0 = _time.perf_counter()

            for device_id, ast_meta in ast_devices.items():
                entry = self._build_device_entry_from_ast(device_id, ast_meta)
                if entry:
                    self.device_type_registry[device_id] = entry

            for resource_id, ast_meta in ast_resources.items():
                entry = self._build_resource_entry_from_ast(resource_id, ast_meta)
                if entry:
                    self.resource_type_registry[resource_id] = entry

            build_elapsed = _time.perf_counter() - build_t0
            logger.info(f"[UniLab Registry] entry 构建耗时: {build_elapsed:.2f}s")

            unified_cache["_build_results"] = {
                "devices": {k: v for k, v in self.device_type_registry.items() if k in ast_devices},
                "resources": {k: v for k, v in self.resource_type_registry.items() if k in ast_resources},
            }

        # upload 模式下，利用线程池并行 import pylabrobot 资源并生成 config_info
        if upload_registry:
            self._populate_resource_config_info(config_cache=unified_cache)

        # 统一保存一次
        self._save_config_cache(unified_cache)

        ast_device_count = len(ast_devices)
        ast_resource_count = len(ast_resources)
        scan_elapsed = _time.perf_counter() - scan_t0
        if ast_device_count > 0 or ast_resource_count > 0:
            logger.info(
                f"[UniLab Registry] AST 扫描完成: {ast_device_count} 设备, "
                f"{ast_resource_count} 资源 (耗时 {scan_elapsed:.2f}s)"
            )

        if own_executor:
            self._startup_executor.shutdown(wait=False)
            self._startup_executor = None

    # ------------------------------------------------------------------
    # 类型辅助 (共享, 去重后的单一实现)
    # ------------------------------------------------------------------

    def _replace_type_with_class(self, type_name: str, device_id: str, field_name: str) -> Any:
        """将类型名称替换为实际的 ROS 消息类对象（带缓存）"""
        if not type_name or type_name == "":
            return type_name

        cached = self._type_resolve_cache.get(type_name)
        if cached is not None:
            return cached

        result = self._resolve_type_uncached(type_name, device_id, field_name)
        self._type_resolve_cache[type_name] = result
        return result

    def _resolve_type_uncached(self, type_name: str, device_id: str, field_name: str) -> Any:
        """实际的类型解析逻辑（无缓存）"""
        # 泛型类型映射
        if "[" in type_name:
            generic_mapping = {
                "List[int]": "Int64MultiArray",
                "list[int]": "Int64MultiArray",
                "List[float]": "Float64MultiArray",
                "list[float]": "Float64MultiArray",
                "List[bool]": "Int8MultiArray",
                "list[bool]": "Int8MultiArray",
            }
            mapped = generic_mapping.get(type_name)
            if mapped:
                cls = msg_converter_manager.search_class(mapped)
                if cls:
                    return cls
            logger.debug(
                f"[Registry] 设备 {device_id} 的 {field_name} "
                f"泛型类型 '{type_name}' 映射为 String"
            )
            return String

        convert_manager = {
            "str": "String",
            "bool": "Bool",
            "int": "Int64",
            "float": "Float64",
        }
        type_name = convert_manager.get(type_name, type_name)
        if ":" in type_name:
            type_class = msg_converter_manager.get_class(type_name)
        else:
            type_class = msg_converter_manager.search_class(type_name)
        if type_class:
            return type_class
        else:
            logger.trace(
                f"[Registry] 类型 '{type_name}' 非 ROS2 消息类型 (设备 {device_id} {field_name})，映射为 String"
            )
            return String

    # ---- 类型字符串 -> JSON Schema type ----
    # (常量和工具函数已移至 unilabos.registry.utils)

    def _generate_schema_from_info(
        self, param_name: str, param_type: Union[str, Tuple[str]], param_default: Any,
        import_map: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """根据参数信息生成 JSON Schema。
        支持复杂类型字符串如 'Optional[Dict[str, Any]]'、'List[int]' 等。
        当提供 import_map 时，可解析 TypedDict 等自定义类型。"""

        prop_schema: Dict[str, Any] = {}

        if isinstance(param_type, str) and ("[" in param_type or "|" in param_type):
            # 复杂泛型 — ast.parse 解析结构，递归生成 schema
            node = parse_type_node(param_type)
            if node is not None:
                prop_schema = type_node_to_schema(node, import_map)
                # slot 标记 fallback（正常不应走到这里，上层会拦截）
                if "$slot" in prop_schema:
                    prop_schema = {"type": "object"}
            else:
                prop_schema["type"] = "string"
        elif isinstance(param_type, str):
            # 简单类型名，但可能是 import_map 中的自定义类型
            json_type = SIMPLE_TYPE_MAP.get(param_type.lower())
            if json_type:
                prop_schema["type"] = json_type
            elif ":" in param_type:
                type_obj = resolve_type_object(param_type)
                if type_obj is not None:
                    prop_schema = type_to_schema(type_obj)
                else:
                    prop_schema["type"] = "object"
            elif import_map and param_type in import_map:
                type_obj = resolve_type_object(import_map[param_type])
                if type_obj is not None:
                    prop_schema = type_to_schema(type_obj)
                else:
                    prop_schema["type"] = "object"
            else:
                json_type = get_json_schema_type(param_type)
                if json_type == "string" and param_type and param_type.lower() not in SIMPLE_TYPE_MAP:
                    prop_schema["type"] = "object"
                else:
                    prop_schema["type"] = json_type
        elif isinstance(param_type, tuple):
            if len(param_type) == 2:
                outer_type, inner_type = param_type
                outer_json_type = get_json_schema_type(outer_type)
                prop_schema["type"] = outer_json_type
                # Any 值类型不加 additionalProperties/items (等同于无约束)
                if isinstance(inner_type, str) and inner_type in ("Any", "None", "Unknown"):
                    pass
                else:
                    inner_json_type = get_json_schema_type(inner_type)
                    if outer_json_type == "array":
                        prop_schema["items"] = {"type": inner_json_type}
                    elif outer_json_type == "object":
                        prop_schema["additionalProperties"] = {"type": inner_json_type}
            else:
                prop_schema["type"] = "string"
        else:
            prop_schema["type"] = get_json_schema_type(param_type)

        if param_default is not None:
            prop_schema["default"] = param_default

        return prop_schema

    @staticmethod
    def _apply_docstring_param_metadata(
        schema: Dict[str, Any],
        doc_info: Dict[str, Any],
        field_to_param: Optional[Dict[str, str]] = None,
        apply_defaults: bool = False,
    ) -> None:
        """Apply parsed docstring display names and descriptions to schema properties."""
        if not schema or not doc_info:
            return

        props = schema.get("properties", {})
        if not isinstance(props, dict):
            return

        param_descs = doc_info.get("params", {}) or {}
        param_display_names = doc_info.get("param_display_names", {}) or {}
        for field_name, prop_schema in props.items():
            if not isinstance(prop_schema, dict):
                continue
            param_name = field_to_param.get(field_name, field_name) if field_to_param else field_name
            if not isinstance(param_name, str):
                continue
            param_name = param_name.removesuffix("[]")
            if param_name in param_display_names:
                prop_schema["title"] = param_display_names[param_name]
            elif apply_defaults and not prop_schema.get("title"):
                prop_schema["title"] = field_name

            if param_name in param_descs:
                prop_schema["description"] = param_descs[param_name]
            elif apply_defaults and "description" not in prop_schema:
                prop_schema["description"] = ""

    def _generate_unilab_json_command_schema(
        self, method_args: list, docstring: Optional[str] = None,
        import_map: Optional[Dict[str, str]] = None,
        apply_doc_defaults: bool = False,
    ) -> Dict[str, Any]:
        """根据方法参数和 docstring 生成 UniLabJsonCommand schema"""
        doc_info = parse_docstring(docstring)

        schema = {
            "type": "object",
            "properties": {},
            "required": [],
        }
        for arg_info in method_args:
            param_name = arg_info.get("name", "")
            param_type = arg_info.get("type", "")
            param_default = arg_info.get("default")
            param_required = arg_info.get("required", True)

            is_slot, is_list_slot = detect_slot_type(param_type)
            if is_slot == "ResourceSlot":
                if is_list_slot:
                    schema["properties"][param_name] = {
                        "items": ros_message_to_json_schema(Resource, param_name),
                        "type": "array",
                    }
                else:
                    schema["properties"][param_name] = ros_message_to_json_schema(
                        Resource, param_name
                    )
            elif is_slot == "DeviceSlot":
                schema["properties"][param_name] = {"type": "string", "description": "device reference"}
            else:
                schema["properties"][param_name] = self._generate_schema_from_info(
                    param_name, param_type, param_default, import_map=import_map
                )

            if param_required:
                schema["required"].append(param_name)

        self._apply_docstring_param_metadata(schema, doc_info, apply_defaults=apply_doc_defaults)
        return schema

    def _generate_status_types_schema(self, status_methods: Dict[str, Any]) -> Dict[str, Any]:
        """根据 status 方法信息生成 status_types schema"""
        status_schema: Dict[str, Any] = {
            "type": "object",
            "properties": {},
            "required": [],
        }
        for status_name, status_info in status_methods.items():
            return_type = status_info.get("return_type", "str")
            status_schema["properties"][status_name] = self._generate_schema_from_info(
                status_name, return_type, None
            )
            status_schema["required"].append(status_name)
        return status_schema

    # ------------------------------------------------------------------
    # 方法签名分析 -- 委托给 ImportManager
    # ------------------------------------------------------------------

    @staticmethod
    def _analyze_method_signature(method) -> Dict[str, Any]:
        """分析方法签名，提取参数信息"""
        from unilabos.utils.import_manager import default_manager
        try:
            return default_manager._analyze_method_signature(method)
        except (ValueError, TypeError):
            return {"args": [], "is_async": inspect.iscoroutinefunction(method)}

    @staticmethod
    def _get_return_type_from_method(method) -> str:
        """获取方法的返回类型字符串"""
        from unilabos.utils.import_manager import default_manager
        return default_manager._get_return_type_from_method(method)

    # ------------------------------------------------------------------
    # 动态类信息提取 (import-based)
    # ------------------------------------------------------------------

    def _extract_class_info(self, cls: type) -> Dict[str, Any]:
        """
        从类中提取 init 参数、状态方法和动作方法信息。
        """
        result = {
            "class_name": cls.__name__,
            "init_params": self._analyze_method_signature(cls.__init__)["args"],
            "status_methods": {},
            "action_methods": {},
            "explicit_actions": {},
            "decorated_no_type_actions": {},
        }

        for name, method in cls.__dict__.items():
            if name.startswith("_"):
                continue

            # property => status
            if isinstance(method, property):
                return_type = self._get_return_type_from_method(method.fget) if method.fget else "Any"
                status_entry = {
                    "name": name,
                    "return_type": return_type,
                }
                if method.fget:
                    tc = get_topic_config(method.fget)
                    if tc:
                        status_entry["topic_config"] = tc
                result["status_methods"][name] = status_entry

                if method.fset:
                    setter_info = self._analyze_method_signature(method.fset)
                    action_meta = get_action_meta(method.fset)
                    if action_meta and action_meta.get("action_type") is not None:
                        result["explicit_actions"][name] = {
                            "method_info": setter_info,
                            "action_meta": action_meta,
                        }
                continue

            if not callable(method):
                continue

            if is_not_action(method):
                continue

            # @topic_config 装饰的非 property 方法视为状态方法，不作为 action
            tc = get_topic_config(method)
            if tc:
                return_type = self._get_return_type_from_method(method)
                prop_name = name[4:] if name.startswith("get_") else name
                result["status_methods"][prop_name] = {
                    "name": prop_name,
                    "return_type": return_type,
                    "topic_config": tc,
                }
                continue

            method_info = self._analyze_method_signature(method)
            action_meta = get_action_meta(method)

            if action_meta:
                action_type = action_meta.get("action_type")
                if action_type is not None:
                    result["explicit_actions"][name] = {
                        "method_info": method_info,
                        "action_meta": action_meta,
                    }
                else:
                    result["decorated_no_type_actions"][name] = {
                        "method_info": method_info,
                        "action_meta": action_meta,
                    }
            elif has_action_decorator(method):
                result["explicit_actions"][name] = {
                    "method_info": method_info,
                    "action_meta": action_meta or {},
                }
            else:
                result["action_methods"][name] = method_info

        return result

    # ------------------------------------------------------------------
    # 内置动作
    # ------------------------------------------------------------------

    def _add_builtin_actions(self, device_config: Dict[str, Any], device_id: str):
        """为设备添加内置的驱动命令动作（运行时需要，上报注册表时会过滤掉）"""
        str_single_input = self._replace_type_with_class("StrSingleInput", device_id, "内置动作")
        for additional_action in ["_execute_driver_command", "_execute_driver_command_async"]:
            try:
                goal_default = ROS2MessageInstance(str_single_input.Goal()).get_python_dict()
            except Exception:
                goal_default = {"string": ""}

            device_config["class"]["action_value_mappings"][additional_action] = {
                "type": str_single_input,
                "goal": {"string": "string"},
                "feedback": {},
                "result": {},
                "schema": ros_action_to_json_schema(str_single_input),
                "goal_default": goal_default,
                "handles": {},
            }

    # ------------------------------------------------------------------
    # AST-based 注册表条目构建
    # ------------------------------------------------------------------

    def _build_device_entry_from_ast(self, device_id: str, ast_meta: dict) -> Dict[str, Any]:
        """
        Build a device registry entry from AST-scanned metadata.
        Uses only string types -- no module imports required (except for TypedDict resolution).
        """
        module_str = ast_meta.get("module", "")
        file_path = ast_meta.get("file_path", "")
        imap = ast_meta.get("import_map") or {}

        # --- status_types (string version) ---
        status_types_str: Dict[str, str] = {}
        for name, info in ast_meta.get("status_properties", {}).items():
            ret_type = info.get("return_type", "str")
            if not ret_type or ret_type in ("Any", "None", "Unknown", ""):
                ret_type = "String"
            # 归一化泛型容器类型: Dict[str, Any] → dict, List[int] → list 等
            elif "[" in ret_type:
                base = ret_type.split("[", 1)[0].strip()
                base_lower = base.lower()
                if base_lower in ("dict", "mapping", "ordereddict"):
                    ret_type = "dict"
                elif base_lower in ("list", "tuple", "set", "sequence", "iterable"):
                    ret_type = "list"
                elif base_lower == "optional":
                    # Optional[X] → 取内部类型再归一化
                    inner = ret_type.split("[", 1)[1].rsplit("]", 1)[0].strip()
                    inner_lower = inner.lower()
                    if inner_lower in ("dict", "mapping"):
                        ret_type = "dict"
                    elif inner_lower in ("list", "tuple", "set"):
                        ret_type = "list"
                    else:
                        ret_type = inner
            status_types_str[name] = ret_type
        status_types_str = dict(sorted(status_types_str.items()))

        # --- action_value_mappings ---
        action_value_mappings: Dict[str, Any] = {}

        def _build_json_command_entry(method_name, method_info, action_args=None):
            """构建 UniLabJsonCommand 类型的 action entry"""
            is_async = method_info.get("is_async", False)
            type_str = "UniLabJsonCommandAsync" if is_async else "UniLabJsonCommand"
            params = method_info.get("params", [])
            method_doc = method_info.get("docstring")
            method_doc_info = parse_docstring(method_doc)
            goal_schema = self._generate_schema_from_ast_params(params, method_name, method_doc, imap)

            if action_args is not None:
                action_name = action_args.get("action_name", method_name)
                if action_args.get("auto_prefix"):
                    action_name = f"auto-{action_name}"
            else:
                action_name = f"auto-{method_name}"

            # Source C: 从 schema 生成类型默认值
            goal_default = JSONSchemaMessageInstance.generate_default_from_schema(goal_schema)
            # Source B: method param 显式 default 覆盖 Source C
            for p in params:
                if p.get("default") is not None:
                    goal_default[p["name"]] = p["default"]
            # goal 为 identity mapping {param_name: param_name}, 默认值只放在 goal_default
            goal = {p["name"]: p["name"] for p in params}

            # @action 中的显式 goal/goal_default 覆盖
            goal_override = dict((action_args or {}).get("goal", {}))
            goal_default_override = dict((action_args or {}).get("goal_default", {}))
            if goal_override:
                override_values = set(goal_override.values())
                goal = {k: v for k, v in goal.items() if not (k == v and v in override_values)}
            goal.update(goal_override)
            goal_default.update(goal_default_override)

            # action handles: 从 @action(handles=[...]) 提取并转换为标准格式
            raw_handles = (action_args or {}).get("handles")
            handles = (
                normalize_ast_action_handles(raw_handles)
                if isinstance(raw_handles, list)
                else (raw_handles or {})
            )

            # placeholder_keys: 先从参数类型自动检测，再用装饰器显式配置覆盖/补充
            pk = detect_placeholder_keys(params)
            pk.update((action_args or {}).get("placeholder_keys") or {})

            # 从方法返回值类型生成 result schema
            result_schema = None
            ret_type_str = method_info.get("return_type", "")
            if ret_type_str and ret_type_str not in ("None", "Any", ""):
                result_schema = self._generate_schema_from_info(
                    "result", ret_type_str, None, imap
                )

            entry = {
                "type": type_str,
                "goal": goal,
                "feedback": (action_args or {}).get("feedback") or {},
                "result": (action_args or {}).get("result") or {},
                "schema": wrap_action_schema(
                    goal_schema,
                    action_name,
                    description=(action_args or {}).get("description") or method_doc_info.get("description", ""),
                    result_schema=result_schema,
                ),
                "goal_default": goal_default,
                "handles": handles,
                "placeholder_keys": pk,
            }
            if (action_args or {}).get("always_free") or method_info.get("always_free"):
                entry["always_free"] = True
            _fb_iv = (action_args or {}).get("feedback_interval", method_info.get("feedback_interval", 1.0))
            entry["feedback_interval"] = _fb_iv
            nt = normalize_enum_value((action_args or {}).get("node_type"), NodeType)
            if nt:
                entry["node_type"] = nt
            return action_name, entry

        # 1) auto- actions
        for method_name, method_info in ast_meta.get("auto_methods", {}).items():
            action_name, action_entry = _build_json_command_entry(method_name, method_info)
            action_value_mappings[action_name] = action_entry

        # 2) @action() without action_type
        for method_name, method_info in ast_meta.get("actions", {}).items():
            action_args = method_info.get("action_args", {})
            if action_args.get("action_type"):
                continue
            action_name, action_entry = _build_json_command_entry(method_name, method_info, action_args)
            action_value_mappings[action_name] = action_entry

        # 3) @action(action_type=X)
        for method_name, method_info in ast_meta.get("actions", {}).items():
            action_args = method_info.get("action_args", {})
            action_type = action_args.get("action_type")
            if not action_type:
                continue

            action_name = action_args.get("action_name", method_name)
            if action_args.get("auto_prefix"):
                action_name = f"auto-{action_name}"

            raw_handles = action_args.get("handles")
            handles = (
                normalize_ast_action_handles(raw_handles)
                if isinstance(raw_handles, list)
                else (raw_handles or {})
            )

            method_params = method_info.get("params", [])

            # goal/feedback/result: 字段映射
            # parent=True 时直接通过 import class + MRO 获取; 否则从 AST 方法参数获取, 最后从 ROS2 Goal 获取
            # feedback/result 从 ROS2 获取; 默认 identity mapping {k: k}, 再用 @action 参数 update
            goal_override = dict(action_args.get("goal", {}))
            feedback_override = dict(action_args.get("feedback", {}))
            result_override = dict(action_args.get("result", {}))
            goal_default_override = dict(action_args.get("goal_default", {}))

            if action_args.get("parent"):
                # @action(parent=True): 直接通过 import class + MRO 获取父类方法签名
                goal = resolve_method_params_via_import(module_str, method_name)
            else:
                # 从 AST 方法参数构建 goal identity mapping
                real_params = [p for p in method_params if p["name"] not in ("self", "cls")]
                goal = {p["name"]: p["name"] for p in real_params}

            feedback = {}
            result = {}
            schema = {}
            goal_default = {}

            # 尝试 import ROS2 action type 获取 feedback/result/schema/goal_default, 以及 goal fallback
            if ":" not in action_type:
                action_type = imap.get(action_type, action_type)
            action_type_obj = resolve_type_object(action_type) if ":" in action_type else None
            if action_type_obj is None:
                logger.warning(
                    f"[AST] device action '{action_name}': resolve_type_object('{action_type}') returned None"
                )
            if action_type_obj is not None:
                # 始终从 ROS2 Goal 获取字段作为基础, 再用方法参数覆盖
                try:
                    if hasattr(action_type_obj, "Goal"):
                        goal_fields = action_type_obj.Goal.get_fields_and_field_types()
                        ros2_goal = {k: k for k in goal_fields}
                        ros2_goal.update(goal)
                        goal = ros2_goal
                except Exception as e:
                    logger.debug(f"[AST] device action '{action_name}': Goal enrichment from ROS2 failed: {e}")
                try:
                    if hasattr(action_type_obj, "Feedback"):
                        fb_fields = action_type_obj.Feedback.get_fields_and_field_types()
                        feedback = {k: k for k in fb_fields}
                except Exception as e:
                    logger.debug(f"[AST] device action '{action_name}': Feedback enrichment failed: {e}")
                try:
                    if hasattr(action_type_obj, "Result"):
                        res_fields = action_type_obj.Result.get_fields_and_field_types()
                        result = {k: k for k in res_fields}
                except Exception as e:
                    logger.debug(f"[AST] device action '{action_name}': Result enrichment failed: {e}")
                try:
                    schema = ros_action_to_json_schema(action_type_obj)
                except Exception:
                    pass
                # 直接从 ROS2 Goal 实例获取默认值 (msgcenterpy)
                try:
                    goal_default = ROS2MessageInstance(action_type_obj.Goal()).get_python_dict()
                except Exception:
                    pass

            # 如果 ROS2 action type 未提供 result schema, 用方法返回值类型生成 fallback
            if not schema.get("properties", {}).get("result"):
                ret_type_str = method_info.get("return_type", "")
                if ret_type_str and ret_type_str not in ("None", "Any", ""):
                    ret_schema = self._generate_schema_from_info(
                        "result", ret_type_str, None, imap
                    )
                    if ret_schema:
                        schema.setdefault("properties", {})["result"] = ret_schema

            # @action 中的显式 goal/feedback/result/goal_default 覆盖默认值
            # 移除被 override 取代的 identity 条目 (如 {source: source} 被 {sources: source} 取代)
            if goal_override:
                override_values = set(goal_override.values())
                goal = {k: v for k, v in goal.items() if not (k == v and v in override_values)}
            goal.update(goal_override)
            feedback.update(feedback_override)
            result.update(result_override)
            goal_default.update(goal_default_override)

            action_entry = {
                "type": action_type.split(":")[-1],
                "goal": goal,
                "feedback": feedback,
                "result": result,
                "schema": schema,
                "goal_default": goal_default,
                "handles": handles,
                "placeholder_keys": {
                    **detect_placeholder_keys(method_params),
                    **(action_args.get("placeholder_keys") or {}),
                },
            }
            if action_args.get("always_free") or method_info.get("always_free"):
                action_entry["always_free"] = True
            _fb_iv = action_args.get("feedback_interval", method_info.get("feedback_interval", 1.0))
            action_entry["feedback_interval"] = _fb_iv
            nt = normalize_enum_value(action_args.get("node_type"), NodeType)
            if nt:
                action_entry["node_type"] = nt
            goal_schema_for_docs = action_entry.get("schema", {}).get("properties", {}).get("goal", {})
            self._apply_docstring_param_metadata(
                goal_schema_for_docs,
                parse_docstring(method_info.get("docstring")),
                goal,
                apply_defaults=True,
            )
            action_value_mappings[action_name] = action_entry

        action_value_mappings = dict(sorted(action_value_mappings.items()))

        # --- init_param_schema = { config: <init_params>, data: <status_types> } ---
        init_params = ast_meta.get("init_params", [])
        config_schema = self._generate_schema_from_ast_params(
            init_params, "__init__", ast_meta.get("init_docstring"), import_map=imap
        )
        data_schema = self._generate_status_schema_from_ast(
            ast_meta.get("status_properties", {}), imap
        )
        init_schema: Dict[str, Any] = {
            "config": config_schema,
            "data": data_schema,
        }

        # --- handles ---
        handles_raw = ast_meta.get("handles", [])
        handles = normalize_ast_handles(handles_raw)

        entry: Dict[str, Any] = {
            "category": ast_meta.get("category", []),
            "class": {
                "module": module_str,
                "status_types": status_types_str,
                "action_value_mappings": action_value_mappings,
                "type": ast_meta.get("device_type", "python"),
            },
            "config_info": [],
            "description": ast_meta.get("description", ""),
            "handles": handles,
            "icon": ast_meta.get("icon", ""),
            "init_param_schema": init_schema,
            "version": ast_meta.get("version", "1.0.0"),
            "registry_type": "device",
            "file_path": file_path,
        }
        model = ast_meta.get("model")
        if model is not None:
            entry["model"] = model
        hardware_interface = ast_meta.get("hardware_interface")
        if hardware_interface is not None:
            # AST 解析 HardwareInterface(...) 得到 {"_call": "...", "name": ..., "read": ..., "write": ...}
            # 归一化为 YAML 格式，去掉 _call
            if isinstance(hardware_interface, dict) and "_call" in hardware_interface:
                hardware_interface = {k: v for k, v in hardware_interface.items() if k != "_call"}
            entry["class"]["hardware_interface"] = hardware_interface
        return entry

    def _generate_schema_from_ast_params(
        self, params: list, method_name: str, docstring: Optional[str] = None,
        import_map: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Generate JSON Schema from AST-extracted parameter list."""
        doc_info = parse_docstring(docstring)

        schema: Dict[str, Any] = {
            "type": "object",
            "properties": {},
            "required": [],
        }
        for p in params:
            pname = p.get("name", "")
            ptype = p.get("type", "")
            pdefault = p.get("default")
            prequired = p.get("required", True)

            # --- 检测 ResourceSlot / DeviceSlot (兼容 runtime 和 AST 两种格式) ---
            is_slot, is_list_slot = detect_slot_type(ptype)
            if is_slot == "ResourceSlot":
                if is_list_slot:
                    schema["properties"][pname] = {
                        "items": ros_message_to_json_schema(Resource, pname),
                        "type": "array",
                    }
                else:
                    schema["properties"][pname] = ros_message_to_json_schema(Resource, pname)
            elif is_slot == "DeviceSlot":
                schema["properties"][pname] = {"type": "string", "description": "device reference"}
            else:
                schema["properties"][pname] = self._generate_schema_from_info(
                    pname, ptype, pdefault, import_map
                )

            if prequired:
                schema["required"].append(pname)

        self._apply_docstring_param_metadata(schema, doc_info, apply_defaults=True)
        return schema

    def _generate_status_schema_from_ast(
        self, status_properties: Dict[str, Any],
        import_map: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Generate status_types schema from AST-extracted status properties."""
        schema: Dict[str, Any] = {
            "type": "object",
            "properties": {},
            "required": [],
        }
        for name, info in status_properties.items():
            ret_type = info.get("return_type", "str")
            schema["properties"][name] = self._generate_schema_from_info(
                name, ret_type, None, import_map
            )
            schema["required"].append(name)
        return schema

    def _build_resource_entry_from_ast(self, resource_id: str, ast_meta: dict) -> Dict[str, Any]:
        """Build a resource registry entry from AST-scanned metadata."""
        module_str = ast_meta.get("module", "")
        file_path = ast_meta.get("file_path", "")

        handles_raw = ast_meta.get("handles", [])
        handles = normalize_ast_handles(handles_raw)

        entry: Dict[str, Any] = {
            "category": ast_meta.get("category", []),
            "class": {
                "module": module_str,
                "type": ast_meta.get("class_type", "python"),
            },
            "config_info": [],
            "description": ast_meta.get("description", ""),
            "handles": handles,
            "icon": ast_meta.get("icon", ""),
            "init_param_schema": {},
            "version": ast_meta.get("version", "1.0.0"),
            "registry_type": "resource",
            "file_path": file_path,
        }

        if ast_meta.get("model"):
            entry["model"] = ast_meta["model"]

        return entry

    # ------------------------------------------------------------------
    # 定向 AST 扫描（供 complete_registry Case 1 使用）
    # ------------------------------------------------------------------

    def _ast_scan_module(self, module_str: str) -> Optional[Dict[str, Any]]:
        """对单个 module_str 做定向 AST 扫描，返回 ast_meta 或 None。

        用于 complete_registry 模式下 YAML 中存在但 AST 全量扫描未覆盖的设备/资源。
        仅做文件定位 + AST 解析，不实例化类。
        """
        from unilabos.registry.ast_registry_scanner import _parse_file

        mod_part = module_str.split(":")[0]
        try:
            mod = importlib.import_module(mod_part)
            src_file = Path(inspect.getfile(mod))
        except Exception:
            return None

        python_path = Path(__file__).resolve().parent.parent.parent
        try:
            devs, ress = _parse_file(src_file, python_path)
        except Exception:
            return None

        for d in devs:
            if d.get("module") == module_str:
                return d
        for r in ress:
            if r.get("module") == module_str:
                return r
        return None

    # ------------------------------------------------------------------
    # config_info 缓存 (pickle 格式，比 JSON 快 ~10x，debug 模式下差异更大)
    # ------------------------------------------------------------------

    @staticmethod
    def _get_config_cache_path() -> Optional[Path]:
        if BasicConfig.working_dir:
            return Path(BasicConfig.working_dir) / "registry_cache.pkl"
        return None

    _CACHE_VERSION = 4

    def _load_config_cache(self) -> dict:
        import pickle
        cache_path = self._get_config_cache_path()
        if cache_path is None or not cache_path.is_file():
            return {}
        try:
            data = pickle.loads(cache_path.read_bytes())
            if not isinstance(data, dict) or data.get("_version") != self._CACHE_VERSION:
                return {}
            return data
        except Exception:
            return {}

    def _save_config_cache(self, cache: dict) -> None:
        import pickle
        cache_path = self._get_config_cache_path()
        if cache_path is None:
            return
        try:
            cache["_version"] = self._CACHE_VERSION
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache_path.with_suffix(".tmp")
            tmp.write_bytes(pickle.dumps(cache, protocol=pickle.HIGHEST_PROTOCOL))
            tmp.replace(cache_path)
        except Exception as e:
            logger.debug(f"[UniLab Registry] 缓存保存失败: {e}")

    @staticmethod
    def _module_source_hash(module_str: str) -> Optional[str]:
        """Fast MD5 of the source file backing *module_str*. Results are
        cached for the process lifetime so the same file is never read twice."""
        if module_str in _module_hash_cache:
            return _module_hash_cache[module_str]

        import hashlib
        import importlib.util
        mod_part = module_str.split(":")[0] if ":" in module_str else module_str
        result = None
        try:
            spec = importlib.util.find_spec(mod_part)
            if spec and spec.origin and os.path.isfile(spec.origin):
                result = hashlib.md5(open(spec.origin, "rb").read()).hexdigest()
        except Exception:
            pass
        _module_hash_cache[module_str] = result
        return result

    def _populate_resource_config_info(self, config_cache: Optional[dict] = None):
        """
        利用线程池并行 import pylabrobot 资源类，生成 config_info。
        仅在 upload_registry=True 时调用。

        启用缓存：以 module_str 为 key，记录源文件 MD5。若源文件未变则
        直接复用上次的 config_info，跳过 import + 实例化 + dump。

        Args:
            config_cache: 共享的缓存 dict。未提供时自行加载/保存；
                          由 load_resource_types 传入时由调用方统一保存。
        """
        import time as _time

        executor = self._startup_executor
        if executor is None:
            return

        # 筛选需要 import 的 pylabrobot 资源（跳过已有 config_info 的缓存条目）
        pylabrobot_entries = {
            rid: entry
            for rid, entry in self.resource_type_registry.items()
            if entry.get("class", {}).get("type") == "pylabrobot"
            and entry.get("class", {}).get("module")
            and not entry.get("config_info")
        }
        if not pylabrobot_entries:
            return

        t0 = _time.perf_counter()
        own_cache = config_cache is None
        if own_cache:
            config_cache = self._load_config_cache()
        cache_hits = 0
        cache_misses = 0

        def _import_and_dump(resource_id: str, module_str: str):
            """Import class, create instance, dump tree. Returns (rid, config_info)."""
            try:
                res_class = import_class(module_str)
                if callable(res_class) and not isinstance(res_class, type):
                    res_instance = res_class(res_class.__name__)
                    tree_set = ResourceTreeSet.from_plr_resources([res_instance], known_newly_created=True, old_size=True)
                    dumped = tree_set.dump(old_position=True)
                    return resource_id, dumped[0] if dumped else []
            except Exception as e:
                logger.warning(f"[UniLab Registry] 资源 {resource_id} config_info 生成失败: {e}")
            return resource_id, []

        # Separate into cache-hit vs cache-miss
        need_generate: dict = {}  # rid -> module_str
        for rid, entry in pylabrobot_entries.items():
            module_str = entry["class"]["module"]
            cached = config_cache.get(module_str)
            if cached and isinstance(cached, dict) and "config_info" in cached:
                src_hash = self._module_source_hash(module_str)
                if src_hash is not None and cached.get("src_hash") == src_hash:
                    self.resource_type_registry[rid]["config_info"] = cached["config_info"]
                    cache_hits += 1
                    continue
            need_generate[rid] = module_str

        cache_misses = len(need_generate)

        if need_generate:
            future_to_rid = {
                executor.submit(_import_and_dump, rid, mod): rid
                for rid, mod in need_generate.items()
            }
            for future in as_completed(future_to_rid):
                try:
                    resource_id, config_info = future.result()
                    self.resource_type_registry[resource_id]["config_info"] = config_info
                    module_str = need_generate[resource_id]
                    src_hash = self._module_source_hash(module_str)
                    config_cache[module_str] = {
                        "src_hash": src_hash,
                        "config_info": config_info,
                    }
                except Exception as e:
                    rid = future_to_rid[future]
                    logger.warning(f"[UniLab Registry] 资源 {rid} config_info 线程异常: {e}")

        if own_cache:
            self._save_config_cache(config_cache)

        elapsed = _time.perf_counter() - t0
        total = cache_hits + cache_misses
        logger.info(
            f"[UniLab Registry] config_info 缓存统计: "
            f"{cache_hits}/{total} 命中, {cache_misses} 重新生成 "
            f"(耗时 {elapsed:.2f}s)"
        )

    # ------------------------------------------------------------------
    # Verify & Resolve (实际 import 验证)
    # ------------------------------------------------------------------

    def verify_and_resolve_registry(self):
        """
        对 AST 扫描得到的注册表执行实际 import 验证（使用共享线程池并行）。
        """
        errors = []
        import_success_count = 0
        resolved_count = 0
        total_items = len(self.device_type_registry) + len(self.resource_type_registry)

        lock = threading.Lock()

        def _verify_device(device_id: str, entry: dict):
            nonlocal import_success_count, resolved_count
            module_str = entry.get("class", {}).get("module", "")
            if not module_str or ":" not in module_str:
                with lock:
                    import_success_count += 1
                return None

            try:
                cls = import_class(module_str)
                with lock:
                    import_success_count += 1
                    resolved_count += 1

                # 尝试用动态信息增强注册表
                try:
                    self.resolve_types_for_device(device_id, cls)
                except Exception as e:
                    logger.debug(f"[UniLab Registry/Verify] 设备 {device_id} 类型解析失败: {e}")

                return None
            except Exception as e:
                logger.warning(
                    f"[UniLab Registry/Verify] 设备 {device_id}: "
                    f"导入模块 {module_str} 失败: {e}"
                )
                return f"device:{device_id}: {e}"

        def _verify_resource(resource_id: str, entry: dict):
            nonlocal import_success_count
            module_str = entry.get("class", {}).get("module", "")
            if not module_str or ":" not in module_str:
                with lock:
                    import_success_count += 1
                return None

            try:
                import_class(module_str)
                with lock:
                    import_success_count += 1
                return None
            except Exception as e:
                logger.warning(
                    f"[UniLab Registry/Verify] 资源 {resource_id}: "
                    f"导入模块 {module_str} 失败: {e}"
                )
                return f"resource:{resource_id}: {e}"

        executor = self._startup_executor or ThreadPoolExecutor(max_workers=8)
        try:
            device_futures = {}
            resource_futures = {}

            for device_id, entry in list(self.device_type_registry.items()):
                fut = executor.submit(_verify_device, device_id, entry)
                device_futures[fut] = device_id

            for resource_id, entry in list(self.resource_type_registry.items()):
                fut = executor.submit(_verify_resource, resource_id, entry)
                resource_futures[fut] = resource_id

            for future in as_completed(device_futures):
                result = future.result()
                if result:
                    errors.append(result)

            for future in as_completed(resource_futures):
                result = future.result()
                if result:
                    errors.append(result)
        finally:
            if self._startup_executor is None:
                executor.shutdown(wait=True)

        if errors:
            logger.warning(
                f"[UniLab Registry/Verify] 验证完成: {import_success_count}/{total_items} 成功, "
                f"{len(errors)} 个错误"
            )
        else:
            logger.info(
                f"[UniLab Registry/Verify] 验证完成: {import_success_count}/{total_items} 全部通过, "
                f"{resolved_count} 设备类型已解析"
            )

        return errors

    def resolve_types_for_device(self, device_id: str, cls=None):
        """
        将 AST 扫描得到的字符串类型引用替换为实际的 ROS 消息类对象。
        """
        entry = self.device_type_registry.get(device_id)
        if not entry:
            return

        class_info = entry.get("class", {})

        # 解析 status_types
        status_types = class_info.get("status_types", {})
        resolved_status = {}
        for name, type_ref in status_types.items():
            if isinstance(type_ref, str):
                resolved = self._replace_type_with_class(type_ref, device_id, f"状态 {name}")
                if resolved:
                    resolved_status[name] = resolved
                else:
                    resolved_status[name] = type_ref
            else:
                resolved_status[name] = type_ref
        class_info["status_types"] = resolved_status

        # 解析 action_value_mappings
        _KEEP_AS_STRING = {"UniLabJsonCommand", "UniLabJsonCommandAsync"}
        action_mappings = class_info.get("action_value_mappings", {})
        for action_name, action_config in action_mappings.items():
            type_ref = action_config.get("type", "")
            if isinstance(type_ref, str) and type_ref and type_ref not in _KEEP_AS_STRING:
                resolved = self._replace_type_with_class(type_ref, device_id, f"动作 {action_name}")
                if resolved:
                    action_config["type"] = resolved
                    if not action_config.get("schema"):
                        try:
                            action_config["schema"] = ros_action_to_json_schema(resolved)
                        except Exception:
                            pass
                    if not action_config.get("goal_default"):
                        try:
                            action_config["goal_default"] = ROS2MessageInstance(resolved.Goal()).get_python_dict()
                        except Exception:
                            pass

        # 如果提供了类，用动态信息增强
        if cls is not None:
            try:
                dynamic_info = self._extract_class_info(cls)

                for name, info in dynamic_info.get("status_methods", {}).items():
                    if name not in resolved_status:
                        ret_type = info.get("return_type", "str")
                        resolved = self._replace_type_with_class(ret_type, device_id, f"状态 {name}")
                        if resolved:
                            class_info["status_types"][name] = resolved

                for action_name_key, action_config in action_mappings.items():
                    type_obj = action_config.get("type")
                    if isinstance(type_obj, str) and type_obj in (
                        "UniLabJsonCommand", "UniLabJsonCommandAsync"
                    ):
                        method_name = action_name_key
                        if method_name.startswith("auto-"):
                            method_name = method_name[5:]

                        actual_method = getattr(cls, method_name, None)
                        if actual_method:
                            method_info = self._analyze_method_signature(actual_method)
                            schema = self._generate_unilab_json_command_schema(
                                method_info["args"],
                                docstring=getattr(actual_method, "__doc__", None),
                            )
                            action_config["schema"] = schema
            except Exception as e:
                logger.debug(f"[Registry] 设备 {device_id} 动态增强失败: {e}")

        # 添加内置动作
        self._add_builtin_actions(entry, device_id)

    def resolve_all_types(self):
        """将所有注册表条目中的字符串类型引用替换为实际的 ROS2 消息类对象。

        仅做 ROS2 消息类型查找，不 import 任何设备模块，速度快且无副作用。
        """
        t0 = time.time()
        for device_id in list(self.device_type_registry):
            try:
                self.resolve_types_for_device(device_id)
            except Exception as e:
                logger.debug(f"[Registry] 设备 {device_id} 类型解析失败: {e}")
        logger.info(
            f"[UniLab Registry] 类型解析完成: {len(self.device_type_registry)} 设备 "
            f"(耗时 {time.time() - t0:.2f}s)"
        )

    # ------------------------------------------------------------------
    # YAML 注册表加载 (兼容旧格式)
    # ------------------------------------------------------------------

    def _load_single_resource_file(
        self, file: Path, complete_registry: bool
    ) -> Tuple[Dict[str, Any], Dict[str, Any], bool]:
        """
        加载单个资源文件 (线程安全)

        Returns:
            (data, complete_data, is_valid): 资源数据, 完整数据, 是否有效
        """
        try:
            with open(file, encoding="utf-8", mode="r") as f:
                data = yaml.safe_load(io.StringIO(f.read()))
        except Exception as e:
            logger.warning(f"[UniLab Registry] 读取资源文件失败: {file}, 错误: {e}")
            return {}, {}, False

        if not data:
            return {}, {}, False

        complete_data = {}
        skip_ids = set()
        for resource_id, resource_info in data.items():
            if not isinstance(resource_info, dict):
                continue

            # AST 已有该资源 → 跳过，提示冗余
            if self.resource_type_registry.get(resource_id):
                logger.warning(
                    f"[UniLab Registry] 资源 '{resource_id}' 已由 AST 扫描注册，"
                    f"YAML 定义冗余，跳过 YAML 处理"
                )
                skip_ids.add(resource_id)
                continue

            if "version" not in resource_info:
                resource_info["version"] = "1.0.0"
            if "category" not in resource_info:
                resource_info["category"] = [file.stem]
            elif file.stem not in resource_info["category"]:
                resource_info["category"].append(file.stem)
            elif not isinstance(resource_info.get("category"), list):
                resource_info["category"] = [resource_info["category"]]
            if "config_info" not in resource_info:
                resource_info["config_info"] = []
            if "icon" not in resource_info:
                resource_info["icon"] = ""
            if "handles" not in resource_info:
                resource_info["handles"] = []
            if "init_param_schema" not in resource_info:
                resource_info["init_param_schema"] = {}
            if "config_info" in resource_info:
                del resource_info["config_info"]
            if "file_path" in resource_info:
                del resource_info["file_path"]
            resource_info["registry_type"] = "resource"
            resource_info["file_path"] = str(file.absolute()).replace("\\", "/")
            complete_data[resource_id] = copy.deepcopy(dict(sorted(resource_info.items())))

        for rid in skip_ids:
            data.pop(rid, None)

        complete_data = dict(sorted(complete_data.items()))

        if complete_registry:
            write_data = copy.deepcopy(complete_data)
            for res_id, res_cfg in write_data.items():
                res_cfg.pop("file_path", None)
                res_cfg.pop("registry_type", None)
            try:
                with open(file, "w", encoding="utf-8") as f:
                    yaml.dump(write_data, f, allow_unicode=True, default_flow_style=False, Dumper=NoAliasDumper)
            except Exception as e:
                logger.warning(f"[UniLab Registry] 写入资源文件失败: {file}, 错误: {e}")

        return data, complete_data, True

    def load_resource_types(self, path: os.PathLike, upload_registry: bool, complete_registry: bool = False):
        abs_path = Path(path).absolute()
        resources_path = abs_path / "resources"
        files = list(resources_path.rglob("*.yaml"))
        logger.trace(
            f"[UniLab Registry] resources: {resources_path.exists()}, total: {len(files)}"
        )

        if not files:
            return

        import hashlib as _hl

        # --- YAML-level cache: per-file entries with config_info ---
        config_cache = self._load_config_cache() if upload_registry else None
        yaml_cache: dict = config_cache.get("_yaml_resources", {}) if config_cache else {}
        yaml_cache_hits = 0
        yaml_cache_misses = 0
        uncached_files: list[Path] = []
        yaml_file_rids: dict[str, list[str]] = {}

        if complete_registry:
            uncached_files = files
            yaml_cache_misses = len(files)
        else:
            for file in files:
                file_key = str(file.absolute()).replace("\\", "/")
                if upload_registry and yaml_cache:
                    try:
                        yaml_md5 = _hl.md5(file.read_bytes()).hexdigest()
                    except OSError:
                        uncached_files.append(file)
                        yaml_cache_misses += 1
                        continue
                    cached = yaml_cache.get(file_key)
                    if cached and cached.get("yaml_md5") == yaml_md5:
                        module_hashes: dict = cached.get("module_hashes", {})
                        all_ok = all(
                            self._module_source_hash(m) == h
                            for m, h in module_hashes.items()
                        ) if module_hashes else True
                        if all_ok and cached.get("entries"):
                            for rid, entry in cached["entries"].items():
                                self.resource_type_registry[rid] = entry
                            yaml_cache_hits += 1
                            continue
                uncached_files.append(file)
                yaml_cache_misses += 1

        # Process uncached YAML files with thread pool
        executor = self._startup_executor
        future_to_file = {
            executor.submit(self._load_single_resource_file, file, complete_registry): file
            for file in uncached_files
        }

        for future in as_completed(future_to_file):
            file = future_to_file[future]
            try:
                data, complete_data, is_valid = future.result()
                if is_valid:
                    self.resource_type_registry.update(complete_data)
                    file_key = str(file.absolute()).replace("\\", "/")
                    yaml_file_rids[file_key] = list(complete_data.keys())
            except Exception as e:
                logger.warning(f"[UniLab Registry] 加载资源文件失败: {file}, 错误: {e}")

        # upload 模式下，统一利用线程池为 pylabrobot 资源生成 config_info
        if upload_registry:
            self._populate_resource_config_info(config_cache=config_cache)

            # Update YAML cache for newly processed files (entries now have config_info)
            if yaml_file_rids and config_cache is not None:
                for file_key, rids in yaml_file_rids.items():
                    entries = {}
                    module_hashes = {}
                    for rid in rids:
                        entry = self.resource_type_registry.get(rid)
                        if entry:
                            entries[rid] = copy.deepcopy(entry)
                            mod_str = entry.get("class", {}).get("module", "")
                            if mod_str and mod_str not in module_hashes:
                                src_h = self._module_source_hash(mod_str)
                                if src_h:
                                    module_hashes[mod_str] = src_h
                    try:
                        yaml_md5 = _hl.md5(Path(file_key).read_bytes()).hexdigest()
                    except OSError:
                        continue
                    yaml_cache[file_key] = {
                        "yaml_md5": yaml_md5,
                        "module_hashes": module_hashes,
                        "entries": entries,
                    }
                config_cache["_yaml_resources"] = yaml_cache
                self._save_config_cache(config_cache)

        total_yaml = yaml_cache_hits + yaml_cache_misses
        if upload_registry and total_yaml > 0:
            logger.info(
                f"[UniLab Registry] YAML 资源缓存: "
                f"{yaml_cache_hits}/{total_yaml} 文件命中, "
                f"{yaml_cache_misses} 重新加载"
            )

    def _load_single_device_file(
        self, file: Path, complete_registry: bool
    ) -> Tuple[Dict[str, Any], Dict[str, Any], bool, List[str]]:
        """
        加载单个设备文件 (线程安全)

        Returns:
            (data, complete_data, is_valid, device_ids): 设备数据, 完整数据, 是否有效, 设备ID列表
        """
        try:
            with open(file, encoding="utf-8", mode="r") as f:
                data = yaml.safe_load(io.StringIO(f.read()))
        except Exception as e:
            logger.warning(f"[UniLab Registry] 读取设备文件失败: {file}, 错误: {e}")
            return {}, {}, False, []

        if not data:
            return {}, {}, False, []

        complete_data = {}
        action_str_type_mapping = {
            "UniLabJsonCommand": "UniLabJsonCommand",
            "UniLabJsonCommandAsync": "UniLabJsonCommandAsync",
        }
        status_str_type_mapping = {}
        device_ids = []

        skip_ids = set()
        for device_id, device_config in data.items():
            if not isinstance(device_config, dict):
                continue

            # 补全默认字段
            if "version" not in device_config:
                device_config["version"] = "1.0.0"
            if "category" not in device_config:
                device_config["category"] = [file.stem]
            elif file.stem not in device_config["category"]:
                device_config["category"].append(file.stem)
            if "config_info" not in device_config:
                device_config["config_info"] = []
            if "description" not in device_config:
                device_config["description"] = ""
            if "icon" not in device_config:
                device_config["icon"] = ""
            if "handles" not in device_config:
                device_config["handles"] = []
            if "init_param_schema" not in device_config:
                device_config["init_param_schema"] = {}

            if "class" in device_config:
                # --- AST 已有该设备 → 跳过，提示冗余 ---
                if self.device_type_registry.get(device_id):
                    logger.warning(
                        f"[UniLab Registry] 设备 '{device_id}' 已由 AST 扫描注册，"
                        f"YAML 定义冗余，跳过 YAML 处理"
                    )
                    skip_ids.add(device_id)
                    continue

                # --- 正常 YAML 处理 ---
                if "status_types" not in device_config["class"] or device_config["class"]["status_types"] is None:
                    device_config["class"]["status_types"] = {}
                if (
                    "action_value_mappings" not in device_config["class"]
                    or device_config["class"]["action_value_mappings"] is None
                ):
                    device_config["class"]["action_value_mappings"] = {}

                enhanced_info = {}
                enhanced_import_map: Dict[str, str] = {}
                if complete_registry:
                    original_status_keys = set(device_config["class"]["status_types"].keys())
                    device_config["class"]["status_types"].clear()
                    enhanced_info = get_enhanced_class_info(device_config["class"]["module"])
                    if not enhanced_info.get("ast_analysis_success", False):
                        continue
                    enhanced_import_map = enhanced_info.get("import_map", {})
                    for st_k, st_v in enhanced_info["status_methods"].items():
                        if st_k in original_status_keys:
                            device_config["class"]["status_types"][st_k] = st_v["return_type"]

                # --- status_types: 字符串 → class 映射 ---
                for status_name, status_type in device_config["class"]["status_types"].items():
                    if isinstance(status_type, tuple) or status_type in ["Any", "None", "Unknown"]:
                        status_type = "String"
                        device_config["class"]["status_types"][status_name] = status_type
                    try:
                        target_type = self._replace_type_with_class(status_type, device_id, f"状态 {status_name}")
                    except ROSMsgNotFound:
                        continue
                    if target_type in [dict, list]:
                        target_type = String
                    status_str_type_mapping[status_type] = target_type
                device_config["class"]["status_types"] = dict(sorted(device_config["class"]["status_types"].items()))

                if complete_registry:
                    old_action_configs = dict(device_config["class"]["action_value_mappings"])

                    device_config["class"]["action_value_mappings"] = {
                        k: v
                        for k, v in device_config["class"]["action_value_mappings"].items()
                        if not k.startswith("auto-")
                    }
                    for k, v in enhanced_info["action_methods"].items():
                        if k in device_config["class"]["action_value_mappings"]:
                            action_key = k
                        elif k.startswith("get_"):
                            continue
                        else:
                            action_key = f"auto-{k}"
                        goal_schema = self._generate_unilab_json_command_schema(
                            v["args"], docstring=v.get("docstring"), import_map=enhanced_import_map
                        )
                        ret_type = v.get("return_type", "")
                        result_schema = None
                        if ret_type and ret_type not in ("None", "Any", ""):
                            result_schema = self._generate_schema_from_info(
                                "result", ret_type, None, import_map=enhanced_import_map
                            )
                        old_cfg = old_action_configs.get(action_key) or old_action_configs.get(f"auto-{k}", {})
                        doc_info = parse_docstring(v.get("docstring"))
                        new_schema = wrap_action_schema(
                            goal_schema,
                            action_key,
                            description=doc_info.get("description", ""),
                            result_schema=result_schema,
                        )
                        old_schema = old_cfg.get("schema", {})
                        if old_schema:
                            preserve_field_descriptions(new_schema, old_schema)
                            if "description" in old_schema:
                                new_schema["description"] = old_schema["description"]
                        new_schema.setdefault("description", "")

                        old_type = old_cfg.get("type", "")
                        entry_goal = old_cfg.get("goal", {})
                        entry_feedback = {}
                        entry_result = {}
                        entry_schema = new_schema
                        entry_goal_default = {i["name"]: i.get("default") for i in v["args"]}

                        if old_type and not old_type.startswith("UniLabJsonCommand"):
                            entry_type = old_type
                            try:
                                action_type_obj = self._replace_type_with_class(
                                    old_type, device_id, f"动作 {action_key}"
                                )
                            except ROSMsgNotFound:
                                action_type_obj = None
                            if action_type_obj is not None and not isinstance(action_type_obj, str):
                                real_params = [p for p in v["args"]]
                                ros_goal = {p["name"]: p["name"] for p in real_params}
                                try:
                                    if hasattr(action_type_obj, "Goal"):
                                        goal_fields = action_type_obj.Goal.get_fields_and_field_types()
                                        ros2_goal = {f: f for f in goal_fields}
                                        ros2_goal.update(ros_goal)
                                        entry_goal = ros2_goal
                                except Exception:
                                    pass
                                try:
                                    if hasattr(action_type_obj, "Feedback"):
                                        fb_fields = action_type_obj.Feedback.get_fields_and_field_types()
                                        entry_feedback = {f: f for f in fb_fields}
                                except Exception:
                                    pass
                                try:
                                    if hasattr(action_type_obj, "Result"):
                                        res_fields = action_type_obj.Result.get_fields_and_field_types()
                                        entry_result = {f: f for f in res_fields}
                                except Exception:
                                    pass
                                try:
                                    entry_schema = ros_action_to_json_schema(action_type_obj)
                                    if old_schema:
                                        preserve_field_descriptions(entry_schema, old_schema)
                                        if "description" in old_schema:
                                            entry_schema["description"] = old_schema["description"]
                                    entry_schema.setdefault("description", "")
                                except Exception:
                                    pass
                                try:
                                    entry_goal_default = ROS2MessageInstance(
                                        action_type_obj.Goal()
                                    ).get_python_dict()
                                except Exception:
                                    entry_goal_default = old_cfg.get("goal_default", {})
                        else:
                            entry_type = "UniLabJsonCommandAsync" if v["is_async"] else "UniLabJsonCommand"

                        merged_pk = dict(old_cfg.get("placeholder_keys", {}))
                        merged_pk.update(detect_placeholder_keys(v["args"]))
                        goal_schema_for_docs = (
                            entry_schema.get("properties", {}).get("goal", {})
                            if isinstance(entry_schema, dict)
                            else {}
                        )
                        self._apply_docstring_param_metadata(goal_schema_for_docs, doc_info, entry_goal)

                        entry = {
                            "type": entry_type,
                            "goal": entry_goal,
                            "feedback": entry_feedback,
                            "result": entry_result,
                            "schema": entry_schema,
                            "goal_default": entry_goal_default,
                            "handles": old_cfg.get("handles", []),
                            "placeholder_keys": merged_pk,
                        }
                        if v.get("always_free"):
                            entry["always_free"] = True
                        old_node_type = old_cfg.get("node_type")
                        if old_node_type in [NodeType.ILAB.value, NodeType.MANUAL_CONFIRM.value]:
                            entry["node_type"] = old_node_type
                        device_config["class"]["action_value_mappings"][action_key] = entry

                    device_config["init_param_schema"] = {}
                    init_schema = self._generate_unilab_json_command_schema(
                        enhanced_info["init_params"],
                        docstring=enhanced_info.get("init_docstring"),
                        import_map=enhanced_import_map,
                    )
                    device_config["init_param_schema"]["config"] = init_schema

                    data_schema: Dict[str, Any] = {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    }
                    for st_name in device_config["class"]["status_types"]:
                        st_type_str = device_config["class"]["status_types"][st_name]
                        if isinstance(st_type_str, str):
                            data_schema["properties"][st_name] = self._generate_schema_from_info(
                                st_name, st_type_str, None, import_map=enhanced_import_map
                            )
                        else:
                            data_schema["properties"][st_name] = {"type": "string"}
                        data_schema["required"].append(st_name)
                    device_config["init_param_schema"]["data"] = data_schema

                # --- action_value_mappings: 处理非 UniLabJsonCommand 类型 ---
                device_config.pop("schema", None)
                device_config["class"]["action_value_mappings"] = dict(
                    sorted(device_config["class"]["action_value_mappings"].items())
                )
                for action_name, action_config in device_config["class"]["action_value_mappings"].items():
                    if "handles" not in action_config:
                        action_config["handles"] = {}
                    elif isinstance(action_config["handles"], list):
                        if len(action_config["handles"]):
                            logger.error(f"设备{device_id} {action_name} 的handles配置错误，应该是字典类型")
                            continue
                        else:
                            action_config["handles"] = {}
                    if "type" in action_config:
                        action_type_str: str = action_config["type"]
                        if not action_type_str.startswith("UniLabJsonCommand"):
                            try:
                                target_type = self._replace_type_with_class(
                                    action_type_str, device_id, f"动作 {action_name}"
                                )
                            except ROSMsgNotFound:
                                continue
                            action_str_type_mapping[action_type_str] = target_type
                            if target_type is not None:
                                try:
                                    action_config["goal_default"] = ROS2MessageInstance(
                                        target_type.Goal()
                                    ).get_python_dict()
                                except Exception:
                                    action_config["goal_default"] = {}
                                prev_schema = action_config.get("schema", {})
                                action_config["schema"] = ros_action_to_json_schema(target_type)
                                if prev_schema:
                                    preserve_field_descriptions(action_config["schema"], prev_schema)
                                    if "description" in prev_schema:
                                        action_config["schema"]["description"] = prev_schema["description"]
                                action_config["schema"].setdefault("description", "")
                            else:
                                logger.warning(
                                    f"[UniLab Registry] 设备 {device_id} 的动作 {action_name} 类型为空，跳过替换"
                                )

                # deepcopy 保存可序列化的 complete_data（此时 type 字段仍为字符串）
                device_config["file_path"] = str(file.absolute()).replace("\\", "/")
                device_config["registry_type"] = "device"
                complete_data[device_id] = copy.deepcopy(dict(sorted(device_config.items())))

                # 之后才把 type 字符串替换为 class 对象（仅用于运行时 data）
                for status_name, status_type in device_config["class"]["status_types"].items():
                    if status_type in status_str_type_mapping:
                        device_config["class"]["status_types"][status_name] = status_str_type_mapping[status_type]
                for action_name, action_config in device_config["class"]["action_value_mappings"].items():
                    if action_config.get("type") in action_str_type_mapping:
                        action_config["type"] = action_str_type_mapping[action_config["type"]]

                self._add_builtin_actions(device_config, device_id)

            device_ids.append(device_id)

        for did in skip_ids:
            data.pop(did, None)

        complete_data = dict(sorted(complete_data.items()))
        complete_data = copy.deepcopy(complete_data)
        if complete_registry:
            write_data = copy.deepcopy(complete_data)
            for dev_id, dev_cfg in write_data.items():
                dev_cfg.pop("file_path", None)
                dev_cfg.pop("registry_type", None)
            try:
                with open(file, "w", encoding="utf-8") as f:
                    yaml.dump(write_data, f, allow_unicode=True, default_flow_style=False, Dumper=NoAliasDumper)
            except Exception as e:
                logger.warning(f"[UniLab Registry] 写入设备文件失败: {file}, 错误: {e}")

        return data, complete_data, True, device_ids

    def _rebuild_device_runtime_data(self, complete_data: Dict[str, Any]) -> Dict[str, Any]:
        """从 complete_data（纯字符串）重建运行时数据（type 字段替换为 class 对象）。"""
        data = copy.deepcopy(complete_data)
        for device_id, device_config in data.items():
            if "class" not in device_config:
                continue
            # status_types: str → class
            for st_name, st_type in device_config["class"].get("status_types", {}).items():
                if isinstance(st_type, str):
                    device_config["class"]["status_types"][st_name] = self._replace_type_with_class(
                        st_type, device_id, f"状态 {st_name}"
                    )
            # action type: str → class (non-UniLabJsonCommand only)
            for _act_name, act_cfg in device_config["class"].get("action_value_mappings", {}).items():
                t_ref = act_cfg.get("type", "")
                if isinstance(t_ref, str) and t_ref and not t_ref.startswith("UniLabJsonCommand"):
                    resolved = self._replace_type_with_class(t_ref, device_id, f"动作 {_act_name}")
                    if resolved:
                        act_cfg["type"] = resolved
            self._add_builtin_actions(device_config, device_id)
        return data

    def load_device_types(self, path: os.PathLike, complete_registry: bool = False):
        import hashlib as _hl
        t0 = time.time()
        abs_path = Path(path).absolute()
        devices_path = abs_path / "devices"
        device_comms_path = abs_path / "device_comms"
        files = list(devices_path.glob("*.yaml")) + list(device_comms_path.glob("*.yaml"))
        logger.trace(
            f"[UniLab Registry] devices: {devices_path.exists()}, device_comms: {device_comms_path.exists()}, "
            + f"total: {len(files)}"
        )

        if not files:
            return

        config_cache = self._load_config_cache()
        yaml_dev_cache: dict = config_cache.get("_yaml_devices", {})
        cache_hits = 0
        uncached_files: list[Path] = []

        if complete_registry:
            uncached_files = files
        else:
            for file in files:
                file_key = str(file.absolute()).replace("\\", "/")
                try:
                    yaml_md5 = _hl.md5(file.read_bytes()).hexdigest()
                except OSError:
                    uncached_files.append(file)
                    continue
                cached = yaml_dev_cache.get(file_key)
                if cached and cached.get("yaml_md5") == yaml_md5 and cached.get("entries"):
                    complete_data = cached["entries"]
                    # 过滤掉 AST 已有的设备
                    complete_data = {
                        did: cfg for did, cfg in complete_data.items()
                        if not self.device_type_registry.get(did)
                    }
                    runtime_data = self._rebuild_device_runtime_data(complete_data)
                    self.device_type_registry.update(runtime_data)
                    cache_hits += 1
                    continue
                uncached_files.append(file)

        executor = self._startup_executor
        future_to_file = {
            executor.submit(
                self._load_single_device_file, file, complete_registry
            ): file
            for file in uncached_files
        }

        for future in as_completed(future_to_file):
            file = future_to_file[future]
            try:
                data, _complete_data, is_valid, device_ids = future.result()
                if is_valid:
                    runtime_data = {did: data[did] for did in device_ids if did in data}
                    self.device_type_registry.update(runtime_data)
                    # 写入缓存
                    file_key = str(file.absolute()).replace("\\", "/")
                    try:
                        yaml_md5 = _hl.md5(file.read_bytes()).hexdigest()
                        yaml_dev_cache[file_key] = {
                            "yaml_md5": yaml_md5,
                            "entries": _complete_data,
                        }
                    except OSError:
                        pass
            except Exception as e:
                logger.warning(f"[UniLab Registry] 加载设备文件失败: {file}, 错误: {e}")

        if uncached_files and yaml_dev_cache:
            latest_cache = self._load_config_cache()
            latest_cache["_yaml_devices"] = yaml_dev_cache
            self._save_config_cache(latest_cache)

        total = len(files)
        extra = " (complete_registry 跳过缓存)" if complete_registry else ""
        logger.info(
            f"[UniLab Registry] YAML 设备加载: "
            f"{cache_hits}/{total} 缓存命中, "
            f"{len(uncached_files)} 重新加载 "
            f"(耗时 {time.time() - t0:.2f}s){extra}"
        )

    # ------------------------------------------------------------------
    # 注册表信息输出
    # ------------------------------------------------------------------

    def obtain_registry_device_info(self):
        devices = []
        for device_id, device_info in self.device_type_registry.items():
            device_info_copy = copy.deepcopy(device_info)
            if "class" in device_info_copy and "action_value_mappings" in device_info_copy["class"]:
                action_mappings = device_info_copy["class"]["action_value_mappings"]
                builtin_actions = ["_execute_driver_command", "_execute_driver_command_async"]
                filtered_action_mappings = {
                    action_name: action_config
                    for action_name, action_config in action_mappings.items()
                    if action_name not in builtin_actions
                }
                device_info_copy["class"]["action_value_mappings"] = filtered_action_mappings

                for action_name, action_config in filtered_action_mappings.items():
                    type_obj = action_config.get("type")
                    if hasattr(type_obj, "__name__"):
                        action_config["type"] = type_obj.__name__
                    if "schema" in action_config and action_config["schema"]:
                        schema = action_config["schema"]
                        # 确保schema结构存在
                        if (
                            "properties" in schema
                            and "goal" in schema["properties"]
                            and "properties" in schema["properties"]["goal"]
                        ):
                            schema["properties"]["goal"]["properties"] = {
                                "unilabos_device_id": {
                                    "type": "string",
                                    "default": "",
                                    "title": "设备ID",
                                    "description": "UniLabOS设备ID，用于指定执行动作的具体设备实例",
                                },
                                **schema["properties"]["goal"]["properties"],
                            }
                    # 将 placeholder_keys 信息添加到 schema 中
                    if "placeholder_keys" in action_config and action_config.get("schema", {}).get(
                        "properties", {}
                    ).get("goal", {}):
                        action_config["schema"]["properties"]["goal"]["_unilabos_placeholder_info"] = action_config[
                            "placeholder_keys"
                        ]
                status_types = device_info_copy["class"].get("status_types", {})
                for status_name, status_type in status_types.items():
                    if hasattr(status_type, "__name__"):
                        status_types[status_name] = status_type.__name__

            msg = {"id": device_id, **device_info_copy}
            devices.append(msg)
        return devices

    def obtain_registry_resource_info(self):
        resources = []
        for resource_id, resource_info in self.resource_type_registry.items():
            msg = {"id": resource_id, **resource_info}
            resources.append(msg)
        return resources

    def get_yaml_output(self, device_id: str) -> str:
        """将指定设备的注册表条目导出为 YAML 字符串。"""
        entry = self.device_type_registry.get(device_id)
        if not entry:
            return ""

        entry = copy.deepcopy(entry)

        if "class" in entry:
            status_types = entry["class"].get("status_types", {})
            for name, type_obj in status_types.items():
                if hasattr(type_obj, "__name__"):
                    status_types[name] = type_obj.__name__

            for action_name, action_config in entry["class"].get("action_value_mappings", {}).items():
                type_obj = action_config.get("type")
                if hasattr(type_obj, "__name__"):
                    action_config["type"] = type_obj.__name__

        entry.pop("registry_type", None)
        entry.pop("file_path", None)

        if "class" in entry and "action_value_mappings" in entry["class"]:
            entry["class"]["action_value_mappings"] = {
                k: v
                for k, v in entry["class"]["action_value_mappings"].items()
                if not k.startswith("_execute_driver_command")
            }

        return yaml.dump(
            {device_id: entry},
            allow_unicode=True,
            default_flow_style=False,
            Dumper=NoAliasDumper,
        )


# ---------------------------------------------------------------------------
# 全局单例实例 & 构建入口
# ---------------------------------------------------------------------------

lab_registry = Registry()


def build_registry(
    registry_paths=None,
    devices_dirs=None,
    upload_registry=False,
    check_mode=False,
    complete_registry=False,
    external_only=False,
):
    """
    构建或获取Registry单例实例
    """
    logger.info("[UniLab Registry] 构建注册表实例")

    global lab_registry

    if registry_paths:
        current_paths = lab_registry.registry_paths.copy()
        for path in registry_paths:
            if path not in current_paths:
                lab_registry.registry_paths.append(path)

    lab_registry.setup(
        devices_dirs=devices_dirs,
        upload_registry=upload_registry,
        complete_registry=complete_registry,
        external_only=external_only,
    )

    # 将 AST 扫描的字符串类型替换为实际 ROS2 消息类（仅查找 ROS2 类型，不 import 设备模块）
    lab_registry.resolve_all_types()

    if check_mode:
        lab_registry.verify_and_resolve_registry()

    # noinspection PyProtectedMember
    if lab_registry._startup_executor is not None:
        # noinspection PyProtectedMember
        lab_registry._startup_executor.shutdown(wait=False)
        lab_registry._startup_executor = None

    return lab_registry
