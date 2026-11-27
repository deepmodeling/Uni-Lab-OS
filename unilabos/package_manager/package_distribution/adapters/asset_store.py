"""资产库只读存储 Adapter：OSS（readable/1 布局）与本地镜像目录。

OSS 配置与资产库工具链的 ``config/asset-library/oss-readable.json`` 同形：
``endpoint``、``bucket``、``region``、``prefix``（如 ``asset-library/``）。凭据只从环境变量
``OSS_ACCESS_KEY_ID`` / ``OSS_ACCESS_KEY_SECRET`` 读取；OSS 错误原文可能含签名请求，
因此只向上抛出错误码，不回显原文。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..asset_fetch.native_package import inside

DEFAULT_OSS_CONFIG: dict[str, str] = {
    "endpoint": "https://oss-cn-zhangjiakou.aliyuncs.com",
    "bucket": "uni-lab-test",
    "region": "cn-zhangjiakou",
    "prefix": "asset-library/",
}


class AssetStoreError(RuntimeError):
    """存储读取失败（已去掉可能含签名的原文）。"""


class LocalMirrorStore:
    """以本地资产库镜像目录（与 OSS 前缀下相对路径一致）作为存储。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        if not (self.root / "library.json").is_file():
            raise AssetStoreError(f"本地镜像缺少 library.json：{self.root}")

    def get(self, key: str) -> bytes:
        return inside(self.root, key).read_bytes()


class OssAssetStore:
    """阿里云 OSS 私有桶上的资产库（只读）。"""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        try:
            import oss2
            from oss2.credentials import EnvironmentVariableCredentialsProvider
        except ImportError as error:  # 只在真正需要 OSS 时才要求 SDK
            raise AssetStoreError("需要 oss2：pip install 'oss2>=2.19'") from error
        if not os.environ.get("OSS_ACCESS_KEY_ID") or not os.environ.get("OSS_ACCESS_KEY_SECRET"):
            raise AssetStoreError("未设置 OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET 环境变量")
        merged = {**DEFAULT_OSS_CONFIG, **(config or {})}
        self.prefix = merged["prefix"]
        if not self.prefix or not self.prefix.endswith("/"):
            raise AssetStoreError("OSS 前缀必须以 / 结尾")
        self._oss2 = oss2
        self.bucket = oss2.Bucket(
            oss2.ProviderAuthV4(EnvironmentVariableCredentialsProvider()),
            merged["endpoint"],
            merged["bucket"],
            region=merged["region"],
            connect_timeout=20,
            session=oss2.Session(pool_size=32),
        )

    def get(self, key: str) -> bytes:
        try:
            return self.bucket.get_object(self.prefix + key).read()
        except self._oss2.exceptions.OssError as error:
            raise AssetStoreError(
                f"OSS 读取失败：key={key} status={getattr(error, 'status', None)} code={getattr(error, 'code', None)}"
            ) from None


def open_asset_store(store: str | None = None, mirror: str | None = None) -> OssAssetStore | LocalMirrorStore:
    """按命令行选择存储：``mirror`` 本地镜像优先，否则 OSS（``store`` 为可选配置 JSON 路径）。"""

    if mirror:
        return LocalMirrorStore(mirror)
    config = json.loads(Path(store).read_text(encoding="utf-8")) if store else None
    return OssAssetStore(config)


__all__ = [
    "DEFAULT_OSS_CONFIG",
    "AssetStoreError",
    "LocalMirrorStore",
    "OssAssetStore",
    "open_asset_store",
]
