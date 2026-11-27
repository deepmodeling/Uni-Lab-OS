"""隔离工作区完整候选编译的 Authoring Worker 深模块。"""

from __future__ import annotations

import argparse
import base64
import importlib
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from unilabos.workflow.source_manifest import parse_editable_package_manifest

from ..package_catalog.model import (
    PackageAsset,
    PackageCatalog,
    PackageDefinition,
    PackageDefinitionCatalog,
    PackageDistributionIdentity,
)
from ..package_catalog.project_metadata import parse_project_metadata
from ..package_catalog.registry_snapshot import compile_registry_snapshot
from ..package_catalog.sources import WorkspaceSource
from .activation import (
    WorkspaceRegistryRuntime,
    _freeze_graph_json,
    workflow_source_plan_from_catalog,
)
from .discovery import WorkspaceStartupPlan
from .generation import WorkspaceInputGeneration
from .monitor import StableWorkspaceFileMonitor
from .package_source import PackageCatalogSource

_PROTOCOL_VERSION = "1"
_DEFAULT_TIMEOUT_SECONDS = 90.0
_ARGUMENT_PATCH_KEYS = (
    "working_dir",
    "graph",
    "config",
    "app_bridges",
    "_ensure_dependencies",
    "_workspace_root",
)


class AuthoringWorkerError(RuntimeError):
    """Authoring Worker 无法产生完整、可信候选。"""

    def __init__(self, code: str, message: str) -> None:
        """保存稳定错误码和不包含源码正文的说明。"""

        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class AuthoringWorkerResult:
    """一次隔离编译返回的候选、输入代和工作进程身份。"""

    candidate: WorkspaceRegistryRuntime
    input_generation: WorkspaceInputGeneration
    monitor: StableWorkspaceFileMonitor
    worker_pid: int


