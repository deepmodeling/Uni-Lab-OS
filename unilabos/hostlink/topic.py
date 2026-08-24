"""HostLink 的 JSON topic 编解码与本地消息总线。"""

from __future__ import annotations

from array import array
import dataclasses
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, Optional, Protocol

TopicCallback = Callable[[Any], Any]
TopicEventListener = Callable[["TopicEvent"], None]
SubscriptionListener = Callable[[str, bool], None]

_logger = logging.getLogger("unilabos.hostlink.topic")


def normalize_topic(topic: str, device_id: str = "") -> str:
    """Return one canonical absolute topic name.

    Relative names follow the existing ROS device namespace convention:
    ``temperature`` on ``pump-1`` becomes ``/devices/pump-1/temperature``.
    """

    value = str(topic or "").strip().replace("\\", "/")
    if not value:
        raise ValueError("topic 不能为空")
    if not value.startswith("/"):
        owner = str(device_id or "").strip().strip("/")
        value = f"/devices/{owner}/{value}" if owner else f"/{value}"
    parts = [part for part in value.split("/") if part]
    if not parts:
        raise ValueError("topic 不能是根路径")
    return "/" + "/".join(parts)


def message_type_name(message_type: Any) -> str:
    """Describe a Python or ROS message class without importing ROS."""

    if message_type is None:
        return ""
    if isinstance(message_type, str):
        return message_type
    module = str(getattr(message_type, "__module__", "") or "")
    name = str(
        getattr(message_type, "__qualname__", "")
        or getattr(message_type, "__name__", "")
        or type(message_type).__name__
    )
    return f"{module}.{name}" if module else name


