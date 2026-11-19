"""自定义枪头的兼容几何；估算值只用于建模，不能替代真机标定。"""

from copy import deepcopy

from pylabrobot.resources import Tip


# 这是按型号选取的兼容包络，不是通用的容量到直径换算表。
TIP_GEOMETRY_ESTIMATES: dict[str, tuple[float, str]] = {
    "PRCXI_10uL_Tips": (5.0, "现有模板孔中心偏移使用 5/2"),
    "PRCXI_10ul_eTips": (7.0, "现有模板 TipSpot 的 7 mm 包络"),
    "PRCXI_200uL_Tips": (5.5, "现有模板孔中心偏移使用 5.5/2"),
    "PRCXI_300ul_Tips": (7.0, "现有模板 TipSpot 的 7 mm 包络"),
    "PRCXI_1000uL_Tips": (7.95, "现有模板孔中心偏移使用 7.95/2"),
    "PRCXI_1250uL_Tips": (7.95, "现有模板孔中心偏移使用 7.95/2"),
    "BIOYOND_PolymerStation_TipBox": (8.0, "现有模板 TipSpot 的 8 mm 包络"),
    "LaiYuTipRack200": (5.23, "借用 Opentrons 200 uL 滤芯枪头模型的直径，仅作占位"),
    "LaiYuTipRack1000": (7.23, "借用 Opentrons 1000 uL 枪头模型的直径，仅作占位"),
    "TipBox64": (7.23, "借用 Opentrons 1000 uL 枪头模型的直径，仅作占位"),
}


def tip_geometry_metadata(model: str) -> dict:
    """返回可序列化的未实测标记及依据，每次返回独立对象。"""
    diameter, basis = TIP_GEOMETRY_ESTIMATES[model]
    review_notes = {
        "PRCXI_10ul_eTips": "旧模板 fitting_depth=45.1、length=52，需确认是否混用了安装深度和有效长度。",
        "PRCXI_300ul_Tips": "旧模板 fitting_depth=51、length=60，需确认是否混用了安装深度和有效长度。",
        "PRCXI_1000uL_Tips": "旧模板 length=55；保留原值，需与实际 1000 uL 枪头核对。",
        "TipBox64": "旧模板 length=20；保留原值，需与实际 1000 uL 枪头核对。",
    }
    return {
        "geometry_validation": {
            "status": "unmeasured",
            "measured": False,
            "model": model,
            "unit": "mm",
            "estimated_fields": {"diameter": {"value": diameter, "basis": basis}},
            "unknown_fields": ["collar_height", "pick_up_location", "is_conductive"],
            "unverified_existing_fields": ["size_z", "fitting_depth", "has_filter"],
            "review_note": review_notes.get(model, "现有长度、安装深度和滤芯配置均未进行实物复核。"),
            "warning": "兼容建模参数，未实测；真机取放和液面检测前必须校准。",
        }
    }


def make_unmeasured_tip(
    name: str, *, model: str, volume: float, length: float, fitting_depth: float
) -> Tip:
    """按已登记型号创建新版 PLR 具名枪头。"""
    metadata = tip_geometry_metadata(model)
    common = dict(
        name=name, has_filter=False, maximal_volume=volume,
        nominal_volume=volume, fitting_depth=fitting_depth,
    )
    tip = Tip(
        **common, diameter=TIP_GEOMETRY_ESTIMATES[model][0], size_z=length, model=model
    )
    tip.metadata.update(deepcopy(metadata))
    return tip
