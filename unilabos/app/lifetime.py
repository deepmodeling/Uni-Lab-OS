"""进程寿命：本机拉起的子进程跟随父进程退出，停机限时完成、不被解释器收尾拖住。

默认拓扑里调度权威（``unilab``）直接拉起 Host 子进程，Host 再拉起受管设备进程（Slave）；
``--address`` 的顶层 Host 外面还有一层薄监督进程。这些子进程只为本机的父进程工作：父进程
无论怎么没了（SIGTERM、Ctrl+C、``kill -9``、崩溃、任务管理器结束进程），子进程都不能留下来
继续占着端口和数据库根目录锁（``<根目录>/edge`` 等），否则下一个 ``unilab`` 拉起的 Host 子进程
会被拒绝（退出码 73）。

跟随父进程
    父进程拉起子进程时用 :func:`follow_parent_env` 把自己的 pid 写进子进程环境；子进程在
    ``main()`` 最开始调用 :func:`follow_parent_from_env` 认领（读完即从 ``os.environ`` 删掉，
    不会传给孙进程），之后：

    - Linux：``prctl(PR_SET_PDEATHSIG, SIGTERM)``，父进程一消失内核立即发 SIGTERM。死亡信号
      跟随的是**拉起子进程的那个线程**而不是整个父进程，所以只在父进程用常驻线程拉起子进程时
      启用（``death_signal=True``：权威的 Host 看护线程、薄监督进程的主线程）；受管设备进程
      可能由 API 工作线程 / 定时器线程拉起，只用下面的轮询；
    - 所有平台：守护线程盯住父进程——POSIX 看 ``getppid()`` 是否变了（父进程退出后子进程被
      init / subreaper 收养），Windows 等父进程句柄（打开时核对创建时间，排除 pid 复用）；
    - Windows：父进程另把子进程放进 KILL_ON_JOB_CLOSE 的 Job Object（:class:`KillOnCloseJob`），
      父进程的 job 句柄随它消亡而关闭时，系统结束 job 内全部进程——这是兜底的硬结束。

    察觉父进程不在后，子进程走自己收到 SIGTERM 的有序退出路径（Host 关库、停受管进程），并以
    :data:`SHUTDOWN_DEADLINE_S` 限时，超时强制结束。只盯本机父进程：``--address`` 连远端权威的
    Host 掉线时只是重连，从不因为网络断开而退出。

停机收尾
    进程自己的有序收尾（停子进程、关库）做完后由 :func:`exit_now` 跑 atexit、刷新输出，随即
    ``os._exit``：不再等解释器 join ``concurrent.futures`` 的工作线程（等 Host 路由就绪的
    ``backend_http``、运行中的设备动作、受管进程重启定时器……都能把退出拖上几分钟），也省掉
    大堆的析构 GC（高负载下就要好几秒）。
"""

from __future__ import annotations

import atexit
import os
import select
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Callable, MutableMapping, NoReturn, Optional

from unilabos.utils.banner_print import print_status

# 进程编排环境变量与配置覆盖共用 UNILABOS_ 前缀，已登记在 config.config._NON_CONFIG_ENV_KEYS。
#: 子进程应跟随的父进程 pid（父进程拉起子进程时写入，子进程启动时读走）
PARENT_PID_ENV = "UNILABOS_PARENT_PID"
#: "1" 表示父进程由常驻线程拉起，子进程可在 Linux 上启用 PR_SET_PDEATHSIG
PARENT_DEATH_SIGNAL_ENV = "UNILABOS_PARENT_DEATH_SIGNAL"
#: 开始停机（收到停机信号或父进程消失）后，给本进程走完退出链路的上限，超时强制结束
SHUTDOWN_DEADLINE_S = 20.0
#: 父进程轮询间隔
PARENT_POLL_S = 0.5
#: 看门狗轮询间隔：限时到点后最多晚这么久强制结束
WATCHDOG_POLL_S = 0.2
#: 父进程消失、或停机超时被强制结束时的退出码（没有人再看这个码，只为日志里可辨认）
PARENT_LOST_EXIT_CODE = 1

#: 只保护 ``_watch``（普通上下文里使用）；停机与限时的路径一律不拿锁
_state_lock = threading.Lock()
_watch: Optional["_ParentWatch"] = None
#: 停机是否已经开始。信号处理器里只做普通赋值：处理器可能打断正持有任意锁的主线程，
#: 也可能被下一个信号嵌套打断，拿锁就可能和被打断的代码互相等死。
_shutdown_flag = False
#: 已布置的限时 ``(到点的 monotonic 时刻, 退出码, 原因, 秒数)``；先布置者生效
_deadline: Optional[tuple[float, int, str, float]] = None
_watchdog: Optional[threading.Thread] = None
_watchdog_lock = threading.Lock()


