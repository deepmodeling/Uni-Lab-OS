"""公开属性注解必须保持 ROS 标量类型，不能在发布时降级为文本。"""
from types import SimpleNamespace

import pytest
from std_msgs.msg import Bool, Float64, Int64, String

from unilabos.ros.nodes.base_device_node import PropertyPublisher, ROS2DeviceNode


@pytest.mark.parametrize('annotation,value,expected', [
    ('float', 42.25, Float64), ('int', 123, Int64), ('bool', True, Bool),
    ('str', 'ready', String), (float, 42.25, Float64), (Float64, 42.25, Float64),
])
def test_python_status_annotation_publishes_typed_ros_value(monkeypatch, annotation, value, expected):
    messages=[]
    publisher=SimpleNamespace(publish=messages.append)
    def create_publisher(message_type, topic, qos):
        assert message_type is expected
        return publisher
    node=SimpleNamespace(create_publisher=create_publisher, create_timer=lambda *_: None,
        device_id='probe', lab_logger=lambda: SimpleNamespace(trace=lambda *_: None))
    monkeypatch.setattr(ROS2DeviceNode, 'get_asyncio_loop', lambda: None)
    status=PropertyPublisher(node, 'value', lambda: value, annotation)
    status.publish_property()
    assert len(messages)==1
    assert isinstance(messages[0], expected)
    assert messages[0].data==value
    assert type(messages[0].data) is type(value)
