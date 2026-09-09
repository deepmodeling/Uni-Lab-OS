"""OPC 模拟器 profile 草稿生成、校验与安全持久化。"""

from __future__ import annotations

import errno
import json
import hashlib
import math
import os
import re
import secrets
import stat
import threading
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - 生产目标为 macOS/Linux
    fcntl = None  # type: ignore[assignment]

from scripts.szlab_action_sensor_variables import (
    ROBOT_HANDSHAKE_MANUAL_INITIAL_VALUES,
    ROBOT_MANUAL_OPC_VARIABLES,
    infer_sensor_variable_data_type,
    resolve_action_sensor_variables,
    resolve_robot_action_opc_variables,
)
from scripts.szlab_task_opc_simulator import SimulatorProfile, _parse_profile


_SAFE_FILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*\.json$")
MAX_COLLECTION_ITEMS = 500
MAX_TRIGGERS = 1000
MAX_PARAMS_DEPTH = 20
MAX_PARAMS_SCALARS = 10_000
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_SCHEDULED_TEMPLATE_IDS = 1000
MAX_UNIQUE_SCHEDULED_TEMPLATES = 500
MAX_TRIGGER_OBSERVATIONS = 5000
MAX_ACTION_CATALOG_ITEMS = 500
MAX_OPC_VARIABLES_PER_DECLARATION = 500
MAX_TOTAL_DECLARED_OPC_VARIABLES = 5000
MAX_VARIABLE_CATALOG_ITEMS = 500
MAX_IDENTIFIER_LENGTH = 256
MAX_VARIABLE_NAME_LENGTH = 512
MAX_TEMPLATE_NODE_ASSOCIATIONS = 5000
_LOCK_DIRECTORY_NAME = ".opc-profile-locks"
_FILE_LOCKS: dict[tuple[str, str], threading.RLock] = {}
_FILE_LOCKS_GUARD = threading.Lock()


class ProfileValidationError(ValueError):
    """runnable profile 含校验错误。"""

    def __init__(self, result: dict[str, Any]) -> None:
        super().__init__("runnable profile 含校验错误")
        self.result = result


class ProfileLimitError(ValueError):
    """profile 请求超过有界复杂度。"""

    def __init__(self, validation_errors: list[str]) -> None:
        super().__init__("profile 请求超过资源限制")
        self.validation_errors = validation_errors


class RevisionConflict(ValueError):
    """If-Match 与当前文件 revision 不一致。"""


REVISION_FORCE_REPLACE = "*"


class ProfileStorageError(ValueError):
    """profile 文件类型、所有权或权限不安全。"""


def _file_lock(config_dir: str | Path, file_name: str) -> threading.RLock:
    key = (str(Path(config_dir).resolve()), file_name)
    with _FILE_LOCKS_GUARD:
        return _FILE_LOCKS.setdefault(key, threading.RLock())


def _error(errors: dict[str, None], path: str) -> None:
    errors.setdefault(path, None)


def _scalar_type(value: Any) -> str | None:
    if type(value) is bool:
        return "bool"
    if type(value) is int:
        return "int"
    if type(value) is float and math.isfinite(value):
        return "float"
    if type(value) is str:
        return "string"
    return None


def _catalog_type_accepts(catalog_type: str, observed_type: str | None) -> bool:
    if catalog_type == "float":
        return observed_type in {"int", "float"}
    return catalog_type == observed_type


_VALID_PROFILE_DATA_TYPES = frozenset({"bool", "int", "float", "string"})
_PC_TO_PLC_MARKERS = (
    "工艺选择",
    "参数写入完成",
    "任务号",
    "Robot_任务写入完成",
    "取放料产品",
    "取放料编号",
    "倒料产品选择",
)
_PLC_TO_PC_MARKERS = (
    "工艺完成",
    "加工完成",
    "允许加工",
    "原点信号",
    "准备信号",
    "Robot_Home",
    "Robot_任务允许写入",
    "Robot_任务完成",
    "工站状态",
    "磁搅状态",
    "读数稳定",
    "剩余液量",
)


def _scalar_to_data_type(value: Any) -> str:
    observed = _scalar_type(value)
    if observed in _VALID_PROFILE_DATA_TYPES:
        return observed
    return "unknown"


def _infer_variable_direction(name: str, *, source: str) -> str:
    if source in {"task_input", "task_output", "action_sensor"}:
        return "plc_to_pc"
    if name.startswith("传感器状态_上位机"):
        return "plc_to_pc"
    if any(marker in name for marker in _PC_TO_PLC_MARKERS):
        return "pc_to_plc"
    if any(marker in name for marker in _PLC_TO_PC_MARKERS):
        return "plc_to_pc"
    return "plc_to_pc"


def _infer_variable_data_type(
    name: str,
    *,
    source: str,
    catalog_type: str | None = None,
    observed_value: Any = None,
) -> str:
    if catalog_type in _VALID_PROFILE_DATA_TYPES:
        return catalog_type
    if source == "action_sensor" or name.startswith("传感器状态_上位机"):
        return infer_sensor_variable_data_type(name)
    if observed_value is not None:
        return _scalar_to_data_type(observed_value)
    if "工艺完成" in name or name in {"Robot_任务完成", "任务号"}:
        return "int"
    if any(marker in name for marker in ("温度", "速度", "重量", "液量", "读数", "时间设置")):
        return "float"
    if any(marker in name for marker in ("完成", "允许", "原点", "准备", "Robot_Home", "稳定")):
        if "工艺完成" in name:
            return "int"
        return "bool"
    return "unknown"


def _strict_text_alias(
    raw: dict[str, Any],
    path: str,
    aliases: tuple[str, ...],
    errors: dict[str, None],
    *,
    max_length: int = MAX_IDENTIFIER_LENGTH,
) -> tuple[str | None, str]:
    field_name = next((alias for alias in aliases if alias in raw), aliases[0])
    value = raw.get(field_name)
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.strip()) > max_length
    ):
        _error(errors, f"{path}.{field_name}")
        return None, field_name
    return value.strip(), field_name


