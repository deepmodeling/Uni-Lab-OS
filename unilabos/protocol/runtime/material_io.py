"""人工物料边界节点 ``material_io``：装入 / 取走 / 补量的参数合同。

工作流执行到这一步时停下等人工确认，确认后由物料权威在一个事务里改库存，不派发任何设备动作。
本模块只校验与规范化节点 ``param``，作者编译（``ctx.material_io``）、保存整图和调度器起跑共用同一套规则：

- ``mode``：``load``（按 ``resource_template`` 在 ``mount`` 的 ``site`` 上建新物料）、``unload``（取走已有
  ``material``，``disposition`` + ``scope`` 决定解除挂载还是软删除）、``refill``（给原位容器补量，``reagent``
  只能有 ``quantity`` / ``quantity_unit``）；
- ``mount``（挂载物料）与 ``site``（它的库位名或库位 UUID）三种模式都必填，是调度器锁定的库位；
- ``timeout_seconds`` 1..86400（不写按 3600），``assignee_user_ids`` 与 ``manual_confirm`` 同一规则：必须显式给出，
  ``[]`` 表示任何人可确认。

``allow_references=True``（作者编译、保存整图）时 ``mount`` / ``site`` / ``material`` 等可以是工作流输入或上游
结果引用，运行时解析完引用后再按 ``allow_references=False`` 严格校验一次。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any
from uuid import UUID

MATERIAL_IO_NODE_TYPE = "material_io"
MATERIAL_IO_MODES = ("load", "unload", "refill")
UNLOAD_DISPOSITIONS = ("detach", "soft_delete")
UNLOAD_SCOPES = ("material_only", "material_with_contents")
DEFAULT_TIMEOUT_SECONDS = 3600
MAX_TIMEOUT_SECONDS = 86_400
#: 三种模式的默认节点名
DEFAULT_STEP_NAMES = {"load": "人工装入", "unload": "人工取走", "refill": "人工补量"}

#: 各模式允许的字段（``mode`` / ``mount`` / ``site`` / ``timeout_seconds`` / ``assignee_user_ids`` 通用）
_COMMON_FIELDS = frozenset({"mode", "mount", "site", "timeout_seconds", "assignee_user_ids"})
_MODE_FIELDS = {
    "load": frozenset({"resource_template", "material_name", "barcode", "reagent"}),
    "unload": frozenset({"material", "disposition", "scope"}),
    "refill": frozenset({"material", "reagent"}),
}
#: 装入时可选的试剂字段：数量 + 单位，再加 ``reagent_info_uuid`` 或 ``name`` / ``cas`` 之一作身份
_LOAD_REAGENT_FIELDS = frozenset({
    "quantity", "quantity_unit", "reagent_info_uuid", "name", "cas", "physical_state",
    "concentration_value", "concentration_unit", "density_g_per_ml", "description",
})
_REFILL_REAGENT_FIELDS = frozenset({"quantity", "quantity_unit"})
_PHYSICAL_STATES = frozenset({"liquid", "solid", "gas", "unknown"})
_REFERENCE_KINDS = frozenset({"node_output", "workflow_input", "workflow_literal"})


class MaterialIOParamError(ValueError):
    """``material_io`` 节点参数不满足合同。"""


def is_reference(value: Any) -> bool:
    """工作流输入、上游结果引用或字面输入封装：运行时才有具体值。"""

    return isinstance(value, Mapping) and value.get("kind") in _REFERENCE_KINDS


def _fail(message: str) -> MaterialIOParamError:
    return MaterialIOParamError(f"material_io：{message}")


def _text(value: Any, label: str, *, allow_references: bool) -> Any:
    if allow_references and is_reference(value):
        return dict(value)
    if not isinstance(value, str) or not value.strip():
        raise _fail(f"{label} 必须是非空字符串")
    return value.strip()


def _material_ref(value: Any, label: str, *, allow_references: bool) -> Any:
    """物料引用：``{"uuid": ...}``，或物料 UUID / 物料业务 ID 字符串（保持作者写法，物料权威解析）。

    运行时上游结果可能带着 ResourceSlot 的其他字段，只取 ``uuid``。
    """

    if allow_references and is_reference(value):
        return dict(value)
    if isinstance(value, Mapping):
        if isinstance(value.get("uuid"), str) and value["uuid"].strip():
            return {"uuid": _uuid(value["uuid"], label)}
        raise _fail(f"{label} 必须是 {{\"uuid\": ...}}、物料 UUID 或物料业务 ID")
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise _fail(f"{label} 必须是 {{\"uuid\": ...}}、物料 UUID 或物料业务 ID")


def _uuid(value: Any, label: str) -> str:
    try:
        parsed = UUID(str(value).strip())
    except ValueError:
        raise _fail(f"{label} 必须是 UUID") from None
    if parsed.int == 0:
        raise _fail(f"{label} 不能是全零 UUID")
    return str(parsed)


def _positive_number(value: Any, label: str, *, allow_references: bool) -> Any:
    if allow_references and isinstance(value, Mapping) and value.get("kind") == "workflow_input":
        return dict(value)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise _fail(f"{label} 必须是正数")
    return int(value) if isinstance(value, float) and value.is_integer() and abs(value) < 2**53 else value


def normalize_reagent(value: Any, mode: str, *, allow_references: bool = False) -> dict[str, Any]:
    """装入 / 补量的 ``reagent``：必须写成对象；补量只有数量与单位，装入另要一种试剂身份。"""

    if not isinstance(value, Mapping):
        raise _fail("reagent 必须是对象")
    allowed = _REFILL_REAGENT_FIELDS if mode == "refill" else _LOAD_REAGENT_FIELDS
    unknown = sorted(set(value) - allowed)
    if unknown:
        if mode == "refill":
            raise _fail("refill 的 reagent 只能有 quantity 和 quantity_unit（换了一瓶请用 unload 再 load）")
        raise _fail(f"reagent 有未知字段：{', '.join(unknown)}")
    result: dict[str, Any] = {
        "quantity": _positive_number(value.get("quantity"), "reagent.quantity", allow_references=allow_references),
        "quantity_unit": _text(value.get("quantity_unit"), "reagent.quantity_unit", allow_references=False),
    }
    if mode == "refill":
        return result
    info = value.get("reagent_info_uuid")
    name = value.get("name")
    cas = value.get("cas")
    if info not in (None, ""):
        if name not in (None, "") or cas not in (None, ""):
            raise _fail("reagent_info_uuid 不能与 name / cas 同时给出")
        result["reagent_info_uuid"] = _uuid(info, "reagent.reagent_info_uuid")
    else:
        if name in (None, "") and cas in (None, ""):
            raise _fail("装入的 reagent 需要 reagent_info_uuid、name 或 cas 之一")
        if name not in (None, ""):
            result["name"] = _text(name, "reagent.name", allow_references=False)
        if cas not in (None, ""):
            result["cas"] = _text(cas, "reagent.cas", allow_references=False)
    state = value.get("physical_state")
    if state not in (None, ""):
        if not isinstance(state, str) or state.strip().lower() not in _PHYSICAL_STATES:
            raise _fail("reagent.physical_state 只能是 liquid / solid / gas / unknown")
        result["physical_state"] = state.strip().lower()
    for key in ("concentration_value", "density_g_per_ml"):
        if value.get(key) is not None:
            result[key] = _positive_number(value[key], f"reagent.{key}", allow_references=False)
    for key in ("concentration_unit", "description"):
        if value.get(key) not in (None, ""):
            result[key] = _text(value[key], f"reagent.{key}", allow_references=False)
    return result


def normalize_assignees(value: Any) -> list[str]:
    """与 ``manual_confirm`` 同一规则：显式列表，去空白去重；``[]`` 表示任何人可确认。"""

    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise _fail("assignee_user_ids 必须是字符串列表（[] 表示任何人可确认）")
    result: list[str] = []
    for item in value:
        text = item.strip()
        if text and text not in result:
            result.append(text)
    return result


def normalize_timeout(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_TIMEOUT_SECONDS:
        raise _fail(f"timeout_seconds 必须是 1..{MAX_TIMEOUT_SECONDS} 的整数")
    return value


def normalize_material_io_param(value: Any, *, allow_references: bool = True) -> dict[str, Any]:
    """校验并规范 ``material_io`` 节点参数；按模式检查必填与禁止字段，返回只含已给字段的新对象。"""

    if not isinstance(value, Mapping):
        raise _fail("参数必须是对象")
    mode = value.get("mode")
    if mode not in MATERIAL_IO_MODES:
        raise _fail("mode 必须是 load、unload 或 refill")
    present = {key for key, item in value.items() if item is not None}
    unknown = sorted(present - _COMMON_FIELDS - set().union(*_MODE_FIELDS.values()))
    if unknown:
        raise _fail(f"未知字段：{', '.join(unknown)}")
    forbidden = sorted(present - _COMMON_FIELDS - _MODE_FIELDS[mode])
    if forbidden:
        raise _fail(f"{mode} 不能声明 {', '.join(forbidden)}")
    for key in ("mount", "site"):
        if key not in present:
            raise _fail(f"{key} 必填（锁定与确认的库位）")
    if "assignee_user_ids" not in value:
        raise _fail("assignee_user_ids 必须显式给出（[] 表示任何人可确认）")
    result: dict[str, Any] = {
        "mode": mode,
        "mount": _material_ref(value["mount"], "mount", allow_references=allow_references),
        "site": _text(value["site"], "site", allow_references=allow_references),
    }
    if mode == "load":
        if "resource_template" not in present:
            raise _fail("load 必须声明 resource_template")
        result["resource_template"] = _text(value["resource_template"], "resource_template",
                                            allow_references=allow_references)
        if "material_name" in present:
            result["material_name"] = _text(value["material_name"], "material_name", allow_references=allow_references)
        if "barcode" in present:
            barcode = value["barcode"]
            if not (allow_references and is_reference(barcode)) and (
                    not isinstance(barcode, str) or not barcode or barcode != barcode.strip()):
                raise _fail("barcode 必须是无首尾空白的非空字符串")
            result["barcode"] = dict(barcode) if is_reference(barcode) else barcode
        if "reagent" in present:
            result["reagent"] = normalize_reagent(value["reagent"], mode, allow_references=allow_references)
    else:
        if "material" not in present:
            raise _fail(f"{mode} 必须声明 material（要取走或补量的物料）")
        result["material"] = _material_ref(value["material"], "material", allow_references=allow_references)
        if mode == "unload":
            disposition, scope = value.get("disposition"), value.get("scope")
            if disposition not in UNLOAD_DISPOSITIONS:
                raise _fail("unload 的 disposition 必须是 detach 或 soft_delete")
            if scope not in UNLOAD_SCOPES:
                raise _fail("unload 的 scope 必须是 material_only 或 material_with_contents")
            if disposition == "detach" and scope != "material_only":
                raise _fail("detach 只支持 material_only；连内容物一起处理请用 soft_delete")
            result["disposition"], result["scope"] = disposition, scope
        else:
            if "reagent" not in present:
                raise _fail("refill 必须声明 reagent（数量与单位）")
            result["reagent"] = normalize_reagent(value["reagent"], mode, allow_references=allow_references)
    # 未写超时按 DEFAULT_TIMEOUT_SECONDS（与 manual_confirm 相同）；不补进参数，保持作者写法
    if "timeout_seconds" in present:
        result["timeout_seconds"] = normalize_timeout(value["timeout_seconds"])
    result["assignee_user_ids"] = normalize_assignees(value["assignee_user_ids"])
    return result


__all__ = [
    "DEFAULT_STEP_NAMES",
    "DEFAULT_TIMEOUT_SECONDS",
    "MATERIAL_IO_MODES",
    "MATERIAL_IO_NODE_TYPE",
    "MAX_TIMEOUT_SECONDS",
    "MaterialIOParamError",
    "UNLOAD_DISPOSITIONS",
    "UNLOAD_SCOPES",
    "is_reference",
    "normalize_assignees",
    "normalize_material_io_param",
    "normalize_reagent",
    "normalize_timeout",
]
