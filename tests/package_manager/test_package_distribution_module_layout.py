"""包分发（Package Distribution）分层 Module 的规范依赖合同。"""

from __future__ import annotations

import ast
import importlib.util
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.package_manager.test_package_dependency_lock import _write_package
from unilabos.package_manager.package_catalog import PackageCatalog


class _BuildArtifactStub:
    """为云端发布 Adapter 提供最小已审计构建产物 Interface。"""

    def __init__(self, publication_input: dict[str, object]) -> None:
        """保存一次测试发布输入。

        参数：``publication_input`` 是 wheel、软件包信息和资源投影字典。
        返回：无。
        异常：无；具体字段由被测发布编排验证。
        """

        # ``_publication_input`` 是每次调用都要复制的冻结测试模板。
        self._publication_input = publication_input

    def publication_input(self) -> dict[str, object]:
        """返回本次发布独占的浅复制输入。

        参数：无。
        返回：构建产物已完成自审计后交给传输 Adapter 的字段字典。
        异常：无。
        """

        return dict(self._publication_input)


def _package_distribution_reverse_dependency_violations(
    module_root: Path,
) -> list[str]:
    """检查包分发（Package Distribution）全部源码的反向层依赖。

    参数：``module_root`` 是待检查 Module 根，文件按相对路径稳定排序。
    返回：``文件:行号:模块`` 格式的稳定违规列表；任何函数内延迟导入也不能
    绕过分层边界。
    异常：源码不可读或语法无效时传播文件系统或 ``SyntaxError``，不得跳过检查。
    """

    # ``forbidden_prefixes`` 是包分发层禁止拥有的历史或高层 Module 闭集。
    forbidden_prefixes = (
        "unilabos.package_manager.dependency_lock",
        "unilabos.package_manager.installation",
        "unilabos.package_manager.publication",
        "unilabos.package_manager.workspace_runtime",
        "unilabos.package_manager.driver_runtime",
    )
    # ``violations`` 按文件和 AST 顺序保存精确越层依赖位置。
    violations: list[str] = []
    for source_file in sorted(module_root.rglob("*.py")):
        # ``relative_file`` 是报告依赖方向违规时使用的 Module 内稳定路径。
        relative_file = source_file.relative_to(module_root)
        # ``module_parts`` 用于恢复相对 import 所在 Python 包身份。
        module_parts = list(relative_file.with_suffix("").parts)
        if module_parts[-1] == "__init__":
            module_parts.pop()
        # ``package_name`` 是 ``resolve_name`` 解析相对导入所需当前包身份。
        package_name = ".".join(
            [
                "unilabos",
                "package_manager",
                "package_distribution",
                *module_parts[:-1],
            ]
        )
        if relative_file.name == "__init__.py":
            package_name = ".".join(
                [
                    "unilabos",
                    "package_manager",
                    "package_distribution",
                    *module_parts,
                ]
            )
        # ``syntax_tree`` 只观察依赖方向，不导入或执行被检查 Module。
        syntax_tree = ast.parse(source_file.read_text(encoding="utf-8"))
        for node in ast.walk(syntax_tree):
            if isinstance(node, ast.Import):
                # ``imported_names`` 是普通 import 声明的完整模块身份集合。
                imported_names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                # ``imported_name`` 结合当前包把相对声明解析为绝对模块身份。
                imported_name = node.module or ""
                if node.level:
                    imported_name = importlib.util.resolve_name(
                        "." * node.level + imported_name,
                        package_name,
                    )
                imported_names = [imported_name]
            else:
                continue
            for imported_name in imported_names:
                if (
                    imported_name == "unilabos.package_manager"
                    or imported_name.startswith(forbidden_prefixes)
                ):
                    violations.append(f"{relative_file}:{node.lineno}:{imported_name}")
    return violations


