"""按 ROS executor 的跨 Context 推进方式验证真实异步入口和线程驱动身份。"""

from __future__ import annotations

import ast
import contextvars
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from unilabos.utils.tracing import (
    attach_workflow_execution_identity,
    await_with_context,
    capture_context,
    capture_workflow_execution_identity,
    submit_with_context,
)


class RosWait:
    def __await__(self):
        yield None


def _async_entry():
    # 只隔离 ROS 硬件初始化；执行生产方法，不复制其实现。
    path = Path(__file__).resolve().parents[3] / "unilabos/ros/nodes/base_device_node.py"
    tree = ast.parse(path.read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ROS2DeviceNode")
    cls.body = [n for n in cls.body if getattr(n, "name", "") in {"safe_task_wrapper", "run_async_func"}]
    namespace = {
        "Future": Future, "Task": object,
        "capture_context": capture_context, "await_with_context": await_with_context,
        "rclpy": SimpleNamespace(get_global_executor=lambda: SimpleNamespace(create_task=lambda coro: coro)),
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])), str(path), "exec"), namespace)  # noqa: S102
    return namespace["ROS2DeviceNode"].run_async_func


@pytest.mark.parametrize("driver_fails", [False, True])
def test_interleaved_ros_tasks_keep_identity_into_driver_threads(driver_fails):
    entry = _async_entry()
    observed = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        async def action():
            observed.append(capture_workflow_execution_identity())
            await RosWait()
            def driver():
                identity = capture_workflow_execution_identity()
                if driver_fails:
                    raise ValueError(identity["node_job_uuid"])
                return identity
            return submit_with_context(pool, driver).result(timeout=2)

        coros = []
        for label in ("A", "B"):
            with attach_workflow_execution_identity(f"job-{label}", f"task-{label}"):
                coros.append(entry(action, trace_error=False))
        try:
            for coro in coros:
                assert contextvars.Context().run(coro.send, None) is None
            assert observed == [
                {"node_job_uuid": f"job-{label}", "task_uuid": f"task-{label}"}
                for label in ("A", "B")
            ]
            for label, coro in zip(("A", "B"), coros):
                with pytest.raises(StopIteration) as result:
                    contextvars.Context().run(coro.send, None)
                value = result.value.value
                if driver_fails:
                    assert isinstance(value, ValueError) and str(value) == f"job-{label}"
                else:
                    assert value == {"node_job_uuid": f"job-{label}", "task_uuid": f"task-{label}"}
                assert capture_workflow_execution_identity() == {}
        finally:
            for coro in coros:
                coro.close()
