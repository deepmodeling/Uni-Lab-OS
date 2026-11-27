"""软件包构建（Package Build）的暂存、自审计与发布产物合同。"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from tests.package_manager.test_package_catalog_compiler import _write_package


def _prepare_buildable_package(workspace_root: Path) -> None:
    """写入能由标准 Python 构建后端完整收集资产的测试软件包。

    参数：``workspace_root`` 是隔离的软件包工作区（Package Workspace）根。
    返回：无；在既有完整目录夹具上补齐标准 wheel 构建声明。
    异常：测试文件系统不可写时传播原始异常。
    """

    _write_package(workspace_root)
    workspace_root.joinpath("pyproject.toml").write_text(
        "[build-system]\n"
        'requires = ["setuptools>=68", "wheel"]\n'
        'build-backend = "setuptools.build_meta"\n\n'
        "[project]\n"
        'name = "catalog-lab"\n'
        'version = "1.2.3"\n'
        'description = "静态目录测试包"\n\n'
        "[tool.setuptools.packages.find]\n"
        'include = ["catalog_lab*"]\n\n'
        "[tool.setuptools.package-data]\n"
        'catalog_lab = ["models/*.glb", "_generated/*.json", '
        '"_generated/*.toml", "_generated/*.yaml"]\n',
        encoding="utf-8",
    )


def _wheel_members(wheel: Path) -> dict[str, bytes]:
    """读取 wheel 全部普通成员供构建合同断言。

    参数：``wheel`` 是已经完成审计的标准 wheel 路径。
    返回：按成员名索引的原始字节字典。
    异常：wheel 不可读或 ZIP 无效时传播标准库异常。
    """

    with zipfile.ZipFile(wheel) as archive:
        return {
            item.filename: archive.read(item)
            for item in archive.infolist()
            if not item.is_dir()
        }


def _rewrite_wheel_member(wheel: Path, member_name: str, payload: bytes) -> None:
    """篡改一个 wheel 成员并同步 RECORD，模拟内容一致性攻击。

    参数：``wheel`` 是待篡改产物；``member_name`` 是成员身份；``payload`` 是替换
    字节。返回：无；原地替换 wheel。
    异常：wheel 或 RECORD 无效时传播标准库异常。
    """

    # ``members`` 是保留除签名和 RECORD 外全部原成员的测试篡改集合。
    members = {
        name: content
        for name, content in _wheel_members(wheel).items()
        if not name.endswith(("/RECORD", "/RECORD.jws", "/RECORD.p7s"))
    }
    members[member_name] = payload
    # ``record_name`` 是被篡改 wheel 中唯一标准记录文件身份。
    record_names = [
        name for name in _wheel_members(wheel) if name.endswith(".dist-info/RECORD")
    ]
    assert len(record_names) == 1
    record_name = record_names[0]
    # ``record_stream`` 保存与篡改后内容一致的合法 wheel 哈希记录。
    record_stream = io.StringIO(newline="")
    record_writer = csv.writer(record_stream, lineterminator="\n")
    for name, content in sorted(members.items()):
        digest = hashlib.sha256(content).digest()
        import base64

        encoded = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        record_writer.writerow((name, f"sha256={encoded}", len(content)))
    record_writer.writerow((record_name, "", ""))
    members[record_name] = record_stream.getvalue().encode("utf-8")
    # ``replacement`` 隔离重写过程，成功后才替换原测试产物。
    replacement = wheel.with_suffix(".tampered.whl")
    with zipfile.ZipFile(replacement, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(members.items()):
            archive.writestr(name, content)
    replacement.replace(wheel)


def test_build_stages_catalog_and_audits_wheel_without_mutating_source(
    tmp_path: Path,
) -> None:
    """构建在临时暂存树嵌入目录，并输出可发布 wheel 与同源投影。

    参数：``tmp_path`` 提供源码、构建目录和产物目录的隔离父目录。
    返回：无；断言源码不被写入、wheel 闭包完整、规范目录与发布投影同源。
    异常：构建、重编译审计或标准 RECORD 无效时测试失败。
    """

    from unilabos.package_manager.package_distribution import (
        build_workspace_package,
    )
    from unilabos.package_manager.workspace_runtime import compile_package_source

    # ``workspace_root`` 是不得被生成目录污染的作者源码工作区。
    workspace_root = tmp_path / "workspace"
    # ``output_root`` 是全部可发布产物的唯一目标目录。
    output_root = tmp_path / "dist"
    _prepare_buildable_package(workspace_root)

    # ``artifact`` 是完成 wheel 来源重编译和闭包审计后的构建产物。
    artifact = build_workspace_package(
        workspace_root,
        output_root,
        compile_catalog=compile_package_source,
    )
    # ``members`` 用于证明目录、项目声明、工作流清单和资产均进入同一 wheel。
    members = _wheel_members(artifact.wheel)

    assert not workspace_root.joinpath("catalog_lab/_generated").exists()
    assert artifact.wheel.parent == output_root.resolve()
    assert artifact.artifact_digest == (
        "sha256:" + hashlib.sha256(artifact.wheel.read_bytes()).hexdigest()
    )
    assert members["catalog_lab/_generated/package.catalog.json"] == (
        artifact.catalog.to_canonical_bytes()
    )
    assert "catalog_lab/_generated/pyproject.toml" in members
    assert "catalog_lab/_generated/package.yaml" in members
    assert "catalog_lab/models/plate.glb" in members
    assert json.loads(artifact.catalog_path.read_text(encoding="utf-8")) == (
        artifact.catalog.to_dict()
    )
    assert artifact.package_info["sha256"] == artifact.artifact_digest
    assert artifact.package_info["catalog_digest"] == artifact.catalog.catalog_digest
    assert artifact.resources
    assert all(
        item["package_info"] == artifact.package_info for item in artifact.resources
    )


def test_build_rejects_wheel_that_omits_catalog_asset_closure(
    tmp_path: Path,
) -> None:
    """标准构建漏装目录资产时关闭式失败且不发布部分 wheel。

    参数：``tmp_path`` 提供缺少 package-data 声明的软件包与产物目录。
    返回：无；断言资产闭包错误阻止目标 wheel 和发布投影落盘。
    异常：预期 ``PackageBuildError``；若错误 wheel 被接受则测试失败。
    """

    from unilabos.package_manager.package_distribution import (
        PackageBuildError,
        build_workspace_package,
    )
    from unilabos.package_manager.workspace_runtime import compile_package_source

    # ``workspace_root`` 使用现有夹具，但故意不声明非 Python 资产包数据。
    workspace_root = tmp_path / "workspace"
    # ``output_root`` 必须在审计失败后保持不含发布 wheel。
    output_root = tmp_path / "dist"
    _write_package(workspace_root)

    with pytest.raises(PackageBuildError, match="缺失包目录闭包"):
        build_workspace_package(
            workspace_root,
            output_root,
            compile_catalog=compile_package_source,
        )

    assert not tuple(output_root.glob("*.whl"))
    assert not output_root.joinpath("package.catalog.json").exists()


def test_wheel_audit_recompiles_source_instead_of_trusting_embedded_catalog(
    tmp_path: Path,
) -> None:
    """wheel 内嵌目录与实际源码不一致时由重编译审计拒绝。

    参数：``tmp_path`` 提供先成功构建、再篡改内嵌目录的隔离产物。
    返回：无；断言即使 RECORD 已同步，审计仍比较实际源码的规范目录。
    异常：预期 ``PackageBuildError``；若只信任内嵌 JSON 则测试失败。
    """

    from unilabos.package_manager.package_distribution import (
        PackageBuildError,
        audit_package_wheel,
        build_workspace_package,
    )
    from unilabos.package_manager.workspace_runtime import compile_package_source

    # ``workspace_root`` 是产生原始可信目录与 wheel 的源码工作区。
    workspace_root = tmp_path / "workspace"
    _prepare_buildable_package(workspace_root)
    # ``artifact`` 是篡改前已经通过完整审计的构建产物。
    artifact = build_workspace_package(
        workspace_root,
        tmp_path / "dist",
        compile_catalog=compile_package_source,
    )
    # ``lying_catalog`` 只改变展示标题，并重新生成自洽的目录摘要。
    original_device = artifact.catalog.definitions.devices[0]
    lying_definitions = replace(
        artifact.catalog.definitions,
        devices=(replace(original_device, title="伪造名称"),),
    )
    lying_catalog = artifact.catalog.create(
        distribution=artifact.catalog.distribution,
        import_package=artifact.catalog.import_package,
        namespace=artifact.catalog.namespace,
        definitions=lying_definitions,
        assets=artifact.catalog.assets,
        content_digest=artifact.catalog.content_digest,
    )
    _rewrite_wheel_member(
        artifact.wheel,
        "catalog_lab/_generated/package.catalog.json",
        lying_catalog.to_canonical_bytes(),
    )
    # ``tampered_digest`` 是篡改后真实 wheel 摘要，排除仅摘要不匹配导致的失败。
    tampered_digest = (
        "sha256:" + hashlib.sha256(artifact.wheel.read_bytes()).hexdigest()
    )

    with pytest.raises(PackageBuildError, match="内嵌包目录"):
        audit_package_wheel(
            artifact.wheel,
            artifact.catalog,
            expected_digest=tampered_digest,
            compile_catalog=compile_package_source,
        )


def test_build_requires_an_explicit_catalog_compiler() -> None:
    """包分发层不得反向导入工作区运行时编译器。

    参数：无。
    返回：无；断言构建与审计入口都要求关键字注入同一目录编译 Interface。
    异常：函数签名漂移为隐式编译器时测试失败。
    """

    import inspect

    from unilabos.package_manager.package_distribution import (
        audit_package_wheel,
        build_workspace_package,
    )

    for operation in (build_workspace_package, audit_package_wheel):
        # ``compiler_parameter`` 是阻断反向依赖的显式编译器注入点。
        compiler_parameter = inspect.signature(operation).parameters["compile_catalog"]
        assert compiler_parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert compiler_parameter.default is inspect.Parameter.empty


def test_wheel_audit_reconstructs_declared_workspace_startup_inputs(
    tmp_path: Path,
) -> None:
    """包外启动默认文件只作为重编译证据嵌入生成目录。

    参数：``tmp_path`` 提供带物理图和配置默认值的可构建工作区。
    返回：无；断言 wheel 自审计能重建原目录输入，但不增加第二个顶层载荷。
    异常：启动文件缺失或来源 parity 失败时测试失败。
    """

    from unilabos.package_manager.package_distribution import (
        build_workspace_package,
    )
    from unilabos.package_manager.workspace_runtime import compile_package_source

    # ``workspace_root`` 是声明包外运行启动默认文件的作者工作区。
    workspace_root = tmp_path / "workspace"
    _prepare_buildable_package(workspace_root)
    with workspace_root.joinpath("pyproject.toml").open("a", encoding="utf-8") as file:
        file.write(
            "\n[tool.unilabos.startup]\n"
            'graph = "deployment/graph.json"\n'
            'config = "deployment/local_config.py"\n'
        )
    workspace_root.joinpath("deployment").mkdir()
    workspace_root.joinpath("deployment/graph.json").write_text(
        '{"nodes": [], "links": []}',
        encoding="utf-8",
    )
    workspace_root.joinpath("deployment/local_config.py").write_text(
        "CONFIG = {}\n",
        encoding="utf-8",
    )

    # ``artifact`` 必须只依赖 wheel 内生成证据完成第二次目录编译。
    artifact = build_workspace_package(
        workspace_root,
        tmp_path / "dist",
        compile_catalog=compile_package_source,
    )
    members = _wheel_members(artifact.wheel)

    assert "catalog_lab/_generated/workspace/deployment/graph.json" in members
    assert "catalog_lab/_generated/workspace/deployment/local_config.py" in members
    assert "deployment/graph.json" not in members


def test_upload_builds_once_and_publishes_the_same_audited_wheel() -> None:
    """软件包上传（Package Upload）必须复用构建而非检查归档。

    参数：无。
    返回：无；断言构建 Interface 只调用一次，上传路径和模板投影来自同一产物。
    异常：若上传重新检查源码、上传源码 tar 或跳过构建，测试失败。
    """

    from unilabos.package_manager.package_distribution.adapters.cloud import (
        upload_package,
    )

    class AuditedArtifact:
        """提供测试所需最小已审计构建产物 Interface。"""

        def publication_input(self) -> dict[str, object]:
            """返回 wheel 和同源云端投影。

            参数：无。
            返回：固定 wheel、包身份和单个设备模板投影。
            异常：无。
            """

            return {
                "archive_path": "/tmp/catalog_lab-1.2.3-py3-none-any.whl",
                "package_info": {"class_namespace": "community.catalog_lab"},
                "resources": [{"id": "community.catalog_lab.reactor"}],
            }

    class RecordingHttpClient:
        """记录 wheel 上传与资源模板发布调用。"""

        def __init__(self) -> None:
            """初始化空传输记录。

            参数：无。
            返回：无。
            异常：无。
            """

            # 两个列表分别保存产物上传和资源模板发布的实参。
            self.uploaded: list[str] = []
            self.published: list[list[dict[str, object]]] = []

        def upload_file_to_oss(
            self,
            path: str,
            *,
            scene: str,
        ) -> tuple[str, str]:
            """记录 wheel 路径并返回固定云端身份。

            参数：``path`` 是已审计 wheel；``scene`` 是现有对象存储场景。
            返回：公开地址和对象键。
            异常：无。
            """

            assert scene == "models"
            self.uploaded.append(path)
            return "https://packages.example/catalog_lab.whl", "models/catalog.whl"

        def upload_package_resources(
            self,
            resources: list[dict[str, object]],
            package_info: dict[str, object],
        ) -> object:
            """记录模板投影并返回成功响应。

            参数：``resources`` 是模板 DTO；``package_info`` 是同一 wheel 身份。
            返回：带 HTTP 201 的最小响应。
            异常：无。
            """

            from types import SimpleNamespace

            assert resources[0]["package_info"] is package_info
            self.published.append(resources)
            return SimpleNamespace(status_code=201, text="created")

    # ``build_calls`` 证明上传不额外调用软件包检查（Package Inspect）或二次构建。
    build_calls: list[tuple[str, str | None]] = []

    def build_once(path: str, *, out_dir: str | None) -> AuditedArtifact:
        """记录唯一构建调用并返回固定审计产物。

        参数：``path`` 是源码工作区；``out_dir`` 是调用者选择的产物目录。
        返回：最小已审计构建产物。
        异常：无。
        """

        build_calls.append((path, out_dir))
        return AuditedArtifact()

    # ``http_client`` 是本轮唯一云端传输 Adapter。
    http_client = RecordingHttpClient()

    result = upload_package(
        "/workspace/catalog-lab",
        http_client,
        out_dir="/workspace/dist",
        package_builder=build_once,
    )

    assert build_calls == [("/workspace/catalog-lab", "/workspace/dist")]
    assert http_client.uploaded == ["/tmp/catalog_lab-1.2.3-py3-none-any.whl"]
    assert result["artifact"].endswith(".whl")


def test_projection_preparation_failure_never_publishes_the_target_wheel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """任一发布投影准备失败时不得留下作为就绪标志的目标 wheel。

    参数：``tmp_path`` 提供可构建工作区与空产物目录；``monkeypatch`` 在资源投影
    写入点注入确定失败。
    返回：无；断言失败发生在目标 wheel 提交前，正式产物目录没有就绪标志。
    异常：预期测试注入的 ``OSError``；若 wheel 先发布则断言失败。
    """

    from unilabos.package_manager.package_distribution import build as build_module
    from unilabos.package_manager.workspace_runtime import compile_package_source

    # ``workspace_root`` 是本轮构建唯一作者源码来源。
    workspace_root = tmp_path / "workspace"
    # ``output_root`` 是失败后不得出现正式 wheel 的发布目录。
    output_root = tmp_path / "dist"
    output_root.mkdir()
    # ``previous_marker`` 模拟同版本上一次构建留下的正式就绪标志。
    previous_marker = output_root / "catalog_lab-1.2.3-py3-none-any.whl"
    previous_marker.write_bytes(b"previous-ready-generation")
    _prepare_buildable_package(workspace_root)
    # ``original_writer`` 保留非资源投影的真实临时文件写入行为。
    original_writer = build_module._write_output_file

    def fail_resources_projection(target: Path, payload: bytes) -> None:
        """只在资源投影准备点注入确定文件系统失败。

        参数：``target`` 是候选投影路径；``payload`` 是完整投影字节。
        返回：非资源投影委托真实写入；资源投影不返回。
        异常：资源投影固定抛出 ``OSError``。
        """

        if target.name == "resources.json":
            raise OSError("resources projection unavailable")
        original_writer(target, payload)

    monkeypatch.setattr(
        build_module,
        "_write_output_file",
        fail_resources_projection,
    )

    with pytest.raises(OSError, match="resources projection unavailable"):
        build_module.build_workspace_package(
            workspace_root,
            output_root,
            compile_catalog=compile_package_source,
        )

    assert not tuple(output_root.glob("*.whl"))
    assert not output_root.joinpath("package.catalog.json").exists()
    assert not output_root.joinpath("package_info.json").exists()
    assert not output_root.joinpath("resources.json").exists()


def test_wheel_audit_rejects_the_callers_symlink_before_resolving_path(
    tmp_path: Path,
) -> None:
    """wheel 审计必须按调用者原始路径拒绝符号链接。

    参数：``tmp_path`` 提供真实已审计 wheel 和指向它的符号链接。
    返回：无；断言链接即使指向合法 wheel 且摘要匹配也被关闭式拒绝。
    异常：预期 ``PackageBuildError``；若先 resolve 后检查则测试失败。
    """

    from unilabos.package_manager.package_distribution import (
        PackageBuildError,
        audit_package_wheel,
        build_workspace_package,
    )
    from unilabos.package_manager.workspace_runtime import compile_package_source

    # ``workspace_root`` 是产生合法 wheel 和规范目录的源码工作区。
    workspace_root = tmp_path / "workspace"
    _prepare_buildable_package(workspace_root)
    # ``artifact`` 是符号链接目标对应的可信构建产物。
    artifact = build_workspace_package(
        workspace_root,
        tmp_path / "dist",
        compile_catalog=compile_package_source,
    )
    # ``wheel_link`` 保留调用者提交的符号链接身份，不能被 resolve 隐藏。
    wheel_link = tmp_path / "linked-wheel.whl"
    wheel_link.symlink_to(artifact.wheel)

    with pytest.raises(PackageBuildError, match="符号链接"):
        audit_package_wheel(
            wheel_link,
            artifact.catalog,
            expected_digest=artifact.artifact_digest,
            compile_catalog=compile_package_source,
        )


def test_target_wheel_is_the_last_published_generation_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正式 wheel 必须晚于全部投影提交并作为代际就绪标志。

    参数：``tmp_path`` 提供可构建工作区；``monkeypatch`` 记录正式文件提交顺序。
    返回：无；断言三个 JSON 投影在前且 wheel 严格最后。
    异常：构建或发布顺序漂移时测试失败。
    """

    from unilabos.package_manager.package_distribution import build as build_module
    from unilabos.package_manager.workspace_runtime import compile_package_source

    # ``workspace_root`` 是本轮构建唯一作者源码来源。
    workspace_root = tmp_path / "workspace"
    _prepare_buildable_package(workspace_root)
    # ``published_names`` 按实际原子替换顺序记录正式代际文件名。
    published_names: list[str] = []
    # ``original_publisher`` 保留真实文件提交行为。
    original_publisher = build_module._publish_file

    def record_publication(source: Path, target: Path) -> None:
        """记录并执行一个正式代际文件提交。

        参数：``source`` 是临时候选；``target`` 是正式产物路径。
        返回：无；成功委托真实原子替换。
        异常：文件提交失败时传播原始异常。
        """

        published_names.append(target.name)
        original_publisher(source, target)

    monkeypatch.setattr(build_module, "_publish_file", record_publication)

    build_module.build_workspace_package(
        workspace_root,
        tmp_path / "dist",
        compile_catalog=compile_package_source,
    )

    assert published_names[:3] == [
        "package.catalog.json",
        "package_info.json",
        "resources.json",
    ]
    assert published_names[-1].endswith(".whl")
