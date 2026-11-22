"""PLR 附加可序列化类的可插拔注册钩子。

PLR 反序列化（``Resource.deserialize`` / ``LiquidHandler.deserialize`` 等）按
``type`` 字段在**本进程已导入**的子类中查找类（``find_subclass``）。框架内置之外的
PLR 资源类、后端类必须在反序列化前被导入，否则报"找不到类型"。

框架在 :meth:`ResourceTreeSet.to_plr_resources` 和构造 PyLabRobot 风格驱动之前调用
:func:`register`，把登记过的模块全部导入（幂等，只导入一次）。反序列化本身只按 ``type``
（物料的 ``config.type``）找类，登记只负责让类所在的模块先 import 进来。

两种登记方式：

1. 主动登记：外部驱动包在包导入时（如 ``__init__.py``）登记自己的资源/后端模块，可只写模块路径，
   从而延迟导入重模块::

       from unilabos.resources.plr_additional_res_reg import register_plr_module

       register_plr_module("my_driver_pkg.labware", "my_driver_pkg.backends")

   已导入的类也可直接登记（记录其所在模块，可用作类装饰器）::

       from unilabos.resources.plr_additional_res_reg import register_plr_class

       @register_plr_class
       class MyDeck(Deck): ...

2. 按注册表自动登记（:func:`register_registry_resources`）：物料的 ``class`` 是初始化它用的注册表名，
   注册表条目的 ``class.module``（``pkg.labware:make_plate``）多半指向模块里的工厂函数，函数返回的类
   定义在（或 import 进）这个模块。登记模块部分（``pkg.labware``）并 import，类就进了 PLR 的子类表。
   ``to_plr_resources`` 反序列化之前对树里出现的注册表名自动做这一步，不必手写。

不在注册表里、也不在注册表条目模块里的类（如单独文件里的自定义 Deck 子类、液体处理后端）用方式 1。

内置的 ``WorkStationContainer``、``UniLiquidHandlerRvizBackend`` 与 ``unilabos.resources.presets``
里的预设类（``RegularContainer``、``Bottle``、``ItemizedCarrier``、``BottleCarrier``、``WareHouse``）
同样经本钩子登记：新进程里没有代码先 import 它们时也能反序列化。
"""

from __future__ import annotations

import importlib
import threading
from typing import Tuple, TypeVar

from unilabos.utils.log import logger

T = TypeVar("T", bound=type)

# 框架内置、反序列化时需要可见的 PLR 类所在模块
_BUILTIN_PLR_MODULES: Tuple[str, ...] = (
    "unilabos.devices.workstation.workstation_base",  # WorkStationContainer
    "unilabos.devices.liquid_handling.rviz_backend",  # UniLiquidHandlerRvizBackend
    "unilabos.resources.presets.container",  # RegularContainer
    "unilabos.resources.presets.itemized_carrier",  # Bottle、ItemizedCarrier、BottleCarrier
    "unilabos.resources.presets.warehouse",  # WareHouse
)

_lock = threading.Lock()
_import_lock = threading.RLock()
# 登记顺序即导入顺序；dict 用作有序集合
_registered: dict[str, None] = dict.fromkeys(_BUILTIN_PLR_MODULES)
# 已处理（导入成功或已记录失败）的模块，register() 不再重复尝试
_processed: set[str] = set()


def register_plr_module(*module_paths: str) -> None:
    """登记包含 PLR 可序列化类的模块；下一次 :func:`register` 时导入。"""

    with _lock:
        for module_path in module_paths:
            if not module_path or not isinstance(module_path, str):
                raise ValueError(f"PLR 模块路径必须是非空字符串: {module_path!r}")
            _registered.setdefault(module_path, None)


def register_plr_class(cls: T) -> T:
    """登记一个 PLR 类（记录其模块），原样返回，可作类装饰器。"""

    register_plr_module(cls.__module__)
    return cls


def register_registry_resources(*names: str) -> Tuple[str, ...]:
    """按注册表资源名登记实现模块（下一次 :func:`register` 时导入），返回这次新登记的模块。

    名字是物料的 ``class``（初始化它用的注册表名）或模板名；查到条目后取 ``class.module`` 的
    模块部分——条目多半是模块里的工厂函数，import 函数所在的文件，它返回的类随之登记。
    不是注册表资源名（如孔位的 PLR 类名）、条目没有声明实现的名字跳过。
    """

    from unilabos.resources.material_class import registry_resource_module

    added: list[str] = []
    for name in dict.fromkeys(str(item or "").strip() for item in names):
        module_path = registry_resource_module(name) if name else None
        if module_path is None:
            continue
        module = module_path.partition(":")[0]
        with _lock:
            if module in _registered:
                continue
            _registered[module] = None
        added.append(module)
    return tuple(added)


def registered_plr_modules() -> Tuple[str, ...]:
    """当前登记的全部模块（含内置），按登记顺序。"""

    with _lock:
        return tuple(_registered)


def register() -> None:
    """导入全部已登记模块，使其中的 PLR 类对 ``find_subclass`` 可见。

    幂等：每个模块只尝试一次。导入失败只记录错误、不影响其他模块，
    之后对该类型的反序列化会按"找不到类型"报错。导入期间新登记的模块
    （如驱动包 ``__init__`` 里再登记子模块）在同一次调用内一并导入。
    """

    # 并发调用方必须等到导入完成再反序列化；RLock 允许被导入模块重入 register()
    with _import_lock:
        while True:
            with _lock:
                pending = [module for module in _registered if module not in _processed]
                _processed.update(pending)
            if not pending:
                return
            for module_path in pending:
                try:
                    importlib.import_module(module_path)
                except Exception as exc:  # noqa: BLE001 - 单个驱动包模块失败不能拖垮物料反序列化
                    logger.error(f"导入已登记的 PLR 模块 {module_path} 失败: {exc}")


__all__ = [
    "register",
    "register_plr_class",
    "register_plr_module",
    "register_registry_resources",
    "registered_plr_modules",
]
