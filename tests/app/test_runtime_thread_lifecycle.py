"""在独立进程中验证关键 Runtime 线程失败不能留下虚假的就绪状态。"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


def _run_child(script: str, ready: Path) -> subprocess.CompletedProcess[str]:
    """有界执行隔离子进程；超时只清理本测试创建的进程。"""
    try:
        return subprocess.run(
            [sys.executable, "-c", textwrap.dedent(script)],
            cwd=Path(__file__).resolve().parents[2],
            env={**os.environ, "UNILABOS_EDGE_READY_FILE": str(ready)},
            capture_output=True,
            text=True,
            timeout=8,
        )
    except subprocess.TimeoutExpired as error:
        pytest.fail(f"关键线程死亡后进程仍挂住；ready={ready.exists()}\n{error.stderr}")


@pytest.mark.parametrize(
    "failure",
    [
        "raise RuntimeError('致命后台故障')",
        "return",
        "raise SystemExit(0)",
    ],
)
def test_backend_failure_exits_even_with_blocked_non_daemon_worker(
    tmp_path: Path,
    failure: str,
) -> None:
    """真正调用 backend 入口，并保留会阻止解释器正常退出的工作线程。"""
    ready = tmp_path / "ready.json"
    script = textwrap.dedent(f"""
        import sys
        import threading
        import types
        from unilabos.app.backend import start_backend
        from unilabos.app.runtime_topology import publish_edge_runtime_ready_signal

        def fail(*args):
            publish_edge_runtime_ready_signal()
            threading.Thread(target=threading.Event().wait, daemon=False).start()
            {failure}

        module = types.ModuleType('unilabos.ros.runtime')
        module.main = fail
        sys.modules[module.__name__] = module
        backend = start_backend('ros', None, None)
        backend.join()
    """)
    child = _run_child(script, ready)
    assert child.returncode == 1, (child.stdout, child.stderr)
    assert not ready.exists()
    assert "backend_thread" in child.stderr


def test_executor_failure_exits_while_backend_is_blocked(tmp_path: Path) -> None:
    """执行器失败必须直接终止进程，不能依赖 backend 的下一次存活轮询。"""
    ready = tmp_path / "ready.json"
    child = _run_child(
        """
        import threading
        from unilabos.app.runtime_thread_lifecycle import (
            publish_edge_runtime_ready_signal, start_runtime_thread,
        )

        def broken_executor():
            raise RuntimeError('执行器致命异常')

        def blocked_backend():
            publish_edge_runtime_ready_signal()
            threading.Thread(target=threading.Event().wait, daemon=False).start()
            start_runtime_thread(target=broken_executor, name='host_executor_thread')
            threading.Event().wait()

        start_runtime_thread(target=blocked_backend, name='backend_thread').join()
    """,
        ready,
    )
    assert child.returncode == 1, child.stderr
    assert not ready.exists()
    assert "host_executor_thread" in child.stderr
    assert "RuntimeError: 执行器致命异常" in child.stderr


def test_explicit_shutdown_keeps_normal_exit_and_cannot_republish_ready(
    tmp_path: Path,
) -> None:
    """正常退出先撤销 readiness，后续初始化回调也不能恢复它。"""
    ready = tmp_path / "ready.json"
    child = _run_child(
        """
        import os
        import threading
        from pathlib import Path
        from unilabos.app.runtime_thread_lifecycle import (
            begin_runtime_shutdown, publish_edge_runtime_ready_signal,
            start_runtime_thread,
        )

        release = threading.Event()
        running = threading.Event()
        def backend():
            publish_edge_runtime_ready_signal()
            running.set()
            release.wait()
            publish_edge_runtime_ready_signal()

        thread = start_runtime_thread(target=backend, name='backend_thread')
        assert running.wait(2)
        assert Path(os.environ['UNILABOS_EDGE_READY_FILE']).exists()
        begin_runtime_shutdown()
        release.set()
        thread.join(2)
        assert not thread.is_alive()
    """,
        ready,
    )
    assert child.returncode == 0, child.stderr
    assert not ready.exists()


def test_new_backend_generation_does_not_inherit_old_shutdown(tmp_path: Path) -> None:
    """旧代延迟退出可被忽略，新代关键线程仍必须保留失败退出保护。"""
    ready = tmp_path / "ready.json"
    child = _run_child(
        """
        import sys
        import threading
        import types
        from unilabos.app.backend import start_backend
        from unilabos.app.runtime_thread_lifecycle import (
            begin_runtime_shutdown, publish_edge_runtime_ready_signal,
        )

        old_release = threading.Event()
        def old_backend(*args):
            old_release.wait()
            raise RuntimeError('已停止旧代的延迟异常')

        module = types.ModuleType('unilabos.ros.runtime')
        module.main = old_backend
        sys.modules[module.__name__] = module
        old = start_backend('ros', None, None)
        begin_runtime_shutdown()

        def new_backend(*args):
            publish_edge_runtime_ready_signal()
            old_release.set()
            old.join(2)
            assert not old.is_alive()
            raise RuntimeError('新代致命异常')

        module.main = new_backend
        start_backend('ros', None, None).join()
    """,
        ready,
    )
    assert child.returncode == 1, child.stderr
    assert not ready.exists()
    assert "RuntimeError: 新代致命异常" in child.stderr
    assert "已停止旧代的延迟异常" not in child.stderr


@pytest.mark.skipif(not hasattr(signal, "pthread_kill"), reason="需要 POSIX 线程信号")
@pytest.mark.parametrize("signal_name", ["SIGTERM", "SIGINT"])
def test_real_ros_signal_shutdown_cannot_be_misclassified_as_fatal(
    tmp_path: Path, signal_name: str
) -> None:
    """真实设备运行时初始化和 ROS 执行器收到信号时，必须先由应用关闭再正常退出。"""
    ready = tmp_path / "ready.json"
    child = _run_child(
        f"""
        import os
        import signal
        import sys
        import threading
        import time
        import types
        from pathlib import Path
        import rclpy
        from unilabos.app.backend import start_backend
        from unilabos.app.process_shutdown import install_host_shutdown_handlers
        from unilabos.app.runtime_thread_lifecycle import runtime_shutdown_requested
        from unilabos.ros import runtime as runtime

        # 只替换设备、网络和业务关闭；实际 rclpy、信号处理和线程监督不替换。
        runtime.prepare_ros_logging = lambda args, **kwargs: args
        runtime.HostNode = lambda *args, **kwargs: None
        runtime.BasicConfig.process_role = 'edge_runtime'
        for module_name, operation in [
            ('unilabos.package_manager', 'close_workspace_product_lifecycle'),
            ('unilabos.workflow.composition', 'shutdown_workflow_runtime'),
            ('unilabos.app.runtime_storage', 'close_runtime_storage_session'),
        ]:
            module = types.ModuleType(module_name)
            setattr(module, operation, lambda: None)
            sys.modules[module_name] = module

        class Client:
            def stop(self):
                assert runtime_shutdown_requested()
                print('APPLICATION_SHUTDOWN', flush=True)

        install_host_shutdown_handlers([Client()], runtime_shutdown=lambda: None)
        graph = types.SimpleNamespace(root_nodes=[])
        backend = start_backend('ros', graph, None, visual='disable')
        deadline = time.monotonic() + 3
        while not Path(os.environ['UNILABOS_EDGE_READY_FILE']).exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        executor_name = 'host_executor_thread'
        executor_thread = next(t for t in threading.enumerate() if t.name == executor_name)

        def send_signal():
            time.sleep(0.03)
            # 信号交给 ROS 线程，固定原生 handler 先于主线程 Python handler 的交错。
            signal.pthread_kill(executor_thread.ident, signal.{signal_name})

        threading.Thread(target=send_signal, daemon=True).start()
        try:
            time.sleep(0.3)
            raise AssertionError('应用没有收到正常关闭信号')
        except SystemExit as result:
            assert result.code == 0
            assert not rclpy.ok()
            backend.join(2)
            assert not backend.is_alive()
            print('ROS_SHUTDOWN_COMPLETE', flush=True)
        """,
        ready,
    )
    assert child.returncode == 0, (child.stdout, child.stderr)
    assert "APPLICATION_SHUTDOWN" in child.stdout
    assert "ROS_SHUTDOWN_COMPLETE" in child.stdout
    assert "Runtime 关键线程" not in child.stderr
    assert not ready.exists()