def _strict_opc_variables(
    value: Any,
    path: str,
    errors: dict[str, None],
) -> list[str] | None:
    if not isinstance(value, list):
        _error(errors, path)
        return None
    if len(value) > MAX_OPC_VARIABLES_PER_DECLARATION:
        _error(errors, path)
        return None
    variables: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            _error(errors, f"{path}[{index}]")
            continue
        variable = item.strip()
        if len(variable) > MAX_VARIABLE_NAME_LENGTH:
            _error(errors, f"{path}[{index}]")
            continue
        if variable not in variables:
            variables.append(variable)
    return variables


def _workflow_node_map(
    workflow: Any,
    errors: dict[str, None],
    selected_node_ids: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    root = workflow.get("data", workflow) if isinstance(workflow, dict) else {}
    raw_nodes = root.get("nodes", []) if isinstance(root, dict) else []
    indexed_nodes: list[tuple[dict[str, Any], str, tuple[str, ...]]] = []
    if isinstance(root, dict) and not raw_nodes and isinstance(root.get("rules"), list):
        for rule_index, rule in enumerate(root["rules"]):
            if not isinstance(rule, dict) or not isinstance(rule.get("actions"), list):
                continue
            for action_index, action in enumerate(rule["actions"]):
                if isinstance(action, dict) and isinstance(action.get("action"), dict):
                    indexed_nodes.append(
                        (
                            action["action"],
                            f"workflow.rules[{rule_index}].actions[{action_index}].action",
                            ("workflow_node_id", "uuid", "id"),
                        )
                    )
    elif isinstance(raw_nodes, list):
        indexed_nodes = [
            (
                raw,
                f"workflow.nodes[{index}]",
                ("workflow_node_id", "uuid", "id"),
            )
            for index, raw in enumerate(raw_nodes)
            if isinstance(raw, dict)
        ]
    result: dict[str, dict[str, Any]] = {}
    ambiguous: set[str] = set()
    for raw, path, id_aliases in indexed_nodes:
        node_id, id_field = _strict_text_alias(raw, path, id_aliases, errors)
        if (
            node_id is not None
            and selected_node_ids is not None
            and node_id not in selected_node_ids
        ):
            continue
        display_name = raw.get("name")
        if display_name is not None and (
            not isinstance(display_name, str)
            or not display_name.strip()
            or len(display_name.strip()) > MAX_IDENTIFIER_LENGTH
        ):
            _error(errors, f"{path}.name")
        device_id, _ = _strict_text_alias(
            raw,
            path,
            ("device_id", "deviceId", "device_name", "resource_name"),
            errors,
        )
        method, _ = _strict_text_alias(
            raw,
            path,
            ("method",),
            errors,
        )
        if node_id is None or device_id is None or method is None:
            continue
        if node_id in result or node_id in ambiguous:
            _error(errors, f"{path}.{id_field}")
            result.pop(node_id, None)
            ambiguous.add(node_id)
            continue
        params = raw.get("params", raw.get("param", {}))
        raw_catalog_device_id = raw.get("device_id")
        catalog_device_id = (
            raw_catalog_device_id.strip()
            if isinstance(raw_catalog_device_id, str)
            and raw_catalog_device_id.strip()
            else None
        )
        raw_catalog_method = raw.get("method")
        catalog_method = (
            raw_catalog_method.strip()
            if isinstance(raw_catalog_method, str) and raw_catalog_method.strip()
            else None
        )
        has_opc_variables = "opc_variables" in raw
        opc_variables = (
            _strict_opc_variables(
                raw.get("opc_variables"),
                f"{path}.opc_variables",
                errors,
            )
            if has_opc_variables
            else None
        )
        result[node_id] = {
            "workflow_node_id": node_id,
            "device_id": device_id,
            "method": method,
            "catalog_device_id": catalog_device_id,
            "catalog_method": catalog_method,
            "params": dict(params) if isinstance(params, dict) else {},
            "opc_variables": opc_variables,
            "opc_variables_declared": has_opc_variables,
            "validation_path": path,
        }
    return result


def _params_depth_and_scalars(value: Any) -> tuple[int, int]:
    max_depth = 0
    scalars = 0
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        max_depth = max(max_depth, depth)
        if depth > MAX_PARAMS_DEPTH:
            return max_depth, scalars
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
        else:
            scalars += 1
    return max_depth, scalars


def validate_payload_limits(
    *,
    workflow: Any = None,
    templates: Any = None,
    profile: Any = None,
    action_catalog: Any = None,
    variable_catalog: Any = None,
) -> None:
    """拒绝会导致高内存、深递归或超线性处理的输入。"""
    errors: dict[str, None] = {}
    params: list[tuple[str, Any]] = []
    if isinstance(templates, list):
        if len(templates) > MAX_COLLECTION_ITEMS:
            _error(errors, "templates")
        template_node_associations = 0
        for index, template in enumerate(templates[: MAX_COLLECTION_ITEMS + 1]):
            if not isinstance(template, dict):
                continue
            node_ids = template.get("node_ids")
            if isinstance(node_ids, list):
                template_node_associations += len(node_ids)
                if len(node_ids) > MAX_COLLECTION_ITEMS:
                    _error(errors, f"templates[{index}].node_ids")
            for trigger_key in ("input_triggers", "output_triggers"):
                triggers = template.get(trigger_key)
                if isinstance(triggers, list) and len(triggers) > MAX_TRIGGERS:
                    _error(errors, f"templates[{index}].{trigger_key}")
            input_count = (
                len(template["input_triggers"])
                if isinstance(template.get("input_triggers"), list)
                else 0
            )
            output_count = (
                len(template["output_triggers"])
                if isinstance(template.get("output_triggers"), list)
                else 0
            )
            if (
                input_count <= MAX_TRIGGERS
                and output_count <= MAX_TRIGGERS
                and input_count + output_count > MAX_TRIGGERS
            ):
                _error(errors, f"templates[{index}].triggers")
        if template_node_associations > MAX_TEMPLATE_NODE_ASSOCIATIONS:
            _error(errors, "template_node_associations")

    if isinstance(action_catalog, list):
        if len(action_catalog) > MAX_ACTION_CATALOG_ITEMS:
            _error(errors, "action_catalog")
        declared_variable_count = 0
        for index, action in enumerate(
            action_catalog[: MAX_ACTION_CATALOG_ITEMS + 1]
        ):
            if not isinstance(action, dict):
                continue
            opc_variables = action.get("opc_variables")
            if isinstance(opc_variables, list):
                declared_variable_count += len(opc_variables)
                if len(opc_variables) > MAX_OPC_VARIABLES_PER_DECLARATION:
                    _error(errors, f"action_catalog[{index}].opc_variables")
        if declared_variable_count > MAX_TOTAL_DECLARED_OPC_VARIABLES:
            _error(errors, "action_catalog.opc_variables")

    if isinstance(variable_catalog, list) and len(variable_catalog) > MAX_VARIABLE_CATALOG_ITEMS:
        _error(errors, "variable_catalog")

    root = workflow.get("data", workflow) if isinstance(workflow, dict) else {}
    workflow_declared_variable_count = 0
    if isinstance(root, dict):
        nodes = root.get("nodes")
        if isinstance(nodes, list):
            if len(nodes) > MAX_COLLECTION_ITEMS:
                _error(errors, "workflow.nodes")
            for index, node in enumerate(nodes[: MAX_COLLECTION_ITEMS + 1]):
                if isinstance(node, dict):
                    opc_variables = node.get("opc_variables")
                    if isinstance(opc_variables, list):
                        workflow_declared_variable_count += len(opc_variables)
                        if len(opc_variables) > MAX_OPC_VARIABLES_PER_DECLARATION:
                            _error(
                                errors,
                                f"workflow.nodes[{index}].opc_variables",
                            )
                    params.append(
                        (
                            f"workflow.nodes[{index}].params",
                            node.get("params", node.get("param", {})),
                        )
                    )
        elif isinstance(root.get("rules"), list):
            action_count = 0
            for rule_index, rule in enumerate(root["rules"]):
                if not isinstance(rule, dict) or not isinstance(
                    rule.get("actions"), list
                ):
                    continue
                for action_index, wrapped in enumerate(rule["actions"]):
                    if not isinstance(wrapped, dict) or not isinstance(
                        wrapped.get("action"), dict
                    ):
                        continue
                    action_count += 1
                    action = wrapped["action"]
                    opc_variables = action.get("opc_variables")
                    if isinstance(opc_variables, list):
                        workflow_declared_variable_count += len(opc_variables)
                        if len(opc_variables) > MAX_OPC_VARIABLES_PER_DECLARATION:
                            _error(
                                errors,
                                f"workflow.rules[{rule_index}].actions"
                                f"[{action_index}].action.opc_variables",
                            )
                    params.append(
                        (
                            f"workflow.rules[{rule_index}].actions"
                            f"[{action_index}].action.params",
                            action.get("params", action.get("param", {})),
                        )
                    )
            if action_count > MAX_COLLECTION_ITEMS:
                _error(errors, "workflow.nodes")
    if workflow_declared_variable_count > MAX_TOTAL_DECLARED_OPC_VARIABLES:
        _error(errors, "workflow.opc_variables")

    if isinstance(profile, dict):
        variables = profile.get("variables")
        if isinstance(variables, list) and len(variables) > MAX_COLLECTION_ITEMS:
            _error(errors, "profile.variables")
        nodes = profile.get("nodes")
        if isinstance(nodes, list):
            if len(nodes) > MAX_COLLECTION_ITEMS:
                _error(errors, "profile.nodes")
            for index, node in enumerate(nodes[: MAX_COLLECTION_ITEMS + 1]):
                if isinstance(node, dict):
                    params.append(
                        (f"profile.nodes[{index}].params", node.get("params", {}))
                    )

    scalar_total = 0
    scalar_limit_reported = False
    for path, value in params:
        depth, scalar_count = _params_depth_and_scalars(value)
        scalar_total += scalar_count
        if depth > MAX_PARAMS_DEPTH:
            _error(errors, path)
        if scalar_total > MAX_PARAMS_SCALARS and not scalar_limit_reported:
            _error(errors, path)
            scalar_limit_reported = True
    if errors:
        raise ProfileLimitError(list(errors))


def _profile_to_dict(profile: SimulatorProfile) -> dict[str, Any]:
    variables: list[dict[str, Any]] = []
    for item in profile.variables:
        variable = {
            "name": item.name,
            "direction": item.direction,
            "data_type": item.data_type,
            "source": item.source,
        }
        if item.has_initial_value:
            variable["initial_value"] = item.initial_value
        variables.append(variable)

    def group(value: Any) -> dict[str, Any] | None:
        if value is None:
            return None
        return {
            "all": [
                {
                    "variable": condition.variable,
                    "operator": condition.operator,
                    "value": condition.value,
                    "edge": condition.edge,
                }
                for condition in value.all
            ]
        }

    def phase(value: Any, *, delayed: bool) -> dict[str, Any] | None:
        if value is None:
            return None
        result: dict[str, Any] = {
            "writes": [
                {"variable": operation.name, "value": operation.value}
                for operation in value.writes
            ]
        }
        if delayed:
            result["delay"] = value.delay
        return result

    return {
        "schema_version": profile.schema_version,
        "status": profile.status,
        "name": profile.name,
        "opc": {
            "url": profile.url,
            "poll_interval": profile.poll_interval,
            "io_timeout": profile.io_timeout,
        },
        "variables": variables,
        "nodes": [
            {
                "workflow_node_id": node.workflow_node_id,
                "task_template_ids": list(node.task_template_ids),
                "device_id": node.device_id,
                "method": node.method,
                "params": node.params,
                "channel": node.channel,
                "trigger": group(node.trigger),
                "on_trigger": phase(node.on_trigger, delayed=False),
                "on_complete": phase(node.on_complete, delayed=True),
                "reset_when": group(node.reset_when),
                "after_reset": phase(node.after_reset, delayed=True),
            }
            for node in profile.nodes
        ],
    }


def validate_profile(profile: Any, file_name: str) -> dict[str, Any]:
    """通过模拟器 schema 解析器返回规范化 profile 与 JSON path 错误。"""
    validate_payload_limits(profile=profile)
    candidate = dict(profile) if isinstance(profile, dict) else profile
    requested_status = candidate.get("status") if isinstance(candidate, dict) else None
    if requested_status == "runnable":
        candidate = dict(candidate)
        candidate["status"] = "draft"
    parsed = _parse_profile(candidate)
    normalized = _profile_to_dict(parsed)
    if requested_status == "runnable":
        normalized["status"] = "runnable"
    return {
        "profile": normalized,
        "validation_errors": list(parsed.validation_errors),
        "file_name": file_name,
    }


def resolve_profile_path(file_name: Any, config_dir: str | Path) -> Path:
    """仅允许配置目录下一层 ASCII slug JSON 文件。"""
    if (
        not isinstance(file_name, str)
        or len(file_name) > MAX_IDENTIFIER_LENGTH
        or not _SAFE_FILE_NAME.fullmatch(file_name)
    ):
        raise ValueError("file_name 必须是安全 ASCII slug + .json")
    root = Path(config_dir).resolve()
    candidate = root / file_name
    if candidate.parent.resolve() != root:
        raise ValueError("file_name 必须位于模拟器配置目录")
    return candidate


def _open_directory(config_dir: str | Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    return os.open(Path(config_dir).resolve(), flags)


def _open_profile_process_lock(dir_fd: int, file_name: str) -> tuple[int, int]:
    """安全创建并独占稳定 lock file，调用方负责解锁和关闭。"""
    if fcntl is None:
        raise RuntimeError("当前平台不支持 profile 文件锁")
    try:
        os.mkdir(_LOCK_DIRECTORY_NAME, 0o700, dir_fd=dir_fd)
    except FileExistsError:
        pass
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        lock_dir_fd = os.open(
            _LOCK_DIRECTORY_NAME,
            directory_flags,
            dir_fd=dir_fd,
        )
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ValueError("profile 锁目录安全属性无效") from exc
        raise
    try:
        directory_metadata = os.fstat(lock_dir_fd)
        if (
            not stat.S_ISDIR(directory_metadata.st_mode)
            or directory_metadata.st_uid != os.getuid()
            or stat.S_IMODE(directory_metadata.st_mode) != 0o700
        ):
            raise ValueError("profile 锁目录安全属性无效")
        lock_name = f"{file_name}.lock"
        try:
            lock_fd = os.open(
                lock_name,
                os.O_RDWR
                | os.O_CREAT
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=lock_dir_fd,
            )
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.EMLINK):
                raise ValueError("profile 锁文件安全属性无效") from exc
            raise
        try:
            metadata = os.fstat(lock_fd)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_nlink != 1
            ):
                raise ValueError("profile 锁文件安全属性无效")
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
        except BaseException:
            os.close(lock_fd)
            raise
    except BaseException:
        os.close(lock_dir_fd)
        raise
    return lock_dir_fd, lock_fd


