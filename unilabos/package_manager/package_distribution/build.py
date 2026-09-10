"""软件包构建（Package Build）的暂存、标准 wheel 与来源自审计。"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from ..package_catalog import PackageCatalog, WorkspaceSource
from ..package_catalog.project_metadata import (
    parse_project_metadata,
    project_to_legacy_dict,
)
from .inspection import CatalogCompiler
from .legacy_projection import build_package_info, build_resources_from_registry

# 暂存排除集合禁止把本地缓存、运行数据和旧构建产物带入发布 wheel。
_STAGING_EXCLUDES = frozenset(
    {
        ".git",
        ".hg",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        ".tox",
        ".unilabos",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "unilabos_data",
        "venv",
    }
)
# 以下上限约束不可信 wheel 的压缩包观察，避免解压炸弹或异常成员耗尽资源。
_MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
_MAX_MEMBER_BYTES = 1024 * 1024 * 1024
_MAX_TOTAL_UNCOMPRESSED_BYTES = 4 * 1024 * 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 50_000
_MAX_COMPRESSION_RATIO = 1_000


class PackageBuildError(RuntimeError):
    """表示软件包不能构建为经过自审计的标准 wheel。"""


@dataclass(frozen=True, slots=True)
class PackageBuildArtifact:
    """一次通过来源重编译审计的完整软件包发布代际。"""

    # ``wheel`` 是可以交给安装器或云端广场的唯一二进制发布归档。
    wheel: Path
    # ``artifact_digest`` 是 wheel 完整字节的稳定 SHA-256 身份。
    artifact_digest: str
    # ``catalog`` 是从暂存源码编译并由 wheel 来源重新证明的包目录。
    catalog: PackageCatalog
    # 三个路径是与同一 wheel 和目录绑定的可读发布投影。
    catalog_path: Path
    package_info_path: Path
    resources_path: Path
    # ``package_info`` 与 ``resources`` 是现有云端广场接口消费的兼容 DTO。
    package_info: Mapping[str, Any]
    resources: tuple[Mapping[str, Any], ...]

    def publication_input(self) -> dict[str, Any]:
        """生成云端发布 Adapter 消费的独立可变输入。

        参数：无。
        返回：以已审计 wheel 作为 ``archive_path`` 的发布字典；兼容 DTO 均复制，
        调用者修改返回值不会改变构建产物对象。
        异常：无；字段已在构建成功前完成验证。
        """

        # ``package_info`` 是本次发布独占的软件包身份容器。
        package_info = dict(self.package_info)
        # ``resources`` 是本次发布独占的模板投影集合，并统一引用同一包身份。
        resources = [dict(item) for item in self.resources]
        for resource in resources:
            resource["package_info"] = package_info
        return {
            "archive_path": str(self.wheel),
            "package_info": package_info,
            "resources": resources,
        }


def build_workspace_package(
    workspace: str | Path,
    output_dir: str | Path,
    *,
    compile_catalog: CatalogCompiler,
) -> PackageBuildArtifact:
    """在临时暂存树构建 wheel，并从 wheel 来源重编译后发布产物。

    参数：``workspace`` 是作者显式选择的软件包工作区（Package Workspace）；
    ``output_dir`` 是最终产物目录；``compile_catalog`` 是组合根注入的唯一包目录
    （PackageCatalog）编译 Interface。
    返回：包含已审计 wheel、摘要、目录和云端兼容投影的不可变构建产物。
    异常：路径、标准构建、wheel 安全、闭包或 parity 无效时抛出
    ``PackageBuildError``/目录编译异常；失败不会把候选 wheel 发布到目标目录。
    """

    if not callable(compile_catalog):
        raise TypeError("compile_catalog 必须可调用")
    # ``workspace_root`` 是本次构建唯一授权的作者源码边界。
    workspace_root = Path(workspace).expanduser().resolve()
    if not workspace_root.is_dir():
        raise PackageBuildError(f"软件包工作区不存在：{workspace_root}")
    # ``artifact_root`` 是审计通过后才写入的最终发布目录。
    artifact_root = Path(output_dir).expanduser().resolve()
    artifact_root.mkdir(parents=True, exist_ok=True)

    # ``artifact_root`` 已规范化；把暂存代际放在该边界内，避免 macOS 的
    # ``/var`` → ``/private/var`` 系统别名被 WorkspaceSource 误判为调用者链接。
    with tempfile.TemporaryDirectory(
        prefix="unilab-package-build-",
        dir=artifact_root,
    ) as temporary:
        # ``temporary_root`` 隔离暂存源码、标准构建输出和 wheel 审计工作区。
        temporary_root = Path(temporary)
        staging_root = temporary_root / "workspace"
        wheel_output = temporary_root / "wheel"
        _copy_workspace(workspace_root, staging_root)
        wheel_output.mkdir()

        # ``staging_source`` 是目录编译和构建共同观察的同一固定暂存来源。
        staging_source = WorkspaceSource(staging_root)
        # ``catalog`` 是本轮构建唯一规范包目录，后续投影和审计不得重新解释源码。
        catalog = compile_catalog(staging_source)
        generated_members = _write_generated_catalog(staging_source, catalog)
        # ``candidate_wheel`` 尚未发布，只有完整自审计通过后才复制到目标目录。
        candidate_wheel = _build_standard_wheel(staging_root, wheel_output)
        _inject_wheel_members(candidate_wheel, generated_members)
        # ``candidate_digest`` 绑定重写 RECORD 后的最终候选 wheel 字节。
        candidate_digest = _artifact_digest(candidate_wheel)
        audit_package_wheel(
            candidate_wheel,
            catalog,
            expected_digest=candidate_digest,
            compile_catalog=compile_catalog,
        )
        # ``target_wheel`` 是本版本正式就绪标志；准备新投影前必须先隐藏旧标志。
        target_wheel = artifact_root / candidate_wheel.name
        if target_wheel.exists() or target_wheel.is_symlink():
            # ``previous_marker`` 只在临时构建代际中保留旧标志，失败后不再误报就绪。
            previous_marker = temporary_root / "previous-wheel" / target_wheel.name
            previous_marker.parent.mkdir()
            target_wheel.replace(previous_marker)
        # ``generation_root`` 是全部发布投影先完整准备的临时代际边界。
        generation_root = temporary_root / "generation"
        generation_root.mkdir()
        # 以下投影只从已通过 wheel 来源重编译的同一目录与摘要生成。
        package_info, resources = _publication_projections(
            catalog,
            staging_project_bytes=generated_members[
                f"{catalog.import_package}/_generated/pyproject.toml"
            ],
            artifact_digest=candidate_digest,
        )
        prepared_catalog = generation_root / "package.catalog.json"
        prepared_package_info = generation_root / "package_info.json"
        prepared_resources = generation_root / "resources.json"
        _write_output_file(prepared_catalog, catalog.to_canonical_bytes())
        _write_output_file(
            prepared_package_info,
            json.dumps(package_info, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        _write_output_file(
            prepared_resources,
            json.dumps(resources, ensure_ascii=False, indent=2).encode("utf-8"),
        )

        # 三个正式投影必须先就绪，wheel 最后提交并作为本代际就绪标志。
        catalog_path = artifact_root / prepared_catalog.name
        package_info_path = artifact_root / prepared_package_info.name
        resources_path = artifact_root / prepared_resources.name
        _publish_file(prepared_catalog, catalog_path)
        _publish_file(prepared_package_info, package_info_path)
        _publish_file(prepared_resources, resources_path)
        # 全部投影成功后才提交新的 ``target_wheel``。
        _publish_file(candidate_wheel, target_wheel)

    return PackageBuildArtifact(
        wheel=target_wheel,
        artifact_digest=candidate_digest,
        catalog=catalog,
        catalog_path=catalog_path,
        package_info_path=package_info_path,
        resources_path=resources_path,
        package_info=package_info,
        resources=tuple(resources),
    )


def audit_package_wheel(
    wheel: str | Path,
    catalog: PackageCatalog,
    *,
    expected_digest: str,
    compile_catalog: CatalogCompiler,
) -> None:
    """验证 wheel 摘要、标准记录、闭包及实际源码目录 parity。

    参数：``wheel`` 是待审计标准 wheel；``catalog`` 是暂存源码的规范目录；
    ``expected_digest`` 是构建后固定的产物摘要；``compile_catalog`` 是同一目录编译
    Interface。
    返回：无；只有所有安全与内容不变量成立时正常返回。
    异常：摘要、ZIP、RECORD、顶层载荷、闭包、内嵌目录或重编译结果无效时抛出
    ``PackageBuildError``。
    """

    if not callable(compile_catalog):
        raise TypeError("compile_catalog 必须可调用")
    if not isinstance(catalog, PackageCatalog):
        raise TypeError("catalog 必须是 PackageCatalog")
    # ``selected_wheel`` 保留调用者原始路径身份，必须在 resolve 前执行 lstat。
    selected_wheel = Path(wheel).expanduser()
    try:
        selected_metadata = selected_wheel.lstat()
    except OSError as error:
        raise PackageBuildError(f"wheel 不存在或不可访问：{selected_wheel}") from error
    if stat.S_ISLNK(selected_metadata.st_mode) or selected_wheel.is_symlink():
        raise PackageBuildError(f"wheel 路径不得是符号链接：{selected_wheel}")
    # ``wheel_path`` 是解析后的普通文件位置，后续仍须验证大小和摘要。
    wheel_path = selected_wheel.resolve()
    _verify_artifact(wheel_path, expected_digest)
    try:
        with zipfile.ZipFile(wheel_path) as archive:
            # ``members`` 是经过压缩包安全校验的唯一普通成员索引。
            members = _validated_wheel_members(archive)
            _verify_wheel_record(members)
            _verify_payload_and_closure(members, catalog)
            embedded_catalog_name = (
                f"{catalog.import_package}/_generated/package.catalog.json"
            )
            if members[embedded_catalog_name] != catalog.to_canonical_bytes():
                raise PackageBuildError("wheel 内嵌包目录与暂存源码目录不一致")
            # ``gettempdir`` 在 macOS 可能返回经过系统符号链接的 ``/var``；审计
            # 来源必须使用其规范父路径，同时仍让 WorkspaceSource 拒绝调用者链接。
            audit_temporary_parent = Path(tempfile.gettempdir()).resolve()
            with tempfile.TemporaryDirectory(
                prefix="unilab-package-audit-",
                dir=audit_temporary_parent,
            ) as temporary:
                # ``audit_root`` 是只从 wheel 成员重建、无作者工作区回退的审计来源。
                audit_root = Path(temporary) / "workspace"
                _reconstruct_workspace_from_wheel(members, catalog, audit_root)
                # ``audited_catalog`` 必须由同一编译器重新解释实际 wheel 源码。
                audited_catalog = compile_catalog(WorkspaceSource(audit_root))
    except zipfile.BadZipFile as error:
        raise PackageBuildError("wheel 不是合法 ZIP 归档") from error
    if audited_catalog.to_canonical_bytes() != catalog.to_canonical_bytes():
        raise PackageBuildError("wheel 来源重编译目录与暂存源码目录不一致")


def _copy_workspace(source: Path, target: Path) -> None:
    """复制作者工作区到隔离暂存树并排除本地运行产物。

    参数：``source`` 是已验证源码根；``target`` 是尚不存在的临时目录。
    返回：无；保留符号链接身份供后续安全编译关闭式拒绝。
    异常：读取或复制失败时传播文件系统异常。
    """

    def ignore(_directory: str, names: list[str]) -> set[str]:
        """返回当前目录中禁止进入暂存树的成员名。

        参数：``_directory`` 是复制库提供但本规则不需要的目录；``names`` 是候选名。
        返回：与固定排除集合相交的名字。
        异常：无。
        """

        return {name for name in names if name in _STAGING_EXCLUDES}

    shutil.copytree(source, target, ignore=ignore, symlinks=True)


def _write_generated_catalog(
    source: WorkspaceSource,
    catalog: PackageCatalog,
) -> dict[str, bytes]:
    """把规范目录及重编译声明嵌入临时暂存树。

    参数：``source`` 是暂存来源；``catalog`` 是同一来源编译的规范目录。
    返回：必须在最终 wheel 中保持精确字节的逻辑成员映射。
    异常：暂存目录不可写或根声明不可读时传播原始异常。
    """

    # ``generated_root`` 是包内唯一的构建生成事实目录，不写回作者源码。
    generated_root = source.root / catalog.import_package / "_generated"
    generated_root.mkdir(parents=True, exist_ok=True)
    # ``generated_members`` 同时用于暂存写入和 wheel RECORD 安全注入。
    project_bytes = source.read_bytes("pyproject.toml")
    generated_members = {
        f"{catalog.import_package}/_generated/package.catalog.json": (
            catalog.to_canonical_bytes()
        ),
        f"{catalog.import_package}/_generated/pyproject.toml": project_bytes,
    }
    if source.has_file("package.yaml"):
        generated_members[f"{catalog.import_package}/_generated/package.yaml"] = (
            source.read_bytes("package.yaml")
        )
    # ``project`` 给出 clean-wheel 重编译仍需存在的显式工作区启动输入。
    project = parse_project_metadata(project_bytes)
    for startup_file in (project.startup_graph, project.startup_config):
        if startup_file is None:
            continue
        # ``logical_startup_file`` 把绝对或相对声明统一约束回暂存来源边界。
        selected_startup_file = Path(startup_file).expanduser()
        if selected_startup_file.is_absolute():
            try:
                logical_startup_file = selected_startup_file.relative_to(
                    source.root
                ).as_posix()
            except ValueError as error:
                raise PackageBuildError("工作区启动文件必须位于软件包根内") from error
        else:
            logical_startup_file = PurePosixPath(startup_file).as_posix()
        generated_members[
            f"{catalog.import_package}/_generated/workspace/{logical_startup_file}"
        ] = source.read_bytes(logical_startup_file)
    for logical_path, payload in generated_members.items():
        # ``generated_file`` 始终落在已验证暂存包的 ``_generated`` 内。
        generated_file = source.root.joinpath(*PurePosixPath(logical_path).parts)
        generated_file.parent.mkdir(parents=True, exist_ok=True)
        generated_file.write_bytes(payload)
    return generated_members


def _build_standard_wheel(staging_root: Path, wheel_output: Path) -> Path:
    """通过当前解释器的标准 pip wheel 前端构建一个无依赖 wheel。

    参数：``staging_root`` 是已经嵌入目录的暂存源码；``wheel_output`` 是临时输出。
    返回：构建产生的唯一 wheel 路径。
    异常：构建工具失败或产生零个/多个 wheel 时抛出 ``PackageBuildError``。
    """

    # ``command`` 使用项目声明的标准构建后端，但不解析或下载运行依赖。
    command = [
        sys.executable,
        "-m",
        "pip",
        "wheel",
        "--no-deps",
        "--no-build-isolation",
        "--wheel-dir",
        str(wheel_output),
        str(staging_root),
    ]
    # ``result`` 收集完整诊断，避免子进程输出混入结构化 CLI 结果。
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise PackageBuildError(f"标准 wheel 构建失败：{detail}")
    # ``wheels`` 必须只有当前工作区的一个构建结果。
    wheels = tuple(sorted(wheel_output.glob("*.whl")))
    if len(wheels) != 1:
        raise PackageBuildError(
            f"标准构建必须产生一个 wheel，实际产生 {len(wheels)} 个"
        )
    return wheels[0]


def _inject_wheel_members(wheel: Path, injected: Mapping[str, bytes]) -> None:
    """确保生成事实进入 wheel，并重建完整标准 RECORD。

    参数：``wheel`` 是标准构建候选；``injected`` 是必须精确嵌入的逻辑成员。
    返回：无；原子替换同一路径 wheel。
    异常：ZIP 或 RECORD 结构无效时抛出 ``PackageBuildError``。
    """

    try:
        with zipfile.ZipFile(wheel) as archive:
            # ``original_infos`` 保留非签名成员元数据，生成成员与 RECORD 将被替换。
            original_infos = {
                item.filename: item
                for item in archive.infolist()
                if not item.is_dir()
                and not item.filename.endswith(("/RECORD.jws", "/RECORD.p7s"))
            }
            # ``members`` 是重写后 wheel 的完整普通文件内容。
            members = {
                name: archive.read(item) for name, item in original_infos.items()
            }
    except zipfile.BadZipFile as error:
        raise PackageBuildError("标准构建产物不是合法 wheel ZIP") from error
    members.update(injected)
    # ``record_names`` 必须唯一，避免更新错误发行元数据。
    record_names = [name for name in members if name.endswith(".dist-info/RECORD")]
    if len(record_names) != 1:
        raise PackageBuildError(f"wheel RECORD 数量不是 1：{len(record_names)}")
    record_name = record_names[0]
    members[record_name] = _wheel_record(members, record_name)
    replacement = wheel.with_suffix(".rewrite.whl")
    with zipfile.ZipFile(replacement, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in sorted(members.items()):
            # ``original_info`` 尽可能保留标准后端生成的权限和时间元数据。
            original_info = original_infos.get(name)
            if original_info is None or name in injected or name == record_name:
                archive.writestr(name, payload)
            else:
                archive.writestr(original_info, payload)
    replacement.replace(wheel)


def _wheel_record(members: Mapping[str, bytes], record_name: str) -> bytes:
    """生成与全部 wheel 普通成员一致的标准 RECORD 字节。

    参数：``members`` 是成员内容映射；``record_name`` 是唯一 RECORD 身份。
    返回：CSV 编码记录，其中 RECORD 自身摘要与大小留空。
    异常：成员名不能由 CSV 编码时传播标准库异常。
    """

    # ``stream`` 与 ``writer`` 共同生成固定换行的标准记录内容。
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    for name, payload in sorted(members.items()):
        if name == record_name:
            writer.writerow((name, "", ""))
            continue
        digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).decode()
        writer.writerow((name, f"sha256={digest.rstrip('=')}", len(payload)))
    return stream.getvalue().encode("utf-8")


def _verify_artifact(wheel: Path, expected_digest: str) -> None:
    """验证 wheel 是大小受限的普通文件且摘要匹配。

    参数：``wheel`` 是候选路径；``expected_digest`` 是带前缀 SHA-256。
    返回：无。
    异常：路径、大小或摘要不符时抛出 ``PackageBuildError``。
    """

    if wheel.is_symlink() or not wheel.is_file():
        raise PackageBuildError(f"wheel 不存在或不是普通文件：{wheel}")
    if wheel.stat().st_size > _MAX_ARCHIVE_BYTES:
        raise PackageBuildError("wheel 超过归档大小上限")
    # ``actual_digest`` 是对候选最终字节重新计算的事实摘要。
    actual_digest = _artifact_digest(wheel)
    if not expected_digest or actual_digest != expected_digest:
        raise PackageBuildError(
            f"wheel 摘要不匹配：{actual_digest} != {expected_digest or '-'}"
        )


def _validated_wheel_members(archive: zipfile.ZipFile) -> dict[str, bytes]:
    """关闭式验证 wheel 成员安全并读取普通文件内容。

    参数：``archive`` 是已打开的候选 wheel。
    返回：按逻辑成员名索引的普通文件字节。
    异常：重复、加密、符号链接、路径逃逸或压缩资源超限时抛出
    ``PackageBuildError``。
    """

    # ``infos`` 与 ``names`` 用于在读取内容前完成数量和重复身份校验。
    infos = tuple(archive.infolist())
    if len(infos) > _MAX_ARCHIVE_MEMBERS:
        raise PackageBuildError("wheel 成员数量超过上限")
    names: set[str] = set()
    total_size = 0
    members: dict[str, bytes] = {}
    for item in infos:
        _safe_member_name(item.filename.rstrip("/"))
        if item.filename in names:
            raise PackageBuildError(f"wheel 包含重复成员：{item.filename}")
        names.add(item.filename)
        mode = item.external_attr >> 16
        if item.flag_bits & 0x1:
            raise PackageBuildError(f"wheel 包含加密成员：{item.filename}")
        if stat.S_ISLNK(mode):
            raise PackageBuildError(f"wheel 包含符号链接成员：{item.filename}")
        if item.file_size > _MAX_MEMBER_BYTES:
            raise PackageBuildError(f"wheel 成员超过大小上限：{item.filename}")
        total_size += item.file_size
        if total_size > _MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise PackageBuildError("wheel 解压后总大小超过上限")
        if (item.file_size > 0 and item.compress_size == 0) or (
            item.compress_size > 0
            and item.file_size / item.compress_size > _MAX_COMPRESSION_RATIO
        ):
            raise PackageBuildError(f"wheel 成员压缩比超过上限：{item.filename}")
        if not item.is_dir():
            members[item.filename] = archive.read(item)
    return members


def _safe_member_name(member_name: str) -> PurePosixPath:
    """验证 wheel 成员名是规范 POSIX 相对路径。

    参数：``member_name`` 是 ZIP 中声明的逻辑路径。
    返回：完成检查的 ``PurePosixPath``。
    异常：空、绝对、反斜杠或父目录语义抛出 ``PackageBuildError``。
    """

    if not member_name or "\\" in member_name:
        raise PackageBuildError("wheel 成员路径必须是非空 POSIX 相对路径")
    # ``logical_path`` 只表达归档内部身份，不允许文件系统规范化掩盖逃逸。
    logical_path = PurePosixPath(member_name)
    if logical_path.is_absolute() or any(
        part in {"", ".", ".."} for part in logical_path.parts
    ):
        raise PackageBuildError(f"wheel 成员路径非法：{member_name}")
    return logical_path


def _verify_wheel_record(members: Mapping[str, bytes]) -> None:
    """验证 wheel RECORD 覆盖全部成员并匹配摘要与大小。

    参数：``members`` 是安全读取后的完整普通成员。
    返回：无。
    异常：RECORD 缺失、重复、字段或哈希不匹配时抛出 ``PackageBuildError``。
    """

    # ``record_names`` 必须唯一，且签名文件不参与当前无签名 wheel 合同。
    record_names = [name for name in members if name.endswith(".dist-info/RECORD")]
    if len(record_names) != 1:
        raise PackageBuildError(f"wheel RECORD 数量不是 1：{len(record_names)}")
    record_name = record_names[0]
    try:
        # ``rows`` 是 RECORD 中按成员身份索引的三字段记录。
        rows = {
            row[0]: row
            for row in csv.reader(
                io.StringIO(members[record_name].decode("utf-8"), newline="")
            )
            if row
        }
    except (UnicodeError, csv.Error, IndexError) as error:
        raise PackageBuildError("wheel RECORD 不是合法 UTF-8 CSV") from error
    if set(rows) != set(members):
        raise PackageBuildError("wheel RECORD 未完整覆盖普通成员")
    for name, payload in members.items():
        # ``row`` 是当前成员在 RECORD 中的唯一完整性声明。
        row = rows[name]
        if len(row) != 3:
            raise PackageBuildError(f"wheel RECORD 字段数量无效：{name}")
        if name == record_name:
            if row[1:] != ["", ""]:
                raise PackageBuildError("wheel RECORD 自身摘要或大小必须为空")
            continue
        expected_hash = (
            base64.urlsafe_b64encode(hashlib.sha256(payload).digest())
            .decode("ascii")
            .rstrip("=")
        )
        if row[1] != f"sha256={expected_hash}" or row[2] != str(len(payload)):
            raise PackageBuildError(f"wheel RECORD 摘要或大小不匹配：{name}")


def _verify_payload_and_closure(
    members: Mapping[str, bytes],
    catalog: PackageCatalog,
) -> None:
    """验证 wheel 只有规范导入根且完整携带目录源码与资产闭包。

    参数：``members`` 是安全 wheel 内容；``catalog`` 是暂存源码目录。
    返回：无。
    异常：额外顶层载荷或任一必需成员缺失时抛出 ``PackageBuildError``。
    """

    # ``payload_roots`` 排除标准发行元数据后，只允许唯一规范 import package。
    payload_roots = {
        PurePosixPath(name).parts[0]
        for name in members
        if not PurePosixPath(name).parts[0].endswith((".dist-info", ".data"))
    }
    if payload_roots != {catalog.import_package}:
        raise PackageBuildError(
            "wheel 必须只有规范顶层导入包；实际为：" + ", ".join(sorted(payload_roots))
        )
    # ``required_members`` 是目录定义、静态资产及重编译证据的完整闭包。
    required_members = {
        *(item.declaring_file for item in catalog.definitions.devices),
        *(item.declaring_file for item in catalog.definitions.resources),
        *(item.declaring_file for item in catalog.definitions.workflows),
        *(item.logical_path for item in catalog.assets),
        f"{catalog.import_package}/_generated/package.catalog.json",
        f"{catalog.import_package}/_generated/pyproject.toml",
    }
    if catalog.definitions.workflows:
        required_members.add(f"{catalog.import_package}/_generated/package.yaml")
    # ``missing_members`` 稳定报告标准构建没有实际携带的目录闭包。
    missing_members = sorted(required_members - set(members))
    if missing_members:
        raise PackageBuildError("wheel 缺失包目录闭包：" + ", ".join(missing_members))


def _reconstruct_workspace_from_wheel(
    members: Mapping[str, bytes],
    catalog: PackageCatalog,
    audit_root: Path,
) -> None:
    """仅从 wheel 普通成员重建可交给统一编译器的安全工作区。

    参数：``members`` 是已经验证的 wheel 内容；``catalog`` 给出唯一 import package；
    ``audit_root`` 是隔离临时目标。
    返回：无；写出包源码、根 pyproject 和可选 workflow 清单。
    异常：临时目录不可写时传播文件系统异常。
    """

    audit_root.mkdir(parents=True)
    package_prefix = f"{catalog.import_package}/"
    generated_prefix = f"{catalog.import_package}/_generated"
    for name, payload in members.items():
        if not name.startswith(package_prefix) or name.startswith(
            f"{generated_prefix}/"
        ):
            continue
        # ``target`` 由已验证 POSIX 成员路径映射到隔离审计根。
        target = audit_root.joinpath(*PurePosixPath(name).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    # ``generated_prefix`` 保存根声明在 wheel 内的规范生成位置。
    audit_root.joinpath("pyproject.toml").write_bytes(
        members[f"{generated_prefix}/pyproject.toml"]
    )
    package_manifest = members.get(f"{generated_prefix}/package.yaml")
    if package_manifest is not None:
        audit_root.joinpath("package.yaml").write_bytes(package_manifest)
    workspace_evidence_prefix = f"{generated_prefix}/workspace/"
    for name, payload in members.items():
        if not name.startswith(workspace_evidence_prefix):
            continue
        # ``workspace_relative`` 已通过 wheel 成员检查，只恢复到隔离审计根。
        workspace_relative = name.removeprefix(workspace_evidence_prefix)
        target = audit_root.joinpath(*PurePosixPath(workspace_relative).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)


def _publication_projections(
    catalog: PackageCatalog,
    *,
    staging_project_bytes: bytes,
    artifact_digest: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """从已审计目录和 wheel 摘要生成现有云端广场兼容投影。

    参数：``catalog`` 是已证明的规范目录；``staging_project_bytes`` 是同次项目声明；
    ``artifact_digest`` 是最终 wheel 摘要。
    返回：软件包信息和资源模板 DTO 列表。
    异常：项目或注册条目不能形成既有接口 DTO 时传播原始校验异常。
    """

    # ``project`` 是统一项目元数据到遗留发布字段的唯一投影。
    project = project_to_legacy_dict(parse_project_metadata(staging_project_bytes))
    # ``package_info`` 把云端安装身份绑定到已审计 wheel，而不是源码 tar。
    package_info = build_package_info(
        project,
        catalog.namespace,
        artifact_digest,
    )
    package_info["artifact_digest"] = artifact_digest
    package_info["catalog_digest"] = catalog.catalog_digest
    package_info["content_digest"] = catalog.content_digest
    # ``catalog_document`` 解冻注册条目，避免兼容投影误把只读映射当成缺失字典。
    catalog_document = catalog.to_dict()
    registry_entries = {
        item["id"]: item["details"]["registry_entry"]
        for definition_kind in ("devices", "resources")
        for item in catalog_document["definitions"][definition_kind]
    }
    resources = build_resources_from_registry(registry_entries, package_info)
    return package_info, resources


def _artifact_digest(path: Path) -> str:
    """分块计算 wheel 的带前缀 SHA-256 摘要。

    参数：``path`` 是已完成写入的候选或发布 wheel。
    返回：``sha256:<hex>`` 格式摘要。
    异常：文件不可读时传播原始 IO 异常。
    """

    # ``digest`` 累积完整 wheel 字节，不把文件整体载入内存。
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            # ``chunk`` 是下一段固定上限原始 wheel 字节。
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _publish_file(source: Path, target: Path) -> None:
    """在目标目录内先复制临时文件，再原子替换正式产物。

    参数：``source`` 是已审计候选；``target`` 是最终路径。
    返回：无。
    异常：复制、同步或替换失败时传播原始文件系统异常。
    """

    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
        delete=False,
    ) as temporary_file:
        # ``temporary_path`` 与目标同文件系统，保证最后替换操作原子。
        temporary_path = Path(temporary_file.name)
        with source.open("rb") as source_file:
            shutil.copyfileobj(source_file, temporary_file)
    temporary_path.replace(target)


def _write_output_file(target: Path, payload: bytes) -> None:
    """在目标目录原子写入一个构建投影文件。

    参数：``target`` 是最终文件；``payload`` 是完整 UTF-8 JSON 或规范目录字节。
    返回：无。
    异常：目标目录不可写时传播原始文件系统异常。
    """

    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
        delete=False,
    ) as temporary_file:
        temporary_file.write(payload)
        # ``temporary_path`` 与目标同文件系统，保证成功结果不出现半写文件。
        temporary_path = Path(temporary_file.name)
    temporary_path.replace(target)


__all__ = [
    "PackageBuildArtifact",
    "PackageBuildError",
    "audit_package_wheel",
    "build_workspace_package",
]
