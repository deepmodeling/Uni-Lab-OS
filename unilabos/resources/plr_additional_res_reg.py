"""登记 PLR 扩展资源：公开的工站物料容器总是登记，授权驱动的扩展仅在已安装时登记。"""

from importlib import import_module

# 授权驱动随独立设备包安装，开源版可以没有。
_LICENSED_EXTENSIONS = ("unilabos.devices.liquid_handling.rviz_backend",)


def register() -> None:
    # 工站物料容器是框架公开类，导入即在 PLR 中登记，用于序列化工站物料。
    from unilabos.workstation import WorkStationContainer  # noqa: F401

    for module in _LICENSED_EXTENSIONS:
        try:
            import_module(module)
        except ModuleNotFoundError as error:
            # 扩展本身未安装可省略；已安装扩展内部缺依赖仍应明确报错。
            if error.name is None or not (module == error.name or module.startswith(error.name + ".")):
                raise