def _close_profile_process_lock(lock_dir_fd: int, lock_fd: int) -> None:
    if fcntl is not None:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    os.close(lock_fd)
    os.close(lock_dir_fd)


def _validate_profile_fd(fd: int) -> None:
    metadata = os.fstat(fd)
    mode = stat.S_IMODE(metadata.st_mode)
    if not stat.S_ISREG(metadata.st_mode):
        raise ProfileStorageError("profile 目标必须是普通文件")
    if metadata.st_uid != os.getuid() or mode & 0o022 or metadata.st_nlink != 1:
        raise ProfileStorageError("profile 文件安全属性无效")


def _open_profile_fd(dir_fd: int, file_name: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(file_name, flags, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise ProfileStorageError("拒绝读取或覆盖符号链接") from exc
        raise
    try:
        _validate_profile_fd(fd)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _serialized_profile_bytes(profile: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            profile,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _with_revision(
    result: dict[str, Any],
    content: bytes,
) -> dict[str, Any]:
    return {
        **result,
        "revision": hashlib.sha256(content).hexdigest(),
    }


def _read_profile_at(
    dir_fd: int,
    file_name: str,
) -> tuple[dict[str, Any], bytes]:
    fd = _open_profile_fd(dir_fd, file_name)
    with os.fdopen(fd, "rb") as handle:
        content = handle.read(MAX_JSON_BYTES + 1)
    if len(content) > MAX_JSON_BYTES:
        raise ValueError("profile JSON 超过大小限制")
    return json.loads(content), content


def save_profile(
    file_name: str,
    profile: Any,
    config_dir: str | Path,
    *,
    expected_revision: str | None = None,
) -> dict[str, Any]:
    """校验并以同目录临时文件 + replace 原子保存 profile。"""
    path = resolve_profile_path(file_name, config_dir)
    result = validate_profile(profile, file_name)
    if result["profile"]["status"] == "runnable" and result["validation_errors"]:
        raise ProfileValidationError(result)
    serialized = _serialized_profile_bytes(result["profile"])
    if len(serialized) > MAX_JSON_BYTES:
        raise ProfileLimitError(["profile"])
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = _file_lock(config_dir, file_name)
    with lock:
        dir_fd = _open_directory(config_dir)
        lock_dir_fd = -1
        lock_fd = -1
        temporary_name = (
            f".{file_name}.{os.getpid()}.{threading.get_ident()}."
            f"{secrets.token_hex(8)}.tmp"
        )
        temporary_created = False
        try:
            lock_dir_fd, lock_fd = _open_profile_process_lock(dir_fd, file_name)
            try:
                current_payload, current_content = _read_profile_at(
                    dir_fd, file_name
                )
            except FileNotFoundError:
                current_payload = None
                current_content = None
            if expected_revision == REVISION_FORCE_REPLACE:
                pass
            elif current_payload is None:
                if expected_revision is not None:
                    raise RevisionConflict("revision 不匹配")
            else:
                assert current_content is not None
                current = _with_revision(
                    validate_profile(current_payload, file_name),
                    current_content,
                )
                if (
                    expected_revision is None
                    or expected_revision != current["revision"]
                ):
                    raise RevisionConflict("revision 不匹配")

            temporary_fd = os.open(
                temporary_name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=dir_fd,
            )
            temporary_created = True
            with os.fdopen(temporary_fd, "wb") as handle:
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            check_fd = _open_profile_fd(dir_fd, temporary_name)
            os.close(check_fd)
            os.replace(
                temporary_name,
                file_name,
                src_dir_fd=dir_fd,
                dst_dir_fd=dir_fd,
            )
            temporary_created = False
            os.fsync(dir_fd)
        finally:
            if temporary_created:
                try:
                    os.unlink(temporary_name, dir_fd=dir_fd)
                except FileNotFoundError:
                    pass
            if lock_fd >= 0:
                _close_profile_process_lock(lock_dir_fd, lock_fd)
            os.close(dir_fd)
    return _with_revision(result, serialized)


def read_profile(
    file_name: str,
    config_dir: str | Path,
) -> dict[str, Any]:
    """按与保存相同的路径安全规则读取并规范化 profile。"""
    resolve_profile_path(file_name, config_dir)
    lock = _file_lock(config_dir, file_name)
    with lock:
        dir_fd = _open_directory(config_dir)
        try:
            payload, content = _read_profile_at(dir_fd, file_name)
        finally:
            os.close(dir_fd)
    return _with_revision(validate_profile(payload, file_name), content)


def list_profile_files(config_dir: str | Path) -> list[str]:
    """列出配置目录下一层合法 profile 文件名（ASCII slug.json）。"""
    root = Path(config_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    files: list[str] = []
    for path in sorted(root.iterdir()):
        if not path.is_file():
            continue
        if _SAFE_FILE_NAME.fullmatch(path.name):
            files.append(path.name)
    return files


def migrate_legacy_opc_profiles(
    config_dir: str | Path,
    legacy_dir: str | Path,
    *,
    reference_profile: str | None = None,
) -> list[str]:
    """将 scripts/config 下 profile 种子文件复制到 configs（不覆盖已有文件）。"""
    target_root = Path(config_dir).resolve()
    source_root = Path(legacy_dir).resolve()
    target_root.mkdir(parents=True, exist_ok=True)
    migrated: list[str] = []
    if not source_root.is_dir():
        return migrated

    def _copy_if_missing(file_name: str) -> None:
        if not _SAFE_FILE_NAME.fullmatch(file_name):
            return
        source = source_root / file_name
        if not source.is_file():
            return
        target = target_root / file_name
        if target.exists():
            return
        target.write_bytes(source.read_bytes())
        migrated.append(file_name)

    if reference_profile:
        _copy_if_missing(reference_profile)
    for path in sorted(source_root.glob("*-opc-simulator.json")):
        _copy_if_missing(path.name)
    return migrated


def _action_catalog_map(
    action_catalog: Any,
    errors: dict[str, None],
) -> tuple[dict[tuple[str, str], list[str]], set[tuple[str, str]]]:
    if action_catalog is None:
        return {}, set()
    if not isinstance(action_catalog, list):
        _error(errors, "action_catalog")
        return {}, set()
    result: dict[tuple[str, str], list[str]] = {}
    ambiguous: set[tuple[str, str]] = set()
    for index, raw in enumerate(action_catalog):
        path = f"action_catalog[{index}]"
        if not isinstance(raw, dict):
            _error(errors, path)
            continue
        for key in raw:
            if key not in {"device_id", "method", "opc_variables"}:
                _error(errors, f"{path}.{key}")
        device_id, _ = _strict_text_alias(
            raw, path, ("device_id",), errors
        )
        method, _ = _strict_text_alias(raw, path, ("method",), errors)
        variables = _strict_opc_variables(
            raw.get("opc_variables"),
            f"{path}.opc_variables",
            errors,
        )
        if device_id is None or method is None or variables is None:
            continue
        key = (device_id, method)
        if key in result or key in ambiguous:
            _error(errors, path)
            result.pop(key, None)
            ambiguous.add(key)
            continue
        result[key] = variables
    return result, ambiguous


def _variable_type_catalog(
    variable_catalog: Any,
    errors: dict[str, None],
) -> dict[str, tuple[str, str]]:
    if variable_catalog is None:
        return {}
    if not isinstance(variable_catalog, list):
        _error(errors, "variable_catalog")
        return {}
    result: dict[str, tuple[str, str]] = {}
    ambiguous: set[str] = set()
    for index, raw in enumerate(variable_catalog):
        path = f"variable_catalog[{index}]"
        if not isinstance(raw, dict):
            _error(errors, path)
            continue
        for key in raw:
            if key not in {"name", "data_type"}:
                _error(errors, f"{path}.{key}")
        name, _ = _strict_text_alias(
            raw,
            path,
            ("name",),
            errors,
            max_length=MAX_VARIABLE_NAME_LENGTH,
        )
        data_type = raw.get("data_type")
        if data_type not in {"bool", "int", "float", "string"}:
            _error(errors, f"{path}.data_type")
            data_type = None
        if name is None or data_type is None:
            continue
        if name in result or name in ambiguous:
            _error(errors, path)
            result.pop(name, None)
            ambiguous.add(name)
            continue
        result[name] = (data_type, f"{path}.data_type")
    return result


def filter_variable_catalog(
    variable_catalog: Any,
    *,
    workflow: Any = None,
    templates: Any = None,
    scheduled_template_ids: Any = None,
    action_catalog: Any = None,
) -> list[dict[str, Any]]:
    """仅保留生成草稿实际会引用到的变量类型声明。"""
    if not isinstance(variable_catalog, list):
        return []
    referenced: set[str] = set()
    scheduled_ids = {
        str(template_id).strip()
        for template_id in (scheduled_template_ids or [])
        if isinstance(template_id, str) and template_id.strip()
    }
    catalog_by_key: dict[tuple[str, str], list[str]] = {}
    if isinstance(action_catalog, list):
        for action in action_catalog:
            if not isinstance(action, dict):
                continue
            device_id = action.get("device_id")
            method = action.get("method")
            opc_variables = action.get("opc_variables")
            if (
                isinstance(device_id, str)
                and device_id.strip()
                and isinstance(method, str)
                and method.strip()
                and isinstance(opc_variables, list)
            ):
                catalog_by_key[(device_id.strip(), method.strip())] = [
                    variable.strip()
                    for variable in opc_variables
                    if isinstance(variable, str) and variable.strip()
                ]
    node_map = _workflow_node_map(workflow, {}, None)
    if isinstance(templates, list):
        for template in templates:
            if not isinstance(template, dict):
                continue
            template_id = str(template.get("id", "")).strip()
            if scheduled_ids and template_id not in scheduled_ids:
                continue
            node_ids = template.get("node_ids")
            if not isinstance(node_ids, list):
                continue
            for raw_node_id in node_ids:
                if not isinstance(raw_node_id, str):
                    continue
                node_id = raw_node_id.strip()
                node = node_map.get(node_id)
                if node is None:
                    continue
                key = (node["catalog_device_id"], node["catalog_method"])
                if None not in key and key in catalog_by_key:
                    referenced.update(catalog_by_key[key])
                if node.get("opc_variables"):
                    referenced.update(node["opc_variables"])
                referenced.update(
                    resolve_action_sensor_variables(
                        node["device_id"],
                        node["method"],
                        node.get("params") or {},
                    )
                )
                referenced.update(
                    resolve_robot_action_opc_variables(
                        node["device_id"],
                        node["method"],
                    )
                )
            for trigger_key in ("input_triggers", "output_triggers"):
                triggers = template.get(trigger_key)
                if not isinstance(triggers, list):
                    continue
                for trigger in triggers:
                    if not isinstance(trigger, dict) or trigger.get("kind") != "opc":
                        continue
                    config = trigger.get("config")
                    if not isinstance(config, dict):
                        continue
                    variable = config.get("variable")
                    if isinstance(variable, str) and variable.strip():
                        referenced.add(variable.strip())
    if not referenced:
        return []
    filtered: list[dict[str, Any]] = []
    for item in variable_catalog:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if isinstance(name, str) and name.strip() in referenced:
            filtered.append(item)
    return filtered


def generate_opc_simulator_draft(
    workflow: Any,
    templates: Any,
    scheduled_template_ids: Any,
    name: Any,
    opc_url: Any,
    *,
    action_catalog: Any = None,
    variable_catalog: Any = None,
) -> dict[str, Any]:
    """根据本次已排 Task 生成 schema v2 draft，不猜测冲突配置。"""
    raw_variable_catalog = variable_catalog
    # Reject bounded-complexity violations before filtering or building the
    # workflow node map.  Filtering itself needs that map and must not amplify
    # an oversized template/node association payload first.
    validate_payload_limits(
        workflow=workflow,
        templates=templates,
        action_catalog=action_catalog,
    )
    variable_catalog = filter_variable_catalog(
        raw_variable_catalog,
        workflow=workflow,
        templates=templates,
        scheduled_template_ids=scheduled_template_ids,
        action_catalog=action_catalog,
    )
    validate_payload_limits(variable_catalog=variable_catalog)
    errors: dict[str, None] = {}
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > MAX_IDENTIFIER_LENGTH:
        _error(errors, "name")
    action_catalog_by_key, ambiguous_action_keys = _action_catalog_map(
        action_catalog, errors
    )
    all_variable_types = _variable_type_catalog(raw_variable_catalog, errors)
    filtered_variable_names = {
        str(item.get("name") or "").strip()
        for item in variable_catalog
        if isinstance(item, dict)
    }
    variable_types = {
        variable: definition
        for variable, definition in all_variable_types.items()
        if variable in filtered_variable_names
    }
    raw_templates = templates if isinstance(templates, list) else []
    templates_by_id: dict[str, tuple[int, dict[str, Any]]] = {}
    ambiguous_template_ids: set[str] = set()
    for index, template in enumerate(raw_templates):
        if not isinstance(template, dict):
            continue
        template_id = template.get("id")
        if (
            not isinstance(template_id, str)
            or not template_id.strip()
            or len(template_id.strip()) > MAX_IDENTIFIER_LENGTH
        ):
            _error(errors, f"templates[{index}].id")
            continue
        template_id = template_id.strip()
        template_name = template.get("name")
        if template_name is not None and (
            not isinstance(template_name, str)
            or not template_name.strip()
            or len(template_name.strip()) > MAX_IDENTIFIER_LENGTH
        ):
            _error(errors, f"templates[{index}].name")
        if template_id in templates_by_id or template_id in ambiguous_template_ids:
            _error(errors, f"templates[{index}].id")
            templates_by_id.pop(template_id, None)
            ambiguous_template_ids.add(template_id)
            continue
        templates_by_id[template_id] = (index, template)

    scheduled: list[tuple[int, dict[str, Any]]] = []
    raw_scheduled_ids = (
        scheduled_template_ids if isinstance(scheduled_template_ids, list) else []
    )
    if len(raw_scheduled_ids) > MAX_SCHEDULED_TEMPLATE_IDS:
        raise ProfileLimitError(["scheduled_template_ids"])
    unique_scheduled_ids: list[tuple[int, Any]] = []
    scheduled_ids_seen: set[str] = set()
    for index, template_id in enumerate(raw_scheduled_ids):
        if isinstance(template_id, str):
            template_id = template_id.strip()
            if len(template_id) > MAX_IDENTIFIER_LENGTH:
                _error(errors, f"scheduled_template_ids[{index}]")
                continue
            if template_id in scheduled_ids_seen:
                continue
            scheduled_ids_seen.add(template_id)
        unique_scheduled_ids.append((index, template_id))
    if len(unique_scheduled_ids) > MAX_UNIQUE_SCHEDULED_TEMPLATES:
        raise ProfileLimitError(["scheduled_template_ids.unique"])
    for index, template_id in unique_scheduled_ids:
        if isinstance(template_id, str) and template_id in ambiguous_template_ids:
            continue
        match = (
            templates_by_id.get(template_id) if isinstance(template_id, str) else None
        )
        if match is None:
            _error(errors, f"scheduled_template_ids[{index}]")
            continue
        scheduled.append(match)

    observation_count = 0
    for _, template in scheduled:
        for trigger_key in ("input_triggers", "output_triggers"):
            triggers = template.get(trigger_key)
            if not isinstance(triggers, list):
                continue
            for trigger in triggers:
                if isinstance(trigger, dict) and trigger.get("kind") == "opc":
                    observation_count += 1
                    if observation_count > MAX_TRIGGER_OBSERVATIONS:
                        raise ProfileLimitError(["trigger_observations"])

    selected_node_ids = {
        node_id.strip()
        for _, template in scheduled
        for node_id in (
            template.get("node_ids")
            if isinstance(template.get("node_ids"), list)
            else []
        )
        if isinstance(node_id, str) and node_id.strip()
    }
    node_map = _workflow_node_map(workflow, errors, selected_node_ids)
    node_order: list[str] = []
    node_order_seen: set[str] = set()
    node_template_ids: dict[str, list[str]] = {}
    node_template_ids_seen: dict[str, set[str]] = {}
    template_last_nodes: dict[str, str] = {}
    for template_index, template in scheduled:
        template_id = template["id"].strip()
        raw_node_ids = template.get("node_ids")
        if not isinstance(raw_node_ids, list):
            _error(errors, f"templates[{template_index}].node_ids")
            continue
        valid_node_ids: list[str] = []
        template_node_ids: set[str] = set()
        template_valid = True
        for node_index, node_id in enumerate(raw_node_ids):
            if (
                not isinstance(node_id, str)
                or not node_id.strip()
                or len(node_id.strip()) > MAX_IDENTIFIER_LENGTH
            ):
                _error(
                    errors,
                    f"templates[{template_index}].node_ids[{node_index}]",
                )
                template_valid = False
                continue
            node_id = node_id.strip()
            if node_id not in node_map or node_id in template_node_ids:
                _error(
                    errors,
                    f"templates[{template_index}].node_ids[{node_index}]",
                )
                template_valid = False
                continue
            template_node_ids.add(node_id)
            valid_node_ids.append(node_id)
            if node_id not in node_order_seen:
                node_order.append(node_id)
                node_order_seen.add(node_id)
            template_ids = node_template_ids.setdefault(node_id, [])
            template_ids_seen = node_template_ids_seen.setdefault(node_id, set())
            if template_id not in template_ids_seen:
                template_ids.append(template_id)
                template_ids_seen.add(template_id)
        if template_valid and valid_node_ids:
            template_last_nodes[template_id] = valid_node_ids[-1]

    action_variable_order: list[str] = []
    action_variables_seen: set[str] = set()
    robot_handshake_seen: set[str] = set()
    catalog_was_provided = action_catalog is not None
    for node_id in node_order:
        node = node_map[node_id]
        variables = list(node["opc_variables"] or [])
        variables.extend(
            resolve_robot_action_opc_variables(
                node["device_id"],
                node["method"],
            )
        )
        if (
            (not node["opc_variables_declared"] or not node["opc_variables"])
            and catalog_was_provided
        ):
            key = (node["catalog_device_id"], node["catalog_method"])
            catalog_variables = (
                action_catalog_by_key.get(key)
                if None not in key and key not in ambiguous_action_keys
                else None
            )
            if catalog_variables is None:
                _error(errors, f"{node['validation_path']}.opc_variables")
                continue
            variables.extend(catalog_variables)
        for variable in variables:
            if variable not in action_variables_seen:
                action_variables_seen.add(variable)
                action_variable_order.append(variable)
            if variable in ROBOT_MANUAL_OPC_VARIABLES:
                robot_handshake_seen.add(variable)

    sensor_variable_order: list[str] = []
    sensor_variables_seen: set[str] = set()
    for node_id in node_order:
        node = node_map[node_id]
        for variable in resolve_action_sensor_variables(
            node["device_id"],
            node["method"],
            node.get("params") or {},
        ):
            if variable not in sensor_variables_seen:
                sensor_variables_seen.add(variable)
                sensor_variable_order.append(variable)

    variable_order: list[str] = []
    variable_values: dict[str, list[tuple[str, Any, str]]] = {}
    template_output_values_by_id: dict[str, dict[str, Any]] = {}
    for trigger_key, source in (
        ("input_triggers", "task_input"),
        ("output_triggers", "task_output"),
    ):
        for template_index, template in scheduled:
            template_output_values = template_output_values_by_id.setdefault(
                template["id"].strip(), {}
            )
            triggers = template.get(trigger_key)
            if not isinstance(triggers, list):
                _error(errors, f"templates[{template_index}].{trigger_key}")
                continue
            for trigger_index, trigger in enumerate(triggers):
                if not isinstance(trigger, dict) or trigger.get("kind") != "opc":
                    continue
                config = trigger.get("config")
                base_path = (
                    f"templates[{template_index}].{trigger_key}[{trigger_index}].config"
                )
                if not isinstance(config, dict):
                    _error(errors, base_path)
                    continue
                variable = config.get("variable")
                if (
                    not isinstance(variable, str)
                    or not variable.strip()
                    or len(variable.strip()) > MAX_VARIABLE_NAME_LENGTH
                ):
                    _error(errors, f"{base_path}.variable")
                    continue
                variable = variable.strip()
                if "value" not in config or _scalar_type(config["value"]) is None:
                    _error(errors, f"{base_path}.value")
                    continue
                if source == "task_output":
                    if variable in template_output_values and (
                        type(template_output_values[variable])
                        is not type(config["value"])
                        or template_output_values[variable] != config["value"]
                    ):
                        _error(errors, f"{base_path}.value")
                    else:
                        template_output_values.setdefault(variable, config["value"])
                if variable not in variable_values:
                    variable_order.append(variable)
                    variable_values[variable] = []
                variable_values[variable].append(
                    (source, config["value"], f"{base_path}.value")
                )

    variables: list[dict[str, Any]] = []
    conflicting_variables: set[str] = set()
    unresolved_type_conflicts: set[str] = set()
    if len(variable_order) > MAX_COLLECTION_ITEMS:
        raise ProfileLimitError(["profile.variables"])
    for variable in variable_order:
        observations = variable_values[variable]
        types = {_scalar_type(value) for _, value, _ in observations}
        first_type = _scalar_type(observations[0][1])
        catalog_definition = variable_types.get(variable)
        catalog_type = catalog_definition[0] if catalog_definition else None
        catalog_conflict = bool(catalog_type) and any(
            not _catalog_type_accepts(catalog_type, observed_type)
            for observed_type in types
        )
        type_conflict = catalog_conflict or (
            catalog_definition is None and len(types) > 1
        )
        inputs = [
            (
                value,
                path,
            )
            for source, value, path in observations
            if source == "task_input"
        ]
        input_conflict = bool(inputs) and any(
            type(value) is not type(inputs[0][0]) or value != inputs[0][0]
            for value, _ in inputs[1:]
        )
        observation_sources = {source for source, _, _ in observations}
        # Conflicting input conditions still have a useful type: use the
        # first observed input type while omitting an ambiguous initial value.
        # A conflict involving an output cannot be resolved safely because it
        # would also determine an automatic PLC write.
        input_only_conflict = observation_sources == {"task_input"}
        if type_conflict and not input_only_conflict:
            unresolved_type_conflicts.add(variable)
        if type_conflict or input_conflict:
            conflicting_variables.add(variable)
            if catalog_conflict and catalog_definition is not None:
                _error(errors, catalog_definition[1])
            for _, value, path in observations[1:]:
                if catalog_type is None and _scalar_type(value) != first_type:
                    _error(errors, path)
            if input_conflict:
                initial = inputs[0][0]
                for value, path in inputs[1:]:
                    if type(value) is not type(initial) or value != initial:
                        _error(errors, path)
        source = "task_input" if inputs else "task_output"
        observed_value = observations[0][1]
        data_type = (
            "unknown"
            if variable in unresolved_type_conflicts
            else _infer_variable_data_type(
                variable,
                source=source,
                catalog_type=None if catalog_conflict else catalog_type,
                observed_value=(
                    observed_value
                    if len(types) == 1 or input_only_conflict
                    else None
                ),
            )
        )
        definition: dict[str, Any] = {
            "name": variable,
            "direction": _infer_variable_direction(variable, source=source),
            "data_type": data_type,
            "source": source,
        }
        if inputs:
            initial = inputs[0][0]
            if all(
                value == initial and type(value) is type(initial) for value, _ in inputs
            ):
                definition["initial_value"] = initial
            else:
                for value, path in inputs[1:]:
                    if value != initial or type(value) is not type(initial):
                        _error(errors, path)
        variables.append(definition)

    for variable in action_variable_order:
        if variable in variable_values:
            continue
        catalog_type = (
            variable_types[variable][0] if variable in variable_types else None
        )
        if variable in robot_handshake_seen:
            definition: dict[str, Any] = {
                "name": variable,
                "direction": _infer_variable_direction(
                    variable,
                    source="manual",
                ),
                "data_type": _infer_variable_data_type(
                    variable,
                    source="manual",
                    catalog_type=catalog_type,
                ),
                "source": "manual",
            }
            initial_value = ROBOT_HANDSHAKE_MANUAL_INITIAL_VALUES.get(variable)
            if initial_value is not None:
                definition["initial_value"] = initial_value
            variables.append(definition)
            continue
        variables.append(
            {
                "name": variable,
                "direction": _infer_variable_direction(
                    variable,
                    source="action_node",
                ),
                "data_type": _infer_variable_data_type(
                    variable,
                    source="action_node",
                    catalog_type=catalog_type,
                ),
                "source": "action_node",
            }
        )
    for variable in sensor_variable_order:
        if variable in variable_values or variable in action_variables_seen:
            continue
        catalog_type = (
            variable_types[variable][0] if variable in variable_types else None
        )
        variables.append(
            {
                "name": variable,
                "direction": _infer_variable_direction(
                    variable,
                    source="action_sensor",
                ),
                "data_type": _infer_variable_data_type(
                    variable,
                    source="action_sensor",
                    catalog_type=catalog_type,
                ),
                "source": "action_sensor",
            }
        )
    if len(variables) > MAX_COLLECTION_ITEMS:
        raise ProfileLimitError(["profile.variables"])

    nodes: list[dict[str, Any]] = []
    nodes_by_id: dict[str, dict[str, Any]] = {}
    for node_id in node_order:
        source = node_map[node_id]
        node = {
            "workflow_node_id": source["workflow_node_id"],
            "device_id": source["device_id"],
            "method": source["method"],
            "params": source["params"],
            "task_template_ids": node_template_ids[node_id],
            "channel": "",
            "trigger": {"all": []},
            "on_trigger": {"writes": []},
            "on_complete": {"delay": 0.5, "writes": []},
            "reset_when": None,
            "after_reset": None,
        }
        nodes.append(node)
        nodes_by_id[node_id] = node

    node_write_values: dict[str, dict[str, Any]] = {}
    node_write_conflicts: dict[str, set[str]] = {}
    for template_index, template in scheduled:
        template_id = template["id"].strip()
        last_node_id = template_last_nodes.get(template_id)
        if last_node_id is None:
            continue
        seen = node_write_values.setdefault(last_node_id, {})
        conflicts = node_write_conflicts.setdefault(last_node_id, set())
        triggers = template.get("output_triggers")
        if not isinstance(triggers, list):
            continue
        for trigger_index, trigger in enumerate(triggers):
            if not isinstance(trigger, dict) or trigger.get("kind") != "opc":
                continue
            config = trigger.get("config")
            if not isinstance(config, dict):
                continue
            variable = config.get("variable")
            if (
                not isinstance(variable, str)
                or not variable.strip()
                or "value" not in config
                or _scalar_type(config["value"]) is None
            ):
                continue
            variable = variable.strip()
            value = config["value"]
            if variable in conflicts:
                continue
            if variable in seen:
                if type(seen[variable]) is not type(value) or seen[variable] != value:
                    _error(
                        errors,
                        f"templates[{template_index}].output_triggers"
                        f"[{trigger_index}].config.value",
                    )
                    seen.pop(variable, None)
                    conflicts.add(variable)
                continue
            seen[variable] = value

    for node_id, writes_by_variable in node_write_values.items():
        nodes_by_id[node_id]["on_complete"]["writes"] = [
            {"variable": variable, "value": value}
            for variable, value in writes_by_variable.items()
            if variable not in conflicting_variables
        ]

    profile = {
        "schema_version": 2,
        "status": "draft",
        "name": name,
        "opc": {
            "url": opc_url,
            "poll_interval": 0.2,
            "io_timeout": 2.0,
        },
        "variables": variables,
        "nodes": nodes,
    }
    if len(_serialized_profile_bytes(profile)) > MAX_JSON_BYTES:
        raise ProfileLimitError(["generated_profile"])
    parsed = _parse_profile(profile)
    for path in parsed.validation_errors:
        _error(errors, path)
    return {"profile": profile, "validation_errors": list(errors)}
