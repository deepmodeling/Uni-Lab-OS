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
import os
import fcntl
from pathlib import Path
from typing import Optional, Dict, Any
from dataclasses import dataclass, field, asdict


DEFAULT_BASE_URL = "https://leap-lab.bohrium.com/api/v1"


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
    """解析 --addr 参数

    支持别名：
      test  → https://leap-lab.test.bohrium.com/api/v1
      uat   → https://leap-lab.uat.bohrium.com/api/v1
      local → http://127.0.0.1:48197/api/v1
      其他  → 直接作为 URL 使用
    """
    aliases = {
        "test": "https://leap-lab.test.bohrium.com/api/v1",
        "uat": "https://leap-lab.uat.bohrium.com/api/v1",
        "local": "http://127.0.0.1:48197/api/v1",
        "prod": DEFAULT_BASE_URL,
    }
    return aliases.get(addr, addr)


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
        if exc_type is None:
            self._save_state()
        self._release_lock()
        return False

    def _acquire_lock(self):
        self.working_dir.mkdir(parents=True, exist_ok=True)
        lock_file_path = self.working_dir / "session.lock"
        self._lock_file = open(lock_file_path, "w")
        fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX)

    def _release_lock(self):
        if self._lock_file:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
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
