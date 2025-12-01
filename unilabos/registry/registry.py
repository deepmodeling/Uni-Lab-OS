import copy
import io
import os
import sys
import inspect
import importlib
from pathlib import Path
from typing import Any, Dict, List, Union, Tuple

import yaml
from unilabos_msgs.msg import Resource

from unilabos.config.config import BasicConfig
from unilabos.resources.graphio import resource_plr_to_ulab, tree_to_list
from unilabos.ros.msgs.message_converter import (
    msg_converter_manager,
    ros_action_to_json_schema,
    String,
    ros_message_to_json_schema,
)
from unilabos.utils import logger
from unilabos.utils.decorator import singleton
from unilabos.utils.import_manager import get_enhanced_class_info, get_class
from unilabos.utils.type_check import NoAliasDumper

DEFAULT_PATHS = [Path(__file__).absolute().parent]


class ROSMsgNotFound(Exception):
    pass


@singleton
class Registry:
    def __init__(self, registry_paths=None):
        import ctypes

        try:
            import unilabos_msgs
        except ImportError:
            logger.error("[UniLab Registry] unilabos_msgs模块未找到，请确保已根据官方文档安装unilabos_msgs包。")
            sys.exit(1)
        try:
            ctypes.CDLL(str(Path(unilabos_msgs.__file__).parent / "unilabos_msgs_s__rosidl_typesupport_c.pyd"))
        except OSError as e:
            pass

        self.registry_paths = DEFAULT_PATHS.copy()  # 使用copy避免修改默认值
        if registry_paths:
            self.registry_paths.extend(registry_paths)
        self.ResourceCreateFromOuter = self._replace_type_with_class(
            "ResourceCreateFromOuter", "host_node", f"动作 create_resource_detailed"
        )
        self.ResourceCreateFromOuterEasy = self._replace_type_with_class(
            "ResourceCreateFromOuterEasy", "host_node", f"动作 create_resource"
        )
        self.EmptyIn = self._replace_type_with_class("EmptyIn", "host_node", f"")
        self.StrSingleInput = self._replace_type_with_class("StrSingleInput", "host_node", f"")
        self.device_type_registry = {}
        self.device_module_to_registry = {}
        self.resource_type_registry = {}
        self._setup_called = False  # 跟踪setup是否已调用
        # 其他状态变量
        # self.is_host_mode = False  # 移至BasicConfig中

    def setup(self, complete_registry=False, upload_registry=False):
        # 检查是否已调用过setup
        if self._setup_called:
            logger.critical("[UniLab Registry] setup方法已被调用过，不允许多次调用")
            return

        from unilabos.app.web.utils.action_utils import get_yaml_from_goal_type

        self.device_type_registry.update(
            {
                "host_node": {
                    "description": "UniLabOS主机节点",
                    "class": {
                        "module": "unilabos.ros.nodes.presets.host_node",
                        "type": "python",
                        "status_types": {},
                        "action_value_mappings": {
                            "create_resource_detailed": {
                                "type": self.ResourceCreateFromOuter,
                                "goal": {
                                    "resources": "resources",
                                    "device_ids": "device_ids",
                                    "bind_parent_ids": "bind_parent_ids",
                                    "bind_locations": "bind_locations",
                                    "other_calling_params": "other_calling_params",
                                },
                                "feedback": {},
                                "result": {"success": "success"},
                                "schema": ros_action_to_json_schema(
                                    self.ResourceCreateFromOuter, "用于创建或更新物料资源，每次传入多个物料信息。"
                                ),
                                "goal_default": yaml.safe_load(
                                    io.StringIO(get_yaml_from_goal_type(self.ResourceCreateFromOuter.Goal))
                                ),
                                "handles": {},
                            },
                            "create_resource": {
                                "type": self.ResourceCreateFromOuterEasy,
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
                                "schema": ros_action_to_json_schema(
                                    self.ResourceCreateFromOuterEasy, "用于创建或更新物料资源，每次传入一个物料信息。"
                                ),
                                "goal_default": yaml.safe_load(
                                    io.StringIO(get_yaml_from_goal_type(self.ResourceCreateFromOuterEasy.Goal))
                                ),
                                "handles": {
                                    "output": [
                                        {
                                            "handler_key": "labware",
                                            "label": "Labware",
                                            "data_type": "resource",
                                            "data_source": "handle",
                                            "data_key": "liquid",
                                        }
                                    ]
                                },
                                "placeholder_keys": {
                                    "res_id": "unilabos_resources",  # 将当前实验室的全部物料id作为下拉框可选择
                                    "device_id": "unilabos_devices",  # 将当前实验室的全部设备id作为下拉框可选择
                                    "parent": "unilabos_nodes",  # 将当前实验室的设备/物料作为下拉框可选择
                                },
                            },
                            "test_latency": {
                                "type": self.EmptyIn,
                                "goal": {},
                                "feedback": {},
                                "result": {},
                                "schema": ros_action_to_json_schema(
                                    self.EmptyIn, "用于测试延迟的动作，返回延迟时间和时间差。"
                                ),
                                "goal_default": {},
                                "handles": {},
                            },
                            "auto-test_resource": {
                                "type": "UniLabJsonCommand",
                                "goal": {},
                                "feedback": {},
                                "result": {},
                                "schema": {
                                    "description": "",
                                    "properties": {
                                        "feedback": {},
                                        "goal": {
                                            "properties": {
                                                "resource": ros_message_to_json_schema(Resource, "resource"),
                                                "resources": {
                                                    "items": {
                                                        "properties": ros_message_to_json_schema(
                                                            Resource, "resources"
                                                        ),
                                                        "type": "object",
                                                    },
                                                    "type": "array",
                                                },
                                                "device": {"type": "string"},
                                                "devices": {"items": {"type": "string"}, "type": "array"},
                                            },
                                            "type": "object",
                                        },
                                        "result": {},
                                    },
                                    "title": "test_resource",
                                    "type": "object",
                                },
                                "placeholder_keys": {
                                    "device": "unilabos_devices",
                                    "devices": "unilabos_devices",
                                    "resource": "unilabos_resources",
                                    "resources": "unilabos_resources",
                                },
                                "goal_default": {},
                                "handles": {},
                            },
                        },
                    },
                    "version": "1.0.0",
                    "category": [],
                    "config_info": [],
                    "icon": "icon_device.webp",
                    "registry_type": "device",
                    "handles": [],  # virtue采用了不同的handle
                    "init_param_schema": {},
                    "file_path": "/",
                }
            }
        )
        # 为host_node添加内置的驱动命令动作
        self._add_builtin_actions(self.device_type_registry["host_node"], "host_node")
        logger.trace(f"[UniLab Registry] ----------Setup----------")
        self.registry_paths = [Path(path).absolute() for path in self.registry_paths]
        for i, path in enumerate(self.registry_paths):
            sys_path = path.parent
            logger.trace(f"[UniLab Registry] Path {i+1}/{len(self.registry_paths)}: {sys_path}")
            sys.path.append(str(sys_path))
            self.load_device_types(path, complete_registry)
            if BasicConfig.enable_resource_load:
                self.load_resource_types(path, complete_registry, upload_registry)
            else:
                logger.warning("跳过了资源注册表加载！")
        logger.info("[UniLab Registry] 注册表设置完成")
        # 标记setup已被调用
        self._setup_called = True

    def load_resource_types(self, path: os.PathLike, complete_registry: bool, upload_registry: bool):
        abs_path = Path(path).absolute()
        resource_path = abs_path / "resources"
        files = list(resource_path.glob("*/*.yaml"))
        logger.debug(f"[UniLab Registry] resources: {resource_path.exists()}, total: {len(files)}")
        current_resource_number = len(self.resource_type_registry) + 1
        for i, file in enumerate(files):
            with open(file, encoding="utf-8", mode="r") as f:
                data = yaml.safe_load(io.StringIO(f.read()))
            complete_data = {}
            if data:
                # 为每个资源添加文件路径信息
                for resource_id, resource_info in data.items():
                    if "version" not in resource_info:
                        resource_info["version"] = "1.0.0"
                    if "category" not in resource_info:
                        resource_info["category"] = [file.stem]
                    elif file.stem not in resource_info["category"]:
                        resource_info["category"].append(file.stem)
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
                    complete_data[resource_id] = copy.deepcopy(dict(sorted(resource_info.items())))
                    if upload_registry:
                        class_info = resource_info.get("class", {})
                        if len(class_info) and "module" in class_info:
                            if class_info.get("type") == "pylabrobot":
                                res_class = get_class(class_info["module"])
                                if callable(res_class) and not isinstance(
                                    res_class, type
                                ):  # 有的是类，有的是函数，这里暂时只登记函数类的
                                    res_instance = res_class(res_class.__name__)
                                    res_ulr = tree_to_list([resource_plr_to_ulab(res_instance)])
                                    resource_info["config_info"] = res_ulr
                    resource_info["registry_type"] = "resource"
                    resource_info["file_path"] = str(file.absolute()).replace("\\", "/")
                complete_data = dict(sorted(complete_data.items()))
                complete_data = copy.deepcopy(complete_data)
                if complete_registry:
                    with open(file, "w", encoding="utf-8") as f:
                        yaml.dump(complete_data, f, allow_unicode=True, default_flow_style=False, Dumper=NoAliasDumper)

                self.resource_type_registry.update(data)
                logger.trace(  # type: ignore
                    f"[UniLab Registry] Resource-{current_resource_number} File-{i+1}/{len(files)} "
                    + f"Add {list(data.keys())}"
                )
                current_resource_number += 1
            else:
                logger.debug(f"[UniLab Registry] Res File-{i+1}/{len(files)} Not Valid YAML File: {file.absolute()}")

    def _extract_class_docstrings(self, module_string: str) -> Dict[str, str]:
        """
        从模块字符串中提取类和方法的docstring信息

        Args:
            module_string: 模块字符串，格式为 "module.path:ClassName"

        Returns:
            包含类和方法docstring信息的字典
        """
        docstrings = {"class_docstring": "", "methods": {}}

        if not module_string or ":" not in module_string:
            return docstrings

        try:
            module_path, class_name = module_string.split(":", 1)

            # 动态导入模块
            module = importlib.import_module(module_path)

            # 获取类
            if hasattr(module, class_name):
                cls = getattr(module, class_name)

                # 获取类的docstring
                class_doc = inspect.getdoc(cls)
                if class_doc:
                    docstrings["class_docstring"] = class_doc.strip()

                # 获取所有方法的docstring
                for method_name, method in inspect.getmembers(cls, predicate=inspect.isfunction):
                    method_doc = inspect.getdoc(method)
                    if method_doc:
                        docstrings["methods"][method_name] = method_doc.strip()

                # 也获取属性方法的docstring
                for method_name, method in inspect.getmembers(cls, predicate=lambda x: isinstance(x, property)):
                    if hasattr(method, "fget") and method.fget:
                        method_doc = inspect.getdoc(method.fget)
                        if method_doc:
                            docstrings["methods"][method_name] = method_doc.strip()

        except Exception as e:
            logger.warning(f"[UniLab Registry] 无法提取docstring信息，模块: {module_string}, 错误: {str(e)}")

        return docstrings

    def _replace_type_with_class(self, type_name: str, device_id: str, field_name: str) -> Any:
        """
        将类型名称替换为实际的类对象

        Args:
            type_name: 类型名称
            device_id: 设备ID，用于错误信息
            field_name: 字段名称，用于错误信息

        Returns:
            找到的类对象或原始字符串

        Raises:
            SystemExit: 如果找不到类型则终止程序
        """
        # 如果类型名为空，跳过替换
        if not type_name or type_name == "":
            logger.warning(f"[UniLab Registry] 设备 {device_id} 的 {field_name} 类型为空，跳过替换")
            return type_name
        convert_manager = {  # 将python基本对象转为ros2基本对象
            "str": "String",
            "bool": "Bool",
            "int": "Int64",
            "float": "Float64",
        }
        type_name = convert_manager.get(type_name, type_name)  # 替换为ROS2类型
        if ":" in type_name:
            type_class = msg_converter_manager.get_class(type_name)
        else:
            type_class = msg_converter_manager.search_class(type_name)
        if type_class:
            return type_class
        else:
            logger.error(f"[UniLab Registry] 无法找到类型 '{type_name}' 用于设备 {device_id} 的 {field_name}")
            raise ROSMsgNotFound(f"类型 '{type_name}' 未找到，用于设备 {device_id} 的 {field_name}")

    def _get_json_schema_type(self, type_str: str) -> str:
        """
        根据类型字符串返回对应的JSON Schema类型

        Args:
            type_str: 类型字符串

        Returns:
            JSON Schema类型字符串
        """
        type_lower = type_str.lower()
        type_mapping = {
            ("str", "string"): "string",
            ("int", "integer"): "integer",
            ("float", "number"): "number",
            ("bool", "boolean"): "boolean",
            ("list", "array"): "array",
            ("dict", "object"): "object",
        }

        # 遍历映射找到匹配的类型
        for type_variants, json_type in type_mapping.items():
            if type_lower in type_variants:
                return json_type

        # 特殊处理包含冒号的类型（如ROS消息类型）
        if ":" in type_lower:
            return "object"

        # 默认返回字符串类型
        return "string"

    def _generate_schema_from_info(
        self,
        param_name: str,
        param_type: Union[str, Tuple[str]],
        param_default: Any,
    ) -> Dict[str, Any]:
        """
        根据参数信息生成JSON Schema
        """
        prop_schema = {}

        # 处理嵌套类型（Tuple[str]）
        if isinstance(param_type, tuple):
            if len(param_type) == 2:
                outer_type, inner_type = param_type
                outer_json_type = self._get_json_schema_type(outer_type)
                inner_json_type = self._get_json_schema_type(inner_type)

                prop_schema["type"] = outer_json_type

                # 根据外层类型设置内层类型信息
                if outer_json_type == "array":
                    prop_schema["items"] = {"type": inner_json_type}
                elif outer_json_type == "object":
                    prop_schema["additionalProperties"] = {"type": inner_json_type}
            else:
                # 不是标准的嵌套类型，默认为字符串
                prop_schema["type"] = "string"
        else:
            # 处理非嵌套类型
            if param_type:
                prop_schema["type"] = self._get_json_schema_type(param_type)
            else:
                # 如果没有类型信息，默认为字符串
                prop_schema["type"] = "string"

        # 设置默认值
        if param_default is not None:
            prop_schema["default"] = param_default

        return prop_schema

    def _generate_status_types_schema(self, status_types: Dict[str, Any]) -> Dict[str, Any]:
        """
        根据状态类型生成JSON Schema
        """
        status_schema = {
            "type": "object",
            "properties": {},
            "required": [],
        }
        for status_name, status_type in status_types.items():
            status_schema["properties"][status_name] = self._generate_schema_from_info(
                status_name, status_type["return_type"], None
            )
            status_schema["required"].append(status_name)
        return status_schema

    def _generate_unilab_json_command_schema(
        self, method_args: List[Dict[str, Any]], method_name: str, return_annotation: Any = None
    ) -> Dict[str, Any]:
        """
        根据UniLabJsonCommand方法信息生成JSON Schema，暂不支持嵌套类型

        Args:
            method_args: 方法信息字典，包含args等
            method_name: 方法名称
            return_annotation: 返回类型注解，用于生成result schema（仅支持TypedDict）

        Returns:
            JSON Schema格式的参数schema
        """
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
            if param_type == "unilabos.registry.placeholder_type:ResourceSlot":
                schema["properties"][param_name] = ros_message_to_json_schema(Resource, param_name)
            elif param_type == ("list", "unilabos.registry.placeholder_type:ResourceSlot"):
                schema["properties"][param_name] = {
                    "items": ros_message_to_json_schema(Resource, param_name),
                    "type": "array",
                }
            else:
                schema["properties"][param_name] = self._generate_schema_from_info(
                    param_name, param_type, param_default
                )
            if param_required:
                schema["required"].append(param_name)

        # 生成result schema（仅当return_annotation是TypedDict时）
        result_schema = {}
        if return_annotation is not None and self._is_typed_dict(return_annotation):
            result_schema = self._generate_typed_dict_result_schema(return_annotation)

        return {
            "title": f"{method_name}参数",
            "description": f"",
            "type": "object",
            "properties": {"goal": schema, "feedback": {}, "result": result_schema},
            "required": ["goal"],
        }

    def _is_typed_dict(self, annotation: Any) -> bool:
        """
        检查类型注解是否是TypedDict

        Args:
            annotation: 类型注解对象

        Returns:
            是否为TypedDict
        """
        if annotation is None or annotation == inspect.Parameter.empty:
            return False

        # 使用 typing_extensions.is_typeddict 进行检查（Python < 3.12 兼容）
        try:
            from typing_extensions import is_typeddict

            return is_typeddict(annotation)
        except ImportError:
            # 回退方案：检查 TypedDict 特有的属性
            if isinstance(annotation, type):
                return hasattr(annotation, "__required_keys__") and hasattr(annotation, "__optional_keys__")
            return False

    def _generate_typed_dict_result_schema(self, return_annotation: Any) -> Dict[str, Any]:
        """
        根据TypedDict类型生成result的JSON Schema

        Args:
            return_annotation: TypedDict类型注解

        Returns:
            JSON Schema格式的result schema
        """
        if not self._is_typed_dict(return_annotation):
            return {}

        try:
            from msgcenterpy.instances.typed_dict_instance import TypedDictMessageInstance

            result_schema = TypedDictMessageInstance.get_json_schema_from_typed_dict(return_annotation)
            return result_schema
        except ImportError:
            logger.warning("[UniLab Registry] msgcenterpy未安装，无法生成TypedDict的result schema")
            return {}
        except Exception as e:
            logger.warning(f"[UniLab Registry] 生成TypedDict result schema失败: {e}")
            return {}

    def _add_builtin_actions(self, device_config: Dict[str, Any], device_id: str):
        """
        为设备配置添加内置的执行驱动命令动作

        Args:
            device_config: 设备配置字典
            device_id: 设备ID
        """
        from unilabos.app.web.utils.action_utils import get_yaml_from_goal_type

        if "class" not in device_config:
            return

        if "action_value_mappings" not in device_config["class"]:
            device_config["class"]["action_value_mappings"] = {}

        for additional_action in ["_execute_driver_command", "_execute_driver_command_async"]:
            device_config["class"]["action_value_mappings"][additional_action] = {
                "type": self._replace_type_with_class("StrSingleInput", device_id, f"动作 {additional_action}"),
                "goal": {"string": "string"},
                "feedback": {},
                "result": {},
                "schema": ros_action_to_json_schema(
                    self._replace_type_with_class("StrSingleInput", device_id, f"动作 {additional_action}")
                ),
                "goal_default": yaml.safe_load(
                    io.StringIO(
                        get_yaml_from_goal_type(
                            self._replace_type_with_class(
                                "StrSingleInput", device_id, f"动作 {additional_action}"
                            ).Goal
                        )
                    )
                ),
                "handles": {},
            }

    def load_device_types(self, path: os.PathLike, complete_registry: bool):
        # return
        abs_path = Path(path).absolute()
        devices_path = abs_path / "devices"
        device_comms_path = abs_path / "device_comms"
        files = list(devices_path.glob("*.yaml")) + list(device_comms_path.glob("*.yaml"))
        logger.trace(  # type: ignore
            f"[UniLab Registry] devices: {devices_path.exists()}, device_comms: {device_comms_path.exists()}, "
            + f"total: {len(files)}"
        )
        current_device_number = len(self.device_type_registry) + 1
        from unilabos.app.web.utils.action_utils import get_yaml_from_goal_type

        for i, file in enumerate(files):
            with open(file, encoding="utf-8", mode="r") as f:
                data = yaml.safe_load(io.StringIO(f.read()))
            complete_data = {}
            action_str_type_mapping = {
                "UniLabJsonCommand": "UniLabJsonCommand",
                "UniLabJsonCommandAsync": "UniLabJsonCommandAsync",
            }
            status_str_type_mapping = {}
            if data:
                # 在添加到注册表前处理类型替换
                for device_id, device_config in data.items():
                    # 添加文件路径信息 - 使用规范化的完整文件路径
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
                        if (
                            "status_types" not in device_config["class"]
                            or device_config["class"]["status_types"] is None
                        ):
                            device_config["class"]["status_types"] = {}
                        if (
                            "action_value_mappings" not in device_config["class"]
                            or device_config["class"]["action_value_mappings"] is None
                        ):
                            device_config["class"]["action_value_mappings"] = {}
                        enhanced_info = {}
                        if complete_registry:
                            device_config["class"]["status_types"].clear()
                            enhanced_info = get_enhanced_class_info(device_config["class"]["module"], use_dynamic=True)
                            if not enhanced_info.get("dynamic_import_success", False):
                                continue
                            device_config["class"]["status_types"].update(
                                {k: v["return_type"] for k, v in enhanced_info["status_methods"].items()}
                            )
                        for status_name, status_type in device_config["class"]["status_types"].items():
                            if isinstance(status_type, tuple) or status_type in ["Any", "None", "Unknown"]:
                                status_type = "String"  # 替换成ROS的String，便于显示
                                device_config["class"]["status_types"][status_name] = status_type
                            try:
                                target_type = self._replace_type_with_class(
                                    status_type, device_id, f"状态 {status_name}"
                                )
                            except ROSMsgNotFound:
                                continue
                            if target_type in [
                                dict,
                                list,
                            ]:  # 对于嵌套类型返回的对象，暂时处理成字符串，无法直接进行转换
                                target_type = String
                            status_str_type_mapping[status_type] = target_type
                        device_config["class"]["status_types"] = dict(
                            sorted(device_config["class"]["status_types"].items())
                        )
                        if complete_registry:
                            # 保存原有的description信息
                            old_descriptions = {}
                            for action_name, action_config in device_config["class"]["action_value_mappings"].items():
                                if "description" in action_config.get("schema", {}):
                                    description = action_config["schema"]["description"]
                                    if len(description):
                                        old_descriptions[action_name] = action_config["schema"]["description"]

                            device_config["class"]["action_value_mappings"] = {
                                k: v
                                for k, v in device_config["class"]["action_value_mappings"].items()
                                if not k.startswith("auto-")
                            }
                            # 处理动作值映射
                            device_config["class"]["action_value_mappings"].update(
                                {
                                    f"auto-{k}": {
                                        "type": "UniLabJsonCommandAsync" if v["is_async"] else "UniLabJsonCommand",
                                        "goal": {},
                                        "feedback": {},
                                        "result": {},
                                        "schema": self._generate_unilab_json_command_schema(
                                            v["args"], k, v.get("return_annotation")
                                        ),
                                        "goal_default": {i["name"]: i["default"] for i in v["args"]},
                                        "handles": [],
                                        "placeholder_keys": {
                                            i["name"]: (
                                                "unilabos_resources"
                                                if i["type"] == "unilabos.registry.placeholder_type:ResourceSlot"
                                                or i["type"]
                                                == ("list", "unilabos.registry.placeholder_type:ResourceSlot")
                                                else "unilabos_devices"
                                            )
                                            for i in v["args"]
                                            if i.get("type", "")
                                            in [
                                                "unilabos.registry.placeholder_type:ResourceSlot",
                                                "unilabos.registry.placeholder_type:DeviceSlot",
                                                ("list", "unilabos.registry.placeholder_type:ResourceSlot"),
                                                ("list", "unilabos.registry.placeholder_type:DeviceSlot"),
                                            ]
                                        },
                                    }
                                    # 不生成已配置action的动作
                                    for k, v in enhanced_info["action_methods"].items()
                                    if k not in device_config["class"]["action_value_mappings"]
                                }
                            )
                            # 恢复原有的description信息（auto开头的不修改）
                            for action_name, description in old_descriptions.items():
                                if action_name in device_config["class"]["action_value_mappings"]:  # 有一些会被删除
                                    device_config["class"]["action_value_mappings"][action_name]["schema"][
                                        "description"
                                    ] = description
                            device_config["init_param_schema"] = {}
                            device_config["init_param_schema"]["config"] = self._generate_unilab_json_command_schema(
                                enhanced_info["init_params"], "__init__"
                            )["properties"]["goal"]
                            device_config["init_param_schema"]["data"] = self._generate_status_types_schema(
                                enhanced_info["status_methods"]
                            )

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
                                # 通过Json发放指令，而不是通过特殊的ros action进行处理
                                if not action_type_str.startswith("UniLabJsonCommand"):
                                    try:
                                        target_type = self._replace_type_with_class(
                                            action_type_str, device_id, f"动作 {action_name}"
                                        )
                                    except ROSMsgNotFound:
                                        continue
                                    action_str_type_mapping[action_type_str] = target_type
                                    if target_type is not None:
                                        action_config["goal_default"] = yaml.safe_load(
                                            io.StringIO(get_yaml_from_goal_type(target_type.Goal))
                                        )
                                        action_config["schema"] = ros_action_to_json_schema(target_type)
                                    else:
                                        logger.warning(
                                            f"[UniLab Registry] 设备 {device_id} 的动作 {action_name} 类型为空，跳过替换"
                                        )
                        complete_data[device_id] = copy.deepcopy(dict(sorted(device_config.items())))  # 稍后dump到文件
                        for status_name, status_type in device_config["class"]["status_types"].items():
                            device_config["class"]["status_types"][status_name] = status_str_type_mapping[status_type]
                        for action_name, action_config in device_config["class"]["action_value_mappings"].items():
                            if action_config["type"] not in action_str_type_mapping:
                                continue
                            action_config["type"] = action_str_type_mapping[action_config["type"]]
                        # 添加内置的驱动命令动作
                        self._add_builtin_actions(device_config, device_id)
                    device_config["file_path"] = str(file.absolute()).replace("\\", "/")
                    device_config["registry_type"] = "device"
                    logger.trace(  # type: ignore
                        f"[UniLab Registry] Device-{current_device_number} File-{i+1}/{len(files)} Add {device_id} "
                        + f"[{data[device_id].get('name', '未命名设备')}]"
                    )
                    current_device_number += 1
                complete_data = dict(sorted(complete_data.items()))
                complete_data = copy.deepcopy(complete_data)
                with open(file, "w", encoding="utf-8") as f:
                    yaml.dump(complete_data, f, allow_unicode=True, default_flow_style=False, Dumper=NoAliasDumper)
                self.device_type_registry.update(data)
            else:
                logger.debug(
                    f"[UniLab Registry] Device File-{i+1}/{len(files)} Not Valid YAML File: {file.absolute()}"
                )

    def obtain_registry_device_info(self):
        devices = []
        for device_id, device_info in self.device_type_registry.items():
            device_info_copy = copy.deepcopy(device_info)
            if "class" in device_info_copy and "action_value_mappings" in device_info_copy["class"]:
                action_mappings = device_info_copy["class"]["action_value_mappings"]
                # 过滤掉内置的驱动命令动作
                builtin_actions = ["_execute_driver_command", "_execute_driver_command_async"]
                filtered_action_mappings = {
                    action_name: action_config
                    for action_name, action_config in action_mappings.items()
                    if action_name not in builtin_actions
                }
                device_info_copy["class"]["action_value_mappings"] = filtered_action_mappings

                for action_name, action_config in filtered_action_mappings.items():
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

            msg = {"id": device_id, **device_info_copy}
            devices.append(msg)
        return devices

    def obtain_registry_resource_info(self):
        resources = []
        for resource_id, resource_info in self.resource_type_registry.items():
            msg = {"id": resource_id, **resource_info}
            resources.append(msg)
        return resources


# 全局单例实例
lab_registry = Registry()


def build_registry(registry_paths=None, complete_registry=False, upload_registry=False):
    """
    构建或获取Registry单例实例

    Args:
        registry_paths: 额外的注册表路径列表

    Returns:
        Registry实例
    """
    logger.info("[UniLab Registry] 构建注册表实例")

    # 由于使用了单例，这里不需要重新创建实例
    global lab_registry

    # 如果有额外路径，添加到registry_paths
    if registry_paths:
        current_paths = lab_registry.registry_paths.copy()
        # 检查是否有新路径需要添加
        for path in registry_paths:
            if path not in current_paths:
                lab_registry.registry_paths.append(path)

    # 初始化注册表
    lab_registry.setup(complete_registry, upload_registry)

    return lab_registry
