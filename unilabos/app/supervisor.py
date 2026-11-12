"""安全重启的进程编排：长驻的父进程负责拉起，Host 只管干净退出。

默认 ``unilab`` 就是调度权威进程（backend_main：Scheduler / Workflow / Registry /
Materials，持有浏览器连接的管理端口），它用本模块的 ``HostChildSupervisor`` 直接看护
Host 子进程（设备、HostLink、遥测、驱动包、受管设备进程；``--address`` 指回权威）：

    unilab = 调度权威进程
      └─ Host 子进程

``POST /api/v1/restart`` 只重启 Host 子进程：权威与管理端口不动，前端连接不断；Host
以 ``RESTART_EXIT_CODE``（75）退出，权威原参数再拉起。权威自己常驻，不接受整进程重启。

只有 Host 直接接云端 / 独立 Backend（CLI 给了 ``--address``，本机没有权威）时，Host 才
是顶层进程，此时由本模块的 ``run_supervisor`` 包一层薄监督进程负责再拉起。

不能在即将退出的进程里 ``Popen`` 新实例：Windows 上父进程若处于 Job Object
（IDE / ``Start-Process`` 重定向），子进程会随父进程一起被杀掉，看起来就是
「重启变成了退出」——所以拉起动作永远由长驻的父进程完成。

生命周期约定：

- Windows：子进程放进 KILL_ON_JOB_CLOSE 的 Job Object，父进程无论怎么死
  （含 ``TerminateProcess``），子进程及其受管设备子进程一起被系统回收；
- POSIX：SIGTERM / SIGHUP 转发给子进程，让它走正常退出链路；
- Host 子进程建在独立进程组，控制台 Ctrl+C 到不了它；权威停机时用 Ctrl+Break /
  SIGTERM 有序停掉它（uvicorn 当 SIGBREAK 处理，Host 的 finally / atexit 都会跑）；
- 数据库根目录锁（``server/database/root_lock.py``）：权威独占 ``<根目录>``，Host 子进程独占
  ``<根目录>/edge``，各取各的、互不等待。锁随进程退出由操作系统释放，再拉起的下一代 Host
  最多等 ``RELAUNCH_ROOT_LOCK_WAIT_S`` 让上一代释放；根目录被别的进程占着时以
  ``DATABASE_ROOT_LOCKED_EXIT_CODE`` 退出，按崩溃退避再拉起。

``--no-safe-restart`` 关闭全部编排：单进程（调度权威与 Host 同进程）、重启只退出，
便于附加调试器。一次性 CLI 子命令、``--check_mode`` 和 ``--is_slave``（没有管理 API；
受管设备进程按 pid 看护，不能多一层）也不进入任何监督。
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Optional, Sequence

logger = logging.getLogger(__name__)

# 75 = EX_TEMPFAIL：与 Windows ROS DLL 补丁退出码一致，监督循环会自动再拉起。
RESTART_EXIT_CODE = 75
# 98 = EADDRINUSE：管理端口被占（Host 子进程不监听端口，只在单进程 / 权威自己身上出现）
PORT_IN_USE_EXIT_CODE = 98
# 73 = EX_CANTCREAT：数据库根目录已被另一个进程独占（一个根目录只归一个实例）
DATABASE_ROOT_LOCKED_EXIT_CODE = 73
#: 被看护的进程重新拉起时，等上一代进程释放数据库根目录锁的上限。上一代已经退出，POSIX 上锁随
#: 进程立即释放；Windows 释放字节锁可能稍有延迟。顶层启动不等，第二个实例立即失败。
RELAUNCH_ROOT_LOCK_WAIT_S = 10.0
# 进程编排环境变量与配置覆盖共用 UNILABOS_ 前缀；新增时同步登记到
# config.config._NON_CONFIG_ENV_KEYS，否则配置加载会告警"未找到类"。
SUPERVISOR_INNER_ENV = "UNILABOS_SUPERVISOR_INNER"
#: 收到中断后给子进程自己走完退出链路的时间（uvicorn 优雅停机 + 关库 + 停 backend）
CHILD_EXIT_GRACE_S = 30.0
_POLL_S = 0.5


def is_supervised() -> bool:
    return os.environ.get(SUPERVISOR_INNER_ENV) == "1"


def inner_command(argv: Sequence[str] | None = None) -> list[str]:
    args = list(sys.argv[1:] if argv is None else argv)
    return [sys.executable, "-m", "unilabos.app.main", *args]


def should_supervise(args: Any) -> bool:
    """只有直接接远端 Backend 的顶层 Host 需要薄监督进程。

    默认拓扑里权威进程自己就是 Host 的父进程；配置文件里给 remote_addr 而不走 CLI
    ``--address`` 的 Host 不在此覆盖，重启时只退出。
    """

    if getattr(args, "no_safe_restart", False):
        return False
    if getattr(args, "check_mode", False) or getattr(args, "is_slave", False):
        return False
    if not str(getattr(args, "address", "") or "").strip():
        return False
    return not is_supervised()


def maybe_supervise(args: Any) -> None:
    """远端 Backend 的顶层 Host 进入监督循环；调用方在接管后不会再往下执行。"""

    if should_supervise(args):
        raise SystemExit(run_supervisor())


class _KillOnCloseJob:
    """Windows Job Object：句柄随监督进程消亡时，系统结束 job 内全部进程。"""

    def __init__(self, kernel32: Any, handle: int) -> None:
        self._kernel32 = kernel32
        self._handle = handle

    @classmethod
    def create(cls) -> Optional["_KillOnCloseJob"]:
        if sys.platform != "win32":
            return None
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

            class IoCounters(ctypes.Structure):
                _fields_ = [
                    (name, ctypes.c_ulonglong)
                    for name in (
                        "ReadOperationCount",
                        "WriteOperationCount",
                        "OtherOperationCount",
                        "ReadTransferCount",
                        "WriteTransferCount",
                        "OtherTransferCount",
                    )
                ]

            class BasicLimit(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                    ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class ExtendedLimit(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", BasicLimit),
                    ("IoInfo", IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
            kernel32.SetInformationJobObject.restype = wintypes.BOOL
            kernel32.SetInformationJobObject.argtypes = [
                wintypes.HANDLE,
                ctypes.c_int,
                wintypes.LPVOID,
                wintypes.DWORD,
            ]
            kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
            kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                return None
            info = ExtendedLimit()
            info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            job_object_extended_limit_information = 9
            if not kernel32.SetInformationJobObject(
                handle,
                job_object_extended_limit_information,
                ctypes.byref(info),
                ctypes.sizeof(info),
            ):
                kernel32.CloseHandle(handle)
                return None
            return cls(kernel32, handle)
        except Exception:  # noqa: BLE001 - 拿不到 Job 只是少一层兜底
            return None

    def assign(self, process: subprocess.Popen[Any]) -> bool:
        raw = getattr(process, "_handle", None)
        if raw is None:
            return False
        try:
            return bool(self._kernel32.AssignProcessToJobObject(self._handle, int(raw)))
        except Exception:  # noqa: BLE001
            return False


def run_supervisor(argv: Sequence[str] | None = None) -> int:
    """循环拉起 inner 进程，直到退出码不是重启约定码；返回子进程最终退出码。"""

    from unilabos.utils.banner_print import print_status

    command = inner_command(argv)
    env = os.environ.copy()
    env[SUPERVISOR_INNER_ENV] = "1"
    env.setdefault("PYTHONUNBUFFERED", "1")
    job = _KillOnCloseJob.create()
    state: dict[str, Any] = {"child": None, "stopping": False}

    def _forward_terminate(signum: int, frame: Any) -> None:
        state["stopping"] = True
        child = state["child"]
        if child is not None and child.poll() is None:
            child.terminate()

    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, _forward_terminate)
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None:
        signal.signal(sigbreak, signal.default_int_handler)

    print_status("Host 接远端 Backend：已开启安全重启监督（--no-safe-restart 可关闭）", "info")
    while True:
        child = subprocess.Popen(command, env=env)
        state["child"] = child
        if job is not None:
            job.assign(child)
        try:
            code = _wait(child)
        except KeyboardInterrupt:
            # Ctrl+C 已同时送达子进程：先等它走完退出链路，再兜底结束
            state["stopping"] = True
            return _wait_or_terminate(child)
        if code == RESTART_EXIT_CODE and not state["stopping"]:
            print_status("收到安静点重启，正在以相同参数拉起新进程", "warning")
            continue
        return code


def _wait(child: subprocess.Popen[Any]) -> int:
    # 分段等待：Windows 上无限期 WaitForSingleObject 收不到 Ctrl+C
    while True:
        try:
            return int(child.wait(timeout=_POLL_S) or 0)
        except subprocess.TimeoutExpired:
            continue


def _wait_or_terminate(child: subprocess.Popen[Any]) -> int:
    try:
        return int(child.wait(timeout=CHILD_EXIT_GRACE_S) or 0)
    except subprocess.TimeoutExpired:
        pass
    child.terminate()
    try:
        return int(child.wait(timeout=10) or 0)
    except subprocess.TimeoutExpired:
        child.kill()
        return int(child.wait(timeout=5) or 0)


# ── 调度权威进程看护 Host 子进程 ─────────────────────────────────────

HOST_CHILD_ENV = "UNILABOS_HOST_CHILD"
#: Host 意外退出（非重启约定码、非正常 0）后的再拉起退避
_CRASH_BACKOFF_S = (2.0, 5.0, 10.0, 30.0)
_MAX_CONSECUTIVE_CRASHES = 8


def is_host_child() -> bool:
    return os.environ.get(HOST_CHILD_ENV) == "1"


def host_child_command(
    argv: Sequence[str],
    *,
    authority_port: int,
    database_root: str,
) -> list[str]:
    """用权威进程的启动参数派生 Host 子进程命令。

    后出现的同名参数覆盖前面的（argparse 语义），所以只需在末尾追加：Host 连回
    本机权威（--address / 物料权威）、四库落独立目录避免与权威跨进程写同一份 SQLite；
    权威命令行里的单库覆盖（``--runtime_db`` 等）是给权威的，Host 子进程置空后四库全部落在
    ``database_root`` 下——绝对路径的覆盖若被继承，两个进程会打开同一个文件；
    环境检查权威已经做过，浏览器由权威打开。Host 子进程不监听端口（管理 API 经控制面
    WS 在进程内执行），所以不给 --port。
    """

    authority = f"http://127.0.0.1:{authority_port}"
    database_overrides = [
        item for key in ("runtime", "materials", "telemetry", "history") for item in (f"--{key}_db", "")
    ]
    return [
        sys.executable,
        "-m",
        "unilabos.app.main",
        *argv,
        "--address",
        authority,
        "--material_microbackend_addr",
        authority,
        "--server_database_root",
        database_root,
        *database_overrides,
        "--skip_env_check",
        "--disable_browser",
    ]


class HostChildSupervisor:
    """在调度权威进程内长驻看护 Host 子进程。

    - 退出码 75（安静点重启）：立刻原参数再拉起；
    - 退出码 0：Host 自己正常结束（收到 Ctrl+C 等），不再拉起；
    - 其它：视为崩溃，退避后再拉起，连续崩溃过多则停止看护并留日志。
    """

    def __init__(
        self,
        command: Sequence[str],
        *,
        env: Optional[dict[str, str]] = None,
        ready_probe: Optional[Any] = None,
        ready_timeout: float = 90.0,
    ) -> None:
        self._command = list(command)
        self._env = dict(os.environ if env is None else env)
        self._env[SUPERVISOR_INNER_ENV] = "1"
        self._env[HOST_CHILD_ENV] = "1"
        self._env.setdefault("PYTHONUNBUFFERED", "1")
        # 首次拉起前等权威的管理端口就绪：Host 启动早期就要经 HTTP 同步物料模板 / 注册表
        self._ready_probe = ready_probe
        self._ready_timeout = ready_timeout
        self._job = _KillOnCloseJob.create()
        self._lock = threading.Lock()
        self._child: Optional[subprocess.Popen[Any]] = None
        self._stopping = threading.Event()
        # 已向当前子进程发过退出请求：再发一次 Ctrl+Break 会让子进程的 KeyboardInterrupt
        # 打断正在进行的清理
        self._exit_requested = False
        self._thread: Optional[threading.Thread] = None
        self.restart_count = 0
        self.crash_count = 0
        #: 最近一次 Host 子进程的退出码（尚未退出过为 None）
        self.last_exit_code: Optional[int] = None

    # ── 查询 ──

    @property
    def pid(self) -> Optional[int]:
        with self._lock:
            child = self._child
        return child.pid if child is not None and child.poll() is None else None

    def alive(self) -> bool:
        return self.pid is not None

    def state(self) -> dict[str, Any]:
        """看护状态快照（V1 进程管理快照的 ``edge`` 组件据此推导阶段）。"""

        pid = self.pid
        thread = self._thread
        return {
            "pid": pid,
            "alive": pid is not None,
            # 看护线程仍在：崩溃退避或安静点重启的间隙里子进程暂时不在，但会被再拉起
            "supervising": thread is not None and thread.is_alive(),
            "stop_requested": self._stopping.is_set(),
            "restart_count": self.restart_count,
            "crash_count": self.crash_count,
            "last_exit_code": self.last_exit_code,
        }

    # ── 生命周期 ──

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, name="HostChildSupervisor", daemon=True)
        self._thread.start()

    def request_stop(self) -> None:
        """非阻塞地请 Host 开始退出（Ctrl+Break / SIGTERM），不等它结束。

        权威一收到停机信号就调用：Host 与权威并行收尾，而不是等权威的 uvicorn 停完
        再串行地停 Host——那样 Host 会先看到控制 WS 断开、去重连一个已经关掉的端口。
        """

        self._stopping.set()
        with self._lock:
            child = self._child
        if child is not None and child.poll() is None and not self._exit_requested:
            self._exit_requested = True
            _request_graceful_exit(child)

    def stop(self, *, timeout: float = CHILD_EXIT_GRACE_S) -> None:
        """有序停掉 Host：先请它自己走退出链路（Ctrl+Break / SIGTERM），超时再结束。"""

        self._stopping.set()
        with self._lock:
            child = self._child
        if child is not None and child.poll() is None:
            if not self._exit_requested:
                _request_graceful_exit(child)
            try:
                child.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _launch(self) -> subprocess.Popen[Any]:
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
        child = subprocess.Popen(self._command, env=self._env, creationflags=creationflags)
        if self._job is not None:
            self._job.assign(child)
        with self._lock:
            self._child = child
            self._exit_requested = False
        logger.info("[HostChild] Host 子进程已拉起 (pid %s)", child.pid)
        return child

    def _wait_authority_ready(self) -> None:
        if self._ready_probe is None:
            return
        deadline = time.monotonic() + self._ready_timeout
        while not self._stopping.is_set() and time.monotonic() < deadline:
            try:
                if self._ready_probe():
                    return
            except Exception:  # noqa: BLE001 - 探测失败等同未就绪
                pass
            if self._stopping.wait(0.5):
                return
        logger.warning("[HostChild] 等待权威管理端口就绪超时，仍尝试拉起 Host")

    def _run(self) -> None:
        self._wait_authority_ready()
        while not self._stopping.is_set():
            child = self._launch()
            code = _wait(child)
            self.last_exit_code = code
            if self._stopping.is_set():
                return
            if code == RESTART_EXIT_CODE:
                self.restart_count += 1
                self.crash_count = 0
                logger.warning("[HostChild] Host 安静点重启，正在以相同参数拉起新进程 (第 %d 次)", self.restart_count)
                continue
            if code == 0:
                logger.warning("[HostChild] Host 子进程正常退出，不再拉起")
                return
            self.crash_count += 1
            if self.crash_count > _MAX_CONSECUTIVE_CRASHES:
                logger.error("[HostChild] Host 连续崩溃 %d 次 (最近 code=%s)，停止看护", self.crash_count, code)
                return
            delay = _CRASH_BACKOFF_S[min(self.crash_count - 1, len(_CRASH_BACKOFF_S) - 1)]
            if code == DATABASE_ROOT_LOCKED_EXIT_CODE:
                logger.error(
                    "[HostChild] Host 子进程的数据库根目录被其他进程占用（占用者见上方 Host 输出，"
                    "常见于上次遗留的 Host 进程仍在运行），%.0fs 后再拉起",
                    delay,
                )
            else:
                logger.error("[HostChild] Host 子进程异常退出 code=%s，%.0fs 后再拉起", code, delay)
            if self._stopping.wait(delay):
                return


def _request_graceful_exit(child: subprocess.Popen[Any]) -> None:
    if os.name == "nt":
        # 子进程建在独立进程组，控制台 Ctrl+C 到不了它；Ctrl+Break 由 uvicorn 当 SIGBREAK 处理
        try:
            child.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
            return
        except (OSError, ValueError, AttributeError):
            pass
    child.terminate()


__all__ = [
    "CHILD_EXIT_GRACE_S",
    "DATABASE_ROOT_LOCKED_EXIT_CODE",
    "HOST_CHILD_ENV",
    "HostChildSupervisor",
    "PORT_IN_USE_EXIT_CODE",
    "RELAUNCH_ROOT_LOCK_WAIT_S",
    "RESTART_EXIT_CODE",
    "SUPERVISOR_INNER_ENV",
    "host_child_command",
    "inner_command",
    "is_host_child",
    "is_supervised",
    "maybe_supervise",
    "run_supervisor",
    "should_supervise",
]
