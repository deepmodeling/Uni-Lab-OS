"""DoraDeviceNode：把任意 Uni-Lab 设备驱动（纯 Python 类）封装成一个 dora 节点。

它扮演驱动所期望的「ROS 节点」的鸭子类型替身：
  - 提供 `sleep()` / `lab_logger()`，让驱动原样运行（驱动内部 `self._ros_node.sleep(...)`）；
  - 自动内省驱动：`@property` -> 状态；公开 `async def` -> 动作；
  - 在 dora 事件循环里：定时发布状态；收到命令后即时 ack 并异步执行驱动动作。

所有 dora `send_output` 只在 dora 事件线程内发生；驱动协程运行在独立 asyncio 线程，
通过线程安全队列把 feedback/result 交回 dora 线程发送。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import queue
import threading
from typing import Any, Callable, Dict, List, Optional

from unilabos.dora.serialization import decode, encode, make_pad, now_ns

logger = logging.getLogger("unilabos.dora")


def introspect_driver(driver: Any) -> (List[str], Dict[str, Callable]):  # type: ignore[valid-type]
    """内省驱动实例，返回 (状态属性名列表, 动作方法字典)。

    状态：类上定义的、非下划线开头的 property。
    动作：实例上非下划线开头的 async 方法，排除生命周期方法。
    """
    status_props: List[str] = [
        name
        for name, _ in inspect.getmembers(type(driver), lambda o: isinstance(o, property))
        if not name.startswith("_")
    ]
    skip = {"initialize", "cleanup", "post_init"}
    action_methods: Dict[str, Callable] = {}
    for name in dir(driver):
        if name.startswith("_") or name in skip:
            continue
        try:
            attr = getattr(driver, name)
        except Exception:
            continue
        if inspect.iscoroutinefunction(attr):
            action_methods[name] = attr
    return status_props, action_methods


class DoraDeviceNode:
    """把一个设备驱动实例接入 dora 数据流。"""

    # dora 输出通道名（需与 dataflow 中声明一致）
    OUTPUTS = ("status", "reply", "stream", "action_result")

    def __init__(self, driver: Any, device_id: str):
        self.driver = driver
        self.device_id = device_id
        self._logger = logging.getLogger(f"unilabos.dora.{device_id}")
        self.status_props, self.action_methods = introspect_driver(driver)

        # 驱动协程运行的 asyncio 线程
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._run_loop, name=f"dora-driver-{device_id}", daemon=True
        )
        # 由异步动作产出、待 dora 线程发送的出站消息
        self._outbound: "queue.Queue" = queue.Queue()
        self._seq = 0
        self._node = None  # dora Node
        self._burst = None  # 当前 burst 状态（credit 流控）

    # ------------------------------------------------------------------ #
    # 驱动侧鸭子接口（模拟 ROS 节点）
    # ------------------------------------------------------------------ #
    async def sleep(self, rel_time: float, callback_group=None) -> None:
        await asyncio.sleep(rel_time)

    def lab_logger(self) -> logging.Logger:
        return self._logger

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _snapshot(self) -> Dict[str, Any]:
        snap: Dict[str, Any] = {}
        for prop in self.status_props:
            try:
                value = getattr(self.driver, prop)
                # 仅保留可 JSON 序列化的基础类型
                if isinstance(value, (str, int, float, bool, list, dict)) or value is None:
                    snap[prop] = value
            except Exception:
                snap[prop] = None
        return snap

    def _publish_status(self, node) -> None:
        msg = {
            "op": "status",
            "seq": self._next_seq(),
            "t_send_ns": now_ns(),
            "device": self.device_id,
            "body": self._snapshot(),
        }
        node.send_output("status", encode(msg))

    def _drain_outbound(self, node) -> None:
        while True:
            try:
                output_id, payload = self._outbound.get_nowait()
            except queue.Empty:
                break
            node.send_output(output_id, encode(payload))

    def _send_burst_chunk(self, node) -> None:  # 兼容占位，当前 burst 采用直连 flood
        return


    async def _run_action(self, name: str, kwargs: Dict[str, Any], seq: int) -> None:
        t0 = now_ns()
        ok = True
        try:
            result = await self.action_methods[name](**kwargs)
        except Exception as exc:  # 动作执行失败不应打断节点
            result = f"{type(exc).__name__}: {exc}"
            ok = False
        self._outbound.put(
            (
                "action_result",
                {
                    "op": "action_result",
                    "seq": seq,
                    "device": self.device_id,
                    "action": name,
                    "ok": ok,
                    "result": result if isinstance(result, (str, int, float, bool, type(None))) else str(result),
                    "t_done_ns": now_ns(),
                    "dur_ns": now_ns() - t0,
                },
            )
        )

    def _handle_cmd(self, node, msg: Dict[str, Any]) -> None:
        op = msg.get("op")
        seq = msg.get("seq", 0)
        if op == "echo":
            # 纯传输往返：立即原路回显
            node.send_output(
                "reply",
                encode(
                    {
                        "op": "reply",
                        "seq": seq,
                        "t_send_ns": msg.get("t_send_ns"),
                        "t_echo_ns": now_ns(),
                        "device": self.device_id,
                    }
                ),
            )
        elif op == "burst":
            # 尽快连发 count 条定长消息（flood），块末在同一 stream 通道发结束标记。
            # dora 本机传输为 best-effort/latest-value + 浅缓冲：生产快于消费时会丢中间帧，
            # 故此处测的是「消费端可持续接收速率」(host 侧按首末到达窗口计算)。
            body = msg.get("body", {})
            count = int(body.get("count", 2000))
            size = int(body.get("size", 0))
            pad = make_pad(size)
            for _ in range(count):
                node.send_output(
                    "stream",
                    encode({"op": "stream", "t_send_ns": now_ns(), "device": self.device_id, "pad": pad}),
                )
            node.send_output(
                "stream",
                encode({"op": "burst_end", "seq": seq, "sent": count, "device": self.device_id}),
            )
        elif op == "action":
            body = msg.get("body", {})
            name = body.get("name")
            kwargs = body.get("kwargs", {})
            # 命令路径 RTT：立即 ack
            node.send_output(
                "reply",
                encode(
                    {
                        "op": "ack",
                        "seq": seq,
                        "t_send_ns": msg.get("t_send_ns"),
                        "t_echo_ns": now_ns(),
                        "device": self.device_id,
                        "action": name,
                        "known": name in self.action_methods,
                    }
                ),
            )
            if name in self.action_methods:
                asyncio.run_coroutine_threadsafe(
                    self._run_action(name, kwargs, seq), self._loop
                )

    # ------------------------------------------------------------------ #
    # 主入口
    # ------------------------------------------------------------------ #
    def run(self) -> None:
        from dora import Node

        self._loop_thread.start()
        self._node = node = Node()

        # 驱动生命周期：post_init + initialize
        if hasattr(self.driver, "post_init"):
            try:
                self.driver.post_init(self)
            except Exception as exc:
                self._logger.warning(f"post_init 失败: {exc}")
        if hasattr(self.driver, "initialize") and inspect.iscoroutinefunction(self.driver.initialize):
            try:
                asyncio.run_coroutine_threadsafe(self.driver.initialize(), self._loop).result(timeout=30)
            except Exception as exc:
                self._logger.warning(f"initialize 失败: {exc}")

        self._logger.info(
            f"dora 节点就绪 device={self.device_id} "
            f"status={len(self.status_props)} actions={len(self.action_methods)}"
        )

        for event in node:
            etype = event["type"]
            if etype == "INPUT":
                eid = event["id"]
                if eid == "tick":
                    self._publish_status(node)
                    self._drain_outbound(node)
                elif eid == "cmd":
                    try:
                        self._handle_cmd(node, decode(event["value"]))
                    except Exception as exc:
                        self._logger.warning(f"命令处理失败: {exc}")
            elif etype in ("STOP", "ERROR"):
                break

        # 清理
        if hasattr(self.driver, "cleanup") and inspect.iscoroutinefunction(self.driver.cleanup):
            try:
                asyncio.run_coroutine_threadsafe(self.driver.cleanup(), self._loop).result(timeout=10)
            except Exception:
                pass
        self._loop.call_soon_threadsafe(self._loop.stop)