# ── 停机：限时、一次性 ─────────────────────────────────────────────


def begin_shutdown(code: int, *, reason: str, deadline: float = SHUTDOWN_DEADLINE_S) -> bool:
    """标记本进程开始停机并布置限时强制退出（在主线程的信号处理器里调用）。

    第一次调用返回 True；之后（重复的 SIGTERM / Ctrl+C、进程组信号与父进程转发的信号同时到达）
    返回 False，调用方应忽略这次信号，不要再往正在进行的收尾里抛异常。只做普通赋值，不拿锁、
    不起线程。
    """

    global _shutdown_flag
    if _shutdown_flag:
        return False
    _shutdown_flag = True
    arm_exit_deadline(deadline, code, reason=reason)
    return True


def shutdown_started() -> bool:
    return _shutdown_flag


def arm_exit_deadline(seconds: float, code: int, *, reason: str) -> None:
    """``seconds`` 后若进程仍在，强制以 ``code`` 结束；只布置一次，先布置者生效。

    这里只记下到点时刻（一次普通赋值，信号处理器里调用也安全），计时与强制结束由常驻看门狗线程
    完成。限时是最后的保证，不能依赖任何可能被卡住的东西：布置时不拿锁、不起线程，到点时不打印、
    不起线程。看门狗还没启动时（测试、一次性工具）在这里补启动；装信号处理器的进程须在装处理器
    之前调用 :func:`start_exit_watchdog`。
    """

    global _deadline
    if _deadline is None:
        _deadline = (time.monotonic() + float(seconds), code, reason, float(seconds))
    if not _watchdog_running():
        start_exit_watchdog()


def start_exit_watchdog() -> None:
    """启动常驻看门狗线程（幂等）。须在普通上下文里调用，不能在信号处理器里。"""

    global _watchdog
    with _watchdog_lock:
        if _watchdog_running():
            return
        watchdog = threading.Thread(target=_run_watchdog, name="UniLabExitDeadline", daemon=True)
        watchdog.start()
        _watchdog = watchdog


def _watchdog_running() -> bool:
    watchdog = _watchdog
    return watchdog is not None and watchdog.is_alive()


def _run_watchdog() -> None:
    while True:
        deadline = _deadline
        if deadline is not None and time.monotonic() >= deadline[0]:
            _force_exit(deadline[1], deadline[2], deadline[3])
        time.sleep(WATCHDOG_POLL_S)


def _force_exit(code: int, reason: str, seconds: float) -> NoReturn:
    """到点：报告一行再 ``os._exit``。不经过 print / logging（它们的锁可能被卡住的线程占着），
    也不起新线程；输出端写不进去（管道满、读端停住）就不写，绝不阻塞。"""

    message = f"[ERROR] {reason}：{seconds:.0f}s 内未完成退出，强制结束进程\n".encode("utf-8", "replace")
    try:
        writable = True
        if os.name != "nt":
            writable = bool(select.select([], [2], [], 0)[1])
        if writable:
            os.write(2, message)
    except (OSError, ValueError):
        pass
    os._exit(code)


def request_main_exit() -> None:
    """从任意线程请主线程按本进程收到 SIGTERM 的路径退出。

    POSIX 上把 SIGTERM 定向发给主线程：主线程阻塞在系统调用里也会被 EINTR 唤醒去跑 Python 信号
    处理器；没装处理器（启动早期、Slave）时就是 SIGTERM 的默认动作——立即结束进程。Windows 没有
    真正的 SIGTERM，按 SIGTERM / SIGINT 的 Python 处理器在主线程模拟一次信号。
    """

    if os.name != "nt":
        main_ident = threading.main_thread().ident
        if main_ident is not None:
            signal.pthread_kill(main_ident, signal.SIGTERM)
            return
    import _thread

    for sig in (signal.SIGTERM, signal.SIGINT):
        if callable(signal.getsignal(sig)):
            _thread.interrupt_main(sig)
            return
    _flush_std_streams()
    os._exit(PARENT_LOST_EXIT_CODE)


def exit_code_for(exc: BaseException) -> int:
    """把结束主流程的异常换成进程退出码（与解释器自己处理未捕获异常的结果一致）。"""

    if isinstance(exc, SystemExit):
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        print(code, file=sys.stderr)
        return 1
    if isinstance(exc, KeyboardInterrupt):
        return 130
    sys.excepthook(type(exc), exc, exc.__traceback__)
    return 1


