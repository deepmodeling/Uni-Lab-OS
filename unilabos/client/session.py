"""会话状态管理

管理 HTTP 客户端会话状态，包括：
- 认证信息（ak/sk）
- 后端地址（base_url）
- 上下文信息（当前实验室、项目）

会话文件存储在 <working_dir>/session.json
使用文件锁确保并发安全
"""

import base64
import json
from pathlib import Path
from typing import Optional, Dict, Any
from dataclasses import dataclass, field, asdict

from unilabos.utils.file_lock import (
    acquire_exclusive_file_lock,
    release_file_lock,
)
from unilabos.utils.address import DEFAULT_BACKEND_ADDRESS, resolve_address


DEFAULT_BASE_URL = DEFAULT_BACKEND_ADDRESS


@dataclass
class AuthInfo:
    """认证信息（基于 ak/sk）"""
    ak: str = ""
    sk: str = ""
    user_name: str = ""

    def is_valid(self) -> bool:
        """检查是否已配置 ak/sk"""
        return bool(self.ak and self.sk)

    def auth_secret(self) -> str:
        """生成 base64(ak:sk) 用作 Authorization header"""
        if not self.is_valid():
            return ""
        target = f"{self.ak}:{self.sk}"
        return base64.b64encode(target.encode("utf-8")).decode("utf-8")


@dataclass
class ContextInfo:
    """上下文信息"""
    lab_uuid: str = ""
    project_uuid: str = ""


@dataclass
class SessionState:
    """会话状态"""
    base_url: str = DEFAULT_BASE_URL
    auth: AuthInfo = field(default_factory=AuthInfo)
    context: ContextInfo = field(default_factory=ContextInfo)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "base_url": self.base_url,
            "auth": asdict(self.auth),
            "context": asdict(self.context),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SessionState":
        return cls(
            base_url=data.get("base_url", DEFAULT_BASE_URL),
            auth=AuthInfo(**data.get("auth", {})),
            context=ContextInfo(**data.get("context", {})),
        )


def resolve_addr(addr: str) -> str:
    """兼容旧调用名；新代码统一使用 ``resolve_address``。"""

    return resolve_address(addr)


class SessionManager:
    """会话管理器

    使用文件锁确保并发安全，支持上下文管理器：

    with SessionManager(working_dir="/path/to/wd") as manager:
        state = manager.get_state()
        state.auth.ak = "..."
        # 退出时自动保存
    """

    def __init__(self, working_dir: Optional[str] = None):
        if working_dir:
            self.working_dir = Path(working_dir)
        else:
            self.working_dir = Path.cwd()
        self.session_file = self.working_dir / "session.json"
        self._lock_file: Optional[Any] = None
        self._state: Optional[SessionState] = None

    def __enter__(self):
        self._acquire_lock()
        self._state = self._load_state()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type is None:
                self._save_state()
        finally:
            self._release_lock()
        return False

    def _acquire_lock(self):
        self.working_dir.mkdir(parents=True, exist_ok=True)
        lock_file_path = self.working_dir / "session.lock"
        self._lock_file = open(lock_file_path, "a+b")
        try:
            acquire_exclusive_file_lock(self._lock_file)
        except Exception:
            self._lock_file.close()
            self._lock_file = None
            raise

    def _release_lock(self):
        if self._lock_file:
            release_file_lock(self._lock_file)
            self._lock_file.close()
            self._lock_file = None

    def _load_state(self) -> SessionState:
        if self.session_file.exists():
            try:
                with open(self.session_file, "r") as f:
                    return SessionState.from_dict(json.load(f))
            except (json.JSONDecodeError, KeyError):
                pass
        return SessionState()

    def _save_state(self):
        if self._state is None:
            return
        self.working_dir.mkdir(parents=True, exist_ok=True)
        with open(self.session_file, "w") as f:
            json.dump(self._state.to_dict(), f, indent=2)

    def get_state(self) -> SessionState:
        if self._state is None:
            raise RuntimeError("必须在上下文管理器中使用 SessionManager")
        return self._state
