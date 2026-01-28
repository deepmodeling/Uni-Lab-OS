"""
导入管理器

该模块提供了一个动态导入和管理模块的系统，避免误删未使用的导入。
"""

import builtins
import importlib
import inspect
import sys
import traceback
import ast
import os
from pathlib import Path
from typing import Dict, List, Any, Optional, Callable, Type, Union, Tuple

__all__ = [
    "ImportManager",
    "default_manager",
    "load_module",
    "get_class",
    "get_module",
    "init_from_list",
    "get_class_info_static",
    "get_registry_class_info",
]

from ast import Constant

from unilabos.utils import logger


class ImportManager:
    """导入管理器类，用于动态加载和管理模块"""

    def __init__(self, module_list: Optional[List[str]] = None):
        """
        初始化导入管理器

        Args:
            module_list: 要预加载的模块路径列表
        """
        self._modules: Dict[str, Any] = {}
        self._classes: Dict[str, Type] = {}
        self._functions: Dict[str, Callable] = {}

        if module_list:
            for module_path in module_list:
                self.load_module(module_path)

    def load_module(self, module_path: str) -> Any:
        """
        加载指定路径的模块

        Args:
            module_path: 模块路径

        Returns:
            加载的模块对象

        Raises:
            ImportError: 如果模块导入失败
        """
        try:
            if module_path in self._modules:
                return self._modules[module_path]

            module = importlib.import_module(module_path)
            self._modules[module_path] = module

            # 索引模块中的类和函数
            for name, obj in inspect.getmembers(module):
                if inspect.isclass(obj):
                    full_name = f"{module_path}.{name}"
                    self._classes[name] = obj
                    self._classes[full_name] = obj
                elif inspect.isfunction(obj):
                    full_name = f"{module_path}.{name}"
                    self._functions[name] = obj
                    self._functions[full_name] = obj

            return module
        except Exception as e:
            logger.error(f"导入模块 '{module_path}' 时发生错误：{str(e)}")
            logger.warning(traceback.format_exc())
            raise ImportError(f"无法导入模块 {module_path}: {str(e)}")

    def get_module(self, module_path: str) -> Any:
        """
        获取已加载的模块

        Args:
            module_path: 模块路径

        Returns:
            模块对象

        Raises:
            KeyError: 如果模块未加载
        """
        if module_path not in self._modules:
            return self.load_module(module_path)
        return self._modules[module_path]

    def get_class(self, class_name: str) -> Type:
        """
        获取类对象

        Args:
            class_name: 类名或完整类路径

        Returns:
            类对象

        Raises:
            KeyError: 如果找不到类
        """
        if class_name in self._classes:
            return self._classes[class_name]

        # 尝试动态导入
        if ":" in class_name:
            module_path, cls_name = class_name.rsplit(":", 1)
            module = self.load_module(module_path)
            if hasattr(module, cls_name):
                cls = getattr(module, cls_name)
                self._classes[class_name] = cls
                self._classes[cls_name] = cls
                return cls
        else:
            # 如果cls_name是builtins中的关键字，则返回对应类
            if class_name in builtins.__dict__:
                return builtins.__dict__[class_name]

        raise KeyError(f"找不到类: {class_name}")

    def list_modules(self) -> List[str]:
        """列出所有已加载的模块路径"""
        return list(self._modules.keys())

    def list_classes(self) -> List[str]:
        """列出所有已索引的类名"""
        return list(self._classes.keys())

    def list_functions(self) -> List[str]:
        """列出所有已索引的函数名"""
        return list(self._functions.keys())

    def search_class(self, class_name: str, search_lower=False) -> Optional[Type]:
        """
        在所有已加载的模块中搜索特定类名

        Args:
            class_name: 要搜索的类名
            search_lower: 以小写搜索

        Returns:
            找到的类对象，如果未找到则返回None
        """
        # 如果cls_name是builtins中的关键字，则返回对应类
        if class_name in builtins.__dict__:
            return builtins.__dict__[class_name]
        # 首先在已索引的类中查找
        if class_name in self._classes:
            return self._classes[class_name]

        if search_lower:
            classes = {name.lower(): obj for name, obj in self._classes.items()}
            if class_name in classes:
                return classes[class_name]

        # 遍历所有已加载的模块进行搜索
        for module_path, module in self._modules.items():
            for name, obj in inspect.getmembers(module):
                if inspect.isclass(obj) and (
                    (name.lower() == class_name.lower()) if search_lower else (name == class_name)
                ):
                    # 将找到的类添加到索引中
                    self._classes[name] = obj
                    self._classes[f"{module_path}:{name}"] = obj
                    return obj

        return None

    def get_enhanced_class_info(self, module_path: str, use_dynamic: bool = True) -> Dict[str, Any]:
        """
        获取增强的类信息，支持动态导入和静态分析

        Args:
            module_path: 模块路径，格式为 "module.path" 或 "module.path:ClassName"
            use_dynamic: 是否优先使用动态导入

        Returns:
            包含详细类信息的字典
        """
        result = {
            "module_path": module_path,
            "dynamic_import_success": False,
            "static_analysis_success": False,
            "init_params": {},
            "status_methods": {},  # get_ 开头和 @property 方法
            "action_methods": {},  # set_ 开头和其他非_开头方法
        }

        # 尝试动态导入
        dynamic_info = None
        static_info = None
        if use_dynamic:
            try:
                dynamic_info = self._get_dynamic_class_info(module_path)
                result["dynamic_import_success"] = True
                logger.debug(f"[ImportManager] 动态导入类 {module_path} 成功")
            except Exception as e:
                logger.warning(
                    f"[UniLab Registry] 在补充注册表时，动态导入类 "
                    f"{module_path} 失败（将使用静态分析，"
                    f"建议修复导入错误，以实现更好的注册表识别效果！）: {e}"
                )
                use_dynamic = False
        if not use_dynamic:
            # 尝试静态分析
            try:
                static_info = self._get_static_class_info(module_path)
                result["static_analysis_success"] = True
                logger.debug(f"[ImportManager] 静态分析类 {module_path} 成功")
            except Exception as e:
                logger.warning(f"[ImportManager] 静态分析类 {module_path} 失败: {e}")

        # 合并信息（优先使用动态导入的信息）
        if dynamic_info:
            result.update(dynamic_info)
        elif static_info:
            result.update(static_info)

        return result

    def _get_dynamic_class_info(self, class_path: str) -> Dict[str, Any]:
        """使用inspect模块动态获取类信息"""
        cls = get_class(class_path)
        class_name = cls.__name__

        result = {
            "class_name": class_name,
            "init_params": self._analyze_method_signature(cls.__init__)["args"],
            "status_methods": {},
            "action_methods": {},
        }
        # 分析类的所有成员
        for name, method in cls.__dict__.items():
            if name.startswith("_"):
                continue

            # 检查是否是property
            if isinstance(method, property):
                # @property 装饰的方法
                # noinspection PyTypeChecker
                return_type = self._get_return_type_from_method(method.fget) if method.fget else "Any"
                prop_info = {
                    "name": name,
                    "return_type": return_type,
                }
                result["status_methods"][name] = prop_info

                # 检查是否有对应的setter
                if method.fset:
                    setter_info = self._analyze_method_signature(method.fset)
                    result["action_methods"][name] = setter_info

            elif inspect.ismethod(method) or inspect.isfunction(method):
                if name.startswith("get_"):
                    actual_name = name[4:]  # 去掉get_前缀
                    if actual_name in result["status_methods"]:
                        continue
                    # get_ 开头的方法归类为status
                    method_info = self._analyze_method_signature(method)
                    result["status_methods"][actual_name] = method_info
                elif not name.startswith("_"):
                    # 其他非_开头的方法归类为action
                    method_info = self._analyze_method_signature(method)
                    result["action_methods"][name] = method_info

        return result

    def _get_static_class_info(self, module_path: str) -> Dict[str, Any]:
        """使用AST静态分析获取类信息"""
        module_name, class_name = module_path.rsplit(":", 1)
        # 将模块路径转换为文件路径
        file_path = self._module_path_to_file_path(module_name)
        if not file_path or not os.path.exists(file_path):
            raise FileNotFoundError(f"找不到模块文件: {module_name} -> {file_path}")

        with open(file_path, "r", encoding="utf-8") as f:
            source_code = f.read()

        tree = ast.parse(source_code)

        # 查找目标类
        target_class = None
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                if node.name == class_name:
                    target_class = node
                    break

        if target_class is None:
            raise AttributeError(f"在文件 {file_path} 中找不到类 {class_name}")

        result = {
            "class_name": class_name,
            "init_params": {},
            "status_methods": {},
            "action_methods": {},
        }

        # 分析类的方法
        for node in target_class.body:
            if isinstance(node, ast.FunctionDef):
                method_info = self._analyze_method_node(node)
                method_name = node.name
                if method_name == "__init__":
                    result["init_params"] = method_info["args"]
                elif method_name.startswith("_"):
                    continue
                elif self._is_property_method(node):
                    # @property 装饰的方法
                    result["status_methods"][method_name] = method_info
                elif method_name.startswith("get_"):
                    # get_ 开头的方法归类为status
                    actual_name = method_name[4:]  # 去掉get_前缀
                    if actual_name not in result["status_methods"]:
                        result["status_methods"][actual_name] = method_info
                else:
                    # 其他非_开头的方法归类为action
                    result["action_methods"][method_name] = method_info
        return result

    def _analyze_method_signature(self, method) -> Dict[str, Any]:
        """
        分析方法签名，提取具体的命名参数信息

        注意：此方法会跳过*args和**kwargs，只提取具体的命名参数
        这样可以确保通过**dict方式传参时的准确性

        示例用法：
            method_info = self._analyze_method_signature(some_method)
            params = {"param1": "value1", "param2": "value2"}
            result = some_method(**params)  # 安全的参数传递
        """
        signature = inspect.signature(method)
        args = []
        num_required = 0

        for param_name, param in signature.parameters.items():
            # 跳过self参数
            if param_name == "self":
                continue

            # 跳过*args和**kwargs参数
            if param.kind == param.VAR_POSITIONAL:  # *args
                continue
            if param.kind == param.VAR_KEYWORD:  # **kwargs
                continue

            is_required = param.default == inspect.Parameter.empty
            if is_required:
                num_required += 1

            args.append(
                {
                    "name": param_name,
                    "type": self._get_type_string(param.annotation),
                    "required": is_required,
                    "default": None if param.default == inspect.Parameter.empty else param.default,
                }
            )

        return {
            "name": method.__name__,
            "args": args,
            "return_type": self._get_type_string(signature.return_annotation),
            "return_annotation": signature.return_annotation,  # 保留原始类型注解，用于TypedDict等特殊处理
            "is_async": inspect.iscoroutinefunction(method),
        }

    def _get_return_type_from_method(self, method) -> str:
        """从方法中获取返回类型"""
        signature = inspect.signature(method)
        return self._get_type_string(signature.return_annotation)

    def _get_type_string(self, annotation) -> Union[str, Tuple[str, Any]]:
        """将类型注解转换为Class Library中可搜索的类名"""
        if annotation == inspect.Parameter.empty:
            return "Any"  # 如果没有注解，返回Any
        if annotation is None:
            return "None"  # 明确的None类型
        if hasattr(annotation, "__origin__"):
            # 处理typing模块的类型
            origin = annotation.__origin__
            if origin in (list, set, tuple):
                if hasattr(annotation, "__args__") and annotation.__args__:
                    if len(annotation.__args__):
                        arg0 = annotation.__args__[0]
                        if isinstance(arg0, int):
                            return "Int64MultiArray"
                        elif isinstance(arg0, float):
                            return "Float64MultiArray"
                return "list", self._get_type_string(arg0)
            elif origin is dict:
                return "dict"
            elif origin is Optional:
                return "Unknown"
            return f"Unknown"
        annotation_str = str(annotation)
        # 处理typing模块的复杂类型
        if "typing." in annotation_str:
            # 简化typing类型显示
            return (
                annotation_str.replace("typing.", "")
                if getattr(annotation, "_name", None) is None
                else annotation._name.lower()
            )
        # 如果是类型对象
        if hasattr(annotation, "__name__"):
            # 如果是内置类型
            if annotation.__module__ == "builtins":
                return annotation.__name__
            else:
                # 如果是自定义类，返回完整路径
                return f"{annotation.__module__}:{annotation.__name__}"
        # 如果是typing模块的类型
        elif hasattr(annotation, "_name"):
            return annotation._name
        # 如果是字符串形式的类型注解
        elif isinstance(annotation, str):
            return annotation
        else:
            return annotation_str

    def _is_property_method(self, node: ast.FunctionDef) -> bool:
        """检查是否是@property装饰的方法"""
        for decorator in node.decorator_list:
            if isinstance(decorator, ast.Name) and decorator.id == "property":
                return True
        return False

    def _is_setter_method(self, node: ast.FunctionDef) -> bool:
        """检查是否是@xxx.setter装饰的方法"""
        for decorator in node.decorator_list:
            if isinstance(decorator, ast.Attribute) and decorator.attr == "setter":
                return True
        return False

    def _get_property_name_from_setter(self, node: ast.FunctionDef) -> str:
        """从setter装饰器中获取属性名"""
        for decorator in node.decorator_list:
            if isinstance(decorator, ast.Attribute) and decorator.attr == "setter":
                if isinstance(decorator.value, ast.Name):
                    return decorator.value.id
        return node.name

    def get_class_info_static(self, module_class_path: str) -> Dict[str, Any]:
        """
        静态分析获取类的方法信息，不需要实际导入模块

        Args:
            module_class_path: 格式为 "module.path:ClassName" 的字符串

        Returns:
            包含类方法信息的字典
        """
        try:
            if ":" not in module_class_path:
                raise ValueError("module_class_path必须是 'module.path:ClassName' 格式")

            module_path, class_name = module_class_path.rsplit(":", 1)

            # 将模块路径转换为文件路径
            file_path = self._module_path_to_file_path(module_path)
            if not file_path or not os.path.exists(file_path):
                logger.warning(f"找不到模块文件: {module_path} -> {file_path}")
                return {}

            # 解析源码
            with open(file_path, "r", encoding="utf-8") as f:
                source_code = f.read()

            tree = ast.parse(source_code)

            # 查找目标类
            class_node = None
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name == class_name:
                    class_node = node
                    break

            if not class_node:
                logger.warning(f"在模块 {module_path} 中找不到类 {class_name}")
                return {}

            # 分析类的方法
            methods_info = {}
            for node in class_node.body:
                if isinstance(node, ast.FunctionDef):
                    method_info = self._analyze_method_node(node)
                    methods_info[node.name] = method_info

            return {
                "class_name": class_name,
                "module_path": module_path,
                "file_path": file_path,
                "methods": methods_info,
            }

        except Exception as e:
            logger.error(f"静态分析类 {module_class_path} 时出错: {str(e)}")
            return {}

    def _module_path_to_file_path(self, module_path: str) -> Optional[str]:
        for path in sys.path:
            potential_path = Path(path) / module_path.replace(".", "/")

            # 检查是否为包
            if (potential_path / "__init__.py").exists():
                return str(potential_path / "__init__.py")

            # 检查是否为模块文件
            if (potential_path.parent / f"{potential_path.name}.py").exists():
                return str(potential_path.parent / f"{potential_path.name}.py")

        return None

    def _analyze_method_node(self, node: ast.FunctionDef) -> Dict[str, Any]:
        """分析方法节点，提取参数和返回类型信息"""
        method_info = {
            "name": node.name,
            "args": [],
            "return_type": None,
            "is_async": isinstance(node, ast.AsyncFunctionDef),
        }
        # 获取默认值列表
        defaults = node.args.defaults
        num_defaults = len(defaults)

        # 计算必需参数数量
        total_args = len(node.args.args)
        num_required = total_args - num_defaults

        # 提取参数信息
        for i, arg in enumerate(node.args.args):
            if arg.arg == "self":
                continue
            arg_info = {
                "name": arg.arg,
                "type": None,
                "default": None,
                "required": i < num_required,
            }

            # 提取类型注解
            if arg.annotation:
                arg_info["type"] = ast.unparse(arg.annotation) if hasattr(ast, "unparse") else str(arg.annotation)

            # 提取默认值并推断类型
            if i >= num_required:
                default_index = i - num_required
                if default_index < len(defaults):
                    default_value: Constant = defaults[default_index]  # type: ignore
                    assert isinstance(default_value, Constant), "暂不支持对非常量类型进行推断，可反馈开源仓库"
                    arg_info["default"] = default_value.value
                    # 如果没有类型注解，尝试从默认值推断类型
                    if not arg_info["type"]:
                        arg_info["type"] = self._get_type_string(type(arg_info["default"]))
            method_info["args"].append(arg_info)

        # 提取返回类型
        if node.returns:
            method_info["return_type"] = ast.unparse(node.returns) if hasattr(ast, "unparse") else str(node.returns)

        return method_info

    def _infer_type_from_default(self, node: ast.AST) -> Optional[str]:
        """从默认值推断参数类型"""
        if isinstance(node, ast.Constant):
            value = node.value
            if isinstance(value, bool):
                return "bool"
            elif isinstance(value, int):
                return "int"
            elif isinstance(value, float):
                return "float"
            elif isinstance(value, str):
                return "str"
            elif value is None:
                return "Optional[Any]"
        elif isinstance(node, ast.List):
            return "List"
        elif isinstance(node, ast.Dict):
            return "Dict"
        elif isinstance(node, ast.Tuple):
            return "Tuple"
        elif isinstance(node, ast.Set):
            return "Set"
        elif isinstance(node, ast.Name):
            # 常见的默认值模式
            if node.id in ["None"]:
                return "Optional[Any]"
            elif node.id in ["True", "False"]:
                return "bool"

        return None

    def _infer_types_from_docstring(self, method_info: Dict[str, Any]) -> None:
        """从docstring中推断参数类型"""
        docstring = method_info.get("docstring", "")
        if not docstring:
            return

        lines = docstring.split("\n")
        in_args_section = False

        for line in lines:
            line = line.strip()

            # 检测Args或Arguments段落
            if line.lower().startswith(("args:", "arguments:")):
                in_args_section = True
                continue
            elif line.startswith(("returns:", "return:", "yields:", "raises:")):
                in_args_section = False
                continue
            elif not line or not in_args_section:
                continue

            # 解析参数行，格式通常是: param_name (type): description 或 param_name: description
            if ":" in line:
                parts = line.split(":", 1)
                param_part = parts[0].strip()

                # 提取参数名和类型
                param_name = None
                param_type = None

                if "(" in param_part and ")" in param_part:
                    # 格式: param_name (type)
                    param_name = param_part.split("(")[0].strip()
                    type_part = param_part.split("(")[1].split(")")[0].strip()
                    param_type = type_part
                else:
                    # 格式: param_name
                    param_name = param_part

                # 更新对应参数的类型信息
                if param_name:
                    for arg_info in method_info["args"]:
                        if arg_info["name"] == param_name and not arg_info["type"]:
                            if param_type:
                                arg_info["inferred_type"] = param_type
                            elif not arg_info["inferred_type"]:
                                # 从描述中推断类型
                                description = parts[1].strip().lower()
                                if any(word in description for word in ["path", "file", "directory", "filename"]):
                                    arg_info["inferred_type"] = "str"
                                elif any(
                                    word in description for word in ["port", "number", "count", "size", "length"]
                                ):
                                    arg_info["inferred_type"] = "int"
                                elif any(
                                    word in description for word in ["rate", "ratio", "percentage", "temperature"]
                                ):
                                    arg_info["inferred_type"] = "float"
                                elif any(word in description for word in ["flag", "enable", "disable", "option"]):
                                    arg_info["inferred_type"] = "bool"

    def get_registry_class_info(self, module_class_path: str) -> Dict[str, Any]:
        """
        获取适用于注册表的类信息，包含完整的类型推断

        Args:
            module_class_path: 格式为 "module.path:ClassName" 的字符串

        Returns:
            适用于注册表的类信息字典
        """
        class_info = self.get_class_info_static(module_class_path)
        if not class_info:
            return {}

        registry_info = {
            "class_name": class_info["class_name"],
            "module_path": class_info["module_path"],
            "file_path": class_info["file_path"],
            "methods": {},
            "properties": [],
            "init_params": {},
            "action_methods": {},
        }

        for method_name, method_info in class_info["methods"].items():
            # 分类处理不同类型的方法
            if method_info["is_property"]:
                registry_info["properties"].append(
                    {
                        "name": method_name,
                        "return_type": method_info.get("return_type"),
                        "docstring": method_info.get("docstring"),
                    }
                )
            elif method_name == "__init__":
                # 处理初始化参数
                init_params = {}
                for arg in method_info["args"]:
                    if arg["name"] != "self":
                        param_info = {
                            "name": arg["name"],
                            "type": arg.get("type") or arg.get("inferred_type"),
                            "required": arg.get("is_required", True),
                            "default": arg.get("default"),
                        }
                        init_params[arg["name"]] = param_info
                registry_info["init_params"] = init_params
            elif not method_name.startswith("_"):
                # 处理公共方法（可能的action方法）
                action_info = {
                    "name": method_name,
                    "params": {},
                    "return_type": method_info.get("return_type"),
                    "docstring": method_info.get("docstring"),
                    "num_required": method_info.get("num_required", 0) - 1,  # 减去self
                    "num_defaults": method_info.get("num_defaults", 0),
                }

                for arg in method_info["args"]:
                    if arg["name"] != "self":
                        param_info = {
                            "name": arg["name"],
                            "type": arg.get("type") or arg.get("inferred_type"),
                            "required": arg.get("is_required", True),
                            "default": arg.get("default"),
                        }
                        action_info["params"][arg["name"]] = param_info

                registry_info["action_methods"][method_name] = action_info

        return registry_info


# 全局实例，便于直接使用
default_manager = ImportManager()


def load_module(module_path: str) -> Any:
    """加载模块的便捷函数"""
    return default_manager.load_module(module_path)


def get_class(class_name: str) -> Type:
    """获取类的便捷函数"""
    return default_manager.get_class(class_name)


def get_module(module_path: str) -> Any:
    """获取模块的便捷函数"""
    return default_manager.get_module(module_path)


def init_from_list(module_list: List[str]) -> None:
    """从模块列表初始化默认管理器"""
    global default_manager
    default_manager = ImportManager(module_list)


def get_class_info_static(module_class_path: str) -> Dict[str, Any]:
    """静态分析获取类信息的便捷函数"""
    return default_manager.get_class_info_static(module_class_path)


def get_registry_class_info(module_class_path: str) -> Dict[str, Any]:
    """获取适用于注册表的类信息的便捷函数"""
    return default_manager.get_registry_class_info(module_class_path)


def get_enhanced_class_info(module_path: str, use_dynamic: bool = True) -> Dict[str, Any]:
    """获取增强的类信息的便捷函数"""
    return default_manager.get_enhanced_class_info(module_path, use_dynamic)