def exit_now(code: int) -> NoReturn:
    """有序收尾已经做完：跑 atexit 回调、刷新输出后立即结束进程。

    atexit 里有受管设备进程的停止、HostLink 组网服务关闭、数据库根目录锁释放和日志刷新，照常
    执行；解释器自己的收尾（join 全部非守护线程 / 线程池工作线程、析构全部对象）跳过。
    """

    try:
        atexit._run_exitfuncs()
    except BaseException:  # noqa: BLE001 - 回调异常 atexit 已打印；进程无论如何都要结束
        pass
    finally:
        _flush_std_streams()
        os._exit(code)


def _flush_std_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None:
                stream.flush()
        except (OSError, ValueError):
            pass


# ── 跟随父进程 ──────────────────────────────────────────────────────


def follow_parent_env(
    env: MutableMapping[str, str], *, death_signal: bool = False
) -> MutableMapping[str, str]:
    """让 ``env`` 启动的子进程跟随本进程：写入本进程 pid（原地修改并返回 ``env``）。

    ``death_signal=True`` 只能用于由常驻线程（子进程活着时一直在的线程）拉起的子进程，见模块说明。
    """

    env[PARENT_PID_ENV] = str(os.getpid())
    if death_signal:
        env[PARENT_DEATH_SIGNAL_ENV] = "1"
    else:
        env.pop(PARENT_DEATH_SIGNAL_ENV, None)
    return env


def follow_parent_from_env(environ: Optional[MutableMapping[str, str]] = None) -> Optional[int]:
    """子进程启动时认领父进程：环境里有 :data:`PARENT_PID_ENV` 就开始跟随，返回父进程 pid。

    两个环境变量读完即删除：本进程再拉起的进程（驱动包安装、受管设备进程……）不能误以为自己的
    父进程是本进程的父进程；需要跟随本进程的子进程由本进程重新写入。
    """

    environ = os.environ if environ is None else environ
    raw = environ.pop(PARENT_PID_ENV, None)
    death_signal = environ.pop(PARENT_DEATH_SIGNAL_ENV, "") == "1"
    if not raw:
        return None
    try:
        parent_pid = int(raw)
    except ValueError:
        print_status(f"忽略无效的 {PARENT_PID_ENV}={raw!r}，本进程不跟随父进程", "warning")
        return None
    follow_parent(parent_pid, death_signal=death_signal)
    return parent_pid


def follow_parent(
    parent_pid: int,
    *,
    death_signal: bool = False,
    on_lost: Optional[Callable[[int], None]] = None,
    poll: float = PARENT_POLL_S,
) -> None:
    """开始跟随 ``parent_pid``：父进程消失时调用 ``on_lost(parent_pid)``（默认有序退出本进程）。"""

    global _watch
    start_exit_watchdog()
    handler = on_lost or _exit_with_parent
    if death_signal and sys.platform.startswith("linux") and os.getppid() == parent_pid:
        _set_parent_death_signal(signal.SIGTERM)
    probe = _open_parent_probe(parent_pid)
    if probe is None:
        # 父进程在本进程认领之前就已经没了（或 pid 已被复用）
        handler(parent_pid)
        return
    watch = _ParentWatch(parent_pid, probe, handler, poll)
    with _state_lock:
        previous, _watch = _watch, watch
    if previous is not None:
        previous.stop()
    watch.start()


def stop_following_parent() -> None:
    """停止跟随（测试用；生产进程跟随到退出为止）。"""

    global _watch
    with _state_lock:
        watch, _watch = _watch, None
    if watch is not None:
        watch.stop()


def _exit_with_parent(parent_pid: int) -> None:
    reason = f"父进程 (pid {parent_pid}) 已退出"
    # 先布置限时再打印：打印可能被别的线程占着的输出锁拖住，限时不能排在它后面
    arm_exit_deadline(SHUTDOWN_DEADLINE_S, PARENT_LOST_EXIT_CODE, reason=reason)
    print_status(f"{reason}，本进程随之有序退出", "warning")
    request_main_exit()


def _set_parent_death_signal(sig: int) -> bool:
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        pr_set_pdeathsig = 1
        return libc.prctl(pr_set_pdeathsig, int(sig), 0, 0, 0) == 0
    except (OSError, AttributeError):
        return False


class _ParentWatch:
    def __init__(self, parent_pid: int, probe: Any, on_lost: Callable[[int], None], poll: float) -> None:
        self.parent_pid = parent_pid
        self._probe = probe
        self._on_lost = on_lost
        self._poll = poll
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="UniLabParentWatch", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=self._poll * 4 + 1)
        self._probe.close()

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._probe.lost(self._poll, self._stop):
                if not self._stop.is_set():
                    self._on_lost(self.parent_pid)
                return


