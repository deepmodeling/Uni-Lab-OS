"""工作区模型的不可变 HTTP 发布代。清单、路径授权和字节在同一事务中切换。"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import sqlite3
import struct
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlsplit

from .model import PackageAsset
from .sources import WorkspaceSource

MODEL_TYPES = {".usd", ".usda", ".usdc", ".usdz", ".urdf", ".xacro", ".stl", ".dae", ".gltf", ".glb", ".fbx", ".obj"}
FILE_TYPES = MODEL_TYPES | {
    ".bin",
    ".mtl",
    ".png",
    ".jpg",
    ".jpeg",
    ".avif",
    ".webp",
    ".ktx2",
    ".dds",
    ".bmp",
    ".tga",
    ".gif",
    ".hdr",
    ".exr",
    ".yaml",
    ".yml",
    ".json",
}
HEX = re.compile(r"[0-9a-f]{64}")


def _json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _references(path: str, data: bytes) -> list[str] | None:
    """返回实际引用；无法静态枚举时只发布文件指纹，不承诺完整模型版本。"""
    ext = PurePosixPath(path).suffix.lower()
    if ext in {".usd", ".usda", ".usdc"}:
        try:
            from pxr import UsdUtils
        except ImportError:
            return None
        # 解析已冻结字节，避开 OpenUSD 按原文件名复用旧层缓存。
        with tempfile.TemporaryDirectory() as directory:
            layer = Path(directory) / ("model" + ext)
            layer.write_bytes(data)
            return [ref for group in UsdUtils.ExtractExternalReferences(str(layer)) for ref in group]
    if ext in {".gltf", ".glb"}:
        if ext == ".glb":
            if data[:4] != b"glTF" or len(data) < 20 or data[16:20] != b"JSON":
                return None
            data = data[20 : 20 + struct.unpack_from("<I", data, 12)[0]]
        document = json.loads(data)

        # 扩展可引入额外 URI；递归覆盖所有 uri 字段。
        def uris(value: object) -> Iterable[str]:
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == "uri" and isinstance(item, str):
                        yield item
                    else:
                        yield from uris(item)
            elif isinstance(value, list):
                for item in value:
                    yield from uris(item)

        return list(uris(document))
    if ext in {".urdf", ".xacro"}:
        if b"${" in data or b"$(" in data:
            return None
        return [
            value
            for node in ET.fromstring(data).iter()
            for key, value in node.attrib.items()
            if key in {"filename", "url"}
        ]
    if ext in {".dae", ".fbx"}:
        return None
    if ext == ".obj" and re.search(rb"(?m)^\s*mtllib\s", data):
        return None
    return []


def _model_version(entry: str, digests: dict[str, str], references: dict[str, list[str] | None]) -> str | None:
    pending = [entry]
    closure = {}
    while pending:
        path = pending.pop()
        if path in closure:
            continue
        refs = references.get(path, [])
        if path not in digests or refs is None:
            return None
        closure[path] = digests[path]
        for ref in refs:
            if ref.startswith("data:"):
                continue
            if not ref or any(c in ref for c in "<>[]$\\?#"):
                return None
            uri = urlsplit(ref)
            if uri.scheme or uri.netloc or ref.startswith("/"):
                return None
            pending.append(posixpath.normpath(posixpath.join(posixpath.dirname(path), unquote(uri.path))))
    return "sha256:" + _hash(_json([entry, closure]))


@dataclass(frozen=True)
class MaterialModelManifest:
    database: Path
    base: str
    revision: str
    files: Mapping[str, str]
    versions: Mapping[str, str]

    @classmethod
    def publish(
        cls,
        source: WorkspaceSource,
        distribution: str,
        package: str,
        roots: set[PurePosixPath],
        assets: Iterable[PackageAsset],
    ) -> MaterialModelManifest:
        escaped = quote(distribution, safe="")
        base = f"/api/v1/material-models/{escaped}/"
        directory = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "unilabos" / "model-assets"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        database = directory / (_hash(_json([str(source.root), distribution])) + ".sqlite3")
        digests = {}
        refs = {}
        shared = PurePosixPath(package, "_shared_textures")
        with closing(sqlite3.connect(database, timeout=120)) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS blobs (digest TEXT PRIMARY KEY, content BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS asset_generations (digest TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS bindings (release TEXT, path TEXT, digest TEXT,
                    PRIMARY KEY(release, path));
                CREATE INDEX IF NOT EXISTS lookup_asset ON bindings(path, digest);
            """)
            db.execute("BEGIN IMMEDIATE")
            for asset in assets:
                path = PurePosixPath(asset.logical_path)
                digest = asset.digest.removeprefix("sha256:")
                ext = path.suffix.lower()
                if path.is_relative_to(shared):
                    if path.parent != shared or path.name != digest + ".png":
                        continue
                elif ext not in FILE_TYPES or not any(root in path.parents for root in roots):
                    continue
                name = path.as_posix()
                digests[name] = digest
                exists = db.execute("SELECT 1 FROM blobs WHERE digest=?", (digest,)).fetchone()
                if not exists or ext in MODEL_TYPES:
                    data = source.read_bytes(name)
                    if _hash(data) != digest:
                        raise ValueError(f"发布期间模型资产发生变化: {name}")
                    if not exists:
                        db.execute("INSERT INTO blobs VALUES (?,?)", (digest, data))
                    if ext in MODEL_TYPES:
                        try:
                            refs[name] = _references(name, data)
                        except (RuntimeError, ValueError, ET.ParseError):
                            refs[name] = None

            versions = {}
            for entry in refs:
                version = _model_version(entry, digests, refs)
                if version is not None:
                    versions[entry] = version
            revision = _hash(_json({"basePath": base, "files": digests, "versions": versions}))
            db.execute("INSERT OR REPLACE INTO asset_generations VALUES (?)", (revision,))
            db.executemany(
                "INSERT OR IGNORE INTO bindings VALUES (?,?,?)",
                ((revision, name, digest) for name, digest in digests.items()),
            )
            db.execute(
                "DELETE FROM asset_generations WHERE rowid NOT IN (SELECT rowid FROM asset_generations ORDER BY rowid DESC LIMIT 2)"
            )
            db.execute("DELETE FROM bindings WHERE release NOT IN (SELECT digest FROM asset_generations)")
            db.execute("DELETE FROM blobs WHERE digest NOT IN (SELECT digest FROM bindings)")
        return cls(database, base, revision, digests, versions)

    def describe(self, model: dict) -> dict:
        result = dict(model)
        path = model.get("path") or model.get("mesh")
        if isinstance(path, str) and path.startswith(self.base):
            name = unquote(path[len(self.base) :])
            if name in self.files:
                result["assetRevision"] = self.revision
                if name in self.versions:
                    result["version"] = self.versions[name]
                else:
                    result.pop("version", None)
        if isinstance(model.get("instances"), dict):
            result["instances"] = self.describe(model["instances"])
        return result

    def describe_template(self, detail: dict, declaration: Mapping) -> dict:
        model = detail.get("model") or {}
        if not isinstance(model, dict) or not (model or declaration):
            return detail
        projected = {**declaration, **model}
        # 手写模板可能只有声明文件的 package:// 地址和相对 entry。
        entry = model.get("entry")
        metadata = detail.get("meta_data")
        unilab = metadata.get("unilab") if isinstance(metadata, dict) else None
        source = unilab.get("source_uri") if isinstance(unilab, dict) else None
        if (
            not model.get("path")
            and isinstance(entry, str)
            and isinstance(source, str)
            and source.startswith("package://")
        ):
            parts = source.removeprefix("package://").split("/")
            segments = entry.split("/")
            if len(parts) < 2 or any(part in {"", ".", ".."} or "\\" in part for part in parts + segments):
                return detail
            name = "/".join(parts[:-1] + segments)
            if name not in self.files:
                return detail
            path = self.base + quote(name)
            projected["path"] = path
            projected["meshDir"] = model.get("meshDir") or path.rsplit("/", 1)[0]
        return {**detail, "model": self.describe(projected)}

    def read(self, digest: str, path: str) -> bytes:
        if not HEX.fullmatch(digest):
            raise KeyError(digest)
        with closing(sqlite3.connect(self.database, timeout=120)) as db:
            row = db.execute(
                "SELECT content FROM blobs WHERE digest=? AND EXISTS "
                "(SELECT 1 FROM bindings WHERE path=? AND digest=?)",
                (digest, path, digest),
            ).fetchone()
        if row is None:
            raise KeyError(digest)
        return row[0]
