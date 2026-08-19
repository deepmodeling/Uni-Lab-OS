"""
UniLabOS 应用工具函数

提供清理、重启等工具函数
"""

import glob
import gc
import json
import os
import shutil
import sys
import threading
import time

from unilabos.utils.banner_print import print_status


_PATCH_MARKER = "# UniLabOS DLL Patch"
_PATCH_END_MARKER = "# End UniLabOS DLL Patch"

# 75 = EX_TEMPFAIL: 临时失败、重试即可，避免与业务退出码冲突
_RESTART_EXIT_CODE = 75


def _detect_conda_ros_distro(conda_prefix: str) -> str | None:
    """识别当前 conda 环境的 ROS 发行版。

    ``ROS_DISTRO`` 只有在环境激活脚本完整执行后才可靠；DLL 加载补丁恰好还要
    覆盖 IDE、快捷方式等激活不完整的启动方式。因此优先读取互斥包元数据，
    再回退到环境变量。发现冲突或未知发行版时返回 ``None``，避免修改错误的
    ROS 安装。
    """
    mutex_distros: set[str] = set()
    conda_meta = os.path.join(conda_prefix, "conda-meta")
    for metadata_path in glob.glob(os.path.join(conda_meta, "ros2-distro-mutex-*.json")):
        try:
            with open(metadata_path, "r", encoding="utf-8") as metadata_file:
                metadata = json.load(metadata_file)
        except (OSError, ValueError, TypeError):
            continue

        metadata_text = " ".join(
            str(metadata.get(key, "")) for key in ("name", "version", "build", "channel")
        ).lower()
        for distro in ("humble", "jazzy"):
            if distro in metadata_text:
                mutex_distros.add(distro)

    if len(mutex_distros) == 1:
        return next(iter(mutex_distros))
    if len(mutex_distros) > 1:
        return None

    env_distro = os.environ.get("ROS_DISTRO", "").strip().lower()
    return env_distro if env_distro in {"humble", "jazzy"} else None


def _build_dll_patch(lib_bin: str, preload_pyd: str = "") -> str:
    """生成一段加在目标文件顶部的 DLL 加载补丁源码。

    - 始终把 ``lib_bin`` 加入 DLL 搜索路径，并把 handle 挂在模块属性上，
      防止 GC 清掉搜索路径（``os.add_dll_directory`` 的句柄被回收时
      目录会被移除）。
    - 可选地用 ``ctypes.CDLL`` 预加载一个 .pyd，把它的依赖 DLL 提前装入
      进程内存，作为 ``rclpy._rclpy_pybind11`` 这类首次加载点的兜底。
    """
    # 用 repr() 序列化路径：Python 解析 repr 的结果会还原成原始字符串，
    # 不需要也不能再叠加 raw-string 前缀（叠了反而会让 \\ 变成两个反斜杠）。
    lines = [
        _PATCH_MARKER,
        "import os as _ulab_os",
        f"_ulab_p = {lib_bin!r}",
        'if hasattr(_ulab_os, "add_dll_directory") and _ulab_os.path.isdir(_ulab_p):',
        "    try: _UNILAB_DLL_HANDLE = _ulab_os.add_dll_directory(_ulab_p)",
        "    except Exception: _UNILAB_DLL_HANDLE = None",
    ]
    if preload_pyd:
        lines.extend(
            [
                "import ctypes as _ulab_ctypes",
                f"try: _ulab_ctypes.CDLL({preload_pyd!r})",
                "except Exception: pass",
            ]
        )
    lines.append(_PATCH_END_MARKER)
    return "\n".join(lines) + "\n"


def _apply_dll_patch(file_path: str, lib_bin: str, preload_pyd: str = "") -> bool:
    """把 DLL 补丁前置到 ``file_path``。文件不存在或已打过补丁则返回 False。"""
    if not os.path.isfile(file_path):
        return False
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()
    if _PATCH_MARKER in content:
        return False
    shutil.copy2(file_path, file_path + ".bak")
    # conda 常用硬链接把环境文件指向 package cache。直接以写模式打开目标会
    # 连缓存一起改坏，并让之后创建的 Jazzy/Humble 环境继承错误补丁。先写同目录
    # 临时文件再原子替换，既断开硬链接，也避免进程中断留下半个 Python 文件。
    patched_path = file_path + ".unilabos.tmp"
    try:
        with open(patched_path, "w", encoding="utf-8") as f:
            f.write(_build_dll_patch(lib_bin, preload_pyd) + content)
        shutil.copymode(file_path, patched_path)
        os.replace(patched_path, file_path)
    finally:
        if os.path.exists(patched_path):
            os.remove(patched_path)
    return True