class _PosixParentProbe:
    """POSIX：父进程退出后本进程被收养，``getppid()`` 随之改变；不是直接子进程时按 pid 探测存活。"""

    def __init__(self, parent_pid: int, direct_child: bool) -> None:
        self.parent_pid = parent_pid
        self.direct_child = direct_child

    def lost(self, timeout: float, stop: threading.Event) -> bool:
        if stop.wait(timeout):
            return False
        if self.direct_child:
            return os.getppid() != self.parent_pid
        return not _pid_exists(self.parent_pid)

    def close(self) -> None:
        pass


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class _WindowsParentProbe:
    """Windows：等父进程的进程句柄变为有信号（进程已结束）。"""

    _WAIT_TIMEOUT = 0x102

    def __init__(self, kernel32: Any, handle: int) -> None:
        self._kernel32 = kernel32
        self._handle = handle

    def lost(self, timeout: float, stop: threading.Event) -> bool:
        if stop.is_set() or not self._handle:
            return False
        result = self._kernel32.WaitForSingleObject(self._handle, int(timeout * 1000))
        # WAIT_OBJECT_0：父进程已结束；WAIT_FAILED：句柄失效，同样当作父进程不在
        return result != self._WAIT_TIMEOUT

    def close(self) -> None:
        handle, self._handle = self._handle, 0
        if handle:
            self._kernel32.CloseHandle(handle)


def _open_parent_probe(parent_pid: int) -> Optional[Any]:
    if os.name != "nt":
        direct_child = os.getppid() == parent_pid
        if not direct_child and not _pid_exists(parent_pid):
            return None
        return _PosixParentProbe(parent_pid, direct_child)
    return _open_windows_parent_probe(parent_pid)


def _open_windows_parent_probe(parent_pid: int) -> Optional[_WindowsParentProbe]:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    synchronize = 0x00100000
    process_query_limited_information = 0x1000
    handle = kernel32.OpenProcess(synchronize | process_query_limited_information, False, parent_pid)
    if not handle:
        return None

    def created_at(process_handle: Any) -> Optional[int]:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel32.GetProcessTimes(process_handle, *(ctypes.byref(item) for item in times)):
            return None
        return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime

    parent_created = created_at(handle)
    own_created = created_at(kernel32.GetCurrentProcess())
    probe = _WindowsParentProbe(kernel32, handle)
    if parent_created is not None and own_created is not None and parent_created > own_created:
        # 该 pid 属于本进程之后才创建的进程：真正的父进程早已结束，pid 被复用
        probe.close()
        return None
    if kernel32.WaitForSingleObject(handle, 0) != _WindowsParentProbe._WAIT_TIMEOUT:
        probe.close()
        return None
    return probe


# ── Windows Job Object ───────────────────────────────────────────────


class KillOnCloseJob:
    """Windows Job Object：句柄随父进程消亡关闭时，系统结束 job 内全部进程。

    子进程拉起的进程默认也在同一个 job 里（Windows 8 起 job 可以嵌套），所以权威的 job 能连同
    Host 子进程拉起的受管设备进程一起回收。拿不到 job（非 Windows、父进程所在 job 禁止嵌套等）
    时 :meth:`create` 返回 None，由子进程自己的父进程跟随兜底。
    """

    def __init__(self, kernel32: Any, handle: int) -> None:
        self._kernel32 = kernel32
        self._handle = handle

    @classmethod
    def create(cls) -> Optional["KillOnCloseJob"]:
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
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

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

    def assign(self, process: "subprocess.Popen[Any]") -> bool:
        raw = getattr(process, "_handle", None)
        if raw is None:
            return False
        try:
            return bool(self._kernel32.AssignProcessToJobObject(self._handle, int(raw)))
        except Exception:  # noqa: BLE001
            return False


def _reset_for_test() -> None:
    """测试用：清掉停机标记与已布置的限时（看门狗继续空转）。"""

    global _shutdown_flag, _deadline
    stop_following_parent()
    _shutdown_flag = False
    _deadline = None


__all__ = [
    "KillOnCloseJob",
    "PARENT_DEATH_SIGNAL_ENV",
    "PARENT_LOST_EXIT_CODE",
    "PARENT_PID_ENV",
    "SHUTDOWN_DEADLINE_S",
    "arm_exit_deadline",
    "begin_shutdown",
    "exit_code_for",
    "exit_now",
    "follow_parent",
    "follow_parent_env",
    "follow_parent_from_env",
    "request_main_exit",
    "shutdown_started",
    "start_exit_watchdog",
    "stop_following_parent",
]
