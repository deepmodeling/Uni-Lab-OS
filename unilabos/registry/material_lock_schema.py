"""动作物料锁（Action Material Lock）Schema 的编译、校验和 UUID 提取。"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError

_LOCK_MARKER = "x-unilabos-material-lock"


class MaterialLockSchemaError(ValueError):
    """动作物料锁（Action Material Lock）Schema 或最终参数不合法。

    Attributes:
        code: 对接口稳定的错误码。
        path: 出错字段的 JSON Pointer 路径。
        message: 面向开发者的中文诊断信息。
    """

    def __init__(self, code: str, path: str, message: str) -> None:
        """保存可稳定投影到 HTTP 错误 envelope 的诊断信息。

        Args:
            code: 对接口稳定的错误码。
            path: 出错字段的 JSON Pointer 路径。
            message: 面向开发者的中文诊断信息。
        """

        super().__init__(message)
        self.code = code
        self.path = path
        self.message = message


@dataclass(frozen=True, slots=True)
class CompiledMaterialLockSchema:
    """只读的动作参数 Schema 及其 JSON Schema 校验器。

    Attributes:
        _goal_schema: 最终动作参数对象的独立 Schema 副本。
        _validator: 带 UUID 格式检查的 Draft 2020-12 校验器。
    """

    _goal_schema: dict[str, Any]
    _validator: Draft202012Validator

    def material_lock_uuids(self, param: Mapping[str, Any]) -> tuple[str, ...]:
        """校验最终动作参数并提取需要独占的物料 UUID。

        Args:
            param: 合并节点静态参数和上游 Handle 输出后的最终动作参数。

        Returns:
            去重、规范化并按字典序稳定排序的物料 UUID。

        Raises:
            MaterialLockSchemaError: 参数不满足 Schema，或锁标记对应的 UUID 无法解析。
        """

        if not isinstance(param, Mapping):
            raise MaterialLockSchemaError(
                "invalid_action_param",
                "/",
                "最终动作参数必须是对象",
            )
        # 上游 Handle 可把内部 PLR ``Resource`` 富对象传给后续动作；设备边界会
        # 按 ``ResourceSlot`` UUID 重新取回实例。物料锁解析只投影这类带锁标记
        # 的值，避免内部字段破坏稳定引用合同，同时保持其他参数严格校验。
        validation_param = _project_runtime_material_values(
            self._goal_schema,
            param,
            root_schema=self._goal_schema,
            ref_stack=(),
        )
        try:
            self._validator.validate(validation_param)
        except ValidationError as error:
            # ``error.absolute_path`` 是相对于 Goal 参数根对象的稳定字段路径。
            error_path = _json_pointer(tuple(error.absolute_path))
            code = (
                "material_lock_resolution_error"
                if error.validator == "format" and error.validator_value == "uuid"
                else "invalid_action_param"
            )
            raise MaterialLockSchemaError(
                code,
                error_path,
                f"最终动作参数不符合 Schema：{error.message}",
            ) from error

        # ``material_uuids`` 是本次作业（Job）需要申请物料锁的稳定身份集合。
        material_uuids: set[str] = set()
        _collect_material_uuids(
            self._goal_schema,
            validation_param,
            root_schema=self._goal_schema,
            path=(),
            output=material_uuids,
            ref_stack=(),
        )
        return tuple(sorted(material_uuids))


def compile_material_lock_schema(
    action_schema: Mapping[str, Any],
) -> CompiledMaterialLockSchema:
    """编译注册表（Registry）动作 Schema 的 Goal 参数部分。

    Args:
        action_schema: 注册表保存的完整动作 Schema envelope。

    Returns:
        可重复用于参数校验和物料 UUID 提取的只读编译结果。

    Raises:
        MaterialLockSchemaError: Schema 形状、引用或锁标记不合法。
    """

    if not isinstance(action_schema, Mapping):
        raise MaterialLockSchemaError(
            "invalid_material_lock_schema",
            "/",
            "动作 Schema 必须是对象",
        )
    # ``goal_schema`` 是设备实际收到的最终参数合同，不包含 feedback/result。
    goal_schema = (
        action_schema.get("properties", {}).get("goal")
        if isinstance(action_schema.get("properties"), Mapping)
        else None
    )
    if not isinstance(goal_schema, Mapping):
        raise MaterialLockSchemaError(
            "invalid_material_lock_schema",
            "/properties/goal",
            "动作 Schema 缺少 Goal 参数对象",
        )

    # 深复制隔离注册表的可变容器，避免后续热更新改变已编译合同。
    canonical_goal_schema = copy.deepcopy(dict(goal_schema))
    _validate_material_lock_extensions(canonical_goal_schema, path=())
    try:
        Draft202012Validator.check_schema(canonical_goal_schema)
        validator = Draft202012Validator(
            canonical_goal_schema,
            format_checker=FormatChecker(),
        )
    except SchemaError as error:
        raise MaterialLockSchemaError(
            "invalid_material_lock_schema",
            _json_pointer(tuple(error.absolute_schema_path)),
            f"动作参数 Schema 非法：{error.message}",
        ) from error
    return CompiledMaterialLockSchema(canonical_goal_schema, validator)


def _project_runtime_material_values(
    schema: Mapping[str, Any],
    value: Any,
    *,
    root_schema: Mapping[str, Any],
    ref_stack: tuple[str, ...],
) -> Any:
    """把内部 PLR 物料富对象投影为动作合同中的稳定引用形状。

    只处理带动作物料锁（Action Material Lock）布尔标记的字段；普通对象保留
    未知键，交给原 JSON Schema 继续失败关闭。这样既允许工作流（Workflow）
    Handle 传递完整资源，也不会放宽非物料动作参数。
    """

    reference = schema.get("$ref")
    if isinstance(reference, str):
        if reference in ref_stack:
            return value
        resolved_schema = dict(_resolve_local_ref(root_schema, reference))
        resolved_schema.update(
            {key: item for key, item in schema.items() if key != "$ref"}
        )
        return _project_runtime_material_values(
            resolved_schema,
            value,
            root_schema=root_schema,
            ref_stack=ref_stack + (reference,),
        )

    marker = schema.get(_LOCK_MARKER)
    properties = schema.get("properties")
    if type(marker) is bool and isinstance(value, Mapping):
        if not isinstance(properties, Mapping):
            return value
        projected = {
            name: _project_runtime_material_values(
                child_schema,
                value[name],
                root_schema=root_schema,
                ref_stack=ref_stack,
            )
            for name, child_schema in properties.items()
            if name in value and isinstance(child_schema, Mapping)
        }
        if (
            "uuid" in properties
            and "uuid" not in projected
            and "unilabos_uuid" in value
        ):
            # ROS 结果序列化保留 PLR 属性名；动作合同使用稳定线格式 ``uuid``。
            projected["uuid"] = value["unilabos_uuid"]
        return projected

    for union_key in ("anyOf", "oneOf"):
        union_members = schema.get(union_key)
        if isinstance(union_members, Sequence):
            for member in union_members:
                if not isinstance(member, Mapping):
                    continue
                projected = _project_runtime_material_values(
                    member,
                    value,
                    root_schema=root_schema,
                    ref_stack=ref_stack,
                )
                if _schema_accepts(member, projected, root_schema):
                    return projected

    if isinstance(value, Mapping) and isinstance(properties, Mapping):
        projected = dict(value)
        for name, child_schema in properties.items():
            if name in value and isinstance(child_schema, Mapping):
                projected[name] = _project_runtime_material_values(
                    child_schema,
                    value[name],
                    root_schema=root_schema,
                    ref_stack=ref_stack,
                )
        return projected

    items = schema.get("items")
    if (
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes))
        and isinstance(items, Mapping)
    ):
        return [
            _project_runtime_material_values(
                items,
                item,
                root_schema=root_schema,
                ref_stack=ref_stack,
            )
            for item in value
        ]
    return value


def _validate_material_lock_extensions(
    schema: Any,
    *,
    path: tuple[Any, ...],
) -> None:
    """递归验证锁标记类型，并拒绝会访问外部资源的引用。

    Args:
        schema: 当前递归位置的 Schema 值。
        path: 当前值在 Goal Schema 中的路径。

    Raises:
        MaterialLockSchemaError: 发现外部引用或非布尔锁标记。
    """

    if isinstance(schema, Mapping):
        reference = schema.get("$ref")
        if reference is not None and (
            not isinstance(reference, str) or not reference.startswith("#/")
        ):
            raise MaterialLockSchemaError(
                "invalid_material_lock_schema",
                _json_pointer(path + ("$ref",)),
                "动作物料锁 Schema 只允许同一 Goal Schema 内的本地引用",
            )
        marker = schema.get(_LOCK_MARKER)
        if marker is not None and type(marker) is not bool:
            raise MaterialLockSchemaError(
                "invalid_material_lock_schema",
                _json_pointer(path + (_LOCK_MARKER,)),
                "动作物料锁标记必须是布尔值",
            )
        for key, value in schema.items():
            _validate_material_lock_extensions(value, path=path + (key,))
        return
    if isinstance(schema, Sequence) and not isinstance(schema, (str, bytes)):
        for index, value in enumerate(schema):
            _validate_material_lock_extensions(value, path=path + (index,))


def _collect_material_uuids(
    schema: Mapping[str, Any],
    value: Any,
    *,
    root_schema: Mapping[str, Any],
    path: tuple[Any, ...],
    output: set[str],
    ref_stack: tuple[str, ...],
) -> None:
    """沿已验证参数递归收集标记为独占的物料 UUID。

    Args:
        schema: 当前参数值对应的 Schema。
        value: 当前已通过 JSON Schema 校验的参数值。
        root_schema: 用于解析本地 ``$ref`` 的 Goal Schema 根对象。
        path: 当前参数值的 JSON Pointer 路径。
        output: 本次作业（Job）的物料 UUID 去重集合。
        ref_stack: 当前递归链上的本地引用，用于拒绝循环引用。

    Raises:
        MaterialLockSchemaError: 本地引用非法、循环，或标记值不含合法 UUID。
    """

    reference = schema.get("$ref")
    if isinstance(reference, str):
        if reference in ref_stack:
            raise MaterialLockSchemaError(
                "invalid_material_lock_schema",
                _json_pointer(path),
                "动作物料锁 Schema 包含循环本地引用",
            )
        # Draft 2020-12 允许 ``$ref`` 带兄弟字段；锁标记可能正位于引用旁边。
        resolved_schema = dict(_resolve_local_ref(root_schema, reference))
        resolved_schema.update(
            {
                key: item
                for key, item in schema.items()
                if key != "$ref"
            }
        )
        _collect_material_uuids(
            resolved_schema,
            value,
            root_schema=root_schema,
            path=path,
            output=output,
            ref_stack=ref_stack + (reference,),
        )
        return

    marker = schema.get(_LOCK_MARKER)
    if marker is False or value is None:
        return
    if marker is True:
        if not isinstance(value, Mapping):
            raise MaterialLockSchemaError(
                "material_lock_resolution_error",
                _json_pointer(path),
                "动作物料锁值必须是包含 uuid 的物料稳定引用",
            )
        material_uuid = value.get("uuid")
        try:
            # 规范化可消除 UUID 大小写等文本差异形成的重复锁身份。
            canonical_uuid = str(uuid.UUID(material_uuid))
        except (AttributeError, TypeError, ValueError) as error:
            raise MaterialLockSchemaError(
                "material_lock_resolution_error",
                _json_pointer(path + ("uuid",)),
                "动作物料锁值必须包含合法的物料 UUID",
            ) from error
        output.add(canonical_uuid)
        return

    for union_key in ("allOf", "anyOf", "oneOf"):
        union_members = schema.get(union_key)
        if isinstance(union_members, Sequence):
            for member in union_members:
                if isinstance(member, Mapping) and _schema_accepts(member, value, root_schema):
                    _collect_material_uuids(
                        member,
                        value,
                        root_schema=root_schema,
                        path=path,
                        output=output,
                        ref_stack=ref_stack,
                    )

    properties = schema.get("properties")
    if isinstance(value, Mapping) and isinstance(properties, Mapping):
        for name, child_schema in properties.items():
            if name in value and isinstance(child_schema, Mapping):
                _collect_material_uuids(
                    child_schema,
                    value[name],
                    root_schema=root_schema,
                    path=path + (name,),
                    output=output,
                    ref_stack=ref_stack,
                )

    items = schema.get("items")
    if (
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes))
        and isinstance(items, Mapping)
    ):
        for index, item_value in enumerate(value):
            _collect_material_uuids(
                items,
                item_value,
                root_schema=root_schema,
                path=path + (index,),
                output=output,
                ref_stack=ref_stack,
            )


def _schema_accepts(
    schema: Mapping[str, Any],
    value: Any,
    root_schema: Mapping[str, Any],
) -> bool:
    """判断联合 Schema 的成员是否接受当前参数值。

    Args:
        schema: 联合 Schema 的候选成员。
        value: 需要匹配的最终参数值。
        root_schema: 提供本地定义的 Goal Schema 根对象。

    Returns:
        候选成员能够校验当前值时返回 ``True``。
    """

    # 将根定义并入临时候选，可让候选中的本地引用保持原有语义。
    candidate = dict(schema)
    if "$defs" in root_schema and "$defs" not in candidate:
        candidate["$defs"] = root_schema["$defs"]
    return Draft202012Validator(
        candidate,
        format_checker=FormatChecker(),
    ).is_valid(value)


def _resolve_local_ref(
    root_schema: Mapping[str, Any],
    reference: str,
) -> Mapping[str, Any]:
    """解析 Goal Schema 内的 JSON Pointer 本地引用。

    Args:
        root_schema: Goal Schema 根对象。
        reference: 以 ``#/`` 开头的本地 JSON Pointer。

    Returns:
        引用目标的 Schema 对象。

    Raises:
        MaterialLockSchemaError: 引用路径不存在或目标不是对象。
    """

    current: Any = root_schema
    for raw_token in reference[2:].split("/"):
        # JSON Pointer 使用 ``~1`` 表示斜线，使用 ``~0`` 表示波浪号。
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, Mapping) or token not in current:
            raise MaterialLockSchemaError(
                "invalid_material_lock_schema",
                reference,
                "动作物料锁 Schema 的本地引用不存在",
            )
        current = current[token]
    if not isinstance(current, Mapping):
        raise MaterialLockSchemaError(
            "invalid_material_lock_schema",
            reference,
            "动作物料锁 Schema 的本地引用目标必须是对象",
        )
    return current


def _json_pointer(path: tuple[Any, ...]) -> str:
    """把字段路径编码为稳定的 JSON Pointer。

    Args:
        path: 字段名和数组下标组成的路径。

    Returns:
        以 ``/`` 开头的 JSON Pointer；根路径返回 ``/``。
    """

    if not path:
        return "/"
    encoded_tokens = [
        str(token).replace("~", "~0").replace("/", "~1")
        for token in path
    ]
    return "/" + "/".join(encoded_tokens)


__all__ = [
    "CompiledMaterialLockSchema",
    "MaterialLockSchemaError",
    "compile_material_lock_schema",
]