def _print_restart_banner(patched_files):
    """打印重启提示并以 EX_TEMPFAIL 退出。

    - 不使用 ANSI 颜色码：Windows 旧版 cmd / PowerShell 5 默认不开 VT 处理，
      会把 ``\\033[1;33m`` 当做字面字符显示，反而让用户看不到正文。
    - 同时写入 stderr 与 stdout：某些上层 launcher / supervisor 只重定向
      其中一路，写两遍能保证用户至少看到一份。
    - 写入前防御性把流切到 UTF-8 with replace：``main.py`` 里已经做过一次，
      但本模块也可能被绕过 ``main.py`` 的代码路径直接 import；reconfigure
      失败也只是退回 errors=replace，不影响整体流程。
    """
    if sys.platform == "win32":
        for _stream in (sys.stdout, sys.stderr):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
            except (AttributeError, OSError):
                pass

    bar = "#" * 78
    files_lines = [f"[UniLabOS]   - {p}" for p in patched_files]
    body = "\n".join(
        [
            "",
            bar,
            bar,
            "##",
            "##  [UniLabOS] Windows + conda 下检测到 DLL 加载失败，已自动打补丁。",
            "##  [UniLabOS] DLL load failure detected on Windows + conda;",
            "##  [UniLabOS] the following files have been auto-patched:",
            "##",
            *[f"##  {line}" for line in files_lines],
            "##",
            "##  [UniLabOS] 当前进程的 rclpy 状态已损坏，补丁需要在新进程才生效。",
            "##  [UniLabOS] The current process is unusable; the patch only takes",
            "##  [UniLabOS] effect on a fresh process.",
            "##",
            "##  >>> 请重新运行刚才的命令 / Please re-run the same command. <<<",
            "##",
            bar,
            bar,
            "",
        ]
    )

    for stream in (sys.stderr, sys.stdout):
        try:
            stream.write(body)
            stream.flush()
        except Exception:
            try:
                print(body, file=stream)
            except Exception:
                pass

    sys.exit(_RESTART_EXIT_CODE)


def patch_rclpy_dll_windows():
    """在 Windows + conda Humble/Jazzy 环境下修复 ROS DLL 加载。

    背景：conda 安装的 ros 系列包，其原生扩展依赖 ``$CONDA_PREFIX/Library/bin``
    下的 DLL；只有 conda 环境被正确激活、且 PATH 中含 ``Library/bin`` 时，
    ``os.add_dll_directory`` 才能找到它们。当从快捷方式 / IDE / 子进程 /
    没激活的 shell 启动 ``unilab`` 时，会出现 ``DLL load failed``。

    RoboStack Humble 和 Jazzy 的 ``rclpy`` / ``rpyutils`` 都使用相同的加载
    入口，因此两种发行版共用这一套文件补丁，不再维护两种修复路径。

    本函数会:
        1) 修补 ``rclpy/impl/implementation_singleton.py`` —— rclpy 自身的 C 扩展入口；
        2) 修补 ``rpyutils/add_dll_directories.py`` —— 所有 ``*_s__rosidl_typesupport_c.pyd``
           （``geometry_msgs`` / ``std_msgs`` / ``sensor_msgs`` 等）的统一加载入口。

    打完补丁后**必须重启进程**才能生效（当前进程的 rclpy 已经发生过
    ``ImportError``，子模块仍处于损坏状态）。因此函数会主动退出，并在
    stdout/stderr 同时打印明显的重启提示，避免用户被后续报错淹没。
    """
    if sys.platform != "win32" or not os.environ.get("CONDA_PREFIX"):
        return

    cp = os.environ["CONDA_PREFIX"]
    ros_distro = _detect_conda_ros_distro(cp)
    if ros_distro not in {"humble", "jazzy"}:
        return

    lib_bin = os.path.join(cp, "Library", "bin")
    site_packages = os.path.join(cp, "Lib", "site-packages")
    if not os.path.isdir(lib_bin):
        return

    try:
        import rclpy  # noqa: F401

        return
    except ImportError as e:
        if not str(e).startswith("DLL load failed"):
            return

    patched = []

    # 1) rclpy 自身的入口
    rclpy_impl = os.path.join(site_packages, "rclpy", "impl", "implementation_singleton.py")
    rclpy_pyd_matches = glob.glob(os.path.join(site_packages, "rclpy", "_rclpy_pybind11*.pyd"))
    rclpy_pyd = rclpy_pyd_matches[0] if rclpy_pyd_matches else ""
    if rclpy_pyd and _apply_dll_patch(rclpy_impl, lib_bin, preload_pyd=rclpy_pyd):
        patched.append(rclpy_impl)

    # 2) rpyutils —— 所有 rosidl typesupport pyd 的加载点；放在 rclpy 之后
    #    例：geometry_msgs/geometry_msgs_s__rosidl_typesupport_c.pyd
    rpyutils_dll = os.path.join(site_packages, "rpyutils", "add_dll_directories.py")
    if _apply_dll_patch(rpyutils_dll, lib_bin):
        patched.append(rpyutils_dll)

    if not patched:
        # 已经打过补丁但 rclpy 仍然加载失败：原因不是缺 DLL 搜索路径，
        # 不要再次打补丁污染文件，让上层看到真实的 ImportError。
        return

    _print_restart_banner(patched)


