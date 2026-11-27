"""NVIDIA SimReady 工程包（资产库发布单元）的字节级完整性校验。

算法与资产库工具链（lab-station-asset-pipeline ``toolkit/asset-library/native_package.py``）
逐项一致：``package_digest`` 是包根 ``com.nvidia.simready.packaging.json`` 的 SHA-256；
BOM 聚合 ``content_hash``、清单 ``package_hash``、每个文件大小与摘要都要相符。
这是完整性校验，不是 NVIDIA 合规认证，也不做物理验收。只用 Python 标准库。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

MANIFEST = "com.nvidia.simready.packaging.json"
BOM = ".metadata/com.nvidia.simready.packaging.bom.json"
ROOTS = ".metadata/com.nvidia.simready.root_usds.json"
ADAPTER = "simready_usd/unilab.json"
READINESS = "simready_usd/readiness.json"


class AssetIntegrityError(ValueError):
    """工程包内容与其封存摘要不一致。"""


def read_json(path: Path) -> Any:
    """按 UTF-8 读取 JSON 文件。"""

    return json.loads(Path(path).read_text(encoding="utf-8"))


def sha256_bytes(data: bytes) -> str:
    """返回字节串的十六进制 SHA-256。"""

    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    """返回文件内容的十六进制 SHA-256。"""

    return sha256_bytes(Path(path).read_bytes())


def canonical(value: Any) -> bytes:
    """资产库通用的规范 JSON 字节（键排序、无多余空白、UTF-8）。"""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def inside(root: Path, relative: str) -> Path:
    """把包内相对路径解析到 ``root`` 下；任何越界、反斜杠或空段都拒绝。"""

    root = Path(root).resolve()
    if (
        not isinstance(relative, str)
        or "\\" in relative
        or any(part in (".", "..", "") for part in relative.split("/"))
        or Path(relative).is_absolute()
    ):
        raise AssetIntegrityError(f"不安全的包内路径：{relative!r}")
    target = (root / relative).resolve()
    if target == root or not target.is_relative_to(root):
        raise AssetIntegrityError(f"包内路径越界：{relative!r}")
    return target


def content_hash(items: list[dict[str, Any]]) -> str:
    """BOM 条目的聚合摘要（PKG.HASH.001）。"""

    ordered = sorted(items, key=lambda item: item["relative_path"].encode("utf-8"))
    return sha256_bytes(
        b"".join(
            item["relative_path"].encode("utf-8") + b"\0" + bytes.fromhex(item["hash"]["sha256"])
            for item in ordered
        )
    )


def package_hash(manifest: dict[str, Any]) -> str:
    """清单级聚合摘要（PKG.HASH.001）。"""

    metadata = sorted(manifest["metadata"], key=lambda item: item["name"].encode())
    return sha256_bytes(
        manifest["package_id"].encode()
        + b"\0"
        + manifest["license"].encode()
        + b"\0"
        + bytes.fromhex(manifest["content_hash"]["sha256"])
        + b"".join(item["name"].encode() + b"\0" + bytes.fromhex(item["hash"]["sha256"]) for item in metadata)
    )


def check_manifest(manifest: dict[str, Any], bom: dict[str, Any]) -> None:
    """只凭清单与 BOM 检查聚合摘要（下载完整包之前即可执行）。"""

    if manifest.get("format_version") != "1.0" or not manifest.get("package_id") or not manifest.get("license"):
        raise AssetIntegrityError("工程包清单格式无效")
    if content_hash(bom["items"]) != manifest["content_hash"]["sha256"]:
        raise AssetIntegrityError("BOM 聚合摘要与清单不符")
    if package_hash(manifest) != manifest["package_hash"]["sha256"]:
        raise AssetIntegrityError("清单聚合摘要不符")
    paths = [item["relative_path"] for item in bom["items"]]
    if len(paths) != len({path.casefold() for path in paths}):
        raise AssetIntegrityError("BOM 含重复或仅大小写不同的路径")
    if any(not path.startswith("simready_usd/") for path in paths):
        raise AssetIntegrityError("BOM 交付路径必须位于 simready_usd/ 下")


def file_rows(root: Path) -> list[dict[str, Any]]:
    """列出包内全部受封存文件（BOM 条目 + 元数据文件）。"""

    root = Path(root)
    manifest = read_json(root / MANIFEST)
    bom = read_json(root / BOM)
    rows = [
        {"path": item["relative_path"], "sha256": item["hash"]["sha256"], "bytes": item["size"]}
        for item in bom["items"]
    ]
    rows += [
        {
            "path": ".metadata/" + item["name"],
            "sha256": item["hash"]["sha256"],
            "bytes": (root / ".metadata" / item["name"]).stat().st_size,
        }
        for item in manifest["metadata"]
    ]
    return rows


def verify_package(root: Path, expected_digest: str | None = None) -> dict[str, Any]:
    """完整校验一个已落盘的工程包。

    参数：``root`` 是包目录（目录名须与 USD 入口同名，NP.005）；``expected_digest``
    是锁里固定的 ``package_digest``。
    返回：``{"status": "passed", "package_digest", "files", "asset_id"}``。
    异常：任何身份、摘要、文件集合或入口结构不符都抛 ``AssetIntegrityError``。
    """

    root = Path(root).resolve()
    manifest = read_json(root / MANIFEST)
    digest = sha256_file(root / MANIFEST)
    if expected_digest and digest != expected_digest:
        raise AssetIntegrityError("工程包清单身份与锁不符")
    for entry in manifest["metadata"]:
        if sha256_file(inside(root, ".metadata/" + entry["name"])) != entry["hash"]["sha256"]:
            raise AssetIntegrityError("元数据摘要不符：" + entry["name"])
    bom = read_json(root / BOM)
    check_manifest(manifest, bom)
    rows = file_rows(root)
    paths = [row["path"] for row in rows]
    if len(paths) != len(set(paths)) or MANIFEST in paths:
        raise AssetIntegrityError("包内文件重复或含歧义路径")
    present = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    if present != set(paths) | {MANIFEST}:
        raise AssetIntegrityError("包内文件缺失或多出")
    for row in rows:
        target = inside(root, row["path"])
        if target.stat().st_size != row["bytes"] or sha256_file(target) != row["sha256"]:
            raise AssetIntegrityError("包内文件被改动：" + row["path"])
    adapter = read_json(root / ADAPTER)
    if adapter["asset_id"] != manifest["package_id"]:
        raise AssetIntegrityError("工程包身份与 unilab.json 不一致")
    usd = adapter["entrypoints"]["usd"]
    entry = inside(root, usd)
    if entry.parent != root / "simready_usd" or root.name.lower() not in entry.stem.lower():
        raise AssetIntegrityError("USD 入口结构不符（NP.005：包目录名须与入口同名）")
    usd_files = [path for path in entry.parent.iterdir() if path.suffix.lower() in (".usd", ".usdc", ".usda", ".usdz")]
    if len(usd_files) != 1:
        raise AssetIntegrityError("simready_usd/ 下必须只有一个根 USD")
    if read_json(root / ROOTS)["entries"] != [usd]:
        raise AssetIntegrityError("根 USD 声明不一致")
    for path in adapter["entrypoints"].values():
        if path not in paths:
            raise AssetIntegrityError("入口未在 BOM 中声明：" + path)
    for definition in adapter.get("definitions_data", {}).values():
        for key in ("source", "native"):
            if key in definition and sha256_file(inside(root, definition[key]["path"])) != definition[key]["sha256"]:
                raise AssetIntegrityError("CAD 映射摘要不符")
    readiness = read_json(root / READINESS)
    if readiness["binding"]["source_usd_sha256"] != sha256_file(entry):
        raise AssetIntegrityError("readiness 根 USD 摘要不符")
    closure = adapter["usd_dependencies"]
    for row in closure:
        if sha256_file(inside(root, row["path"])) != row["sha256"]:
            raise AssetIntegrityError("readiness 依赖文件被改动：" + row["path"])
    if readiness["binding"]["usd_closure_sha256"] != sha256_bytes(canonical(closure)):
        raise AssetIntegrityError("readiness 依赖闭包摘要不符")
    return {"status": "passed", "package_digest": digest, "files": len(paths) + 1, "asset_id": adapter["asset_id"]}


__all__ = [
    "ADAPTER",
    "BOM",
    "MANIFEST",
    "AssetIntegrityError",
    "check_manifest",
    "content_hash",
    "inside",
    "package_hash",
    "read_json",
    "sha256_bytes",
    "sha256_file",
    "verify_package",
]
