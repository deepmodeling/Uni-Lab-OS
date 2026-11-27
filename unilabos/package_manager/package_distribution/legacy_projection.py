"""规范包目录（PackageCatalog）到遗留发布 DTO 的投影。"""

from __future__ import annotations

from typing import Any

from unilabos.registry.init_enforce import validate_init_param_enforce

from .errors import PackageCLIError
from .registry_discovery import normalize_name

DEFAULT_SOURCE_TYPE = "community"

_PY_TO_JSON_SCHEMA_TYPE = {
    "float": "number",
    "int": "integer",
    "str": "string",
    "bool": "boolean",
    "dict": "object",
    "list": "array",
    "Dict": "object",
    "List": "array",
    "Any": "string",
}


def _json_schema_type(py_type: str) -> str:
    """把遗留 Python 类型注解映射为 JSON Schema 基础类型。

    参数：``py_type`` 是 AST 扫描得到的类型注解文本。
    返回：支持类型对应的 JSON Schema ``type``；泛型参数被忽略，未知类型稳定
    回退为 ``string``。
    异常：无；该宽松回退仅服务遗留上传投影，不能替代规范动作（Action）Schema
    编译和完整校验。
    """

    # ``base`` 是去除模块限定与泛型参数后的遗留类型名。
    base = (py_type or "").strip().split("[")[0].split(".")[-1]
    return _PY_TO_JSON_SCHEMA_TYPE.get(base, "string")


def build_action_value_mappings(actions: dict[str, Any]) -> dict[str, Any]:
    """把遗留 AST 动作元数据转换为上传接口需要的动作映射。

    参数：``actions`` 是按动作（Action）名索引的扫描元数据，可能包含参数、默认值、
    文档和动作类型。
    返回：按动作名索引的 ``action_value_mappings`` 兼容投影，包含 goal/result/
    feedback Schema 与描述。
    异常：无；形状无效的动作或参数被跳过，未知参数类型使用遗留字符串回退。本函数
    不编译或改变规范包目录（PackageCatalog）。
    """

    # ``result`` 是按稳定动作（Action）名索引的后端兼容映射。
    result: dict[str, Any] = {}
    for name, metadata in actions.items():
        if not isinstance(metadata, dict):
            continue
        # 以下集合共同描述当前动作（Action）的目标参数 Schema 与默认值。
        params = (
            metadata.get("params") if isinstance(metadata.get("params"), list) else []
        )
        goal_properties: dict[str, Any] = {}
        required: list[str] = []
        goal_default: dict[str, Any] = {}
        for parameter in params:
            if not isinstance(parameter, dict):
                continue
            # ``parameter_name`` 是动作（Action）参数在接口上的稳定字段名。
            parameter_name = str(parameter.get("name") or "").strip()
            if not parameter_name:
                continue
            goal_properties[parameter_name] = {
                "type": _json_schema_type(str(parameter.get("type", ""))),
                "title": parameter_name,
            }
            if parameter.get("required"):
                required.append(parameter_name)
            if parameter.get("default") is not None:
                goal_default[parameter_name] = parameter.get("default")
        # ``goal_schema`` 是当前动作（Action）的遗留输入 Schema 投影。
        goal_schema: dict[str, Any] = {
            "type": "object",
            "properties": goal_properties,
        }
        if required:
            goal_schema["required"] = required
        # ``action_args`` 保存装饰器声明；``action_type`` 是最终传输动作类型。
        action_args = (
            metadata.get("action_args")
            if isinstance(metadata.get("action_args"), dict)
            else {}
        )
        action_type_raw = action_args.get("action_type")
        action_type = "UniLabJsonCommand"
        if isinstance(action_type_raw, str) and action_type_raw.strip():
            action_type = action_type_raw.strip().split(":")[-1].split(".")[-1]
        # ``entry`` 是当前动作（Action）完整的后端兼容映射条目。
        entry: dict[str, Any] = {
            "type": action_type,
            "goal": goal_schema,
            "result": {"type": "object", "properties": {}},
            "feedback": {"type": "object", "properties": {}},
            "description": str(
                metadata.get("docstring") or action_args.get("description") or ""
            ),
        }
        if goal_default:
            entry["goal_default"] = goal_default
        result[name] = entry
    return result


