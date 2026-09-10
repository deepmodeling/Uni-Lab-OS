"""
UniLabOS 应用工具函数

提供清理、重启等工具函数
"""

from unilabos.utils.banner_print import print_status
import time
import threading
import gc
import glob
import os
import shutil
import sys


_PATCH_MARKER = "# UniLabOS DLL Patch"
_PATCH_END_MARKER = "# End UniLabOS DLL Patch"

# 75 = EX_TEMPFAIL: 临时失败、重试即可，避免与业务退出码冲突
_RESTART_EXIT_CODE = 75


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
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(_build_dll_patch(lib_bin, preload_pyd) + content)
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
    """在 Windows + conda 环境下修复 rclpy / rosidl typesupport 的 DLL 加载。

    背景：conda 安装的 ros 系列包，其原生扩展依赖 ``$CONDA_PREFIX/Library/bin``
    下的 DLL；只有 conda 环境被正确激活、且 PATH 中含 ``Library/bin`` 时，
    ``os.add_dll_directory`` 才能找到它们。当从快捷方式 / IDE / 子进程 /
    没激活的 shell 启动 ``unilab`` 时，会出现 ``DLL load failed``。

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

    try:
        import rclpy  # noqa: F401

        return
    except ImportError as e:
        if not str(e).startswith("DLL load failed"):
            return

    cp = os.environ["CONDA_PREFIX"]
    lib_bin = os.path.join(cp, "Library", "bin")
    site_packages = os.path.join(cp, "Lib", "site-packages")
    if not os.path.isdir(lib_bin):
        return

    patched = []

    # 1) rclpy 自身的入口
    rclpy_impl = os.path.join(
        site_packages, "rclpy", "impl", "implementation_singleton.py"
    )
    rclpy_pyd_matches = glob.glob(
        os.path.join(site_packages, "rclpy", "_rclpy_pybind11*.pyd")
    )
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


patch_rclpy_dll_windows()


def cleanup_for_restart() -> bool:
    """按安全所有权顺序清理当前进程，为监督器重启做准备。

    参数：无。
    返回：全部关键清理完成时返回 ``True``；ROS 或数据库所有者出现未恢复故障时
    返回 ``False``。工作区文件监视、工作流（Workflow）、通信客户端、ROS 节点、
    Edge 微后端、运行态数据库和进程单例依次关闭，最后执行垃圾回收。
    异常：各可选组件的普通清理异常转换为警告并继续后续步骤；导入或 ROS 清理
    故障按既有策略返回 ``False``。本函数不直接退出进程。
    """
    print_status("[重启] 开始执行重启前清理", "info")
    cleanup_succeeded = True
    storage_owners_closed = True

    # 先停止工作区文件世代监视，避免后续关闭数据库和设备时继续提交刷新。
    try:
        from unilabos.package_manager import close_workspace_product_lifecycle

        close_workspace_product_lifecycle()
        print_status("[重启] 工作区产品生命周期已停止", "info")
    except Exception as error:
        print_status(
            f"[重启] 停止工作区产品生命周期时出错: {error}",
            "warning",
        )

    # 工作流服务与模板投影各自持有 workflow_history.db 连接，必须早于文件重置关闭。
    try:
        from unilabos.workflow.composition import shutdown_workflow_runtime

        shutdown_workflow_runtime()
        print_status("[重启] 工作流运行态已停止", "info")
    except Exception as error:
        storage_owners_closed = False
        cleanup_succeeded = False
        print_status(f"[重启] 停止工作流运行态时出错: {error}", "warning")

    # 第一步：停止 WebSocket 通信客户端。
    print_status("[重启] 第一步：正在停止 WebSocket 客户端", "info")
    try:
        from unilabos.app.communication import get_communication_client

        comm_client = get_communication_client()
        if comm_client is not None:
            comm_client.stop()
            print_status("[重启] WebSocket 客户端已停止", "info")
    except Exception as error:
        print_status(f"[重启] 停止 WebSocket 客户端时出错: {error}", "warning")

    # 第二步：取得主机节点并清理 ROS 运行时。
    print_status("[重启] 第二步：正在清理 ROS 节点", "info")
    try:
        from unilabos.ros.nodes.presets.host_node import HostNode
        import rclpy
        from rclpy.timer import Timer

        # ``host_instance`` 是当前进程唯一的主机节点运行实例。
        host_instance = HostNode.get_instance(timeout=5)
        if host_instance is not None:
            print_status(f"[重启] 已找到主机节点: {host_instance.device_id}", "info")

            # 先有界停止后台线程，避免销毁节点后仍有回调访问它。
            print_status("[重启] 正在停止后台线程", "info")
            HostNode.shutdown_background_threads(timeout=5.0)
            print_status("[重启] 后台线程已停止", "info")

            # 停止发现计时器，阻止清理期间产生新的节点发现回调。
            if hasattr(host_instance, "_discovery_timer") and isinstance(
                host_instance._discovery_timer, Timer
            ):
                host_instance._discovery_timer.cancel()
                print_status("[重启] 节点发现计时器已取消", "info")

            # ``device_count`` 是本轮需要销毁的设备运行实例数量。
            device_count = len(host_instance.devices_instances)
            print_status(f"[重启] 正在销毁 {device_count} 个设备实例", "info")
            for device_id, device_node in list(host_instance.devices_instances.items()):
                try:
                    if (
                        hasattr(device_node, "ros_node_instance")
                        and device_node.ros_node_instance is not None
                    ):
                        device_node.ros_node_instance.destroy_node()
                        print_status(f"[重启] 设备 {device_id} 已销毁", "info")
                except Exception as error:
                    print_status(
                        f"[重启] 销毁设备 {device_id} 时出错: {error}", "warning"
                    )

            # 清除设备实例索引，避免监督器下一代复用旧对象。
            host_instance.devices_instances.clear()
            host_instance.devices_names.clear()

            # 销毁主机节点。
            try:
                host_instance.destroy_node()
                print_status("[重启] 主机节点已销毁", "info")
            except Exception as error:
                print_status(f"[重启] 销毁主机节点时出错: {error}", "warning")

            # 重置进程级主机节点状态。
            HostNode.reset_state()
            print_status("[重启] 主机节点状态已重置", "info")

        # 先停止执行器，确保 ``executor.spin()`` 有序退出。
        if hasattr(rclpy, "__executor") and rclpy.__executor is not None:
            try:
                rclpy.__executor.shutdown()
                rclpy.__executor = None  # 清除旧执行器，允许下一代进程重新创建。
                print_status("[重启] ROS 执行器已停止", "info")
            except Exception as error:
                print_status(f"[重启] 停止 ROS 执行器时出错: {error}", "warning")

        # 最后关闭 rclpy 上下文。
        if rclpy.ok():
            rclpy.shutdown()
            print_status("[重启] rclpy 已关闭", "info")

    except ImportError as error:
        print_status(f"[重启] ROS 模块不可用: {error}", "warning")
    except Exception as error:
        cleanup_succeeded = False
        print_status(f"[重启] 清理 ROS 时出错: {error}", "warning")

    # 第三步：先停止 Edge 微后端，再清理进程单例；这会关闭主机监听器、从机客户端和
    # 调度器（Scheduler）存储，使重启后的进程能重新绑定 HostLink 与 SQLite 资源。
    print_status("[重启] 第三步：正在停止 Edge 微后端", "info")
    try:
        from unilabos.app.scheduler.integration import shutdown_edge_services

        shutdown_edge_services()
        print_status("[重启] Edge 微后端已停止", "info")
    except Exception as error:
        storage_owners_closed = False
        cleanup_succeeded = False
        print_status(f"[重启] 停止 Edge 微后端时出错: {error}", "warning")

    # 只有所有 SQLite 所有者均成功关闭，才允许销毁私有临时目录并释放工作目录锁。
    if storage_owners_closed:
        try:
            from unilabos.app.runtime_storage import (
                close_runtime_storage_session,
                discard_runtime_storage_session,
            )

            discard_runtime_storage_session()
            close_runtime_storage_session()
            print_status("[重启] 运行态数据库会话已释放", "info")
        except Exception as error:
            cleanup_succeeded = False
            print_status(f"[重启] 重置运行态数据库时出错: {error}", "warning")
    else:
        print_status(
            "[重启] 数据库所有者未全部关闭，已跳过文件重置并保留目录锁",
            "warning",
        )

    # 第四步：重置通信客户端进程单例。
    print_status("[重启] 第四步：正在重置进程单例", "info")
    try:
        from unilabos.app import communication

        if hasattr(communication, "_communication_client"):
            communication._communication_client = None
            print_status("[重启] 通信客户端单例已重置", "info")
    except Exception as error:
        print_status(
            f"[重启] 重置通信客户端单例时出错: {error}", "warning"
        )

    # 第五步：给后台线程有限时间完成退出。
    print_status("[重启] 第五步：等待后台线程退出", "info")
    time.sleep(3)

    # ``remaining_threads`` 记录清理后仍存活的非主线程名称，供重启诊断。
    remaining_threads = []
    for t in threading.enumerate():
        if t.name != "MainThread" and t.is_alive():
            remaining_threads.append(t.name)

    if remaining_threads:
        print_status(
            f"[重启] 警告：仍有 {len(remaining_threads)} 个线程运行: {remaining_threads}",
            "warning",
        )
    else:
        print_status("[重启] 所有后台线程均已停止", "info")

    # exporter flush 有界且 fail-open；os._exit 前也尽量送出已结束 span。
    try:
        from unilabos.utils.tracing import shutdown_tracing

        shutdown_tracing()
    except Exception as error:
        print_status(f"[重启] 关闭追踪导出器时出错: {error}", "warning")

    # 第六步：执行垃圾回收并清除弱引用对象。
    print_status("[重启] 第六步：正在执行垃圾回收", "info")
    gc.collect()
    gc.collect()
    print_status("[重启] 垃圾回收已完成", "info")

    print_status("[重启] 清理完成，可以重新初始化", "info")
    return cleanup_succeeded
