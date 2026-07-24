from typing import Any, Dict, Optional

from .prcxi import PRCXI9300ModuleSite


class PRCXI9300FunctionalModule(PRCXI9300ModuleSite):
    """
    PRCXI 9300 功能模块基类（加热/冷却/震荡/加热震荡/磁吸等）。

    设计目标：
    - 作为一个可以在工作台上拖拽摆放的实体资源（继承自 PRCXI9300ModuleSite -> ItemizedCarrier）。
    - 顶面存在一个站点（site），可吸附标准板类资源（plate / tip_rack / tube_rack 等）。
    - 支持注入 `material_info` (UUID 等)，并且在 serialize_state 时做安全过滤。
    """

    def __init__(
        self,
        name: str,
        size_x: float,
        size_y: float,
        size_z: float,
        module_type: Optional[str] = None,
        category: str = "module",
        model: Optional[str] = None,
        material_info: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ):
        super().__init__(
            name=name,
            size_x=size_x,
            size_y=size_y,
            size_z=size_z,
            material_info=material_info,
            model=model,
            category=category,
            **kwargs,
        )

        # 记录模块类型（加热 / 冷却 / 震荡 / 加热震荡 / 磁吸）
        self.module_type = module_type or "generic"

        # 与 PRCXI9300PlateAdapter 一致，使用 _unilabos_state 保存扩展信息
        if not hasattr(self, "_unilabos_state") or self._unilabos_state is None:
            self._unilabos_state = {}

        # super().__init__ 已经在有 material_info 时写入 "Material"，这里仅确保存在
        if material_info is not None and "Material" not in self._unilabos_state:
            self._unilabos_state["Material"] = material_info

        # 额外标记 category 和模块类型，便于前端或上层逻辑区分
        self._unilabos_state.setdefault("category", category)
        self._unilabos_state["module_type"] = module_type


# ============================================================================
# 具体功能模块定义
# 这里的尺寸和 material_info 目前为占位参数，后续可根据实际测量/JSON 配置进行更新。
# 顶面站点尺寸与模块外形一致，保证可以吸附标准 96 板/储液槽等。
# ============================================================================


def PRCXI_Heating_Module(name: str) -> PRCXI9300FunctionalModule:
    """加热模块（顶面可吸附标准板）。"""
    return PRCXI9300FunctionalModule(
        name=name,
        size_x=127.76,
        size_y=85.48,
        size_z=40.0,
        module_type="heating",
        model="PRCXI_Heating_Module",
        material_info={
            "uuid": "TODO-HEATING-MODULE-UUID",
            "Code": "HEAT-MOD",
            "Name": "PRCXI 加热模块",
            "SupplyType": 3,
        },
    )


def PRCXI_MetalCooling_Module(name: str) -> PRCXI9300FunctionalModule:
    """金属冷却模块（顶面可吸附标准板）。"""
    return PRCXI9300FunctionalModule(
        name=name,
        size_x=127.76,
        size_y=85.48,
        size_z=40.0,
        module_type="metal_cooling",
        model="PRCXI_MetalCooling_Module",
        material_info={
            "uuid": "TODO-METAL-COOLING-MODULE-UUID",
            "Code": "METAL-COOL-MOD",
            "Name": "PRCXI 金属冷却模块",
            "SupplyType": 3,
        },
    )


def PRCXI_Shaking_Module(name: str) -> PRCXI9300FunctionalModule:
    """震荡模块（顶面可吸附标准板）。"""
    return PRCXI9300FunctionalModule(
        name=name,
        size_x=127.76,
        size_y=85.48,
        size_z=50.0,
        module_type="shaking",
        model="PRCXI_Shaking_Module",
        material_info={
            "uuid": "TODO-SHAKING-MODULE-UUID",
            "Code": "SHAKE-MOD",
            "Name": "PRCXI 震荡模块",
            "SupplyType": 3,
        },
    )


def PRCXI_Heating_Shaking_Module(name: str) -> PRCXI9300FunctionalModule:
    """加热震荡模块（顶面可吸附标准板）。"""
    return PRCXI9300FunctionalModule(
        name=name,
        size_x=127.76,
        size_y=85.48,
        size_z=55.0,
        module_type="heating_shaking",
        model="PRCXI_Heating_Shaking_Module",
        material_info={
            "uuid": "TODO-HEATING-SHAKING-MODULE-UUID",
            "Code": "HEAT-SHAKE-MOD",
            "Name": "PRCXI 加热震荡模块",
            "SupplyType": 3,
        },
    )


def PRCXI_Magnetic_Module(name: str) -> PRCXI9300FunctionalModule:
    """磁吸模块（顶面可吸附标准板）。"""
    return PRCXI9300FunctionalModule(
        name=name,
        size_x=127.76,
        size_y=85.48,
        size_z=30.0,
        module_type="magnetic",
        model="PRCXI_Magnetic_Module",
        material_info={
            "uuid": "TODO-MAGNETIC-MODULE-UUID",
            "Code": "MAG-MOD",
            "Name": "PRCXI 磁吸模块",
            "SupplyType": 3,
        },
    )