def build_resources(
    devices: dict[str, dict[str, Any]],
    package_info: dict[str, Any],
) -> list[dict[str, Any]]:
    """把遗留 AST 设备扫描结果转换为后端资源上传投影。

    参数：``devices`` 是按设备定义身份索引的 AST 元数据；``package_info`` 是同次
    软件包检查生成的不可分割上传元信息。
    返回：``/lab/resource`` 接口消费的资源 DTO 列表，每项保留对应原始
    ``source_registry`` 兼容投影。
    异常：无；无效的动作或 Handle 由各兼容转换器关闭式忽略。本函数不创建具体
    物料（Material），也不成为注册表（Registry）定义权威。
    """

    # ``resources`` 是本次遗留 AST 定义生成的后端资源上传投影。
    resources: list[dict[str, Any]] = []
    for device_id, metadata in devices.items():
        # 以下局部值共同描述一个稳定设备定义及其动作（Action）、状态和 Handle。
        actions = (
            metadata.get("actions") if isinstance(metadata.get("actions"), dict) else {}
        )
        action_value_mappings = build_action_value_mappings(actions)
        status_properties = (
            metadata.get("status_properties")
            if isinstance(metadata.get("status_properties"), dict)
            else {}
        )
        handles = (
            metadata.get("handles") if isinstance(metadata.get("handles"), list) else []
        )
        # ``registry_class`` 是后端兼容的设备驱动注册投影。
        registry_class = {
            "module": metadata.get("module", ""),
            "type": metadata.get("device_type", "python"),
            "action_value_mappings": action_value_mappings,
            "status_types": status_properties,
        }
        # ``source_registry`` 保留后端构造有效模板所需原始设备注册投影。
        source_registry = {
            "class": registry_class,
            "handles": handles,
            "device_id": device_id,
            "version": metadata.get("version", package_info.get("version", "")),
            "description": metadata.get("description", ""),
            "displayname": metadata.get("displayname") or device_id,
            "icon": metadata.get("icon", ""),
        }
        # ``category`` 是当前设备定义的查询分类，不参与身份或运行时分配。
        category = (
            metadata.get("category")
            if isinstance(metadata.get("category"), list)
            else []
        )
        resources.append(
            {
                "id": device_id,
                "registry_type": "device",
                "version": metadata.get(
                    "version", package_info.get("version", "0.0.1")
                ),
                "description": metadata.get("description", ""),
                "displayname": metadata.get("displayname") or device_id,
                "icon": metadata.get("icon", ""),
                "class": registry_class,
                "category": category,
                "handles": _map_handles(handles),
                "package_info": package_info,
                "source_registry": source_registry,
            }
        )
    return resources


def build_resources_from_registry(
    entries: dict[str, dict[str, Any]],
    package_info: dict[str, Any],
) -> list[dict[str, Any]]:
    """把 YAML 注册表（Registry）条目转换为后端资源上传投影。

    参数：``entries`` 是按设备定义身份索引的已解析 YAML 条目；``package_info``
    是同次软件包检查生成的上传元信息。
    返回：``/lab/resource`` 接口消费的资源 DTO 列表，每项完整保留原始条目作为
    ``source_registry``。
    异常：初始化参数强制策略无效时抛出 ``PackageCLIError``；不得降级丢弃安全
    约束或创建具体物料（Material）。
    """

    # ``resources`` 是本次 YAML 定义生成的后端资源上传投影。
    resources: list[dict[str, Any]] = []
    for device_id, entry in entries.items():
        # ``registry_class`` 是当前设备定义的驱动与动作（Action）注册信息。
        registry_class = (
            entry.get("class") if isinstance(entry.get("class"), dict) else {}
        )
        # ``init_schema`` 与 ``init_enforce`` 共同表达设备初始化安全合同。
        init_schema = (
            entry.get("init_param_schema")
            if isinstance(entry.get("init_param_schema"), dict)
            else None
        )
        init_enforce = validate_init_param_enforce(
            device_id,
            init_schema,
            entry.get("init_param_enforce"),
            error_factory=PackageCLIError,
        )
        # ``category`` 是查询分类兼容投影，不参与设备定义身份。
        category = entry.get("category") or entry.get("tags") or []
        if isinstance(category, str):
            category = [category]
        # ``resource`` 是当前设备定义的完整后端资源上传 DTO。
        resource: dict[str, Any] = {
            "id": device_id,
            "registry_type": str(
                entry.get("registry_type", entry.get("resource_type", "device"))
            ),
            "version": str(entry.get("version", package_info.get("version", "0.0.1"))),
            "description": entry.get("description", ""),
            "icon": entry.get("icon", ""),
            "class": {
                "module": registry_class.get("module", ""),
                "type": registry_class.get("type", "python"),
                "action_value_mappings": registry_class.get(
                    "action_value_mappings", {}
                ),
                "status_types": registry_class.get("status_types", {}),
            },
            "handles": [],
            "category": category if isinstance(category, list) else [],
            "manufacturer": str(entry.get("manufacturer", "")),
            "model": entry.get("model"),
            "scene": entry.get("scene"),
            "device_params": entry.get("device_params"),
            "package_info": package_info,
            "source_registry": entry,
        }
        if init_schema is not None:
            resource["init_param_schema"] = init_schema
        resource["init_param_enforce"] = init_enforce
        resources.append(resource)
    return resources