def message_to_value(value: Any) -> Any:
    """Convert a Python/Pydantic/ROS-like message into JSON-compatible data."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, type):
        return message_type_name(value)
    if isinstance(value, (bytes, bytearray, memoryview, array)):
        return [message_to_value(item) for item in value]
    if isinstance(value, Enum):
        return message_to_value(value.value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return message_to_value(dataclasses.asdict(value))
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return message_to_value(model_dump(mode="json"))
        except TypeError:
            return message_to_value(model_dump())
    legacy_dict = getattr(value, "dict", None)
    if callable(legacy_dict):
        return message_to_value(legacy_dict())
    fields_getter = getattr(value, "get_fields_and_field_types", None)
    if callable(fields_getter):
        return {
            str(name): message_to_value(getattr(value, str(name)))
            for name in fields_getter()
        }
    if isinstance(value, dict):
        return {str(key): message_to_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [message_to_value(item) for item in value]
    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, dict):
        return {
            str(name): message_to_value(item)
            for name, item in attributes.items()
            if not str(name).startswith("_")
        }
    # ROS 消息字段可能包含 numpy 数组；传输层本身不依赖 numpy，只识别其 tolist。
    # 限定模块名可以避免在任意驱动对象上调用同名方法。
    if type(value).__module__.split(".", 1)[0] == "numpy":
        to_list = getattr(value, "tolist", None)
        if callable(to_list):
            return message_to_value(to_list())
    return repr(value)


def value_to_message(message_type: Any, value: Any) -> Any:
    """Rebuild a Python/ROS-like message from JSON-compatible data."""

    if message_type in (None, Any) or not isinstance(value, dict):
        return value
    if message_type is dict:
        return value
    try:
        if isinstance(value, message_type):
            return value
    except TypeError:
        return value
    try:
        return message_type(**value)
    except (TypeError, ValueError):
        try:
            message = message_type()
        except (TypeError, ValueError):
            return value
        for name, item in value.items():
            if not hasattr(message, name):
                continue
            current = getattr(message, name)
            converted = value_to_message(type(current), item)
            try:
                setattr(message, name, converted)
            except (AttributeError, TypeError, ValueError):
                setattr(message, name, item)
        return message


@dataclass(frozen=True)
class TopicEvent:
    """一条可通过 HostLink JSON wire 传输的 topic 消息。"""

    topic: str
    value: Any
    publisher_device_id: str = ""
    message_type: str = ""
    message_id: str = ""
    published_at: float = 0.0
    retain: bool = False

    @classmethod
    def create(
        cls,
        topic: str,
        value: Any,
        *,
        publisher_device_id: str = "",
        message_type: Any = None,
        retain: bool = False,
    ) -> "TopicEvent":
        return cls(
            topic=normalize_topic(topic),
            value=message_to_value(value),
            publisher_device_id=str(publisher_device_id or ""),
            message_type=message_type_name(message_type),
            message_id=uuid.uuid4().hex,
            published_at=time.time(),
            retain=bool(retain),
        )

    def to_wire(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_wire(cls, data: Dict[str, Any]) -> "TopicEvent":
        if not isinstance(data, dict):
            raise TypeError("topic event 必须是对象")
        return cls(
            topic=normalize_topic(str(data.get("topic") or "")),
            value=message_to_value(data.get("value")),
            publisher_device_id=str(data.get("publisher_device_id") or ""),
            message_type=str(data.get("message_type") or ""),
            message_id=str(data.get("message_id") or "") or uuid.uuid4().hex,
            published_at=float(data.get("published_at") or time.time()),
            retain=bool(data.get("retain", False)),
        )


class TopicBus(Protocol):
    def publish(self, event: TopicEvent, *, forward: bool = True) -> None: ...

    def subscribe(
        self,
        topic: str,
        callback: TopicCallback,
        *,
        trigger_when_change: bool = False,
        replay_retained: bool = True,
    ) -> "TopicSubscription": ...


@dataclass
class _SubscriptionRecord:
    topic: str
    callback: TopicCallback
    trigger_when_change: bool
    has_value: bool = False
    last_value: Any = None


class TopicPublisher:
    """Small publisher handle with the same ``publish(value)`` shape as ROS."""

    def __init__(
        self,
        bus: TopicBus,
        topic: str,
        publisher_device_id: str,
        message_type: Any = None,
        *,
        retain: bool = False,
    ) -> None:
        self._bus = bus
        self.topic = normalize_topic(topic)
        self.topic_name = self.topic
        self.publisher_device_id = str(publisher_device_id or "")
        self.message_type = message_type_name(message_type)
        self.retain = bool(retain)

    def publish(self, value: Any) -> None:
        self._bus.publish(
            TopicEvent.create(
                self.topic,
                value,
                publisher_device_id=self.publisher_device_id,
                message_type=self.message_type,
                retain=self.retain,
            )
        )


class TopicSubscription:
    """Destroyable topic subscription handle."""

    def __init__(self, bus: "LocalTopicBus", token: str, topic: str) -> None:
        self._bus = bus
        self._token = token
        self.topic = topic
        self.topic_name = topic
        self._destroyed = False

    def destroy(self) -> None:
        if self._destroyed:
            return
        self._destroyed = True
        self._bus.unsubscribe(self._token)

    close = destroy


class LocalTopicBus:
    """Thread-safe exact-topic broker used by the HostLink local runtime."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subscriptions: Dict[str, _SubscriptionRecord] = {}
        self._topic_counts: Dict[str, int] = {}
        self._retained: Dict[str, TopicEvent] = {}
        self._outbound_listeners: list[TopicEventListener] = []
        self._subscription_listeners: list[SubscriptionListener] = []

    def publish(self, event: TopicEvent, *, forward: bool = True) -> None:
        if not isinstance(event, TopicEvent):
            raise TypeError("publish 需要 TopicEvent")
        callbacks: list[tuple[TopicCallback, Any]] = []
        with self._lock:
            if event.retain:
                self._retained[event.topic] = event
            for record in self._subscriptions.values():
                if record.topic != event.topic:
                    continue
                changed = (not record.has_value) or record.last_value != event.value
                record.has_value = True
                record.last_value = event.value
                if record.trigger_when_change and not changed:
                    continue
                callbacks.append((record.callback, event.value))
            outbound = tuple(self._outbound_listeners) if forward else ()
        for callback, value in callbacks:
            try:
                callback(value)
            except Exception:  # noqa: BLE001 - one subscriber must not block others
                _logger.exception("topic subscriber failed: %s", event.topic)
        for listener in outbound:
            try:
                listener(event)
            except Exception:  # noqa: BLE001 - transport failures are isolated
                _logger.exception("topic outbound listener failed: %s", event.topic)

    def subscribe(
        self,
        topic: str,
        callback: TopicCallback,
        *,
        trigger_when_change: bool = False,
        replay_retained: bool = True,
    ) -> TopicSubscription:
        if not callable(callback):
            raise TypeError("topic callback 必须可调用")
        normalized = normalize_topic(topic)
        token = uuid.uuid4().hex
        retained: Optional[TopicEvent] = None
        with self._lock:
            first = self._topic_counts.get(normalized, 0) == 0
            self._subscriptions[token] = _SubscriptionRecord(
                normalized,
                callback,
                bool(trigger_when_change),
            )
            self._topic_counts[normalized] = self._topic_counts.get(normalized, 0) + 1
            listeners = tuple(self._subscription_listeners) if first else ()
            if replay_retained:
                retained = self._retained.get(normalized)
        for listener in listeners:
            listener(normalized, True)
        if retained is not None:
            with self._lock:
                record = self._subscriptions.get(token)
                if record is not None:
                    record.has_value = True
                    record.last_value = retained.value
            try:
                callback(retained.value)
            except Exception:  # noqa: BLE001 - retained replay is isolated
                _logger.exception("topic retained replay failed: %s", normalized)
        return TopicSubscription(self, token, normalized)

    def unsubscribe(self, token: str) -> None:
        with self._lock:
            record = self._subscriptions.pop(str(token), None)
            if record is None:
                return
            remaining = self._topic_counts.get(record.topic, 1) - 1
            if remaining > 0:
                self._topic_counts[record.topic] = remaining
                listeners: tuple[SubscriptionListener, ...] = ()
            else:
                self._topic_counts.pop(record.topic, None)
                listeners = tuple(self._subscription_listeners)
        for listener in listeners:
            listener(record.topic, False)

    def subscribed_topics(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._topic_counts))

    def add_outbound_listener(self, listener: TopicEventListener) -> None:
        with self._lock:
            if listener not in self._outbound_listeners:
                self._outbound_listeners.append(listener)

    def remove_outbound_listener(self, listener: TopicEventListener) -> None:
        with self._lock:
            if listener in self._outbound_listeners:
                self._outbound_listeners.remove(listener)

    def add_subscription_listener(self, listener: SubscriptionListener) -> None:
        with self._lock:
            if listener not in self._subscription_listeners:
                self._subscription_listeners.append(listener)

    def remove_subscription_listener(self, listener: SubscriptionListener) -> None:
        with self._lock:
            if listener in self._subscription_listeners:
                self._subscription_listeners.remove(listener)

    def close(self) -> None:
        with self._lock:
            self._subscriptions.clear()
            self._topic_counts.clear()
            self._retained.clear()
            self._outbound_listeners.clear()
            self._subscription_listeners.clear()


__all__ = [
    "LocalTopicBus",
    "TopicBus",
    "TopicEvent",
    "TopicPublisher",
    "TopicSubscription",
    "message_to_value",
    "value_to_message",
    "message_type_name",
    "normalize_topic",
]
