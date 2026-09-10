"""把产品注册表（Registry）静态扫描结果投影为目录定义。"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from unilabos.registry.ast_registry_scanner import _parse_file

from ...model import (
    PackageCompileError,
    PackageDefinition,
    PackageDiagnostic,
)


def compile_registry_definitions(
    *,
    workspace_root: Path,
    namespace: str,
    python_files: tuple[Path, ...],
    content_hashes: Mapping[str, str],
) -> tuple[tuple[PackageDefinition, ...], tuple[PackageDefinition, ...]]:
    """用产品唯一 AST 链编译设备和资源静态定义。

    参数：``workspace_root`` 是模块路径基准；``namespace`` 是包的规范社区命名空间；
    ``python_files`` 是已完成 UTF-8 与语法门禁的全部 Python 文件；
    ``content_hashes`` 按工作区逻辑路径提供源码摘要。
    返回：设备定义和资源定义两个稳定集合。
    异常：重复身份、动作合同（Action Contract）无效或投影含非 JSON 值时抛出
    ``PackageCompileError``，不返回部分集合。
    """

    # ``device_metadata`` 与 ``resource_metadata`` 是同一注册表解析器的完整候选结果。
    device_metadata: dict[str, dict[str, Any]] = {}
    resource_metadata: dict[str, dict[str, Any]] = {}
    for python_file in python_files:
        try:
            # ``scanned_devices`` 与 ``scanned_resources`` 是单个源码文件的 AST 候选，
            # 尚未获得跨文件唯一性保证。
            scanned_devices, scanned_resources = _parse_file(
                python_file,
                workspace_root,
            )
        except Exception as error:
            # ``logical_path`` 是编译诊断采用的包内源码证据身份，不泄漏绝对路径。
            logical_path = python_file.relative_to(workspace_root).as_posix()
            raise _compile_error(
                code="registry_compile_error",
                message="注册表静态定义无法编译",
                path=logical_path,
            ) from error
        # ``identity_key`` 选择扫描结果中的包内定义身份字段；``destination`` 是对应
        # 种类的全包候选索引，二者必须成对以防设备/资源身份串线。
        for metadata, identity_key, destination in (
            (scanned_devices, "device_id", device_metadata),
            (scanned_resources, "resource_id", resource_metadata),
        ):
            for item in metadata:
                # ``definition_id`` 是包内稳定短身份，最终与命名空间组成规范 FQID。
                definition_id = item.get(identity_key)
                if not isinstance(definition_id, str) or not definition_id:
                    continue
                if definition_id in destination:
                    raise _compile_error(
                        code="duplicate_definition",
                        message="软件包内存在重复静态定义身份",
                        path=python_file.relative_to(workspace_root).as_posix(),
                    )
                destination[definition_id] = item
    if set(device_metadata) & set(resource_metadata):
        raise _compile_error(
            code="duplicate_definition",
            message="设备和资源定义不能共享同一全限定身份",
        )

    # ``devices`` 是按包内身份稳定排序且完整校验后的设备定义集合。
    devices = tuple(
        _device_definition(
            definition_id=definition_id,
            metadata=metadata,
            workspace_root=workspace_root,
            namespace=namespace,
            content_hashes=content_hashes,
        )
        for definition_id, metadata in sorted(device_metadata.items())
    )
    # ``resources`` 是与设备同代产生的资源定义集合，禁止部分编译返回。
    resources = tuple(
        _resource_definition(
            definition_id=definition_id,
            metadata=metadata,
            workspace_root=workspace_root,
            namespace=namespace,
            content_hashes=content_hashes,
        )
        for definition_id, metadata in sorted(resource_metadata.items())
    )
    return devices, resources


def _device_definition(
    *,
    definition_id: str,
    metadata: dict[str, Any],
    workspace_root: Path,
    namespace: str,
    content_hashes: Mapping[str, str],
) -> PackageDefinition:
    """投影一个设备定义及其规范动作合同。

    参数：``definition_id`` 是包内身份；``metadata`` 是产品注册表 AST 静态结果；
    ``workspace_root`` 是路径基准；``namespace`` 是规范命名空间；
    ``content_hashes`` 是源码摘要索引。
    返回：不含绝对路径、可规范序列化的设备目录定义。
    异常：无效动作合同或非 JSON 注册表值转为 ``PackageCompileError``。
    """

    # ``logical_path`` 绑定设备定义、源码摘要和编译诊断的同一包内路径身份。
    logical_path = _logical_declaring_path(metadata, workspace_root)
    try:
        # ``registry_entry`` 只投影扫描器已经静态编译的规范合同，不调用可能导入
        # 作者模块的运行时 Schema 补全路径。
        registry_entry = _static_device_entry(definition_id, metadata)
        # ``stable_entry`` 是已剔除运行时对象的规范 JSON 注册表投影。
        stable_entry = _json_compatible(registry_entry)
    except (TypeError, ValueError) as error:
        raise _compile_error(
            code="action_contract_invalid",
            message="设备定义包含无效动作合同",
            path=logical_path,
        ) from error
    # ``module`` 与 ``symbol`` 是设备定义的 Python 源码身份，不代替规范 FQID。
    module, symbol = _module_symbol(metadata)
    return PackageDefinition(
        kind="device",
        id=definition_id,
        fqid=f"{namespace}.{definition_id}",
        module=module,
        symbol=symbol,
        declaring_file=logical_path,
        content_hash=content_hashes[logical_path],
        version=str(metadata.get("version") or "1.0.0"),
        title=str(metadata.get("displayname") or definition_id),
        description=str(metadata.get("description") or ""),
        details={"registry_entry": stable_entry},
    )


def _resource_definition(
    *,
    definition_id: str,
    metadata: dict[str, Any],
    workspace_root: Path,
    namespace: str,
    content_hashes: Mapping[str, str],
) -> PackageDefinition:
    """投影一个资源定义及其注册表元数据。

    参数：``definition_id`` 是包内身份；``metadata`` 是产品注册表 AST 静态结果；
    ``workspace_root`` 是路径基准；``namespace`` 是规范命名空间；
    ``content_hashes`` 是源码摘要索引。
    返回：不含绝对路径的资源目录定义。
    异常：注册表详情含非 JSON 值时转为 ``PackageCompileError``。
    """

    # ``logical_path`` 绑定资源定义、源码摘要和编译诊断的同一包内路径身份。
    logical_path = _logical_declaring_path(metadata, workspace_root)
    try:
        # ``registry_entry`` 只保存资源模板静态合同，不包含具体物料（Material）实例。
        registry_entry = {
            "category": metadata.get("category", []),
            "class": {
                "module": metadata.get("module", ""),
                "type": metadata.get("class_type", "python"),
            },
            "description": metadata.get("description", ""),
            "displayname": metadata.get("displayname") or definition_id,
            "handles": metadata.get("handles", []),
            "metadata": metadata.get("metadata") or {},
            "registry_type": "resource",
            "version": metadata.get("version", "1.0.0"),
            # Workbench navigation consumes the package identity published by
            # the same static catalog generation; it never reconstructs a
            # local path from a Python module name.
            "source_uri": _package_source_uri(namespace, logical_path),
        }
        if metadata.get("model") is not None:
            registry_entry["model"] = metadata["model"]
        # ``stable_entry`` 是可以持久化和规范摘要的纯 JSON 资源投影。
        stable_entry = _json_compatible(registry_entry)
    except (TypeError, ValueError) as error:
        raise _compile_error(
            code="resource_contract_invalid",
            message="资源定义包含不可持久化元数据",
            path=logical_path,
        ) from error
    # ``module`` 与 ``symbol`` 是资源定义的 Python 源码身份，不代替规范 FQID。
    module, symbol = _module_symbol(metadata)
    return PackageDefinition(
        kind="resource",
        id=definition_id,
        fqid=f"{namespace}.{definition_id}",
        module=module,
        symbol=symbol,
        declaring_file=logical_path,
        content_hash=content_hashes[logical_path],
        version=str(metadata.get("version") or "1.0.0"),
        title=str(metadata.get("displayname") or definition_id),
        description=str(metadata.get("description") or ""),
        details={"registry_entry": stable_entry},
    )


def _package_source_uri(namespace: str, logical_path: str) -> str:
    """Return the move-stable authoring URI for one package declaration."""

    package_id = namespace.removeprefix("community.")
    path = PurePosixPath(logical_path)
    parts = path.parts
    if not package_id or not parts or parts[0] != package_id or len(parts) < 2:
        raise ValueError("资源定义源码必须位于规范导入包内")
    return f"package://{package_id}/{PurePosixPath(*parts[1:]).as_posix()}"


def _static_device_entry(
    definition_id: str,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    """从已编译 AST 合同构造无导入设备目录投影。

    参数：``definition_id`` 是设备包内身份；``metadata`` 是产品 AST 扫描器结果。
    返回：保留规范动作 Schema、传输映射和展示元数据的注册表形状字典。
    异常：规范动作缺少静态 Schema 或携带合同诊断时抛出 ``ValueError``。
    """

    # ``action_mappings`` 以最终动作业务名索引规范动作合同（Action Contract）。
    action_mappings: dict[str, Any] = {}
    for method_name, method_info in sorted((metadata.get("actions") or {}).items()):
        if not isinstance(method_info, Mapping):
            raise TypeError("invalid_action_metadata")
        if method_info.get("contract_diagnostic"):
            raise ValueError("invalid_typed_action")
        # ``action_args`` 是装饰器经 AST 静态编译后的声明参数，不执行作者代码。
        action_args = method_info.get("action_args") or {}
        if not isinstance(action_args, Mapping):
            raise TypeError("invalid_action_arguments")
        # ``contract_kind`` 决定是否强制要求规范动作 JSON Schema。
        contract_kind = str(method_info.get("contract_kind") or "legacy")
        # ``schema`` 是动作值与物料锁声明的唯一静态合同；typed 动作不得缺失。
        schema = method_info.get("schema")
        if contract_kind == "typed" and not isinstance(schema, Mapping):
            raise ValueError("missing_typed_action_schema")
        # ``action_name`` 是注册表（Registry）和工作流引用采用的动作业务身份。
        action_name = str(action_args.get("action_name") or method_name)
        if action_args.get("auto_prefix"):
            action_name = f"auto-{action_name}"
        # ``goal`` 是设备传输字段映射；动作值 Schema 仍以 ``schema`` 为唯一合同。
        goal = {
            str(item["name"]): str(item["name"])
            for item in method_info.get("params", [])
            if isinstance(item, Mapping)
            and item.get("name") not in {None, "self", "cls"}
        }
        goal.update(action_args.get("goal") or {})
        # ``action_type`` 只标识传输 Adapter 类型，不改变动作业务身份或 Schema。
        action_type = action_args.get("action_type")
        if isinstance(action_type, str) and action_type:
            action_type_name = action_type.rsplit(":", 1)[-1].rsplit(".", 1)[-1]
        else:
            action_type_name = (
                "UniLabJsonCommandAsync"
                if method_info.get("is_async")
                else "UniLabJsonCommand"
            )
        # ``action_entry`` 汇总一个动作的规范 Schema、传输映射和展示元数据。
        action_entry: dict[str, Any] = {
            "contract_kind": contract_kind,
            "displayname": action_args.get("displayname") or action_name,
            "estimate_duration_express": action_args.get(
                "estimate_duration_express",
                "",
            ),
            "estimate_duration_fixed": action_args.get(
                "estimate_duration_fixed",
                60.0,
            ),
            "feedback": action_args.get("feedback") or {},
            "feedback_interval": action_args.get("feedback_interval", 1.0),
            "goal": goal,
            "goal_default": method_info.get("goal_default") or {},
            "handles": action_args.get("handles") or {},
            "placeholder_keys": action_args.get("placeholder_keys") or {},
            "result": action_args.get("result") or {},
            "schema": schema or {},
            "type": action_type_name,
        }
        if action_name.removeprefix("auto-") != method_name:
            action_entry["method_name"] = method_name
        if action_args.get("always_free"):
            action_entry["always_free"] = True
        if action_args.get("error_policy"):
            action_entry["error_policy"] = action_args["error_policy"]
        action_mappings[action_name] = action_entry
    # ``status_types`` 是设备只读状态属性的传输类型投影，不属于动作结果合同。
    status_types = {
        name: str(item.get("return_type") or "String")
        for name, item in sorted((metadata.get("status_properties") or {}).items())
        if isinstance(item, Mapping)
    }
    # ``entry`` 是单个设备定义进入包目录（PackageCatalog）的完整注册表投影。
    entry: dict[str, Any] = {
        "category": metadata.get("category", []),
        "class": {
            "action_value_mappings": action_mappings,
            "module": metadata.get("module", ""),
            "status_types": status_types,
            "type": metadata.get("device_type", "python"),
        },
        "description": metadata.get("description", ""),
        "displayname": metadata.get("displayname") or definition_id,
        "handles": metadata.get("handles", []),
        "icon": metadata.get("icon", ""),
        "legacy_auto_methods": metadata.get("auto_methods", {}),
        "metadata": metadata.get("metadata") or {},
        "registry_type": "device",
        "version": metadata.get("version", "1.0.0"),
    }
    if metadata.get("model") is not None:
        entry["model"] = metadata["model"]
    return entry


def _logical_declaring_path(metadata: Mapping[str, Any], workspace_root: Path) -> str:
    """把扫描器证据路径规范为工作区相对身份。

    参数：``metadata`` 是 AST 元数据；``workspace_root`` 是授权根。
    返回：POSIX 逻辑路径。
    异常：证据路径越界或缺失时抛出 ``PackageCompileError``。
    """

    # ``file_path`` 是扫描器给出的源码证据；返回前必须验证位于授权工作区内。
    file_path = metadata.get("file_path")
    try:
        return Path(str(file_path)).resolve().relative_to(workspace_root).as_posix()
    except (OSError, ValueError) as error:
        raise _compile_error(
            code="declaration_path_invalid",
            message="静态定义证据路径越出软件包来源",
        ) from error


def _module_symbol(metadata: Mapping[str, Any]) -> tuple[str, str]:
    """拆分静态定义的模块和符号身份。

    参数：``metadata`` 是注册表 AST 元数据。
    返回：``module`` 与 ``symbol`` 二元组。
    异常：模块字符串不完整时抛出 ``PackageCompileError``。
    """

    # ``module_identity`` 必须保持 ``module:symbol``，供模板身份映射稳定复用。
    module_identity = metadata.get("module")
    if not isinstance(module_identity, str) or ":" not in module_identity:
        raise _compile_error(
            code="definition_identity_invalid",
            message="静态定义缺少模块或符号身份",
        )
    # ``module`` 与 ``symbol`` 分离后仍共同组成唯一 Python 源码身份。
    module, symbol = module_identity.rsplit(":", 1)
    if not module or not symbol:
        raise _compile_error(
            code="definition_identity_invalid",
            message="静态定义缺少模块或符号身份",
        )
    return module, symbol


def _json_compatible(value: Any) -> Any:
    """复制并验证注册表投影只包含普通 JSON 值。

    参数：``value`` 是产品注册表构造器产生的静态值。
    返回：键稳定排序且不共享容器的 JSON 值。
    异常：出现运行时类、可调用对象或非字符串映射键时抛出 ``TypeError``。
    """

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_json_compatible(item) for item in value]
    if isinstance(value, tuple):
        return [_json_compatible(item) for item in value]
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("注册表映射键必须是字符串")
        return {key: _json_compatible(value[key]) for key in sorted(value)}
    raise TypeError(f"注册表投影含非 JSON 值: {type(value).__name__}")


def _compile_error(
    *,
    code: str,
    message: str,
    path: str | None = None,
) -> PackageCompileError:
    """构造单项、无源码泄漏的软件包编译错误。

    参数：``code`` 是稳定机器码；``message`` 是中文诊断；``path`` 是可选逻辑路径。
    返回：包含一项结构化诊断的 ``PackageCompileError``。
    异常：无。
    """

    return PackageCompileError(
        (PackageDiagnostic(code=code, message=message, path=path),)
    )


__all__ = ["compile_registry_definitions"]
