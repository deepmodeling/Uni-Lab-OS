"""库位选择（Site Selection）动作 Schema 到工作流值 Schema 的严格边界。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

EDITOR_CONTROL_KEY = "x-unilabos-editor-control"
SITE_SELECTOR_KEY = "x-unilabos-site-selector"
_SITE_SELECTOR_FIELDS = (
    "version",
    "owner",
    "occupant",
    "show_occupied",
    "allow_occupied",
)


class SiteSelectorValueSchemaError(ValueError):
    """库位选择（Site Selection）值 Schema 无法安全规范化。"""

    def __init__(self, path: str, message: str) -> None:
        """保存稳定 JSON Pointer 和中文诊断。

        参数说明：``path`` 指向首个非法字段；``message`` 是面向合同调用方的中文
        原因。返回：无；异常对象可由工作流 Schema 边界转换为公共错误。
        """

        super().__init__(message)
        self.path = path
        self.message = message


def parse_site_selector_extension(
    schema: Mapping[str, Any],
    *,
    path: str,
    value_kind: object,
) -> dict[str, Any] | None:
    """严格解析成对出现的库位选择扩展。

    参数说明：``schema`` 是单个工作流值 Schema 成员；``path`` 是它的 JSON
    Pointer；``value_kind`` 是已读取但未信任的 ``type``。返回：无扩展时返回
    ``None``，合法时返回固定五字段副本。

    异常说明：扩展未成对、控件类型错误、值不是字符串、扩展非闭合对象、版本或
    字段类型非法时抛出 ``SiteSelectorValueSchemaError``。
    """

    # ``has_control`` 与 ``has_selector`` 证明两个扩展是否成对出现。
    has_control = EDITOR_CONTROL_KEY in schema
    has_selector = SITE_SELECTOR_KEY in schema
    if not has_control and not has_selector:
        return None
    if value_kind != "string":
        raise SiteSelectorValueSchemaError(
            _pointer(path, "type"),
            "库位选择值必须是字符串或可空字符串",
        )
    if not has_control:
        raise SiteSelectorValueSchemaError(
            _pointer(path, EDITOR_CONTROL_KEY),
            "库位选择合同缺少编辑控件声明",
        )
    if schema.get(EDITOR_CONTROL_KEY) != "site_selector":
        raise SiteSelectorValueSchemaError(
            _pointer(path, EDITOR_CONTROL_KEY),
            "库位选择编辑控件声明无效",
        )
    if not has_selector:
        raise SiteSelectorValueSchemaError(
            _pointer(path, SITE_SELECTOR_KEY),
            "库位选择控件缺少完整库位选择合同",
        )
    # ``raw_selector`` 是尚未验证的五字段库位关系合同。
    raw_selector = schema.get(SITE_SELECTOR_KEY)
    selector_path = _pointer(path, SITE_SELECTOR_KEY)
    if not isinstance(raw_selector, Mapping):
        raise SiteSelectorValueSchemaError(
            selector_path,
            "库位选择合同必须是对象",
        )
    for field in raw_selector:
        if field not in _SITE_SELECTOR_FIELDS:
            raise SiteSelectorValueSchemaError(
                _pointer(selector_path, str(field)),
                "库位选择合同包含未知字段",
            )
    for field in _SITE_SELECTOR_FIELDS:
        if field not in raw_selector:
            raise SiteSelectorValueSchemaError(
                _pointer(selector_path, field),
                "库位选择合同缺少必填字段",
            )
    if type(raw_selector["version"]) is not int or raw_selector["version"] != 1:
        raise SiteSelectorValueSchemaError(
            _pointer(selector_path, "version"),
            "库位选择合同版本无效",
        )
    # ``owner`` 是提供库位集合的物料占位符（ResourceSlot）连接点字段名。
    owner = _required_relation_name(
        raw_selector["owner"],
        path=_pointer(selector_path, "owner"),
    )
    # ``occupant`` 可选指向准备放入目标库位（Site）的物料字段。
    raw_occupant = raw_selector["occupant"]
    occupant = (
        None
        if raw_occupant is None
        else _required_relation_name(
            raw_occupant,
            path=_pointer(selector_path, "occupant"),
        )
    )
    for field in ("show_occupied", "allow_occupied"):
        if type(raw_selector[field]) is not bool:
            raise SiteSelectorValueSchemaError(
                _pointer(selector_path, field),
                "库位选择占用策略必须是布尔值",
            )
    return {
        "version": 1,
        "owner": owner,
        "occupant": occupant,
        "show_occupied": raw_selector["show_occupied"],
        "allow_occupied": raw_selector["allow_occupied"],
    }


def normalize_projected_site_selector_schema(
    schema: Mapping[str, Any],
) -> dict[str, Any] | None:
    """把动作连接点的库位 JSON Schema 规范为工作流第 1 版值 Schema。

    参数说明：``schema`` 是注册表（Registry）投影保留的动作字段 Schema。返回：
    非库位选择字段返回 ``None``；合法字符串返回闭合字符串 Schema；严格二成员
    ``["string", "null"]`` 返回 canonical ``anyOf`` 可空 Schema。

    异常说明：类型不是字符串/严格可空字符串、``format`` 不是 ``uuid`` 或扩展
    合同非法时抛出 ``SiteSelectorValueSchemaError``；不会放行其他扩展。
    """

    # 两个布尔量只判断扩展存在性，任何单边声明均交给严格解析器拒绝。
    has_control = EDITOR_CONTROL_KEY in schema
    has_selector = SITE_SELECTOR_KEY in schema
    if not has_control and not has_selector:
        return None

    # ``raw_type`` 保留动作 JSON Schema 的字符串或二成员可空数组形状。
    raw_type = schema.get("type")
    nullable = False
    if isinstance(raw_type, (list, tuple)):
        # ``type_members`` 避免集合化未信任元素，保持非法对象也能稳定失败关闭。
        type_members = list(raw_type)
        if (
            len(type_members) != 2
            or type_members.count("string") != 1
            or type_members.count("null") != 1
        ):
            raise SiteSelectorValueSchemaError(
                "/type",
                "库位选择可空类型必须只包含 string 和 null",
            )
        nullable = True
        value_kind: object = "string"
    else:
        value_kind = raw_type
    selector = parse_site_selector_extension(
        schema,
        path="",
        value_kind=value_kind,
    )
    assert selector is not None
    raw_format = schema.get("format")
    if raw_format is not None and raw_format != "uuid":
        raise SiteSelectorValueSchemaError(
            "/format",
            "库位选择动作字段 format 必须是 uuid",
        )

    # ``base`` 删除动作执行校验和展示注解，只保留工作流值集合及合法扩展。
    base = dict(schema)
    base["type"] = "string"
    for field in ("default", "title", "description", "format"):
        base.pop(field, None)
    base[EDITOR_CONTROL_KEY] = "site_selector"
    base[SITE_SELECTOR_KEY] = selector
    if nullable:
        return {"anyOf": [base, {"type": "null"}]}
    return base


def _required_relation_name(value: object, *, path: str) -> str:
    """规范化 owner/occupant 引用的连接点字段名。

    参数说明：``value`` 是关系字段名；``path`` 指向其合同位置。返回：原始非空
    且无首尾空白的字符串；类型非法、为空或带额外空白时抛出
    ``SiteSelectorValueSchemaError``。
    """

    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
    ):
        raise SiteSelectorValueSchemaError(
            path,
            "库位选择关系必须引用非空字段名",
        )
    return value


def _pointer(path: str, token: str) -> str:
    """追加一个 JSON Pointer token。

    参数说明：``path`` 是父路径；``token`` 是待转义字段名。返回：规范 JSON
    Pointer；本函数不读取领域状态，也不抛出领域异常。
    """

    escaped = token.replace("~", "~0").replace("/", "~1")
    return f"{path}/{escaped}"