def prepare_workspace_generation_in_worker(
    arguments: dict[str, Any],
    *,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> AuthoringWorkerResult | None:
    """在可丢弃子进程中编译并复原一代工作区候选。

    ``arguments`` 只把工作区启动所需的 JSON 值交给 Worker；成功后父进程接收
    不含 Python 实现对象的版本化 JSON，并再次确认文件代仍与 Worker 观察一致。
    """

    if not isinstance(arguments, dict):
        raise TypeError("Authoring Worker 参数必须是 dict")
    workspace = arguments.get("workspace")
    if workspace is None:
        return None
    if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
        raise ValueError("Authoring Worker 超时必须为正数")
    request = {
        "protocol_version": _PROTOCOL_VERSION,
        "arguments": _worker_arguments(arguments),
    }
    with tempfile.TemporaryDirectory(prefix="unilab-authoring-worker-") as temp_root:
        exchange_root = Path(temp_root)
        request_path = exchange_root / "request.json"
        response_path = exchange_root / "response.json"
        request_path.write_text(
            json.dumps(request, ensure_ascii=False, allow_nan=False),
            encoding="utf-8",
        )
        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "from unilabos.package_manager.workspace_runtime."
                        "authoring_worker import main; raise SystemExit(main())"
                    ),
                    "--request",
                    str(request_path),
                    "--response",
                    str(response_path),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=float(timeout_seconds),
            )
        except subprocess.TimeoutExpired as error:
            raise AuthoringWorkerError(
                "authoring_worker_timeout",
                "工作区候选编译超时，已终止隔离 Authoring Worker",
            ) from error
        if not response_path.is_file():
            raise AuthoringWorkerError(
                "authoring_worker_crashed",
                f"Authoring Worker 异常退出（exit={completed.returncode}）",
            )
        try:
            response = json.loads(response_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise AuthoringWorkerError(
                "authoring_worker_protocol_invalid",
                "Authoring Worker 返回了无效响应",
            ) from error
    if not isinstance(response, dict) or response.get("protocol_version") != (
        _PROTOCOL_VERSION
    ):
        raise AuthoringWorkerError(
            "authoring_worker_protocol_invalid",
            "Authoring Worker 协议版本无效",
        )
    if response.get("ok") is not True:
        failure = response.get("error")
        code = (
            str(failure.get("code"))
            if isinstance(failure, dict)
            else "authoring_worker_failed"
        )
        message = (
            str(failure.get("message"))
            if isinstance(failure, dict)
            else "工作区候选编译失败"
        )
        raise AuthoringWorkerError(code, message)
    payload = response.get("generation")
    worker_pid = response.get("worker_pid")
    if not isinstance(payload, dict) or not isinstance(worker_pid, int):
        raise AuthoringWorkerError(
            "authoring_worker_protocol_invalid",
            "Authoring Worker 成功响应缺少完整候选",
        )
    _apply_argument_patch(arguments, response.get("argument_patch"))
    candidate, input_generation = _decode_generation(payload)
    runtime_directory = arguments.get("working_dir")
    ignored_paths = _runtime_ignored_paths(candidate.source.root, runtime_directory)
    monitor = StableWorkspaceFileMonitor(
        candidate.source.root,
        graph_argument=_graph_argument(candidate),
        ignored_paths=ignored_paths,
    )
    current = monitor.capture()
    if current.identity != input_generation.identity:
        raise AuthoringWorkerError(
            "workspace_generation_changed",
            "工作区在 Authoring Worker 编译期间发生变化，请稳定保存后重试",
        )
    return AuthoringWorkerResult(
        candidate=candidate,
        input_generation=current,
        monitor=monitor,
        worker_pid=worker_pid,
    )


def prepare_candidate_in_worker(
    generation: WorkspaceInputGeneration,
    *,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> WorkspaceRegistryRuntime:
    """为监视器提交的新稳定输入代隔离编译完整候选。"""

    if not isinstance(generation, WorkspaceInputGeneration):
        raise TypeError("generation 必须是 WorkspaceInputGeneration")
    result = prepare_workspace_generation_in_worker(
        {
            "workspace": str(generation.workspace_root),
            "graph": generation.graph_argument,
            "devices": None,
            "workflow_editable_package_root": None,
        },
        timeout_seconds=timeout_seconds,
    )
    if result is None:
        raise AuthoringWorkerError(
            "authoring_worker_failed",
            "稳定工作区输入代未能产生候选",
        )
    if result.input_generation.identity != generation.identity:
        raise AuthoringWorkerError(
            "workspace_generation_changed",
            "工作区输入代在隔离编译前后发生变化",
        )
    return result.candidate


def _worker_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """投影 Worker 唯一需要的 JSON 启动参数。"""

    keys = (
        "workspace",
        "working_dir",
        "graph",
        "config",
        "app_bridges",
        "devices",
        "workflow_editable_package_root",
    )
    projected = {key: arguments.get(key) for key in keys}
    try:
        json.dumps(projected, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise TypeError("Authoring Worker 参数必须是 JSON 值") from error
    return projected


def _apply_argument_patch(arguments: dict[str, Any], raw_patch: Any) -> None:
    """只应用协议允许的产品默认参数。"""

    if not isinstance(raw_patch, dict) or not set(raw_patch).issubset(
        _ARGUMENT_PATCH_KEYS
    ):
        raise AuthoringWorkerError(
            "authoring_worker_protocol_invalid",
            "Authoring Worker 参数补丁无效",
        )
    arguments.update(raw_patch)


def _encode_generation(prepared: Any) -> dict[str, Any]:
    """把完整候选转换为不携带实现对象的 JSON wire value。

    参数：``prepared`` 是隔离 Worker 已完成编译和稳定输入复核的产品候选。
    返回：可由父进程严格解码的 JSON 对象，包含公共物料外形及其模板归属。
    异常：候选或启动计划类型无效时抛出 ``TypeError``，不返回部分协议。
    """

    candidate = prepared.candidate
    startup_plan = candidate.startup_plan
    if not isinstance(candidate, WorkspaceRegistryRuntime) or not isinstance(
        startup_plan,
        WorkspaceStartupPlan,
    ):
        raise TypeError("Authoring Worker 候选缺少同代启动计划")
    return {
        "candidate": {
            "source_root": str(candidate.source.root),
            "graph_argument": _graph_argument(candidate),
            "graph": candidate.graph_copy(),
            "packages": [
                {
                    "source_root": str(item.source.root),
                    "catalog": item.catalog.to_dict(),
                }
                for item in candidate.package_catalog_sources
            ],
            "material_shapes": list(candidate.material_shapes),
            "material_shapes_by_template": {
                template_fqid: dict(shape)
                for template_fqid, shape in candidate.material_shapes_by_template.items()
            },
            "dependency_revision": candidate.dependency_revision,
            "startup": {
                "project_file": _encode_bytes(startup_plan.project_file_bytes),
                "workflow_manifest": (
                    _encode_bytes(startup_plan.workflow_manifest_bytes)
                    if startup_plan.workflow_manifest_bytes is not None
                    else None
                ),
            },
        },
        "input_generation": {
            "identity": prepared.input_generation.identity,
            "workspace_root": str(prepared.input_generation.workspace_root),
            "graph_argument": prepared.input_generation.graph_argument,
            "dependency_revision": prepared.input_generation.dependency_revision,
        },
    }


def _decode_generation(
    payload: dict[str, Any],
) -> tuple[WorkspaceRegistryRuntime, WorkspaceInputGeneration]:
    """验证并复原 Worker 返回的完整静态候选。

    参数：``payload`` 是 Authoring Worker 返回的可疑 JSON 对象。返回：父进程
    重建的工作区注册表运行时及对应稳定输入代。异常：字段缺失、类型错误、目录
    摘要不一致或模板外形绑定无效时抛出 ``AuthoringWorkerError``，候选不发布。
    """

    try:
        raw_candidate = payload["candidate"]
        raw_input = payload["input_generation"]
        source = WorkspaceSource(raw_candidate["source_root"])
        raw_packages = raw_candidate["packages"]
        if not isinstance(raw_packages, list) or not raw_packages:
            raise ValueError("候选缺少包目录")
        packages = tuple(
            PackageCatalogSource(
                source=WorkspaceSource(item["source_root"]),
                catalog=_decode_catalog(item["catalog"]),
            )
            for item in raw_packages
        )
        if packages[0].source.root != source.root:
            raise ValueError("主包来源与工作区不一致")
        catalog = packages[0].catalog
        startup_payload = raw_candidate["startup"]
        project_bytes = _decode_bytes(startup_payload["project_file"])
        manifest_value = startup_payload.get("workflow_manifest")
        manifest_bytes = (
            None if manifest_value is None else _decode_bytes(manifest_value)
        )
        project = parse_project_metadata(project_bytes)
        manifest = (
            None
            if manifest_bytes is None
            else parse_editable_package_manifest(manifest_bytes)
        )
        if project.normalized_name != catalog.import_package:
            raise ValueError("启动计划与主包目录身份不一致")
        startup_plan = WorkspaceStartupPlan(
            source=source,
            project_metadata=project,
            distribution_name=project.name,
            import_package=project.normalized_name,
            package_directory=source.root / project.normalized_name,
            community_namespace=f"community.{project.normalized_name}",
            has_workflow_manifest=manifest is not None,
            workflow_source_count=(len(manifest.workflows) if manifest else 0),
            workflow_manifest=manifest,
            default_graph=project.startup_graph,
            default_config=project.startup_config,
            default_app_bridges=project.startup_app_bridges,
            ensure_dependencies=project.startup_ensure_dependencies,
            project_file_bytes=project_bytes,
            workflow_manifest_bytes=manifest_bytes,
        )
        graph = raw_candidate["graph"]
        if not isinstance(graph, dict):
            raise ValueError("候选物理图必须是对象")
        graph_argument = raw_candidate["graph_argument"]
        graph_path = source.root / Path(graph_argument)
        if not source.has_file(Path(graph_argument).as_posix()):
            raise ValueError("候选物理图不存在")
        registry_snapshot = compile_registry_snapshot(
            tuple(item.catalog for item in packages)
        )
        graph_snapshot = _freeze_graph_json(graph)
        activation_plan = registry_snapshot.select(graph_snapshot)
        workflow_source_plan = workflow_source_plan_from_catalog(
            source=source,
            catalog=catalog,
        )
        material_shapes = raw_candidate["material_shapes"]
        if not isinstance(material_shapes, list) or any(
            not isinstance(item, dict) for item in material_shapes
        ):
            raise ValueError("候选物料外形必须是对象列表")
        # ``material_shapes_by_template`` 是模板同步使用的精确归属；父进程只接受
        # 非空模板 FQID 到严格 JSON 对象的一对一映射。
        material_shapes_by_template = raw_candidate["material_shapes_by_template"]
        if not isinstance(material_shapes_by_template, dict) or any(
            not isinstance(template_fqid, str)
            or not template_fqid.strip()
            or not isinstance(shape, dict)
            for template_fqid, shape in material_shapes_by_template.items()
        ):
            raise ValueError("候选模板物料外形绑定必须是对象映射")
        dependency_revision = raw_candidate["dependency_revision"]
        if not isinstance(dependency_revision, str):
            raise ValueError("候选依赖修订无效")
        candidate = WorkspaceRegistryRuntime(
            source=source,
            graph_path=graph_path.resolve(),
            graph_snapshot=graph_snapshot,
            catalog=catalog,
            registry_snapshot=registry_snapshot,
            activation_plan=activation_plan,
            workflow_source_plan=workflow_source_plan,
            startup_plan=startup_plan,
            package_catalog_sources=packages,
            material_shapes=tuple(dict(item) for item in material_shapes),
            material_shapes_by_template={
                template_fqid: dict(shape)
                for template_fqid, shape in material_shapes_by_template.items()
            },
            dependency_revision=dependency_revision,
        )
        input_generation = WorkspaceInputGeneration(
            identity=raw_input["identity"],
            workspace_root=raw_input["workspace_root"],
            graph_argument=raw_input["graph_argument"],
            dependency_revision=raw_input.get("dependency_revision", ""),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise AuthoringWorkerError(
            "authoring_worker_protocol_invalid",
            "Authoring Worker 候选结构无效",
        ) from error
    if input_generation.workspace_root != source.root:
        raise AuthoringWorkerError(
            "authoring_worker_protocol_invalid",
            "Authoring Worker 输入代与候选工作区不一致",
        )
    return candidate, input_generation


def _decode_catalog(payload: Any) -> PackageCatalog:
    """从 JSON 重建并复算一个不可变包目录。"""

    if not isinstance(payload, dict):
        raise ValueError("包目录必须是对象")
    distribution_payload = payload["distribution"]
    definitions_payload = payload["definitions"]
    distribution = PackageDistributionIdentity(
        name=distribution_payload["name"],
        normalized_name=distribution_payload["normalized_name"],
        version=distribution_payload["version"],
        description=distribution_payload.get("description", ""),
        license=distribution_payload.get("license", ""),
        homepage=distribution_payload.get("homepage", ""),
        requires_python=distribution_payload.get("requires_python", ""),
        dependencies=tuple(distribution_payload.get("dependencies", ())),
    )

    def definitions(kind: str) -> tuple[PackageDefinition, ...]:
        rows = definitions_payload[f"{kind}s"]
        return tuple(
            PackageDefinition(
                kind=row["kind"],
                id=row["id"],
                fqid=row["fqid"],
                module=row["module"],
                symbol=row["symbol"],
                declaring_file=row["declaring_file"],
                content_hash=row["content_hash"],
                version=row.get("version", "1.0.0"),
                title=row.get("title", ""),
                description=row.get("description", ""),
                details=row.get("details", {}),
            )
            for row in rows
        )

    catalog = PackageCatalog.create(
        distribution=distribution,
        import_package=payload["import_package"],
        namespace=payload["namespace"],
        definitions=PackageDefinitionCatalog(
            devices=definitions("device"),
            resources=definitions("resource"),
            workflows=definitions("workflow"),
        ),
        assets=tuple(
            PackageAsset(
                logical_path=row["logical_path"],
                digest=row["digest"],
                size=row["size"],
            )
            for row in payload["assets"]
        ),
        content_digest=payload["content_digest"],
    )
    if catalog.to_dict() != payload:
        raise ValueError("包目录摘要或规范内容不一致")
    return catalog


def _encode_bytes(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _decode_bytes(value: Any) -> bytes:
    if not isinstance(value, str):
        raise ValueError("wire bytes 必须是字符串")
    return base64.b64decode(value.encode("ascii"), validate=True)


def _graph_argument(candidate: WorkspaceRegistryRuntime) -> str:
    return candidate.graph_path.relative_to(candidate.source.root).as_posix()


def _runtime_ignored_paths(
    workspace_root: Path,
    configured_working_directory: Any,
) -> tuple[Path, ...]:
    if configured_working_directory is None:
        return (workspace_root / ".unilabos",)
    if not isinstance(configured_working_directory, (str, Path)):
        raise TypeError("working_dir 必须是字符串或路径")
    runtime_directory = Path(configured_working_directory).expanduser()
    if not runtime_directory.is_absolute():
        runtime_directory = workspace_root / runtime_directory
    return (runtime_directory.absolute(),)


def _run_worker(request_path: Path, response_path: Path) -> int:
    """执行一次请求并把唯一结果原子写入交换目录。"""

    response: dict[str, Any]
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        if (
            not isinstance(request, dict)
            or request.get("protocol_version") != _PROTOCOL_VERSION
            or not isinstance(request.get("arguments"), dict)
        ):
            raise ValueError("请求协议无效")
        arguments = dict(request["arguments"])
        # 局部导入避免生命周期 Module 在父进程加载 Worker Client 时形成环。
        from .lifecycle import prepare_stable_workspace_product_generation

        prepared = prepare_stable_workspace_product_generation(arguments)
        if prepared is None:
            raise ValueError("请求缺少工作区")
        _validate_selected_python_modules(prepared.candidate)
        response = {
            "protocol_version": _PROTOCOL_VERSION,
            "ok": True,
            "worker_pid": os.getpid(),
            "argument_patch": {
                key: arguments[key]
                for key in _ARGUMENT_PATCH_KEYS
                if key in arguments
            },
            "generation": _encode_generation(prepared),
        }
    except Exception as error:  # noqa: BLE001 - 故障必须跨进程结构化返回。
        response = {
            "protocol_version": _PROTOCOL_VERSION,
            "ok": False,
            "worker_pid": os.getpid(),
            "error": {
                "code": _worker_error_code(error),
                "message": str(error) or type(error).__name__,
            },
        }
    temporary_path = response_path.with_suffix(".tmp")
    temporary_path.write_text(
        json.dumps(response, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )
    temporary_path.replace(response_path)
    return 0 if response.get("ok") is True else 2


def _validate_selected_python_modules(candidate: WorkspaceRegistryRuntime) -> None:
    """只在一次性 Worker 中导入本代真正可能执行的作者模块。

    设备和资源限定为物理图激活计划；显式工作流清单中的模块全部验证。导入
    可能产生的装饰器注册、全局变量或第三方副作用都随 Worker 退出销毁，不会
    进入常驻 Local Backend。依赖导入失败与声明符号缺失使用稳定错误码返回。
    """

    import_roots = tuple(
        str(item.source.root) for item in candidate.package_catalog_sources
    )
    for import_root in reversed(import_roots):
        if import_root not in sys.path:
            sys.path.insert(0, import_root)
    definitions = (
        *candidate.activation_plan.devices,
        *candidate.activation_plan.resources,
        *candidate.registry_snapshot.workflows,
    )
    loaded_modules: dict[str, Any] = {}
    for definition in definitions:
        try:
            module = loaded_modules.get(definition.module)
            if module is None:
                module = importlib.import_module(definition.module)
                loaded_modules[definition.module] = module
        except ImportError as error:
            raise AuthoringWorkerError(
                "python_import_error",
                f"作者模块导入失败: {definition.module}: {error}",
            ) from error
        except Exception as error:
            raise AuthoringWorkerError(
                "python_module_error",
                f"作者模块执行失败: {definition.module}: {error}",
            ) from error
        if not hasattr(module, definition.symbol):
            raise AuthoringWorkerError(
                "python_symbol_missing",
                f"作者模块缺少声明符号: {definition.module}:{definition.symbol}",
            )


def _worker_error_code(error: Exception) -> str:
    diagnostics = getattr(error, "diagnostics", None)
    if isinstance(diagnostics, tuple) and diagnostics:
        first_code = getattr(diagnostics[0], "code", None)
        if isinstance(first_code, str) and first_code:
            return first_code
    code = getattr(error, "code", None)
    if isinstance(code, str) and code:
        return code
    return "authoring_worker_compile_failed"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--request", required=True)
    parser.add_argument("--response", required=True)
    arguments = parser.parse_args(argv)
    return _run_worker(Path(arguments.request), Path(arguments.response))


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "AuthoringWorkerError",
    "AuthoringWorkerResult",
    "prepare_candidate_in_worker",
    "prepare_workspace_generation_in_worker",
]