def test_new_package_distribution_interface_manages_explicit_dependency(
    tmp_path: Path,
) -> None:
    """新 Module 通过同一公开 Interface 发布并加载显式软件包依赖。

    参数：``tmp_path`` 提供主工作区与外部软件包的隔离父目录。
    返回：无；断言新路径能发布锁并加载完整包目录（PackageCatalog）。
    异常：新 Module、依赖管理 Interface 或显式来源校验缺失时测试失败。
    """

    from unilabos.package_manager.package_distribution import (
        PackageDependencyManager,
        load_locked_package_catalogs,
    )
    from unilabos.package_manager.workspace_runtime import (
        WorkspaceSource,
        compile_package_source,
    )

    # ``workspace_root`` 是依赖声明与锁的写权威所在主工作区。
    workspace_root = tmp_path / "workspace"
    # ``external_root`` 是只通过显式声明授权的外部软件包来源。
    external_root = tmp_path / "external"
    _write_package(
        workspace_root,
        distribution_name="workspace-lab",
        package_name="workspace_lab",
    )
    _write_package(
        external_root,
        distribution_name="external-lab",
        package_name="external_lab",
        device_ids=("reader",),
    )

    # ``compiled_roots`` 记录两个公开操作实际委托给同一目录编译 Interface 的来源。
    compiled_roots: list[Path] = []

    def compile_catalog(source: WorkspaceSource) -> PackageCatalog:
        """记录包分发（Package Distribution）公开 Interface 的每次目录编译。

        参数：``source`` 是主工作区或显式外部包的安全来源。
        返回：统一工作区编译器产生的不可变包目录（PackageCatalog）。
        异常：来源或静态合同无效时传播规范编译异常。
        """

        # ``compiled_root`` 是本次编译唯一观察的来源根身份。
        compiled_root = source.root
        compiled_roots.append(compiled_root)
        return compile_package_source(source)

    # ``dependency_lock`` 是新 Module 完整校验后发布的依赖代际。
    dependency_lock = PackageDependencyManager(
        workspace_root,
        compile_catalog=compile_catalog,
    ).add("../external")
    # ``catalogs`` 是从显式来源和锁重新编译得到的包目录集合。
    catalogs = load_locked_package_catalogs(
        workspace_root,
        compile_catalog=compile_catalog,
    )

    assert tuple(item.distribution_name for item in dependency_lock.packages) == (
        "external-lab",
    )
    assert tuple(item.id for item in catalogs[0].definitions.devices) == ("reader",)
    assert compiled_roots == [
        external_root.resolve(),
        workspace_root.resolve(),
        external_root.resolve(),
        workspace_root.resolve(),
    ]


def test_flat_package_distribution_wrapper_modules_do_not_exist() -> None:
    """本次尚未发布的包分发（Package Distribution）包装必须直接删除。

    参数：无。
    返回：无；断言安装、依赖和发布只从分层 Module 或正式根门面公开。
    异常：任一未发布平铺包装仍存在时测试失败。
    """

    # ``package_manager_root`` 是尚未发布分层重构的源码边界。
    package_manager_root = (
        Path(__file__).resolve().parents[2] / "unilabos" / "package_manager"
    )
    # ``flat_wrapper_names`` 是本轮必须物理删除的包分发平铺包装文件。
    flat_wrapper_names = {
        "dependency_lock.py",
        "installation.py",
        "publication.py",
    }

    assert {path.name for path in package_manager_root.glob("*.py")}.isdisjoint(
        flat_wrapper_names
    )


