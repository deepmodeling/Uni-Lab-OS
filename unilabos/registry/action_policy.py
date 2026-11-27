"""Action exception policies shared by registry and runtime code."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Mapping, NotRequired, TypedDict

DEFAULT_ERROR_CLASS = "*"

SUCCESS_TYPE_NORMAL = "normal"
SUCCESS_TYPE_SKIP = "user_bypass_error"
SUCCESS_TYPE_OPERATOR_INTERVENTION = "operator_intervention"
SuccessType = Literal["normal", "user_bypass_error", "operator_intervention"]


class FallbackAction(TypedDict):
    """Server-side single action executed after operator approval."""

    action_name: str
    params: NotRequired[Dict[str, Any]]


class ErrorPolicyOption(TypedDict):
    """One option displayed for a matched exception class."""

    action: str
    label: str
    description: NotRequired[str]
    fallback_action: NotRequired[FallbackAction]
    then: NotRequired[Literal["retry", "skip", "abort"]]


class ErrorPolicy(TypedDict):
    """Exception class name -> approval options for one ``@action``."""

    options: Dict[str, List[ErrorPolicyOption]]
    max_retries: NotRequired[int]
    decision_timeout_seconds: NotRequired[float]
    default_on_decision_timeout: NotRequired[Literal["abort", "retry", "skip"]]


@dataclass(frozen=True)
class ActionDecisionOutcome:
    """Internal runtime result carrying the successful resolution type."""

    value: Any
    suc_type: SuccessType
    audit_error: str = ""


def _normalize_fallback_action(value: Any) -> FallbackAction:
    if isinstance(value, str):
        if not value:
            raise ValueError("fallback_action action_name 不能为空")
        return {"action_name": value, "params": {}}
    if not isinstance(value, Mapping):
        raise TypeError("fallback_action 必须是动作名字符串或字典")

    action_name = value.get("action_name") or value.get("name")
    if not isinstance(action_name, str) or not action_name:
        raise ValueError("fallback_action.action_name 必须是非空字符串")
    params = value.get("params", {})
    if not isinstance(params, Mapping):
        raise TypeError("fallback_action.params 必须是字典")
    return {"action_name": action_name, "params": deepcopy(dict(params))}


def _normalize_option(value: Any) -> ErrorPolicyOption:
    if not isinstance(value, Mapping):
        raise TypeError("error_policy option 必须是字典")
    action = value.get("action")
    label = value.get("label")
    if not isinstance(action, str) or not action:
        raise ValueError("error_policy option.action 必须是非空字符串")
    if not isinstance(label, str) or not label:
        raise ValueError("error_policy option.label 必须是非空字符串")

    option: ErrorPolicyOption = {"action": action, "label": label}
    description = value.get("description")
    if description is not None:
        option["description"] = str(description)
    if value.get("fallback_action") is not None:
        option["fallback_action"] = _normalize_fallback_action(
            value["fallback_action"]
        )
    then = value.get("then")
    if then is not None:
        if then not in {"retry", "skip", "abort"}:
            raise ValueError("error_policy option.then 仅支持 retry/skip/abort")
        option["then"] = then
    return option


def normalize_error_policy(
    policy: Mapping[str, Any] | None,
    *,
    default_on_user_timeout: str | None = None,
) -> Dict[str, Any] | None:
    """Validate and copy a policy into a registry-safe representation.

    ``options`` is keyed by exception class name. A legacy flat option list is
    accepted as the ``"*"`` fallback to ease selective migration.
    """

    if not policy:
        return None
    raw_options = policy.get("options")
    # 2.6 的公开写法是一个扁平 options 列表，并用 allow_retry/
    # allow_skip 打开框架选项。旧的“异常类名 -> options”格式继续保留，
    # 以免已接入的驱动被本次升级破坏。
    is_flat_policy = raw_options is None or isinstance(raw_options, list)
    if raw_options is None:
        raw_options = []
    if isinstance(raw_options, list):
        options_list = list(raw_options)
        if policy.get("allow_retry"):
            options_list.insert(0, {"action": "retry", "label": "重试"})
        if policy.get("allow_skip"):
            options_list.append({"action": "skip", "label": "跳过"})
        # 未声明 error_policy 时不会调用本函数；显式空策略仍需要一个
        # 可见的终止选项，否则人工会话无法收敛。
        if not options_list:
            options_list.append({"action": "abort", "label": "终止"})
        raw_options = {DEFAULT_ERROR_CLASS: options_list}
    if not isinstance(raw_options, Mapping) or not raw_options:
        raise ValueError("error_policy.options 必须是非空的异常类名到 option 列表映射")

    options: Dict[str, List[ErrorPolicyOption]] = {}
    for error_class_name, raw_class_options in raw_options.items():
        if not isinstance(error_class_name, str) or not error_class_name:
            raise ValueError("error_policy.options 的异常类名必须是非空字符串")
        if not isinstance(raw_class_options, list) or not raw_class_options:
            raise ValueError(
                f"error_policy.options[{error_class_name!r}] 必须是非空列表"
            )
        options[error_class_name] = [
            _normalize_option(option) for option in raw_class_options
        ]

    normalized: Dict[str, Any] = {"options": options}
    max_retries = policy.get("max_retries", 3)
    if not isinstance(max_retries, int) or max_retries < 0:
        raise ValueError("error_policy.max_retries 必须是非负整数")
    normalized["max_retries"] = max_retries

    decision_timeout = policy.get("decision_timeout_seconds", 300.0)
    if not isinstance(decision_timeout, (int, float)) or decision_timeout <= 0:
        raise ValueError("error_policy.decision_timeout_seconds 必须大于 0")
    normalized["decision_timeout_seconds"] = float(decision_timeout)

    timeout_action = policy.get(
        "default_on_user_timeout",
        policy.get(
            "default_on_decision_timeout",
            default_on_user_timeout or "abort",
        ),
    )
    if timeout_action not in {"abort", "retry", "skip"}:
        raise ValueError("default_on_decision_timeout 仅支持 abort/retry/skip")
    normalized["default_on_decision_timeout"] = timeout_action
    if is_flat_policy:
        normalized["allow_retry"] = bool(policy.get("allow_retry", False))
        normalized["allow_skip"] = bool(policy.get("allow_skip", False))
    return normalized


def resolve_error_options(
    policy: Mapping[str, Any] | None,
    exc: BaseException,
) -> List[Dict[str, Any]]:
    """Resolve options by exception MRO, then the ``*`` fallback."""

    if not policy:
        return []
    options = policy.get("options")
    if not isinstance(options, Mapping):
        return []
    for error_class in type(exc).__mro__:
        matched = options.get(error_class.__name__)
        if isinstance(matched, list):
            return deepcopy(matched)
    fallback = options.get(DEFAULT_ERROR_CLASS)
    return deepcopy(fallback) if isinstance(fallback, list) else []
