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
    "get_enhanced_class_info",
]

from unilabos.resources.resource_tracker import PARAM_SAMPLE_UUIDS
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
        self._search_miss: set = set()

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
        if class_name in builtins.__dict__:
            return builtins.__dict__[class_name]
        if class_name in self._classes:
            return self._classes[class_name]

        cache_key = class_name.lower() if search_lower else class_name
        if cache_key in self._search_miss:
            return None

        if search_lower:
            classes = {name.lower(): obj for name, obj in self._classes.items()}
            if class_name in classes:
                return classes[class_name]

        for module_path, module in self._modules.items():
            for name, obj in inspect.getmembers(module):
                if inspect.isclass(obj) and (
                    (name.lower() == class_name.lower()) if search_lower else (name == class_name)
                ):
                    self._classes[name] = obj
                    self._classes[f"{module_path}:{name}"] = obj
                    return obj

        self._search_miss.add(cache_key)
        return None

    def get_enhanced_class_info(self, module_path: str, **_kwargs) -> Dict[str, Any]:
        """通过 AST 分析获取类的增强信息。

        复用 ``ast_registry_scanner`` 的 ``_collect_imports`` / ``_extract_class_body``，
        与 AST 扫描注册表完全一致。

        Args:
            module_path: 格式 ``"module.path:ClassName"``

        Returns:
            ``{"module_path", "ast_analysis_success", "import_map",
              "init_params", "status_methods", "action_methods"}``
        """
        from unilabos.registry.ast_registry_scanner import (
            _collect_imports,
            _extract_class_body,
            _filepath_to_module,
        )

        result: Dict[str, Any] = {
            "module_path": module_path,
            "ast_analysis_success": False,
            "import_map": {},
            "init_params": [],
            "init_docstring": None,
            "status_methods": {},
            "action_methods": {},
        }

        module_name, class_name = module_path.rsplit(":", 1)
        file_path = self._module_path_to_file_path(module_name)
        if not file_path or not os.path.exists(file_path):
            logger.warning(f"[ImportManager] 找不到模块文件: {module_name} -> {file_path}")
            return result

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=file_path)
        except Exception as e:
            logger.warning(f"[ImportManager] 解析文件 {file_path} 失败: {e}")
            return result

        # 推导 module dotted path → 构建 import_map
        python_path = Path(file_path)
        for sp in sorted(sys.path, key=len, reverse=True):
            try:
                Path(file_path).relative_to(sp)
                python_path = Path(sp)
                break
            except ValueError:
                continue
        module_dotted = _filepath_to_module(Path(file_path), python_path)
        import_map = _collect_imports(tree, module_dotted)
        result["import_map"] = import_map

        # 定位目标类 AST 节点
        target_class = None
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                target_class = node
                break

        if target_class is None:
            logger.warning(f"[ImportManager] 在文件 {file_path} 中找不到类 {class_name}")
            return result

        body = _extract_class_body(target_class, import_map)

        # 映射到统一字段名（与 registry.py complete_registry 消费端一致）
        result["init_params"] = body.get("init_params", [])
        result["init_docstring"] = body.get("init_docstring")
        result["status_methods"] = body.get("status_properties", {})
        result["action_methods"] = {
            k: {
                "args": v.get("params", []),
                "return_type": v.get("return_type", ""),
                "is_async": v.get("is_async", False),
                "always_free": v.get("always_free", False),
                "docstring": v.get("docstring"),
            }
            for k, v in body.get("auto_methods", {}).items()
        }
        result["ast_analysis_success"] = True
        return result

    def _analyze_method_signature(self, method, skip_unilabos_params: bool = True) -> Dict[str, Any]:
        """
        分析方法签名，提取具体的命名参数信息

        注意：此方法会跳过*args和**kwargs，只提取具体的命名参数
        这样可以确保通过**dict方式传参时的准确性

        Args:
            method: 要分析的方法
            skip_unilabos_params: 是否跳过 unilabos 系统参数（如 sample_uuids），
                                  registry 补全时为 True，JsonCommand 执行时为 False

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

            # 跳过 sample_uuids 参数（由系统自动注入，registry 补全时跳过）
            if skip_unilabos_params and param_name == PARAM_SAMPLE_UUIDS:
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
            "is_async": inspect.iscoroutinefunction(method),
        }

    def _get_return_type_from_method(self, method) -> Union[str, Tuple[str, Any]]:
        """从方法中获取返回类型"""
        signature = inspect.signature(method)
        return self._get_type_string(signature.return_annotation)

    def _get_type_string(self, annotation) -> Union[str, Tuple[str, Any]]:
        """将类型注解转换为类型字符串。

        非内建类返回 ``module:ClassName`` 全路径（如
        ``"unilabos.registry.placeholder_type:ResourceSlot"``），
        避免短名冲突；内建类型直接返回短名（如 ``"str"``、``"int"``）。
        """
        if annotation == inspect.Parameter.empty:
            return "Any"
        if annotation is None:
            return "None"
        if hasattr(annotation, "__origin__"):
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
            return "Unknown"
        annotation_str = str(annotation)
        if "typing." in annotation_str:
            return (
                annotation_str.replace("typing.", "")
                if getattr(annotation, "_name", None) is None
                else annotation._name.lower()
            )
        if hasattr(annotation, "__name__"):
            module = getattr(annotation, "__module__", None)
            if module and module != "builtins":
                return f"{module}:{annotation.__name__}"
            return annotation.__name__
        elif hasattr(annotation, "_name"):
            return annotation._name
        elif isinstance(annotation, str):
            return annotation
        else:
            return annotation_str

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


def get_enhanced_class_info(module_path: str, **kwargs) -> Dict[str, Any]:
    """获取增强的类信息的便捷函数"""
    return default_manager.get_enhanced_class_info(module_path, **kwargs)
