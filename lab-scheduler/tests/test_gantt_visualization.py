"""Tests for Gantt visualization helpers."""

from scheduler.visualization.gantt import _device_color, _dtype


def test_dtype_handles_new_chinese_device_names():
    assert _dtype("质谱仪 (MS/LC-MS)_0") == "质谱仪 (MS/LC-MS)"
    assert _dtype("核磁共振仪 (NMR)_0_s1") == "核磁共振仪 (NMR)"


def test_unknown_device_gets_generated_color():
    color = _device_color("质谱仪 (MS/LC-MS)")
    assert color.startswith("#")
    assert color.lower() != "#bdbdbd"
