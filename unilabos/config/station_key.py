"""V1 工站调用的协议密钥（``Authorization: Bearer <station_api_key>``）。

V1 把这把密钥放在 ``EdgeControlConfig.api_key``（配置文件或环境变量
``UNILABOS_EDGECONTROLCONFIG_API_KEY``），由 ``POST /api/v1/station/workflow-invocations`` 以
``hmac.compare_digest("Bearer <key>")`` 校验，密钥为空时一律 401。统一分支的对应存储：

- ``BasicConfig.station_api_key``（配置文件 / 环境变量 ``UNILABOS_BASICCONFIG_STATION_API_KEY``）优先；
- 为空时读 ``<working_dir>/station_api_key``（运维放置的文件，权威不自动生成：工站密钥由上游 Backend
  分发，不能像控制面 token 那样本地生成）；
- V1 的环境变量 ``UNILABOS_EDGECONTROLCONFIG_API_KEY`` 作为迁移兼容仍被识别。

与控制面 token（``X-UniLab-Token``）是两把独立的密钥：控制面 token 不能调用工站入口，工站密钥也不能
代替控制面 token 做其他写操作。本模块不依赖 FastAPI。
"""

from __future__ import annotations

import hmac
import os
from pathlib import Path
from typing import Mapping

from unilabos.config.config import BasicConfig

STATION_KEY_ENV = "UNILABOS_BASICCONFIG_STATION_API_KEY"
#: V1 ``EdgeControlConfig.api_key`` 的环境变量（迁移兼容）
STATION_KEY_V1_ENV = "UNILABOS_EDGECONTROLCONFIG_API_KEY"
STATION_KEY_FILENAME = "station_api_key"
#: 只接受工站密钥、不接受控制面 token 的写入口（V1 工站调用）
STATION_KEY_PATHS = ("/api/v1/station/workflow-invocations",)


def station_key_path(working_dir: str | os.PathLike[str]) -> Path:
    return Path(working_dir).expanduser() / STATION_KEY_FILENAME


def station_api_key() -> str:
    """当前生效的工站密钥：配置 → 环境变量（含 V1 名）→ 工作目录文件；都没有时为空串。"""

    configured = str(getattr(BasicConfig, "station_api_key", "") or "").strip()
    if configured:
        return configured
    for name in (STATION_KEY_ENV, STATION_KEY_V1_ENV):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    working_dir = str(BasicConfig.working_dir or "").strip()
    if not working_dir:
        return ""
    try:
        return station_key_path(working_dir).read_text(encoding="utf-8").strip()
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        return ""


def station_key_accepted(headers: Mapping[str, str]) -> bool:
    """``Authorization`` 恰为 ``Bearer <station_api_key>``（常量时间比较）；未配置密钥时一律拒绝。"""

    key = station_api_key()
    if not key:
        return False
    presented = headers.get("Authorization") or headers.get("authorization") or ""
    return hmac.compare_digest(str(presented).encode("utf-8"), f"Bearer {key}".encode("utf-8"))


def is_station_path(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in STATION_KEY_PATHS)


__all__ = [
    "STATION_KEY_ENV",
    "STATION_KEY_FILENAME",
    "STATION_KEY_PATHS",
    "STATION_KEY_V1_ENV",
    "is_station_path",
    "station_api_key",
    "station_key_accepted",
    "station_key_path",
]
