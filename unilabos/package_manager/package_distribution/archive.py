"""包分发（Package Distribution）的确定性归档构建。"""

from __future__ import annotations

import hashlib
import tarfile
from pathlib import Path

ARCHIVE_EXCLUDE_DIRS = {
    "__pycache__",
    ".git",
    ".idea",
    ".vscode",
    ".unilabos",
    "dist",
    "build",
    ".pytest_cache",
    "unilabos_data",
    ".venv",
    "venv",
    "node_modules",
}
ARCHIVE_EXCLUDE_SUFFIXES = {".pyc", ".pyo"}
ARCHIVE_EXCLUDE_RELATIVE_SUBTREES = {
    (".claude", "skills"),
    (".codex", "skills"),
}


def build_archive(pkg_dir: Path, archive_path: Path) -> str:
    """生成排除本地产物的软件包发布归档及内容摘要。

    参数：``pkg_dir`` 是待归档软件包根；``archive_path`` 是目标 ``tar.gz`` 路径。
    返回：带 ``sha256:`` 前缀的小写归档内容摘要。
    异常：目录创建、归档读取或写入失败时传播原始异常；缓存、版本控制目录、工作
    数据和字节码不得进入归档。
    """

    archive_path.parent.mkdir(parents=True, exist_ok=True)
    # ``arc_root`` 是归档内唯一的软件包顶层目录名。
    arc_root = pkg_dir.name

    def _filter(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo | None:
        """过滤不应进入发布归档的本地产物。

        参数：``tarinfo`` 是归档库当前候选成员。
        返回：可保留成员原值；缓存、工作目录或字节码返回 ``None``。
        异常：无。
        """

        # ``parts`` 用于判断候选归档成员是否落入任何禁止发布的目录。
        member_parts = Path(tarinfo.name).parts
        parts = set(member_parts)
        if parts & ARCHIVE_EXCLUDE_DIRS:
            return None
        relative_parts = member_parts[1:]
        if any(
            relative_parts[: len(subtree)] == subtree
            for subtree in ARCHIVE_EXCLUDE_RELATIVE_SUBTREES
        ):
            return None
        if Path(tarinfo.name).suffix in ARCHIVE_EXCLUDE_SUFFIXES:
            return None
        return tarinfo

    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(str(pkg_dir), arcname=arc_root, filter=_filter)

    return "sha256:" + sha256_file(archive_path)


def sha256_file(path: Path) -> str:
    """分块计算归档文件的 SHA-256 摘要。

    参数：``path`` 是已完成写入的归档路径。
    返回：不带前缀的小写十六进制摘要。
    异常：文件不可读时传播原始 IO 异常；按固定分块读取，不改变文件位置或内容。
    """

    # ``digest`` 累积完整文件字节，形成归档内容指纹。
    digest = hashlib.sha256()
    with path.open("rb") as archive_file:
        while True:
            # ``chunk`` 是归档中下一段固定上限的原始字节。
            chunk = archive_file.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "ARCHIVE_EXCLUDE_DIRS",
    "ARCHIVE_EXCLUDE_RELATIVE_SUBTREES",
    "ARCHIVE_EXCLUDE_SUFFIXES",
    "build_archive",
    "sha256_file",
]