def cleanup_for_restart() -> bool:
    """
    Clean up all resources for restart without exiting the process.

    This function prepares the system for re-initialization by:
    1. Stopping all communication clients
    2. Destroying ROS nodes
    3. Resetting singletons
    4. Waiting for threads to finish

    Returns:
        bool: True if cleanup was successful, False otherwise
    """
    print_status("[Restart] Starting cleanup for restart...", "info")

    # Step 1: Stop WebSocket communication client
    print_status("[Restart] Step 1: Stopping WebSocket client...", "info")
    try:
        from unilabos.app.communication import get_communication_client

        comm_client = get_communication_client()
        if comm_client is not None:
            comm_client.stop()
            print_status("[Restart] WebSocket client stopped", "info")
    except Exception as e:
        print_status(f"[Restart] Error stopping WebSocket: {e}", "warning")

    # Step 2: Get HostNode and cleanup ROS
    print_status("[Restart] Step 2: Cleaning up ROS nodes...", "info")
    try:
        from unilabos.ros.nodes.presets.host_node import HostNode
        import rclpy
        from rclpy.timer import Timer

        host_instance = HostNode.get_instance(timeout=5)
        if host_instance is not None:
            print_status(f"[Restart] Found HostNode: {host_instance.device_id}", "info")

            # Gracefully shutdown background threads
            print_status("[Restart] Shutting down background threads...", "info")
            HostNode.shutdown_background_threads(timeout=5.0)
            print_status("[Restart] Background threads shutdown complete", "info")

            # Stop discovery timer
            if hasattr(host_instance, "_discovery_timer") and isinstance(host_instance._discovery_timer, Timer):
                host_instance._discovery_timer.cancel()
                print_status("[Restart] Discovery timer cancelled", "info")

            # Destroy device nodes
            device_count = len(host_instance.devices_instances)
            print_status(f"[Restart] Destroying {device_count} device instances...", "info")
            for device_id, device_node in list(host_instance.devices_instances.items()):
                try:
                    if hasattr(device_node, "ros_node_instance") and device_node.ros_node_instance is not None:
                        device_node.ros_node_instance.destroy_node()
                        print_status(f"[Restart] Device {device_id} destroyed", "info")
                except Exception as e:
                    print_status(f"[Restart] Error destroying device {device_id}: {e}", "warning")

            # Clear devices instances
            host_instance.devices_instances.clear()
            host_instance.devices_names.clear()

            # Destroy host node
            try:
                host_instance.destroy_node()
                print_status("[Restart] HostNode destroyed", "info")
            except Exception as e:
                print_status(f"[Restart] Error destroying HostNode: {e}", "warning")

            # Reset HostNode state
            HostNode.reset_state()
            print_status("[Restart] HostNode state reset", "info")

        # Shutdown executor first (to stop executor.spin() gracefully)
        if hasattr(rclpy, "__executor") and rclpy.__executor is not None:
            try:
                rclpy.__executor.shutdown()
                rclpy.__executor = None  # Clear for restart
                print_status("[Restart] ROS executor shutdown complete", "info")
            except Exception as e:
                print_status(f"[Restart] Error shutting down executor: {e}", "warning")

        # Shutdown rclpy
        if rclpy.ok():
            rclpy.shutdown()
            print_status("[Restart] rclpy shutdown complete", "info")

    except ImportError as e:
        print_status(f"[Restart] ROS modules not available: {e}", "warning")
    except Exception as e:
        print_status(f"[Restart] Error in ROS cleanup: {e}", "warning")
        return False

    # Step 3: Reset communication client singleton
    print_status("[Restart] Step 3: Resetting singletons...", "info")
    try:
        from unilabos.app import communication

        if hasattr(communication, "_communication_client"):
            communication._communication_client = None
            print_status("[Restart] Communication client singleton reset", "info")
    except Exception as e:
        print_status(f"[Restart] Error resetting communication singleton: {e}", "warning")

    # Step 4: Wait for threads to finish
    print_status("[Restart] Step 4: Waiting for threads to finish...", "info")
    time.sleep(3)  # Give threads time to finish

    # Check remaining threads
    remaining_threads = []
    for t in threading.enumerate():
        if t.name != "MainThread" and t.is_alive():
            remaining_threads.append(t.name)

    if remaining_threads:
        print_status(
            f"[Restart] Warning: {len(remaining_threads)} threads still running: {remaining_threads}", "warning"
        )
    else:
        print_status("[Restart] All threads stopped", "info")

    # Step 5: Force garbage collection
    print_status("[Restart] Step 5: Running garbage collection...", "info")
    gc.collect()
    gc.collect()  # Run twice for weak references
    print_status("[Restart] Garbage collection complete", "info")

    print_status("[Restart] Cleanup complete. Ready for re-initialization.", "info")
    return True
