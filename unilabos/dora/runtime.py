"""dora 运行时辅助：定位 dora CLI、启动/监督 dataflow（自包含 run 模式 + 常驻 daemon 模式）。

背景（macOS + conda）：dora CLI 与 pip 版 pyarrow 的原生扩展都链接系统
`/usr/lib/libiconv.2.dylib`（需要符号 `_iconv`），但 conda 自带的 GNU libiconv
只导出 `_libiconv`，且会被 dyld 按叶名 coalesce，导致 `Symbol not found: _iconv`。
经验证，`DYLD_INSERT_LIBRARIES` 在不同 spawn 上下文下并不稳定，因此改为**确定性修复**：
用 `scripts/fix_macos_libiconv.sh` 为 dora_cli / pyarrow 生成一个 `libiconv_compat.dylib`
兼容垫片（把 `_iconv*` 转发到 GNU `_libiconv*`）并改写其依赖。修复后无需任何 DYLD 变量。

两种启动方式：
  - `run_dataflow`：`dora run`，每次冷启动 coordinator+daemon+建图+spawn，一把梭（简单，适合一次性）。
  - 常驻模式 `ensure_up`/`build_dataflow`/`start_dataflow`/`destroy`：daemon 常驻、dataflow 预建图，
    `start` 只做 spawn，把冷启动编排从每次启动的关键路径上摊薄（适合生产/频繁重启）。

进程组：所有 `dora` 子进程都以 `start_new_session=True` 起在**独立进程组**，`terminate_process`
会对整组发信号，避免 daemon 派生的子节点进程泄漏（否则每次运行都残留一批 python 节点进程）。
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
        raise RuntimeError("未找到 dora CLI，请在 unilab 环境执行 `pip install dora-rs-cli`。")
    return binary


def patched_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """返回运行 dora 所需的环境变量副本（原生库已确定性修复，无需注入 DYLD）。"""
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