def test_internal_callers_depend_on_package_distribution_interface() -> None:
    """命令行与运行时调用者必须直接依赖包分发（Package Distribution）Interface。

    参数：无。
    返回：无；断言产品调用者不再穿过已删除的 ``dependency_lock`` 或
    ``publication`` 平铺路径。
    异常：内部调用者重新引入已删除路径或没有采用新 Module 时测试失败。
    """

    # ``package_manager_root`` 是需要验证内部调用依赖方向的源码根。
    package_manager_root = (
        Path(__file__).resolve().parents[2] / "unilabos" / "package_manager"
    )
    # ``expected_imports`` 固定每个调用者必须采用的新 Module 绝对身份。
    expected_imports = {
        "cli.py": {
            "unilabos.package_manager.package_distribution",
        },
        "workspace_runtime/activation.py": {
            "unilabos.package_manager.package_distribution",
        },
    }
    # ``deleted_flat_modules`` 是本次迁移后禁止重新引入的平铺路径。
    deleted_flat_modules = {
        "unilabos.package_manager.dependency_lock",
        "unilabos.package_manager.installation",
        "unilabos.package_manager.publication",
    }

    for filename, required_modules in expected_imports.items():
        # ``source_file`` 是当前产品调用者的实际源码文件。
        source_file = package_manager_root / filename
        # ``syntax_tree`` 只解析导入，不执行产品启动逻辑。
        syntax_tree = ast.parse(source_file.read_text(encoding="utf-8"))
        # ``caller_package`` 是相对 import 解析所需的实际调用者包身份。
        caller_package = "unilabos.package_manager"
        relative_parent = source_file.parent.relative_to(package_manager_root)
        if relative_parent.parts:
            caller_package += "." + ".".join(relative_parent.parts)
        # ``imported_modules`` 保存解析成绝对身份的直接 import 集合。
        imported_modules: set[str] = set()
        for node in ast.walk(syntax_tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            imported_name = node.module or ""
            if node.level:
                imported_name = importlib.util.resolve_name(
                    "." * node.level + imported_name,
                    caller_package,
                )
            imported_modules.add(imported_name)
        assert required_modules <= imported_modules
        assert imported_modules.isdisjoint(deleted_flat_modules)


def test_package_distribution_module_has_no_reverse_layer_dependency() -> None:
    """包分发（Package Distribution）全树不依赖已删除路径或运行时层。

    参数：无。
    返回：无；解析新 Module 的全部顶层和函数内 import，不允许任何高层反向桥。
    异常：其他位置出现历史根实现、工作区运行时或驱动运行时依赖时测试失败。
    """

    # ``module_root`` 是包分发（Package Distribution）新 Module 的源码边界。
    module_root = (
        Path(__file__).resolve().parents[2]
        / "unilabos"
        / "package_manager"
        / "package_distribution"
    )
    # ``violations`` 必须为空，且伪非法函数回归测试已证明 helper 不会漏检。
    violations = _package_distribution_reverse_dependency_violations(module_root)

    assert violations == []


def test_package_distribution_requires_explicit_catalog_compiler() -> None:
    """所有目录加载编排都必须由组合根显式注入统一编译器。

    参数：无。
    返回：无；断言公开与内部编排函数的 ``compile_catalog`` 均为必填关键字参数。
    异常：任一入口恢复可选默认值或位置参数时测试失败，防止重新产生反向依赖。
    """

    from unilabos.package_manager.package_distribution import (
        PackageDependencyManager,
        load_locked_package_catalogs,
        load_locked_package_sources,
    )
    from unilabos.package_manager.package_distribution.dependency_manager import (
        catalog_for_entry,
        compile_dependency_catalog,
        validate_complete_generation,
    )

    # ``compiler_owners`` 是所有可能启动软件包目录编译的正式编排入口。
    compiler_owners = (
        PackageDependencyManager,
        load_locked_package_catalogs,
        load_locked_package_sources,
        catalog_for_entry,
        compile_dependency_catalog,
        validate_complete_generation,
    )
    for compiler_owner in compiler_owners:
        # ``compiler_parameter`` 是调用者必须显式承担的唯一编译器依赖。
        compiler_parameter = inspect.signature(compiler_owner).parameters[
            "compile_catalog"
        ]
        assert compiler_parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert compiler_parameter.default is inspect.Parameter.empty


def test_reverse_dependency_guard_rejects_function_local_runtime_import(
    tmp_path: Path,
) -> None:
    """依赖方向守卫必须拒绝任意函数内运行时反向导入。

    参数：``tmp_path`` 提供一个隔离的伪包分发（Package Distribution）源码根。
    返回：无；断言延迟导入也会被报告为越层依赖。
    异常：守卫错误跳过函数内导入时，精确违规列表断言失败。
    """

    # ``module_root`` 是只包含一个非法函数内反向导入的伪 Module 边界。
    module_root = tmp_path / "package_distribution"
    module_root.mkdir()
    # ``source_file`` 模拟函数内偷偷依赖工作区运行时（Workspace Runtime）。
    source_file = module_root / "rogue.py"
    source_file.write_text(
        "def load_runtime_compiler():\n"
        "    from unilabos.package_manager.workspace_runtime.discovery "
        "import compile_package_source\n"
        "    return compile_package_source\n",
        encoding="utf-8",
    )

    # ``violations`` 必须包含非法函数内导入，证明延迟导入没有例外。
    violations = _package_distribution_reverse_dependency_violations(module_root)

    assert violations == [
        "rogue.py:2:unilabos.package_manager.workspace_runtime.discovery"
    ]


def test_publication_port_uploads_artifact_before_publishing_resources() -> None:
    """云端发布 Adapter 只消费已审计构建并按顺序完成 wheel 与资源发布。

    参数：无。
    返回：无；断言公开地址、对象键和同一软件包信息进入全部资源 DTO。
    异常：实现重新检查工作区、漏传归档身份或没有发布资源时测试失败。
    """

    from unilabos.package_manager.package_distribution import publish_build

    class RecordingPublicationPort:
        """记录测试中跨系统发布调用的最小传输 Adapter。"""

        def __init__(self) -> None:
            """初始化空调用记录。

            参数：无。
            返回：无。
            异常：无。
            """

            # ``calls`` 保存归档上传与资源发布的可观察先后顺序。
            self.calls: list[tuple[str, object]] = []

        def upload_artifact(self, path: str) -> tuple[str, str]:
            """记录归档上传并返回固定云端身份。

            参数：``path`` 是被发布的本地已审计 wheel 路径。
            返回：独立给定的公开地址和对象键。
            异常：无。
            """

            self.calls.append(("artifact", path))
            return "https://packages.example/lab.whl", "models/lab.whl"

        def publish_resources(
            self,
            resources: list[dict[str, object]],
            package_info: dict[str, object],
        ) -> object:
            """记录资源发布并返回成功状态。

            参数：``resources`` 是兼容资源 DTO；``package_info`` 是发布包身份。
            返回：HTTP 201 的最小响应对象。
            异常：无。
            """

            self.calls.append(
                (
                    "resources",
                    {
                        "resources": resources,
                        "package_info": package_info,
                    },
                )
            )
            return SimpleNamespace(status_code=201, text="created")

    # ``artifact`` 是已经完成 wheel 来源重编译审计的最小构建产物。
    artifact = _BuildArtifactStub(
        {
            "archive_path": "/tmp/lab.whl",
            "package_info": {"class_namespace": "community.lab"},
            "resources": [{"id": "community.lab.reader"}],
        }
    )
    # ``port`` 是测试唯一替代的外部云端系统边界。
    port = RecordingPublicationPort()

    # ``published`` 是新云端 Adapter 返回的稳定发布结果。
    published = publish_build(artifact, port)

    assert [item[0] for item in port.calls] == ["artifact", "resources"]
    assert published["download_url"] == "https://packages.example/lab.whl"
    assert published["package_info"]["oss_object_key"] == "models/lab.whl"
    assert published["resources"][0]["package_info"] == published["package_info"]
    assert published["response_status"] == 201


def test_http_publication_adapter_preserves_delegate_contract() -> None:
    """HTTP 发布 Adapter 保持委托参数及发布传输异常的原始身份。

    参数：无。
    返回：无；断言归档场景、资源 DTO 和软件包信息原样委托，并且
    ``publish_build`` 不改包传输 Adapter 抛出的异常对象。
    异常：参数被复制或改写、传输异常被包装成 ``PackageCLIError`` 时测试失败。
    """

    from unilabos.package_manager.package_distribution import (
        HttpClientPublicationAdapter,
        publish_build,
    )

    class PublicationTransportError(RuntimeError):
        """表示测试云端资源发布边界返回的固定传输失败。"""

    # ``transport_error`` 是必须穿过发布编排保持对象身份的传输失败实例。
    transport_error = PublicationTransportError("publication transport unavailable")

    class RecordingHttpClient:
        """记录现有 HTTP 客户端两个发布操作收到的原始参数。"""

        def __init__(self) -> None:
            """初始化调用记录和可切换的资源发布失败状态。

            参数：无。
            返回：无。
            异常：无。
            """

            # 两类调用分别保留委托参数身份；``fail_publication`` 控制固定点失败。
            self.upload_calls: list[tuple[str, str]] = []
            self.publication_calls: list[
                tuple[list[dict[str, object]], dict[str, object]]
            ] = []
            self.fail_publication = False

        def upload_file_to_oss(
            self,
            path: str,
            *,
            scene: str,
        ) -> tuple[str, str]:
            """记录归档路径和 OSS 场景并返回固定产物身份。

            参数：``path`` 是归档路径；``scene`` 是现有 HTTP 合同要求的上传场景。
            返回：公开下载地址和对象键。
            异常：无。
            """

            self.upload_calls.append((path, scene))
            return "https://packages.example/direct.whl", "models/direct.whl"

        def upload_package_resources(
            self,
            resources: list[dict[str, object]],
            package_info: dict[str, object],
        ) -> object:
            """记录资源发布参数并按测试状态返回成功或原始传输失败。

            参数：``resources`` 是兼容资源 DTO；``package_info`` 是软件包发布信息。
            返回：未启用失败时返回 HTTP 200 的最小响应对象。
            异常：启用失败时抛出同一 ``transport_error`` 对象。
            """

            self.publication_calls.append((resources, package_info))
            if self.fail_publication:
                raise transport_error
            return SimpleNamespace(status_code=200, text="ok")

    # ``http_client`` 是被适配的现有传输实现；``adapter`` 是待验证的真实 Adapter。
    http_client = RecordingHttpClient()
    adapter = HttpClientPublicationAdapter(http_client)
    # ``resources`` 与 ``package_info`` 用于证明直接委托不复制参数容器。
    resources = [{"id": "community.lab.reader"}]
    package_info = {"class_namespace": "community.lab"}

    assert adapter.upload_artifact("/tmp/direct.whl") == (
        "https://packages.example/direct.whl",
        "models/direct.whl",
    )
    adapter.publish_resources(resources, package_info)

    assert http_client.upload_calls == [("/tmp/direct.whl", "models")]
    assert http_client.publication_calls[0][0] is resources
    assert http_client.publication_calls[0][1] is package_info

    http_client.fail_publication = True
    # ``artifact`` 再次经过 wheel 上传，然后在资源发布边界触发原始传输失败。
    artifact = _BuildArtifactStub(
        {
            "archive_path": "/tmp/lab.whl",
            "package_info": {"class_namespace": "community.lab"},
            "resources": [{"id": "community.lab.reader"}],
        }
    )
    with pytest.raises(PublicationTransportError) as caught:
        publish_build(
            artifact,
            adapter,
        )

    assert caught.value is transport_error
    assert http_client.upload_calls[-1] == ("/tmp/lab.whl", "models")


def test_upload_interfaces_have_no_external_download_url_bypass() -> None:
    """规范上传 Interface 必须实际上传本次已审计 wheel。

    参数：无。
    返回：无；断言 CLI、上传编排和发布编排均不接受外部下载 URL 参数。
    异常：任一隐藏绕行参数残留时签名断言失败。
    """

    from unilabos.package_manager.cli import upload_package as cli_upload_package
    from unilabos.package_manager.package_distribution import publish_build
    from unilabos.package_manager.package_distribution.adapters.cloud import (
        upload_package as adapter_upload_package,
    )

    # ``upload_interfaces`` 是所有可能绕过本次 wheel 直传的规范入口集合。
    upload_interfaces = (
        cli_upload_package,
        adapter_upload_package,
        publish_build,
    )

    assert all(
        "download_url" not in inspect.signature(operation).parameters
        for operation in upload_interfaces
    )


def test_publication_port_rejects_backend_error_status() -> None:
    """云端资源发布返回非成功状态时必须保留诊断并关闭式失败。

    参数：无。
    返回：无；断言 HTTP 503 与响应文本被归一为 ``PackageCLIError``。
    异常：若实现把失败响应当成已发布代际，预期异常断言失败。
    """

    from unilabos.package_manager import PackageCLIError
    from unilabos.package_manager.package_distribution import publish_build

    class FailingPublicationPort:
        """模拟归档成功但资源发布失败的云端传输 Adapter。"""

        def upload_artifact(self, path: str) -> tuple[str, str]:
            """返回固定归档地址以推进到资源发布阶段。

            参数：``path`` 是本地归档路径；测试不读取该文件。
            返回：公开地址和空对象键。
            异常：无。
            """

            return "https://packages.example/lab.whl", ""

        def publish_resources(
            self,
            resources: list[dict[str, object]],
            package_info: dict[str, object],
        ) -> object:
            """返回携带诊断文本的 HTTP 503 响应。

            参数：``resources`` 与 ``package_info`` 是待发布兼容投影。
            返回：HTTP 503 的最小响应对象。
            异常：无；错误状态由发布编排统一解释。
            """

            return SimpleNamespace(status_code=503, text="catalog unavailable")

    # ``artifact`` 是无需重新扫描的最小已审计发布产物。
    artifact = _BuildArtifactStub(
        {
            "archive_path": "/tmp/lab.whl",
            "package_info": {"class_namespace": "community.lab"},
            "resources": [{"id": "community.lab.reader"}],
        }
    )

    with pytest.raises(
        PackageCLIError,
        match="503 catalog unavailable",
    ):
        publish_build(artifact, FailingPublicationPort())
