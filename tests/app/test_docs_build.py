"""完整文档不能缺少 ROS 动作定义，也不能把生成物写进源码。"""

from pathlib import Path
import runpy
import sys
from types import SimpleNamespace

import pytest


@pytest.fixture
def docs_config(monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))
    return runpy.run_path(str(Path(__file__).resolve().parents[2] / "docs/conf.py"))


def test_missing_ros_actions_fail_in_complete_mode(docs_config, monkeypatch, tmp_path):
    monkeypatch.setenv("UNILABOS_DOCS_ROS_PREFIX", str(tmp_path))
    monkeypatch.setenv("UNILABOS_DOCS_REQUIRE_ROS", "1")
    with pytest.raises(EnvironmentError, match="完整文档缺少"):
        docs_config["get_conda_share_dir"]("control_msgs/action")


def test_windows_conda_share_directory(docs_config, monkeypatch, tmp_path):
    target = tmp_path / "Library/share/control_msgs/action"
    target.mkdir(parents=True)
    monkeypatch.setenv("UNILABOS_DOCS_ROS_PREFIX", str(tmp_path))
    monkeypatch.setenv("UNILABOS_DOCS_REQUIRE_ROS", "1")
    assert docs_config["get_conda_share_dir"]("control_msgs/action") == target


def test_action_reference_is_generated_only_in_build_directory(docs_config, monkeypatch, tmp_path):
    src = tmp_path / "docs"
    reference = src / "developer_guide/action_includes.md"
    reference.parent.mkdir(parents=True)
    reference.write_text("源码占位页，不允许改写", encoding="utf-8")
    local_actions = tmp_path / "unilabos_msgs/action"
    local_actions.mkdir(parents=True)
    (local_actions / "Simple.action").write_text("# Simple\nstring data\n---\nbool ok\n---\n", encoding="utf-8")
    for package in ("control_msgs", "nav2_msgs"):
        ros_actions = tmp_path / "ros/share" / package / "action"
        ros_actions.mkdir(parents=True)
        (ros_actions / f"{package}.action").write_text("string target\n---\nbool ok\n---\n", encoding="utf-8")
    monkeypatch.setenv("UNILABOS_DOCS_ROS_PREFIX", str(tmp_path / "ros"))
    monkeypatch.setenv("UNILABOS_DOCS_REQUIRE_ROS", "1")
    app = SimpleNamespace(srcdir=str(src), doctreedir=str(tmp_path / "build/doctrees"))
    docs_config["generate_action_includes"](app)
    rendered = (Path(app.doctreedir) / "generated/action_includes.md").read_text(encoding="utf-8")
    assert "`Simple`" in rendered
    assert "`control_msgs`" in rendered and "`nav2_msgs`" in rendered
    assert reference.read_text(encoding="utf-8") == "源码占位页，不允许改写"
    source = [""]
    docs_config["inject_action_includes"](app, "developer_guide/action_includes", source)
    assert source == [rendered]
    assert docs_config["refresh_action_reference"](app, None, set(), set(), set()) == ["developer_guide/action_includes"]


def test_empty_ros_action_directory_is_not_a_complete_reference(docs_config, monkeypatch, tmp_path):
    for name in ("control_msgs", "nav2_msgs"):
        (tmp_path / "share" / name / "action").mkdir(parents=True)
    monkeypatch.setenv("UNILABOS_DOCS_ROS_PREFIX", str(tmp_path))
    monkeypatch.setenv("UNILABOS_DOCS_REQUIRE_ROS", "1")
    app = SimpleNamespace(srcdir=str(tmp_path / "docs"), doctreedir=str(tmp_path / "build"))
    with pytest.raises(EnvironmentError, match="完整文档缺少 ROS 动作定义"):
        docs_config["generate_action_includes"](app)
