from __future__ import annotations

import time

from unilabos.hostlink.local_runtime import HostLinkDriverSpec, HostLinkLocalRuntime
from unilabos.hostlink.topic import LocalTopicBus, TopicEvent
from unilabos.utils.decorator import subscribe


class RosLikeMessage:
    def __init__(self, data: int) -> None:
        self.data = data

    @staticmethod
    def get_fields_and_field_types() -> dict[str, str]:
        return {"data": "int32"}


class SourceDriver:
    def __init__(self, device_id=None, config=None) -> None:
        self.device_id = device_id
        self.publisher = None

    def post_init(self, node) -> None:
        self.publisher = node.create_publisher(RosLikeMessage, "value", 10)

    def send(self, value: int) -> int:
        self.publisher.publish(RosLikeMessage(value))
        return value


class SinkDriver:
    def __init__(self, device_id=None, config=None) -> None:
        self.device_id = device_id
        self.values: list[dict[str, int]] = []

    def post_init(self, node) -> None:
        node.create_subscription(
            RosLikeMessage,
            "/devices/source/value",
            self.values.append,
            10,
            trigger_when_change=True,
        )


class DecoratedSinkDriver:
    def __init__(self, device_id=None, config=None) -> None:
        self.device_id = device_id
        self.values: list[dict[str, int]] = []

    @subscribe(
        device_id="source",
        status_name="value",
        trigger_when_change=True,
    )
    def receive(self, value) -> None:
        self.values.append(value)


def _wait_until(predicate, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def test_hostlink_runtime_supports_ros_shaped_publish_and_subscribe() -> None:
    runtime = HostLinkLocalRuntime()
    source = runtime.add_driver(
        HostLinkDriverSpec(
            device_id="source",
            driver_class=SourceDriver,
            config={},
            action_names=("send",),
        )
    )
    sink = runtime.add_driver(
        HostLinkDriverSpec(
            device_id="sink",
            driver_class=SinkDriver,
            config={},
        )
    )
    decorated_sink = runtime.add_driver(
        HostLinkDriverSpec(
            device_id="decorated-sink",
            driver_class=DecoratedSinkDriver,
            config={},
        )
    )
    try:
        runtime.start()
        assert source.call_action("send", value=7) == 7
        source.call_action("send", value=7)
        source.call_action("send", value=8)
        assert _wait_until(
            lambda: sink.driver.values == [{"data": 7}, {"data": 8}]
        )
        assert _wait_until(
            lambda: decorated_sink.driver.values
            == [{"data": 7}, {"data": 8}]
        )
    finally:
        runtime.stop()


def test_retained_topic_replays_only_to_the_new_subscriber() -> None:
    bus = LocalTopicBus()
    first: list[int] = []
    second: list[int] = []
    bus.subscribe("/temperature", first.append)
    bus.publish(TopicEvent.create("/temperature", 25, retain=True))

    bus.subscribe("/temperature", second.append)

    assert first == [25]
    assert second == [25]
