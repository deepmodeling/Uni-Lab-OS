"""在实际 rclpy 上复现等待队列取到已取消或销毁定时器的竞态。"""

from __future__ import annotations

import asyncio
import time
from threading import Event, Thread

import pytest
from rclpy.context import Context
from rclpy.executors import MultiThreadedExecutor
from rclpy.impl.implementation_singleton import rclpy_implementation as _rclpy
from rclpy.node import Node

from unilabos.ros.executor import RuntimeExecutor, ensure_executor_thread_running


@pytest.fixture
def ros_executor():
    context = Context()
    context.init(args=[])
    node = Node("runtime_executor_timer_test", context=context)
    executor = RuntimeExecutor(context=context, num_threads=2)
    try:
        yield node, executor
    finally:
        executor.shutdown(timeout_sec=1)
        node.destroy_node()
        context.shutdown()


def test_destroyed_timer_in_wait_queue_does_not_run_callback(ros_executor):
    node, executor = ros_executor
    calls = []
    timer = node.create_timer(0.01, lambda: calls.append("called"))
    node.destroy_timer(timer)
    # executor 已经选中 timer 后，动作协程释放了它；等价于现场的取出顺序。
    taken = executor._take_timer(timer)
    asyncio.run(executor._execute_timer(timer, taken))
    assert calls == []


def test_canceled_timer_in_wait_queue_does_not_run_callback(ros_executor):
    """已进入等待队列的真实定时器被取消后，过期回调不能终止执行器。"""
    node, executor = ros_executor
    calls = []
    timer = node.create_timer(0.01, lambda: calls.append("called"))
    timer.cancel()
    taken = executor._take_timer(timer)
    asyncio.run(executor._execute_timer(timer, taken))
    assert calls == []


def test_cancel_after_ready_handler_is_queued_then_reset(ros_executor):
    """真实等待队列已经选中回调后取消；reset 后的新一轮仍可执行。"""
    node, executor = ros_executor
    calls = []
    timer = node.create_timer(0.01, lambda: calls.append("called"))
    executor.add_node(node)
    handler, selected_timer, _ = executor.wait_for_ready_callbacks(timeout_sec=2)
    assert selected_timer is timer
    timer.cancel()
    handler()
    assert handler.done()
    assert handler.result() is None
    assert calls == []

    timer.reset()
    next_handler, selected_timer, _ = executor.wait_for_ready_callbacks(timeout_sec=2)
    assert selected_timer is timer
    next_handler()
    assert next_handler.done()
    assert next_handler.result() is None
    assert calls == ["called"]


def test_reset_between_cancel_error_and_handler_does_not_revive_stale_callback(
    ros_executor, monkeypatch
):
    """真实 call_timer 取消失败后并发 reset，不得误执行那次过期回调。"""
    node, executor = ros_executor
    calls = []
    timer = node.create_timer(0.01, lambda: calls.append("called"))
    timer.cancel()
    failed = Event()
    reset = Event()
    take_timer = MultiThreadedExecutor._take_timer

    def take_with_concurrent_reset(executor, selected_timer):
        try:
            return take_timer(executor, selected_timer)
        except _rclpy.RCLError:
            # 保留真实 RCL 错误，只固定另一个线程 reset 的交错位置。
            failed.set()
            assert reset.wait(timeout=2)
            raise

    def reset_after_failure():
        if failed.wait(timeout=2):
            timer.reset()
            reset.set()

    monkeypatch.setattr(MultiThreadedExecutor, "_take_timer", take_with_concurrent_reset)
    thread = Thread(target=reset_after_failure, daemon=True)
    thread.start()
    try:
        taken = executor._take_timer(timer)
        asyncio.run(executor._execute_timer(timer, taken))
        assert reset.is_set()
        assert not timer.is_canceled()
        assert calls == []

        taken = executor._take_timer(timer)
        asyncio.run(executor._execute_timer(timer, taken))
        assert calls == ["called"]
    finally:
        thread.join(timeout=2)
        assert not thread.is_alive()


@pytest.mark.parametrize("canceled", [False, True])
def test_other_timer_take_errors_still_propagate(ros_executor, monkeypatch, canceled):
    """即使定时器已取消，其他 RCL 故障也必须按原异常传播。"""
    node, executor = ros_executor
    timer = node.create_timer(0.01, lambda: None)
    if canceled:
        timer.cancel()
    failure = _rclpy.RCLError("failed to call timer: timer is invalid")

    def fail(executor, timer):
        raise failure

    monkeypatch.setattr(MultiThreadedExecutor, "_take_timer", fail)
    with pytest.raises(_rclpy.RCLError) as caught:
        executor._take_timer(timer)
    assert caught.value is failure


def test_live_timer_callback_still_executes(ros_executor):
    node, executor = ros_executor
    calls = []
    timer = node.create_timer(0.01, lambda: calls.append("called"))
    taken = executor._take_timer(timer)
    asyncio.run(executor._execute_timer(timer, taken))
    assert calls == ["called"]


@pytest.mark.parametrize(
    "failure",
    [
        ValueError("driver failure"),
        _rclpy.RCLError("failed to call timer: timer is canceled"),
        _rclpy.RCLError("driver RCL failure"),
    ],
)
def test_driver_callback_errors_still_propagate(ros_executor, failure):
    node, executor = ros_executor

    def fail():
        raise failure

    timer = node.create_timer(0.01, fail)
    taken = executor._take_timer(timer)
    with pytest.raises(type(failure)) as caught:
        asyncio.run(executor._execute_timer(timer, taken))
    assert caught.value is failure


def test_executor_exit_is_detected():
    stop = Event()
    thread = Thread(target=stop.wait, name="test-executor")
    thread.start()
    try:
        ensure_executor_thread_running(thread)
    finally:
        stop.set()
        thread.join(timeout=1)
    with pytest.raises(RuntimeError, match="executor thread terminated"):
        ensure_executor_thread_running(thread)


def test_repeated_action_waits_keep_executor_running(ros_executor):
    """真实动作等待方法连续创建/销毁定时器，不得终止多线程 executor。"""
    from unilabos.ros.nodes.base_device_node import ROS2DeviceNode

    node, executor = ros_executor
    executor.add_node(node)

    async def wait_repeatedly():
        for _ in range(100):
            await ROS2DeviceNode.async_wait_for(node, 0.001)
        return "finished"

    completed = executor.create_task(wait_repeatedly())
    thread = Thread(target=executor.spin, name="timer-churn-executor", daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while not completed.done() and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert thread.is_alive()
        assert completed.done()
        assert completed.result() == "finished"
    finally:
        executor.shutdown(timeout_sec=2)
        thread.join(timeout=2)
