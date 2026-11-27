"""mTLS WSS 小通知：先提交收件箱，再确认；HTTP 补拉始终保持可用。"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import ssl
import threading
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import UUID, uuid4

from unilabos.app.center_client.client import CenterClient, CenterClientError, ControlSession, SessionFencedError
from unilabos.app.center_client.config import CenterClientConfig
from unilabos.app.center_client.durable import DurableCenterStore
from unilabos.app.center_client.transport import _reject_constant, _unique_object, retry_after_seconds

logger = logging.getLogger(__name__)
MAX_MESSAGE_BYTES = 16 * 1024
MAX_SEQUENCE = 2**53 - 1
NOTIFICATION_TYPES = {
    "task.available",
    "task.cancel",
    "handoff.available",
    "handoff.changed",
    "catalog.sync_requested",
}


def envelope(session: ControlSession, kind: str, payload: dict[str, Any]) -> str:
    return json.dumps({"protocol_version": "1.0", "type": kind, "message_uuid": str(uuid4()),
                      "sent_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                      "session_uuid": session.session_uuid, "session_epoch": session.session_epoch,
                      "payload": payload}, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def decode_message(raw: Any, session: ControlSession) -> dict[str, Any]:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise CenterClientError("WSS 只接受限额内的文本消息")
    try:
        body = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        if not isinstance(body, dict) or set(body) != {"protocol_version", "type", "message_uuid", "sent_at",
                                                    "session_uuid", "session_epoch", "payload"}:
            raise ValueError("信封字段无效")
        if str(UUID(body["message_uuid"])) != body["message_uuid"]:
            raise ValueError("消息身份无效")
        if (not isinstance(body["sent_at"], str) or not body["sent_at"].endswith("Z")
                or datetime.fromisoformat(body["sent_at"].replace("Z", "+00:00")).utcoffset().total_seconds() != 0):
            raise ValueError("消息时间无效")
        if (body["protocol_version"] != "1.0" or not isinstance(body["type"], str)
                or body["session_uuid"] != session.session_uuid
                or type(body["session_epoch"]) is not int or body["session_epoch"] != session.session_epoch
                or not isinstance(body["payload"], dict)):
            raise ValueError("消息会话或正文无效")
        return body
    except (ValueError, TypeError, AttributeError, KeyError) as error:
        raise CenterClientError("WSS 消息验证失败") from error


def validate_fenced(payload: dict[str, Any], session: ControlSession) -> None:
    if (set(payload) != {"reason", "new_session_uuid", "new_session_epoch"}
            or not isinstance(payload["reason"], str) or not payload["reason"].strip()
            or len(payload["reason"].encode("utf-8")) > 500
            or type(payload["new_session_epoch"]) is not int
            or not session.session_epoch <= payload["new_session_epoch"] <= MAX_SEQUENCE):
        raise CenterClientError("WSS fencing 提示无效")
    if payload["new_session_uuid"] is not None:
        try:
            if str(UUID(payload["new_session_uuid"])) != payload["new_session_uuid"]:
                raise ValueError("非规范 UUID")
        except (ValueError, TypeError, AttributeError) as error:
            raise CenterClientError("WSS fencing 会话身份无效") from error


def validate_attention_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    """校验当前事项结构；事项数据由 HTTP 提供，不用于 WSS 发送。"""

    if set(payload) != {"manual_confirmation_count", "exception_count", "items"}:
        raise CenterClientError("人工事项快照字段无效")
    for field in ("manual_confirmation_count", "exception_count"):
        if type(payload[field]) is not int or not 0 <= payload[field] <= 1000:
            raise CenterClientError("人工事项快照数量无效")
    items = payload["items"]
    if (not isinstance(items, dict) or not {"manual_confirmations", "exceptions"} <= set(items)
            or set(items) - {"manual_confirmations", "exceptions", "human_interactions"}):
        raise CenterClientError("人工事项快照明细无效")
    if any(not isinstance(items[key], list) or len(items[key]) > 1000 for key in items):
        raise CenterClientError("人工事项快照明细数量无效")
    for item in items["manual_confirmations"]:
        if not isinstance(item, dict) or not {
            "workflow_node_job_uuid", "workflow_task_uuid", "status",
            "opened_at", "deadline_at", "actions",
        }.issubset(item):
            raise CenterClientError("人工确认快照字段无效")
        if (
            item["status"] != "pending"
            or not all(
                isinstance(item[name], str) and item[name].strip()
                for name in (
                    "workflow_node_job_uuid", "workflow_task_uuid",
                    "opened_at", "deadline_at",
                )
            )
            or item["actions"] != ["approve", "reject"]
        ):
            raise CenterClientError("人工确认快照字段无效")
    for item in items.get("human_interactions", []):
        if not isinstance(item, dict) or not {
            "uuid", "job_uuid", "task_uuid", "kind", "arguments", "status",
            "result", "opened_at", "deadline_at", "decided_at",
        }.issubset(item):
            raise CenterClientError("人工交互快照字段无效")
        if (item["kind"] not in {"choose", "text", "numbers", "sketch", "review", "form"}
                or item["status"] != "pending" or not isinstance(item["arguments"], dict)
                or item["result"] is not None or item["decided_at"] is not None
                or not all(isinstance(item[name], str) and item[name].strip()
                           for name in ("uuid", "job_uuid", "task_uuid", "opened_at", "deadline_at"))):
            raise CenterClientError("人工交互快照字段无效")
        try:
            for name in ("uuid", "job_uuid", "task_uuid"):
                UUID(item[name])
        except (ValueError, TypeError, AttributeError) as error:
            raise CenterClientError("人工交互快照身份无效") from error
    for item in items["exceptions"]:
        if isinstance(item, dict) and item.get("decision_kind") in {"material_export", "handoff_reconciliation"}:
            from unilabos.center_protocol.attention import validate_task_attention
            if not validate_task_attention(item):
                raise CenterClientError("任务级物料异常快照字段无效")
            continue
        if not isinstance(item, dict) or not {
            "decision_kind", "workflow_task_uuid", "workflow_node_job_uuid",
            "revision", "status", "options", "meta_data", "opened_at",
        }.issubset(item):
            raise CenterClientError("异常快照字段无效")
        if (
            item["decision_kind"] not in {"device_intervention", "edge_node_error"}
            or type(item["revision"]) is not int
            or item["revision"] < 1
            or not isinstance(item["options"], list)
            or not isinstance(item["meta_data"], dict)
            or not isinstance(item["opened_at"], str)
            or not item["opened_at"].strip()
            or not all(
                isinstance(item[name], str) and item[name].strip()
                for name in ("workflow_task_uuid", "workflow_node_job_uuid")
            )
        ):
            raise CenterClientError("异常快照字段无效")
        if item["decision_kind"] == "device_intervention":
            if item["status"] != "open":
                raise CenterClientError("设备干预已经不再等待选择")
            if not isinstance(item.get("uuid"), str) or not item["uuid"].strip():
                raise CenterClientError("异常快照身份无效")
            if not isinstance(item.get("delivery_status"), str) or not item["delivery_status"].strip():
                raise CenterClientError("异常快照状态无效")
        else:
            if (
                not isinstance(item.get("decision_id"), str)
                or not item["decision_id"].strip()
                or item.get("stage") not in {"DECISION_REQUIRED", "MANUAL_HANDLING", "APPLYING"}
                or item["status"] not in {"open", "selected"}
                or not isinstance(item.get("error_phase"), str)
                or type(item.get("observed_error_epoch")) is not int
                or item["observed_error_epoch"] < 0
            ):
                raise CenterClientError("异常快照身份无效")
    if payload["manual_confirmation_count"] != len(items["manual_confirmations"]) + len(items.get("human_interactions", [])):
        raise CenterClientError("人工确认数量与快照明细不一致")
    if payload["exception_count"] != len(items["exceptions"]):
        raise CenterClientError("异常数量与快照明细不一致")
    return payload


class NotificationReceiver:
    """一个线程负责读写；连接故障只唤醒 HTTP，不创建或执行 Task。"""

    def __init__(self, config: CenterClientConfig, client: CenterClient, store: DurableCenterStore,
                 wake: Callable[[], None], *, attention_provider: Callable[[], dict[str, Any]] | None = None) -> None:
        self.config, self.client, self.store, self.wake = config, client, store, wake
        # 兼容构造参数只表示是否启用事项提示；通知线程不能调用完整提供器。
        self._attention_enabled = attention_provider is not None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._connection: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._catalog_pending: dict[str, Any] | None = None
        self._attention_loop: asyncio.AbstractEventLoop | None = None
        self._attention_event: asyncio.Event | None = None
        self._thread: threading.Thread | None = None
        self._public_address_acknowledged = False
        self._reported_public_url: str | None = None
        from unilabos.app.center_client.diagnostics import ConnectionStatistics

        self._statistics = ConnectionStatistics()

    def statistics(self) -> dict[str, Any]:
        return self._statistics.snapshot()

    def public_address_synced(self, value: str | None) -> bool:
        with self._lock:
            return self._public_address_acknowledged and self._reported_public_url == value

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("WSS 通知线程已启动")
        self._thread = threading.Thread(target=self._run, name="center-wss", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            loop, task = self._loop, self._task
            if loop is not None and task is not None:
                loop.call_soon_threadsafe(task.cancel)
        if self._thread is not None:
            self._thread.join(timeout=self.config.timeout_seconds + 6)
            if self._thread.is_alive():
                raise RuntimeError("WSS 通知线程尚未退出，拒绝关闭持久存储")

    def notify_catalog_changed(self, receipt: dict[str, Any]) -> None:
        """只有 HTTPS 提交成功的回执可以成为提示；合并成一个有界最新值。"""
        from unilabos.app.center_client.catalog import validate_receipt

        checked = validate_receipt(receipt, self.config.station_uuid)
        if checked is None:
            raise CenterClientError("目录提示必须来自已提交回执")
        payload = {key: checked[key] for key in ("catalog_version", "catalog_epoch_uuid", "catalog_sequence", "catalog_digest")}
        with self._lock:
            self._catalog_pending = payload

    def notify_attention_changed(self) -> None:
        """唤醒当前 WSS，仅提示中心重新读取 HTTP 当前事项。"""

        with self._lock:
            loop, event = self._attention_loop, self._attention_event
        if loop is not None and event is not None:
            loop.call_soon_threadsafe(event.set)

    def _run(self) -> None:
        # TLS 的读取与写入必须在同一个事件循环执行。同步客户端会在握手之前启动
        # SSL 接收线程，TLS 1.3 握手后的控制记录可能与首次写入发生竞争。
        asyncio.run(self._run_async())

    async def _run_async(self) -> None:
        from websockets.exceptions import InvalidStatus

        with self._lock:
            self._loop, self._task = asyncio.get_running_loop(), asyncio.current_task()
        delay = 1.0
        try:
            while not self._stop.is_set():
                session = self.client.session
                if session is None:
                    await asyncio.sleep(min(self.config.poll_seconds, 1.0))
                    continue
                retry_after = 0.0
                self._statistics.begin_attempt()
                cancelled = False
                try:
                    await self._connect(session)
                    delay = 1.0
                except asyncio.CancelledError:
                    cancelled = True
                    raise
                except SessionFencedError:
                    # 控制权撤销后仍可由原有 HTTP 证据会话补报，WSS 不自动抢占。
                    return
                except InvalidStatus as error:
                    if error.response.status_code in {429, 503}:
                        values = error.response.headers.get_all("Retry-After")
                        retry_after = (retry_after_seconds(values[0]) or 0.0) if len(values) == 1 else 0.0
                    logger.info("WSS 握手暂未成功，等待退避后重连：HTTP %s", error.response.status_code)
                except Exception as error:
                    logger.info("WSS 通知不可用，继续 HTTP 恢复：%s", type(error).__name__)
                finally:
                    if self._statistics.finish_attempt(cancelled=cancelled or self._stop.is_set()):
                        # 已建立后读写异常也应重置退避，不能沿用初连失败的长退避。
                        delay = 1.0
                    self.wake()
                pause = min(delay, self.config.max_backoff_seconds) * random.uniform(0.875, 1.125)
                await asyncio.sleep(max(pause, retry_after))
                delay = min(delay * 2, self.config.max_backoff_seconds)
        except asyncio.CancelledError:
            pass
        finally:
            with self._lock:
                self._loop, self._task = None, None

    async def _connect(self, session: ControlSession) -> None:
        from websockets.asyncio.client import connect

        context = ssl.create_default_context(cafile=str(self.config.server_ca) if self.config.server_ca else None)
        context.load_cert_chain(str(self.config.client_cert), str(self.config.client_key))
        url = self.config.endpoint.replace("https://", "wss://", 1).rstrip("/")
        url += f"/api/v1/stations/{self.config.station_uuid}/ws"
        async with connect(url, ssl=context,
                     open_timeout=self.config.timeout_seconds, close_timeout=2, max_size=MAX_MESSAGE_BYTES,
                     max_queue=16, ping_interval=None, proxy=None) as connection:
            with self._lock:
                if self._stop.is_set():
                    return
                self._connection = connection
            try:
                await self._exchange(connection, session)
            finally:
                with self._lock:
                    self._connection = None

    async def _send(self, connection: Any, message: str) -> None:
        if len(message.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise CenterClientError("WSS 消息超过大小限制")
        await asyncio.wait_for(connection.send(message), self.config.timeout_seconds)

    async def _exchange(self, connection: Any, session: ControlSession) -> None:
        cursor = self.store.cursor()
        public_url = self.config.public_http_base_url
        hello_payload = {
            "instance_uuid": self.config.instance_uuid,
            "process_uuid": session.process_uuid,
            "protocol_version": "1.0",
            "last_command_ack": cursor,
            "event_epoch_uuid": self.store.event_epoch_uuid,
            "last_event_ack": 0,
            "open_invocations": [],
        }
        if self.config.http_base_url is not None:
            hello_payload["http_base_url"] = self.config.http_base_url
        if public_url is not None:
            hello_payload["public_http_base_url"] = public_url
        await self._send(connection, envelope(session, "hello", hello_payload))
        hello = decode_message(await asyncio.wait_for(connection.recv(), self.config.timeout_seconds), session)
        fields = {"station_uuid", "instance_uuid", "session_uuid", "session_epoch", "heartbeat_seconds",
                  "server_time", "replay_from_command_sequence"}
        payload = hello["payload"]
        if (hello["type"] != "hello_ack" or set(payload) != fields
                or payload["station_uuid"] != self.config.station_uuid or payload["instance_uuid"] != self.config.instance_uuid
                or payload["session_uuid"] != session.session_uuid or type(payload["session_epoch"]) is not int
                or payload["session_epoch"] != session.session_epoch
                or type(payload["replay_from_command_sequence"]) is not int
                or payload["replay_from_command_sequence"] != cursor + 1
                or type(payload["heartbeat_seconds"]) not in (int, float)
                or not 0 < payload["heartbeat_seconds"] <= 60):
            raise CenterClientError("WSS 握手确认无效")
        self._statistics.established()
        with self._lock:
            self._reported_public_url = public_url
            self._public_address_acknowledged = True
        # 先注册唤醒，再发首次提示，避免发送期间的状态变化丢失通知。
        attention_event = asyncio.Event()
        with self._lock:
            self._attention_loop = asyncio.get_running_loop()
            self._attention_event = attention_event
        pending: dict[str, dict[str, Any]] = {}
        try:
            if self._attention_enabled:
                await self._send(connection, envelope(session, "attention.changed", {}))
            while not self._stop.is_set():
                if self.client.session != session:
                    return
                with self._lock:
                    catalog = self._catalog_pending
                    self._catalog_pending = None
                if catalog is not None:
                    await self._send(connection, envelope(session, "catalog.changed", catalog))
                receive_task = asyncio.create_task(
                    asyncio.wait_for(connection.recv(), payload["heartbeat_seconds"] * 3)
                )
                attention_task = asyncio.create_task(attention_event.wait())
                done, pending_tasks = await asyncio.wait(
                    {receive_task, attention_task}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending_tasks:
                    task.cancel()
                if attention_task in done:
                    attention_event.clear()
                    if self._attention_enabled:
                        await self._send(connection, envelope(session, "attention.changed", {}))
                    if receive_task not in done:
                        continue
                body = decode_message(receive_task.result(), session)
                kind, data = body["type"], body["payload"]
                if kind == "session.fenced":
                    validate_fenced(data, session)
                    if self.client.fence_session(session):
                        raise SessionFencedError("中心已撤销当前控制会话，停止领取")
                    return
                if kind == "ping":
                    if data:
                        raise CenterClientError("WSS 心跳包含未知字段")
                    await self._send(connection, envelope(session, "pong", {}))
                elif kind in NOTIFICATION_TYPES:
                    from unilabos.app.center_client.runtime import validate_command_page

                    sequence = data.get("command", {}).get("command_sequence")
                    if type(sequence) is not int or not 1 <= sequence <= MAX_SEQUENCE:
                        raise CenterClientError("WSS 命令序列无效")
                    validate_command_page({"commands": [data], "has_more": False, "next_sequence": sequence},
                                          self.config.station_uuid, sequence - 1)
                    if kind != data["command"]["type"]:
                        raise CenterClientError("WSS 命令类型不一致")
                    if not self.store.has_command(data["command"]["command_uuid"]):
                        from unilabos.app.center_client.capacity import snapshot

                        if snapshot(self.store, self.config.capacity)["new_claim_paused"]:
                            raise CenterClientError("工站容量不足，暂缓接收新通知并保留结果空间")
                    self.store.receive(data)
                    self.wake()
                    command = data["command"]
                    ack = {"command_uuid": command["command_uuid"], "command_sequence": sequence,
                           "command_digest": data["command_digest"], "last_contiguous_command_ack": self.store.cursor()}
                    if len(pending) >= 64 and command["command_uuid"] not in pending:
                        raise CenterClientError("WSS 确认队列已满，改由 HTTP 补拉")
                    pending[command["command_uuid"]] = ack
                    await self._send(connection, envelope(session, "command.ack", ack))
                elif kind == "command.acknowledged":
                    if set(data) != {"command_uuid", "command_sequence", "command_digest", "acknowledged_sequence"}:
                        raise CenterClientError("WSS 确认字段无效")
                    ack = pending.pop(data["command_uuid"], None)
                    if (ack is None or any(data[key] != ack[key] for key in ("command_uuid", "command_sequence", "command_digest"))
                            or type(data["acknowledged_sequence"]) is not int
                            or not 0 <= data["acknowledged_sequence"] <= MAX_SEQUENCE):
                        raise CenterClientError("WSS 确认身份不匹配")
                    self.store.acknowledge_command(data["command_uuid"])
                else:
                    raise CenterClientError("WSS 消息类型不支持，改由 HTTP 核实")
        finally:
            with self._lock:
                self._attention_loop = None
                self._attention_event = None
