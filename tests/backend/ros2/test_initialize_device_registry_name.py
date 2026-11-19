from types import SimpleNamespace

import pytest


initialize_device = pytest.importorskip(
    "unilabos.backend.ros2.initialize_device",
    reason="设备初始化测试依赖 Jazzy 版 unilabos_msgs",
    exc_type=ImportError,
)


def _device_config(template_name, klass=""):
    return SimpleNamespace(
        res_content=SimpleNamespace(
            template_name=template_name,
            klass=klass,
            uuid="local-device-uuid",
            config={},
        )
    )


def test_device_registry_name_is_read_from_template_name_not_class():
    """运行态只读 template_name；class 仅在图读取边界回填，不在此处兜底。"""
    with pytest.raises(initialize_device.DeviceClassInvalid, match="cannot be empty"):
        initialize_device.initialize_device_from_dict(
            "device-1", _device_config("", klass="legacy_registry_name")
        )


def test_device_class_input_must_be_registry_name_string():
    with pytest.raises(initialize_device.DeviceClassInvalid, match="registry name string"):
        initialize_device.initialize_device_from_dict(
            "device-1",
            _device_config({"module": "package.module:Driver"}),
        )


def test_device_registry_name_cannot_be_empty():
    with pytest.raises(initialize_device.DeviceClassInvalid, match="cannot be empty"):
        initialize_device.initialize_device_from_dict("device-1", _device_config("  "))


def test_registry_driver_config_must_be_internal_object(monkeypatch):
    monkeypatch.setitem(
        initialize_device.lab_registry.device_type_registry,
        "invalid-driver-config",
        {"class": "package.module:Driver"},
    )

    with pytest.raises(initialize_device.DeviceClassInvalid, match=r"registry .*\.class must be an object"):
        initialize_device.initialize_device_from_dict(
            "device-1",
            _device_config("invalid-driver-config"),
        )
