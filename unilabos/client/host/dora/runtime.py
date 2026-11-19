"""Dora 运行时辅助：定位 CLI，并启动或监督 dataflow。

在 macOS + conda 环境中，dora CLI 与 pip 版 pyarrow 的原生扩展链接系统
`/usr/lib/libiconv.2.dylib`（需要符号 `_iconv`），但 conda 自带的 GNU libiconv
只导出 `_libiconv`，可能因 dyld 按叶名合并而触发 `Symbol not found: _iconv`。
`scripts/fix_macos_libiconv.sh` 为 dora_cli / pyarrow 生成
`libiconv_compat.dylib` 垫片，将 `_iconv*` 转发到 GNU `_libiconv*`，
并改写对应依赖。处理后的二进制不依赖 DYLD 注入变量。

两种启动方式：
  - `run_dataflow`：通过 `dora run` 完成 coordinator、daemon、建图和节点启动，
    适合一次性运行。
  - 常驻模式 `ensure_up`/`build_dataflow`/`start_dataflow`/`destroy`：
    daemon 常驻且 dataflow 预先建图，`start` 仅启动节点，适合频繁运行。

进程组：所有 `dora` 子进程都以 `start_new_session=True` 起在**独立进程组**，`terminate_process`
会向整个进程组发送信号，确保 daemon 派生的节点一并退出。
"""

from __future__ import annotations

import os
import signal
import subprocess
import shutil
import time
from typing import Dict, List, Optional


def dora_binary() -> Optional[str]:
    """返回 dora CLI 可执行文件路径（找不到返回 None）。"""
    return shutil.which("dora")


def _require_binary() -> str:
    binary = dora_binary()
    if binary is None:
        raise RuntimeError(
            "未找到 dora CLI；请执行 `cargo install dora-cli`，"
            "或使用 Dora 官方平台安装脚本。"
        )
    return binary


def patched_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """返回运行 Dora 所需的环境变量副本。"""
    env = os.environ.copy()
    if extra:
        env.update(extra)
    return env


def run_dataflow(
    dataflow_path: str,
    *,
    extra_env: Optional[Dict[str, str]] = None,
    stdout=None,
    stderr=None,
) -> subprocess.Popen:
    """以 `dora run` 方式启动一个自包含 dataflow（内部自动拉起 coordinator/daemon）。

    进程起在独立进程组，返回 Popen；请用 `terminate_process` 整组回收，避免子节点泄漏。
    """
    binary = _require_binary()
    return subprocess.Popen(
        [binary, "run", dataflow_path],
        env=patched_env(extra_env),
        stdout=stdout,
        stderr=stderr,
        start_new_session=True,  # 独立进程组，便于整组终止
    )


def terminate_process(proc: Optional[subprocess.Popen], timeout: float = 10.0) -> None:
    """终止一个 dora 子进程及其整个进程组（回收 daemon 派生的所有子节点进程）。"""
    if proc is None or proc.poll() is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=timeout if sig is signal.SIGTERM else 3.0)
            return
        except subprocess.TimeoutExpired:
            continue


# ----------------------------------------------------------------------------- #
# 常驻 daemon 模式
# ----------------------------------------------------------------------------- #
def _run_cli(args: List[str], timeout: float = 120.0) -> subprocess.CompletedProcess:
    binary = _require_binary()
    return subprocess.run(
        [binary, *args],
        env=patched_env(),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def is_up() -> bool:
    """coordinator/daemon 是否已在运行（用 `dora list` 探测）。"""
    try:
        return _run_cli(["list"], timeout=15).returncode == 0
    except Exception:
        return False


def ensure_up() -> bool:
    """确保 coordinator+daemon 常驻运行；已在跑则直接返回。返回是否可用。"""
    if is_up():
        return True
    try:
        _run_cli(["up"], timeout=60)
    except Exception:
        return False
    # up 之后稍等 daemon 就绪
    for _ in range(20):
        if is_up():
            return True
        time.sleep(0.5)
    return is_up()


def build_dataflow(dataflow_path: str) -> subprocess.CompletedProcess:
    """预建图：执行 dataflow 中各节点的 build 命令（无 build: 字段则近似 no-op）。"""
    return _run_cli(["build", dataflow_path], timeout=600)


def start_dataflow(dataflow_path: str, *, name: Optional[str] = None, detach: bool = True) -> subprocess.CompletedProcess:
    """在常驻 daemon 上启动已建图的 dataflow。detach=True 立即返回。"""
    args = ["start", dataflow_path]
    if name:
        args += ["--name", name]
    args += ["--detach"] if detach else ["--attach"]
    return _run_cli(args, timeout=600)


def stop_dataflow(name: Optional[str] = None) -> None:
    try:
        _run_cli(["stop", "--name", name] if name else ["stop"], timeout=60)
    except Exception:
        pass


def destroy() -> None:
    """销毁常驻 coordinator+daemon（会先停止仍在运行的 dataflow）。"""
    try:
        _run_cli(["destroy"], timeout=60)
    except Exception:
        pass


def check_available() -> Dict[str, object]:
    """快速自检 dora 是否可用，返回诊断信息。"""
    info: Dict[str, object] = {"binary": dora_binary(), "python_ok": False, "cli_ok": False}
    try:
        import dora  # noqa: F401

        info["python_ok"] = True
    except Exception as exc:  # pragma: no cover - 环境相关
        info["python_error"] = repr(exc)
    binary = info["binary"]
    if binary:
        try:
            out = _run_cli(["--version"], timeout=20)
            info["cli_ok"] = out.returncode == 0
            info["cli_version"] = (out.stdout or out.stderr).strip().splitlines()[:1]
        except Exception as exc:  # pragma: no cover - 环境相关
            info["cli_error"] = repr(exc)
    return info