def build_package_info(
    project: dict[str, Any],
    class_namespace: str,
    sha256: str,
) -> dict[str, Any]:
    """构造后端与 Edge 共同消费的遗留 ``package_info`` 上传投影。

    参数：``project`` 是统一项目解析结果；``class_namespace`` 是本次编译采用的
    类命名空间；``sha256`` 是归档内容摘要。云端地址和对象键只能由本次已审计
    wheel 的上传回执补入，不能由投影调用者预置。
    返回：包含发行身份、版本、安装声明、依赖、摘要和远端定位字段的字典。
    异常：项目缺少必须的 ``name`` 或 ``version`` 时传播 ``KeyError``；该投影不
    替代包目录（PackageCatalog）的规范身份。
    """

    # ``name`` 是项目声明的稳定发行身份。
    name = project["name"]
    # ``info`` 是本次归档摘要绑定的完整遗留上传投影。
    info: dict[str, Any] = {
        "name": name,
        "version": project["version"],
        "class_namespace": class_namespace,
        "module_prefix": (
            class_namespace.split(".")[0] if class_namespace else "community"
        ),
        "normalized_name": normalize_name(name),
        "source_type": DEFAULT_SOURCE_TYPE,
        "install_spec": (
            f"{name}=={project['version']}" if project.get("version") else name
        ),
        "summary": project.get("summary", ""),
        "license": project.get("license", ""),
        "homepage": project.get("homepage", ""),
        "dependencies": list(project.get("dependencies") or []),
        "sha256": sha256,
        "download_url": "",
    }
    return info


def _map_handles(handles: list[Any]) -> list[dict[str, Any]]:
    """把遗留 Handle 扫描结果转换为后端兼容字段集合。

    参数：``handles`` 是扫描器产生的任意 Handle 候选列表。
    返回：只包含后端 ``RegHandle`` 已知字段的字典列表；非字典项被跳过，缺失字段
    以空字符串保持遗留上传兼容。
    异常：无；本函数不校验工作流（Workflow）连接语义，也不改变规范包目录
    （PackageCatalog）中的 Handle 定义。
    """

    # ``mapped`` 是保持输入顺序的后端 Handle 兼容投影。
    mapped: list[dict[str, Any]] = []
    for handle in handles:
        if isinstance(handle, dict):
            mapped.append(
                {
                    "data_key": str(handle.get("data_key", "")),
                    "data_source": str(handle.get("data_source", "")),
                    "data_type": str(handle.get("data_type", "")),
                    "description": str(handle.get("description", "")),
                    "handler_key": str(handle.get("handler_key", "")),
                    "io_type": str(handle.get("io_type", "")),
                    "label": str(handle.get("label", "")),
                    "side": str(handle.get("side", "")),
                }
            )
    return mapped


__all__ = [
    "build_action_value_mappings",
    "build_package_info",
    "build_resources",
    "build_resources_from_registry",
]
