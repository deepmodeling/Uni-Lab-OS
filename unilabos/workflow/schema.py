"""工作流（Workflow）版本 1 合同与严格 JSON 值 Schema。"""

from __future__ import annotations

import keyword
import math
from itertools import pairwise
from typing import Any, Never, Self

from unilabos.workflow.json_codec import (
    MAX_BACKEND_JSON_DEPTH,
    decode_json_bytes,
    encode_json,
)
from unilabos.workflow.models import validate_uuid
from unilabos.workflow.site_selector_value_schema import (
    EDITOR_CONTROL_KEY,
    SITE_SELECTOR_KEY,
    SiteSelectorValueSchemaError,
    parse_site_selector_extension,
)

_INVALID_SCHEMA = "工作流值 Schema 不符合版本 1 合同"
_INVALID_CONTRACT = "工作流输入输出合同格式不正确"
_INVALID_VALUE = "工作流值不符合声明的 Schema"
_MISSING = object()
_CANONICAL_CONSTRUCTOR_TOKEN = object()


class WorkflowSchemaError(ValueError):
    """可稳定投影为编译诊断或请求错误的 Schema 失败。"""

    def __init__(self, code: str, path: str, message: str):
        super().__init__(message)
        self.code = code
        self.path = path
        self.message = message


class _CanonicalValue:
    """由 parser 独占构造、以不可变 JSON 字节持有的 canonical value。"""

    __slots__ = ("_payload",)

    def __new__(cls, *_args: Any, **_kwargs: Any) -> Self:
        raise TypeError("请通过 Workflow Schema parser 创建 canonical value")

    def __setattr__(self, _name: str, _value: Any) -> Never:
        raise AttributeError("Workflow canonical value 不可修改")

    def __delattr__(self, _name: str) -> Never:
        raise AttributeError("Workflow canonical value 不可修改")

    def __eq__(self, other: object) -> bool:
        return type(self) is type(other) and self._payload == other._payload

    def __hash__(self) -> int:
        return hash(self._payload)

    @classmethod
    def _from_canonical(
        cls,
        data: dict[str, Any],
        *,
        token: object,
    ) -> Self:
        if token is not _CANONICAL_CONSTRUCTOR_TOKEN:
            raise TypeError("canonical value 只能由模块内 parser 创建")
        value = object.__new__(cls)
        object.__setattr__(value, "_payload", encode_json(data))
        return value

    def _canonical_dict(self) -> dict[str, Any]:
        data = decode_json_bytes(self._payload)
        assert isinstance(data, dict)
        return data

    def to_dict(self) -> dict[str, Any]:
        """返回不与对象或其他 dump 共享容器的 canonical JSON。"""

        return self._canonical_dict()


class WorkflowValueSchema(_CanonicalValue):
    """已规范化、不可变的版本 1 值 Schema。"""

    __slots__ = ()


class WorkflowInputContract(_CanonicalValue):
    """有序、闭合的版本 1 工作流输入合同（WorkflowInputContract）。"""

    __slots__ = ()


class WorkflowOutputContract(_CanonicalValue):
    """有序、闭合的版本 1 工作流输出合同（WorkflowOutputContract）。"""

    __slots__ = ()


def _fail(code: str, path: str, message: str) -> Never:
    raise WorkflowSchemaError(code, path, message)


def _pointer(path: str, token: str | int) -> str:
    escaped = str(token).replace("~", "~0").replace("/", "~1")
    return f"{path}/{escaped}"


def _require_object(
    raw: Any,
    *,
    code: str,
    path: str,
    message: str,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        _fail(code, path, message)
    return raw


def _reject_unknown(
    raw: dict[str, Any],
    allowed: set[str],
    *,
    code: str,
    path: str,
    message: str,
) -> None:
    for key in raw:
        if key not in allowed:
            _fail(code, _pointer(path, key), message)


def _normalize_scalar(
    kind: str,
    value: Any,
    *,
    code: str,
    path: str,
    message: str,
) -> str | int | float | bool:
    if kind == "string":
        if type(value) is not str:
            _fail(code, path, message)
        return value
    if kind == "boolean":
        if type(value) is not bool:
            _fail(code, path, message)
        return value
    if kind == "integer":
        if type(value) is int:
            return value
        if type(value) is float and math.isfinite(value) and value.is_integer():
            return int(value)
        _fail(code, path, message)
    if kind == "number":
        if type(value) not in {int, float}:
            _fail(code, path, message)
        if type(value) is float and not math.isfinite(value):
            _fail(code, path, message)
        return value
    raise AssertionError(f"unsupported scalar kind: {kind}")


def _enum_equal(kind: str, left: Any, right: Any) -> bool:
    if kind == "number":
        return left == right
    return type(left) is type(right) and left == right


def _first_duplicate_enum_index(
    kind: str,
    values: list[Any],
) -> int | None:
    ordered = sorted(enumerate(values), key=lambda item: item[1])
    duplicate_index: int | None = None
    for left, right in pairwise(ordered):
        if not _enum_equal(kind, left[1], right[1]):
            continue
        current_index = max(left[0], right[0])
        if duplicate_index is None or current_index < duplicate_index:
            duplicate_index = current_index
    return duplicate_index


def _check_numeric_bound(
    kind: str,
    value: Any,
    *,
    path: str,
) -> int | float:
    normalized = _normalize_scalar(
        kind,
        value,
        code="invalid_schema",
        path=path,
        message=_INVALID_SCHEMA,
    )
    assert isinstance(normalized, (int, float)) and not isinstance(normalized, bool)
    return normalized


def _check_length_bound(value: Any, *, path: str) -> int:
    if type(value) is not int or value < 0:
        _fail("invalid_schema", path, _INVALID_SCHEMA)
    return value


def _normalize_allowlist(raw: Any, *, path: str) -> list[str]:
    if not isinstance(raw, list) or not raw:
        _fail("invalid_schema", path, _INVALID_SCHEMA)
    normalized: list[str] = []
    for index, value in enumerate(raw):
        item_path = _pointer(path, index)
        if not isinstance(value, str):
            _fail("invalid_schema", item_path, _INVALID_SCHEMA)
        try:
            identity = validate_uuid(value)
        except (TypeError, ValueError):
            _fail("invalid_schema", item_path, _INVALID_SCHEMA)
        if identity in normalized:
            _fail("invalid_schema", item_path, _INVALID_SCHEMA)
        normalized.append(identity)
    return normalized


def _validate_constraints(
    schema: dict[str, Any],
    value: Any,
    *,
    code: str,
    path: str,
    message: str,
    check_enum: bool = True,
) -> None:
    kind = schema.get("type")
    if kind in {"integer", "number"}:
        minimum = schema.get("minimum", _MISSING)
        maximum = schema.get("maximum", _MISSING)
        if minimum is not _MISSING and value < minimum:
            _fail(code, path, message)
        if maximum is not _MISSING and value > maximum:
            _fail(code, path, message)
    elif kind == "string":
        minimum = schema.get("minLength", _MISSING)
        maximum = schema.get("maxLength", _MISSING)
        if minimum is not _MISSING and len(value) < minimum:
            _fail(code, path, message)
        if maximum is not _MISSING and len(value) > maximum:
            _fail(code, path, message)
    elif kind == "array":
        minimum = schema.get("minItems", _MISSING)
        maximum = schema.get("maxItems", _MISSING)
        if minimum is not _MISSING and len(value) < minimum:
            _fail(code, path, message)
        if maximum is not _MISSING and len(value) > maximum:
            _fail(code, path, message)

    enum = schema.get("enum")
    if check_enum and enum is not None:
        assert isinstance(kind, str)
        if not any(_enum_equal(kind, value, member) for member in enum):
            _fail(code, path, message)


def _normalize_enum(
    kind: str,
    raw: Any,
    schema: dict[str, Any],
    *,
    path: str,
) -> list[Any]:
    if not isinstance(raw, list) or not raw:
        _fail("invalid_schema", path, _INVALID_SCHEMA)
    normalized: list[Any] = []
    for index, value in enumerate(raw):
        item_path = _pointer(path, index)
        member = _normalize_scalar(
            kind,
            value,
            code="invalid_schema",
            path=item_path,
            message=_INVALID_SCHEMA,
        )
        _validate_constraints(
            schema,
            member,
            code="invalid_schema",
            path=item_path,
            message=_INVALID_SCHEMA,
            check_enum=False,
        )
        normalized.append(member)
    duplicate_index = _first_duplicate_enum_index(kind, normalized)
    if duplicate_index is not None:
        _fail(
            "invalid_schema",
            _pointer(path, duplicate_index),
            _INVALID_SCHEMA,
        )
    return normalized


def _parse_nullable(raw: dict[str, Any], *, path: str) -> dict[str, Any]:
    _reject_unknown(
        raw,
        {"anyOf"},
        code="invalid_schema",
        path=path,
        message=_INVALID_SCHEMA,
    )
    members = raw["anyOf"]
    any_of_path = _pointer(path, "anyOf")
    if not isinstance(members, list) or len(members) != 2:
        _fail("invalid_schema", any_of_path, _INVALID_SCHEMA)

    null_indexes: list[int] = []
    for index, member in enumerate(members):
        member_path = _pointer(any_of_path, index)
        if not isinstance(member, dict):
            _fail("invalid_schema", any_of_path, _INVALID_SCHEMA)
        if member.get("type") == "null":
            _reject_unknown(
                member,
                {"type"},
                code="invalid_schema",
                path=member_path,
                message=_INVALID_SCHEMA,
            )
            null_indexes.append(index)
    if len(null_indexes) != 1:
        _fail("invalid_schema", any_of_path, _INVALID_SCHEMA)

    base_index = 1 - null_indexes[0]
    base = _parse_schema_dict(
        members[base_index],
        path=_pointer(any_of_path, base_index),
        allow_array=True,
        allow_nullable=False,
    )
    return {"anyOf": [base, {"type": "null"}]}


def _parse_slot(raw: dict[str, Any], *, path: str) -> dict[str, Any]:
    _reject_unknown(
        raw,
        {"$slot", "allowed_resource_template_uuids"},
        code="invalid_schema",
        path=path,
        message=_INVALID_SCHEMA,
    )
    if raw["$slot"] != "ResourceSlot":
        _fail("invalid_schema", _pointer(path, "$slot"), _INVALID_SCHEMA)
    result: dict[str, Any] = {"$slot": "ResourceSlot"}
    if "allowed_resource_template_uuids" in raw:
        allowlist_path = _pointer(path, "allowed_resource_template_uuids")
        result["allowed_resource_template_uuids"] = _normalize_allowlist(
            raw["allowed_resource_template_uuids"],
            path=allowlist_path,
        )
    return result


def _parse_typed_schema(
    raw: dict[str, Any],
    *,
    path: str,
    allow_array: bool,
) -> dict[str, Any]:
    """严格解析一个带显式 ``type`` 的工作流值 Schema 成员。

    参数说明：``raw`` 是未信任的闭合 Schema；``path`` 是当前 JSON Pointer；
    ``allow_array`` 决定本层是否允许数组。返回：规范化且容器分离的 Schema。

    异常说明：类型、约束、未知字段或库位选择（Site Selection）扩展非法时通过
    ``_fail`` 抛出 ``WorkflowSchemaError``。
    """

    kind = raw["type"]
    type_path = _pointer(path, "type")
    try:
        # ``site_selector`` 只承载编辑关系，不得把整数等其他值类型伪装为库位。
        site_selector = parse_site_selector_extension(
            raw,
            path=path,
            value_kind=kind,
        )
    except SiteSelectorValueSchemaError as error:
        _fail("invalid_schema", error.path, _INVALID_SCHEMA)
    supported = {"string", "integer", "number", "boolean", "object", "array"}
    if (
        type(kind) is not str
        or kind not in supported
        or (kind == "array" and not allow_array)
    ):
        _fail("invalid_schema", type_path, _INVALID_SCHEMA)

    allowed_by_kind = {
        "string": {
            "type",
            "enum",
            "minLength",
            "maxLength",
            EDITOR_CONTROL_KEY,
            SITE_SELECTOR_KEY,
        },
        "integer": {"type", "enum", "minimum", "maximum"},
        "number": {"type", "enum", "minimum", "maximum"},
        "boolean": {"type", "enum"},
        "object": {"type"},
        "array": {"type", "items", "minItems", "maxItems"},
    }
    _reject_unknown(
        raw,
        allowed_by_kind[kind],
        code="invalid_schema",
        path=path,
        message=_INVALID_SCHEMA,
    )
    result: dict[str, Any] = {"type": kind}

    if kind in {"integer", "number"}:
        for field in ("minimum", "maximum"):
            if field in raw:
                field_path = _pointer(path, field)
                _check_numeric_bound(kind, raw[field], path=field_path)
                result[field] = raw[field]
        if (
            "minimum" in result
            and "maximum" in result
            and result["minimum"] > result["maximum"]
        ):
            _fail("invalid_schema", _pointer(path, "maximum"), _INVALID_SCHEMA)
    elif kind == "string":
        if site_selector is not None:
            result[EDITOR_CONTROL_KEY] = "site_selector"
            result[SITE_SELECTOR_KEY] = site_selector
        for field in ("minLength", "maxLength"):
            if field in raw:
                result[field] = _check_length_bound(
                    raw[field],
                    path=_pointer(path, field),
                )
        if (
            "minLength" in result
            and "maxLength" in result
            and result["minLength"] > result["maxLength"]
        ):
            _fail("invalid_schema", _pointer(path, "maxLength"), _INVALID_SCHEMA)
    elif kind == "array":
        if "items" not in raw:
            _fail("invalid_schema", _pointer(path, "items"), _INVALID_SCHEMA)
        result["items"] = _parse_schema_dict(
            raw["items"],
            path=_pointer(path, "items"),
            allow_array=False,
            allow_nullable=False,
        )
        for field in ("minItems", "maxItems"):
            if field in raw:
                result[field] = _check_length_bound(
                    raw[field],
                    path=_pointer(path, field),
                )
        if (
            "minItems" in result
            and "maxItems" in result
            and result["minItems"] > result["maxItems"]
        ):
            _fail("invalid_schema", _pointer(path, "maxItems"), _INVALID_SCHEMA)

    if "enum" in raw:
        if kind not in {"string", "integer", "number", "boolean"}:
            _fail("invalid_schema", _pointer(path, "enum"), _INVALID_SCHEMA)
        result["enum"] = _normalize_enum(
            kind,
            raw["enum"],
            result,
            path=_pointer(path, "enum"),
        )
    return result


def _parse_schema_dict(
    raw: Any,
    *,
    path: str,
    allow_array: bool,
    allow_nullable: bool,
) -> dict[str, Any]:
    schema = _require_object(
        raw,
        code="invalid_schema",
        path=path,
        message=_INVALID_SCHEMA,
    )
    if not schema:
        _fail("invalid_schema", path, _INVALID_SCHEMA)
    if "anyOf" in schema:
        if not allow_nullable:
            _fail("invalid_schema", _pointer(path, "anyOf"), _INVALID_SCHEMA)
        return _parse_nullable(schema, path=path)
    if "$slot" in schema:
        return _parse_slot(schema, path=path)
    if "type" in schema:
        return _parse_typed_schema(schema, path=path, allow_array=allow_array)
    _fail("invalid_schema", path, _INVALID_SCHEMA)


def parse_value_schema(raw: Any) -> WorkflowValueSchema:
    """校验并规范化一个闭合的工作流（Workflow）v1 值 Schema。"""

    return WorkflowValueSchema._from_canonical(
        _parse_schema_dict(
            raw,
            path="",
            allow_array=True,
            allow_nullable=True,
        ),
        token=_CANONICAL_CONSTRUCTOR_TOKEN,
    )


def _validate_json_value(
    value: Any,
    *,
    code: str,
    path: str,
    message: str,
) -> Any:
    active: set[int] = set()
    stack: list[tuple[Any, str, int, bool]] = [(value, path, 0, False)]
    while stack:
        item, item_path, depth, leaving = stack.pop()
        if leaving:
            active.remove(id(item))
            continue
        if item is None or type(item) in {bool, int, str}:
            continue
        if type(item) is float:
            if not math.isfinite(item):
                _fail(code, item_path, message)
            continue
        if type(item) not in {dict, list}:
            _fail(code, item_path, message)
        if depth + 1 > MAX_BACKEND_JSON_DEPTH:
            _fail(code, item_path, message)
        identity = id(item)
        if identity in active:
            _fail(code, item_path, message)
        active.add(identity)
        stack.append((item, item_path, depth, True))
        if type(item) is dict:
            entries = list(item.items())
            for key, child in reversed(entries):
                if type(key) is not str:
                    _fail(code, item_path, message)
                stack.append((child, _pointer(item_path, key), depth + 1, False))
        else:
            for index in range(len(item) - 1, -1, -1):
                stack.append(
                    (item[index], _pointer(item_path, index), depth + 1, False)
                )
    return decode_json_bytes(encode_json(value))


def _normalize_slot(value: Any, *, path: str) -> dict[str, str]:
    slot = _require_object(
        value,
        code="invalid_value",
        path=path,
        message=_INVALID_VALUE,
    )
    if "uuid" not in slot:
        _fail("invalid_value", _pointer(path, "uuid"), _INVALID_VALUE)
    _reject_unknown(
        slot,
        {"uuid"},
        code="invalid_value",
        path=path,
        message=_INVALID_VALUE,
    )
    if not isinstance(slot["uuid"], str):
        _fail("invalid_value", _pointer(path, "uuid"), _INVALID_VALUE)
    try:
        identity = validate_uuid(slot["uuid"])
    except (TypeError, ValueError):
        _fail("invalid_value", _pointer(path, "uuid"), _INVALID_VALUE)
    return {"uuid": identity}


def _normalize_with_schema(
    schema: dict[str, Any],
    value: Any,
    *,
    path: str,
) -> Any:
    if "anyOf" in schema:
        if value is None:
            return None
        return _normalize_with_schema(schema["anyOf"][0], value, path=path)
    if "$slot" in schema:
        return _normalize_slot(value, path=path)

    kind = schema["type"]
    if kind in {"string", "integer", "number", "boolean"}:
        normalized = _normalize_scalar(
            kind,
            value,
            code="invalid_value",
            path=path,
            message=_INVALID_VALUE,
        )
        _validate_constraints(
            schema,
            normalized,
            code="invalid_value",
            path=path,
            message=_INVALID_VALUE,
        )
        return normalized
    if kind == "object":
        if type(value) is not dict:
            _fail("invalid_value", path, _INVALID_VALUE)
        return value
    if kind == "array":
        if type(value) is not list:
            _fail("invalid_value", path, _INVALID_VALUE)
        _validate_constraints(
            schema,
            value,
            code="invalid_value",
            path=path,
            message=_INVALID_VALUE,
        )
        return [
            _normalize_with_schema(
                schema["items"],
                item,
                path=_pointer(path, index),
            )
            for index, item in enumerate(value)
        ]
    raise AssertionError(f"unsupported schema kind: {kind}")


def normalize_value(schema: WorkflowValueSchema, raw_value: Any) -> Any:
    """按同一个严格规则规范化一个值，并返回独立 JSON 容器。"""

    if not isinstance(schema, WorkflowValueSchema):
        _fail("invalid_schema", "", _INVALID_SCHEMA)
    normalized = _normalize_with_schema(
        schema._canonical_dict(),
        raw_value,
        path="",
    )
    if type(normalized) not in {dict, list}:
        return normalized
    return _validate_json_value(
        normalized,
        code="invalid_value",
        path="",
        message=_INVALID_VALUE,
    )


def _normalize_name(value: Any, *, path: str) -> str:
    if (
        type(value) is not str
        or not value
        or not value.isidentifier()
        or keyword.iskeyword(value)
    ):
        _fail("invalid_contract", path, _INVALID_CONTRACT)
    return value


def _normalize_presentation(value: Any, *, path: str) -> str:
    if type(value) is not str:
        _fail("invalid_contract", path, _INVALID_CONTRACT)
    normalized = value.strip()
    if not normalized:
        _fail("invalid_contract", path, _INVALID_CONTRACT)
    return normalized


def _schema_is_nullable(schema: WorkflowValueSchema) -> bool:
    return "anyOf" in schema._canonical_dict()


def _schema_base(schema: WorkflowValueSchema) -> dict[str, Any]:
    data = schema._canonical_dict()
    return data["anyOf"][0] if "anyOf" in data else data


def _normalize_default(
    schema: WorkflowValueSchema,
    value: Any,
    *,
    path: str,
) -> Any:
    base = _schema_base(schema)
    if "$slot" in base:
        _fail("invalid_contract", path, _INVALID_CONTRACT)
    if base.get("type") == "array" and "$slot" in base["items"] and value != []:
        _fail("invalid_contract", path, _INVALID_CONTRACT)
    try:
        return normalize_value(schema, value)
    except WorkflowSchemaError as error:
        suffix = error.path
        _fail("invalid_contract", f"{path}{suffix}", _INVALID_CONTRACT)


def _parse_contract_envelope(
    raw: Any,
    *,
    collection_key: str,
) -> tuple[dict[str, Any], list[Any]]:
    contract = _require_object(
        raw,
        code="invalid_contract",
        path="",
        message=_INVALID_CONTRACT,
    )
    if "version" not in contract:
        _fail("invalid_contract", "/version", _INVALID_CONTRACT)
    if collection_key not in contract:
        _fail("invalid_contract", _pointer("", collection_key), _INVALID_CONTRACT)
    _reject_unknown(
        contract,
        {"version", collection_key},
        code="invalid_contract",
        path="",
        message=_INVALID_CONTRACT,
    )
    if type(contract["version"]) is not int or contract["version"] != 1:
        _fail("invalid_contract", "/version", _INVALID_CONTRACT)
    collection = contract[collection_key]
    if type(collection) is not list:
        _fail(
            "invalid_contract",
            _pointer("", collection_key),
            _INVALID_CONTRACT,
        )
    return contract, collection


def parse_input_contract(raw: Any) -> WorkflowInputContract:
    """校验有序、闭合的工作流输入合同（WorkflowInputContract）。"""

    _, parameters = _parse_contract_envelope(raw, collection_key="parameters")
    normalized: list[dict[str, Any]] = []
    names: set[str] = set()
    for index, value in enumerate(parameters):
        path = f"/parameters/{index}"
        descriptor = _require_object(
            value,
            code="invalid_contract",
            path=path,
            message=_INVALID_CONTRACT,
        )
        for required_field in ("name", "schema", "required"):
            if required_field not in descriptor:
                _fail(
                    "invalid_contract",
                    _pointer(path, required_field),
                    _INVALID_CONTRACT,
                )
        _reject_unknown(
            descriptor,
            {
                "name",
                "schema",
                "required",
                "default",
                "title",
                "description",
            },
            code="invalid_contract",
            path=path,
            message=_INVALID_CONTRACT,
        )
        name_path = _pointer(path, "name")
        name = _normalize_name(descriptor["name"], path=name_path)
        if name in names:
            _fail("invalid_contract", name_path, _INVALID_CONTRACT)
        names.add(name)
        required = descriptor["required"]
        if type(required) is not bool:
            _fail(
                "invalid_contract",
                _pointer(path, "required"),
                _INVALID_CONTRACT,
            )

        schema_path = _pointer(path, "schema")
        schema = WorkflowValueSchema._from_canonical(
            _parse_schema_dict(
                descriptor["schema"],
                path=schema_path,
                allow_array=True,
                allow_nullable=True,
            ),
            token=_CANONICAL_CONSTRUCTOR_TOKEN,
        )
        has_default = "default" in descriptor
        default_path = _pointer(path, "default")
        if required:
            if _schema_is_nullable(schema):
                _fail(
                    "invalid_contract",
                    f"{schema_path}/anyOf",
                    _INVALID_CONTRACT,
                )
            if has_default:
                _fail("invalid_contract", default_path, _INVALID_CONTRACT)
        else:
            if not has_default:
                _fail("invalid_contract", default_path, _INVALID_CONTRACT)
            if _schema_is_nullable(schema):
                if descriptor["default"] is not None:
                    _fail("invalid_contract", default_path, _INVALID_CONTRACT)
                default = None
            else:
                if descriptor["default"] is None:
                    _fail("invalid_contract", default_path, _INVALID_CONTRACT)
                default = _normalize_default(
                    schema,
                    descriptor["default"],
                    path=default_path,
                )

        item: dict[str, Any] = {
            "name": name,
            "schema": schema.to_dict(),
            "required": required,
        }
        if "title" in descriptor:
            item["title"] = _normalize_presentation(
                descriptor["title"],
                path=_pointer(path, "title"),
            )
        if "description" in descriptor:
            item["description"] = _normalize_presentation(
                descriptor["description"],
                path=_pointer(path, "description"),
            )
        if has_default:
            item["default"] = default
        normalized.append(item)
    canonical = _validate_json_value(
        {"version": 1, "parameters": normalized},
        code="invalid_contract",
        path="",
        message=_INVALID_CONTRACT,
    )
    return WorkflowInputContract._from_canonical(
        canonical,
        token=_CANONICAL_CONSTRUCTOR_TOKEN,
    )


def parse_output_contract(raw: Any) -> WorkflowOutputContract:
    """校验有序、闭合的工作流输出合同（WorkflowOutputContract）。"""

    _, outputs = _parse_contract_envelope(raw, collection_key="outputs")
    normalized: list[dict[str, Any]] = []
    names: set[str] = set()
    for index, value in enumerate(outputs):
        path = f"/outputs/{index}"
        descriptor = _require_object(
            value,
            code="invalid_contract",
            path=path,
            message=_INVALID_CONTRACT,
        )
        for required_field in ("name", "schema"):
            if required_field not in descriptor:
                _fail(
                    "invalid_contract",
                    _pointer(path, required_field),
                    _INVALID_CONTRACT,
                )
        _reject_unknown(
            descriptor,
            {"name", "schema", "title", "description", "implicit"},
            code="invalid_contract",
            path=path,
            message=_INVALID_CONTRACT,
        )
        name_path = _pointer(path, "name")
        name = _normalize_name(descriptor["name"], path=name_path)
        if name in names:
            _fail("invalid_contract", name_path, _INVALID_CONTRACT)
        names.add(name)
        schema = WorkflowValueSchema._from_canonical(
            _parse_schema_dict(
                descriptor["schema"],
                path=_pointer(path, "schema"),
                allow_array=True,
                allow_nullable=True,
            ),
            token=_CANONICAL_CONSTRUCTOR_TOKEN,
        )
        implicit = descriptor.get("implicit", False)
        if type(implicit) is not bool:
            _fail(
                "invalid_contract",
                _pointer(path, "implicit"),
                _INVALID_CONTRACT,
            )
        item: dict[str, Any] = {
            "name": name,
            "schema": schema.to_dict(),
        }
        if "title" in descriptor:
            item["title"] = _normalize_presentation(
                descriptor["title"],
                path=_pointer(path, "title"),
            )
        if "description" in descriptor:
            item["description"] = _normalize_presentation(
                descriptor["description"],
                path=_pointer(path, "description"),
            )
        item["implicit"] = implicit
        normalized.append(item)
    canonical = _validate_json_value(
        {"version": 1, "outputs": normalized},
        code="invalid_contract",
        path="",
        message=_INVALID_CONTRACT,
    )
    return WorkflowOutputContract._from_canonical(
        canonical,
        token=_CANONICAL_CONSTRUCTOR_TOKEN,
    )


__all__ = [
    "WorkflowInputContract",
    "WorkflowOutputContract",
    "WorkflowSchemaError",
    "WorkflowValueSchema",
    "normalize_value",
    "parse_input_contract",
    "parse_output_contract",
    "parse_value_schema",
]
