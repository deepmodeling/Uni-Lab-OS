"""受管 Slave 的启动环境与停止方式：本机拉起的 Slave 与另一台机器上的 Slave 一样只走通信协议。

- ROS 2 受管 Slave 的 Fast DDS 只用 UDPv4，不用共享内存（被强制结束的 Slave 不会留下进程间锁，
  拖垮 Host 与其它 Slave）；用户自己配置的 Fast DDS 传输 / 配置文件原样保留；本机回环 UDP 送不了
  超过 MTU 的数据报（WSL2 镜像网络）时保留默认传输；
- Host 为自己的 ROS 节点改写的组网变量不传给 Slave，组网参数由 Slave 经 HostLink 握手领取；
- Windows 上先发 Ctrl+Break 请 Slave 有序退出，限时后才强制结束。
"""

from __future__ import annotations

import io
import os
import socket
import subprocess
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import pytest

from unilabos.app.cli.parser import build_parser
from unilabos.client.host.hostlink import network, ros_assist
from unilabos.client.host.hostlink.ros_assist import (
    FASTDDS_USER_TRANSPORT_ENV,
    NETWORK_ONLY_PROFILE_FILENAME,
    RosNetworkInfo,
)
from unilabos.client.host.hostlink.server import HostLinkServer, get_hostlink_server, set_hostlink_server
from unilabos.config.config import BasicConfig
from unilabos.server.services import device_processes as dpx


class RecordingPopen:
    """子进程桩：记下命令与环境，一直「运行」到被结束。"""

    instances: list["RecordingPopen"] = []

    def __init__(self, command: list[str], **kwargs: Any) -> None:
        self.command = command
        self.env: dict[str, str] = dict(kwargs.get("env") or {})
        self.pid = 5000 + len(RecordingPopen.instances)
        self.exit_code: int | None = None
        self.stdout = io.BytesIO(b"")
        self.signals: list[Any] = []
        RecordingPopen.instances.append(self)

    def poll(self) -> int | None:
        return self.exit_code

    def send_signal(self, sig: Any) -> None:
        self.signals.append(sig)

    def terminate(self) -> None:
        self.signals.append("terminate")
        self.exit_code = 0

    def kill(self) -> None:
        self.signals.append("kill")
        self.exit_code = -9

    def wait(self, timeout: float | None = None) -> int:
        if self.exit_code is None:
            raise subprocess.TimeoutExpired(self.command, timeout or 0)
        return self.exit_code


@pytest.fixture()
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dpx.DeviceProcessService:
    RecordingPopen.instances.clear()
    monkeypatch.setattr(dpx.subprocess, "Popen", RecordingPopen)
    monkeypatch.setattr(dpx, "STOP_GRACE_S", 0.01)
    # 回环 UDP 能送超过 MTU 的数据报（普通 Linux / Windows）；WSL2 镜像网络的情形单独覆盖
    monkeypatch.setattr(ros_assist, "loopback_carries_fragmented_udp", lambda: True)
    monkeypatch.setattr(network, "_env_before_host_policy", {})
    for name in (*FASTDDS_USER_TRANSPORT_ENV, *network.HOST_POLICY_ENV, "ROS_LOCALHOST_ONLY", "RMW_FASTRTPS_USE_QOS_FROM_XML"):
        monkeypatch.delenv(name, raising=False)
    svc = dpx.DeviceProcessService(tmp_path)
    monkeypatch.setattr(dpx, "_service", svc)
    yield svc
    svc._closing = True


def _start(service: dpx.DeviceProcessService, monkeypatch: pytest.MonkeyPatch, backend: str) -> RecordingPopen:
    monkeypatch.setattr(BasicConfig, "backend", backend)
    view = service.create(
        {"name": "反应站", "graph_nodes": [dpx.build_device_node("station_1", "virtual_stirrer")], "max_restarts": 0}
    )
    service.start(view["id"])
    return RecordingPopen.instances[-1]


def test_ros2_managed_slave_runs_network_only_transport(service, monkeypatch, tmp_path) -> None:
    proc = _start(service, monkeypatch, "ros2")
    assert proc.command[proc.command.index("--backend") + 1] == "ros2"
    assert "FASTRTPS_DEFAULT_PROFILES_FILE" not in proc.env
    assert not build_parser().parse_args(proc.command[3:]).ros_shm
    assert ros_assist.configure_slave_transport(proc.env, allow_shm=False, rmw="rmw_fastrtps_cpp") == "network"
    profile = Path(proc.env["FASTRTPS_DEFAULT_PROFILES_FILE"])
    assert profile.is_absolute() and profile.is_file()
    assert profile.name == NETWORK_ONLY_PROFILE_FILENAME
    root = ElementTree.parse(profile).getroot()
    namespace = {"p": "http://www.eprosima.com/XMLSchemas/fastRTPS_Profiles"}
    participant = root.find("p:participant", namespace)
    assert participant is not None and participant.get("is_default_profile") == "true"
    assert participant.findtext("p:rtps/p:useBuiltinTransports", namespaces=namespace) == "false"
    transport_ids = [item.text for item in participant.findall("p:rtps/p:userTransports/p:transport_id", namespace)]
    types = {
        item.findtext("p:transport_id", namespaces=namespace): item.findtext("p:type", namespaces=namespace)
        for item in root.findall("p:transport_descriptors/p:transport_descriptor", namespace)
    }
    assert [types[item] for item in transport_ids] == ["UDPv4"]
    # 不设发现方式：托管 Discovery Server 照常由 ROS_DISCOVERY_SERVER 接入；只靠 2.13+ 才认的变量覆盖不了 Humble
    assert "discovery_config" not in profile.read_text(encoding="utf-8")
    assert "FASTDDS_BUILTIN_TRANSPORTS" not in proc.env


@pytest.mark.parametrize("name", FASTDDS_USER_TRANSPORT_ENV)
def test_user_fast_dds_transport_settings_are_kept(service, monkeypatch, tmp_path, name) -> None:
    monkeypatch.setenv(name, "SHM" if name == "FASTDDS_BUILTIN_TRANSPORTS" else str(tmp_path / "mine.xml"))
    proc = _start(service, monkeypatch, "ros2")
    assert ros_assist.configure_slave_transport(proc.env, allow_shm=True, rmw="rmw_fastrtps_cpp") == "user"
    assert proc.env[name] == os.environ[name]
    if name != "FASTRTPS_DEFAULT_PROFILES_FILE":
        assert "FASTRTPS_DEFAULT_PROFILES_FILE" not in proc.env
    assert not list(tmp_path.rglob(NETWORK_ONLY_PROFILE_FILENAME))



@pytest.mark.parametrize("flag", ["--ros_shm", "--ros-shm", "--rosmem"])
def test_ros_shm_is_inherited_by_managed_slaves(service, monkeypatch, flag) -> None:
    args = build_parser().parse_args([flag])
    assert args.ros_shm
    monkeypatch.setattr(BasicConfig, "ros_shm", args.ros_shm)
    proc = _start(service, monkeypatch, "ros2")
    child = build_parser().parse_args(proc.command[3:])
    assert child.ros_shm
    assert ros_assist.configure_slave_transport(proc.env, allow_shm=child.ros_shm, rmw="rmw_fastrtps_cpp") == "shm"
    assert "FASTRTPS_DEFAULT_PROFILES_FILE" not in proc.env


@pytest.mark.parametrize(("rmw", "env"), [
    ("rmw_cyclonedds_cpp", {}),
    ("rmw_fastrtps_cpp", {"ROS_LOCALHOST_ONLY": "1"}),
    ("rmw_fastrtps_cpp", {"ROS_AUTOMATIC_DISCOVERY_RANGE": "LOCALHOST"}),
    ("rmw_fastrtps_cpp", {"RMW_FASTRTPS_USE_QOS_FROM_XML": "1"}),
])
def test_nonstandard_middleware_or_discovery_is_not_reported_as_network(rmw, env) -> None:
    original = env.copy()
    assert ros_assist.configure_slave_transport(env, allow_shm=False, rmw=rmw) == "user"
    assert env == original


def test_loopback_that_drops_fragments_keeps_fast_dds_defaults(service, monkeypatch, tmp_path) -> None:
    """WSL2 的镜像网络丢掉 127.0.0.1 上的 IP 分片：只走 UDP 时较大的消息两个方向都送不到，
    受管 Slave 保留 Fast DDS 默认传输（含共享内存）并告警；Host 自己的 ROS 组网设置照样不传下去。"""

    monkeypatch.setattr(ros_assist, "loopback_carries_fragmented_udp", lambda: False)
    monkeypatch.setenv("ROS_SUPER_CLIENT", "TRUE")
    monkeypatch.setattr(network, "_env_before_host_policy", {"ROS_SUPER_CLIENT": None})
    proc = _start(service, monkeypatch, "ros2")
    assert ros_assist.configure_slave_transport(proc.env, allow_shm=False, rmw="rmw_fastrtps_cpp") == "fallback"
    assert "FASTRTPS_DEFAULT_PROFILES_FILE" not in proc.env
    assert "ROS_SUPER_CLIENT" not in proc.env
    assert not list(tmp_path.rglob(NETWORK_ONLY_PROFILE_FILENAME))


def test_udp_loopback_probe(monkeypatch) -> None:
    assert ros_assist.udp_loopback_delivers(512) is True
    # 超过 UDP 数据报上限：发不出去，按送不到处理
    assert ros_assist.udp_loopback_delivers(70000) is False

    # 连套接字都建不了（例如文件描述符用完）：同样按送不到处理，不让受管 Slave 拉不起来
    class NoSockets:
        AF_INET = socket.AF_INET
        SOCK_DGRAM = socket.SOCK_DGRAM

        @staticmethod
        def socket(*_args: Any, **_kwargs: Any) -> Any:
            raise OSError("too many open files")

    monkeypatch.setattr(ros_assist, "socket", NoSockets)
    assert ros_assist.udp_loopback_delivers(512) is False

def test_hostlink_managed_slave_environment_has_no_dds_settings(service, monkeypatch) -> None:
    proc = _start(service, monkeypatch, "hostlink")
    assert "FASTRTPS_DEFAULT_PROFILES_FILE" not in proc.env
    assert proc.env["NO_COLOR"] == "1" and proc.env["PYTHONUNBUFFERED"] == "1"


def test_managed_slave_does_not_inherit_the_host_ros_policy(service, monkeypatch) -> None:
    """Host 为自己的节点设的组网变量（经托管 Discovery Server 做 super client）不传给受管 Slave：
    用户原来的值照旧，其余由 Slave 经 HostLink 握手领取，与别处的 Slave 一样。"""

    monkeypatch.setenv("ROS_DOMAIN_ID", "83")
    monkeypatch.setenv("ROS_STATIC_PEERS", "10.0.0.9")
    server = HostLinkServer("127.0.0.1", 0)
    previous = get_hostlink_server()
    host_network = network.HostNetworkService(
        server,
        RosNetworkInfo(domain_id=83, static_peers=["127.0.0.1"], discovery_server="127.0.0.1:19311"),
    )
    host_network.start()
    try:
        # Host 进程自己的环境：按它的组网策略改写
        assert os.environ["ROS_SUPER_CLIENT"] == "TRUE"
        assert os.environ["ROS_DISCOVERY_SERVER"] == "127.0.0.1:19311"
        assert os.environ["ROS_STATIC_PEERS"] == "127.0.0.1"
        proc = _start(service, monkeypatch, "ros2")
    finally:
        host_network.stop()
        set_hostlink_server(previous)
    assert "ROS_SUPER_CLIENT" not in proc.env and "ROS_DISCOVERY_SERVER" not in proc.env
    assert proc.env["ROS_STATIC_PEERS"] == "10.0.0.9" and proc.env["ROS_DOMAIN_ID"] == "83"


def test_windows_stop_asks_the_slave_to_exit_before_killing(service, monkeypatch) -> None:
    monkeypatch.setattr(dpx, "_CTRL_BREAK_EVENT", "CTRL_BREAK")
    proc = _start(service, monkeypatch, "ros2")
    process_id = service.list()[0]["id"]
    # 不理会 Ctrl+Break 的 Slave：宽限期过后强制结束
    service.stop(process_id, wait=True)
    assert proc.signals == ["CTRL_BREAK", "kill"]

    # Ctrl+Break 发不出去（例如 Host 没有控制台）：直接结束进程
    failing = RecordingPopen(["slave"])

    def no_console(_sig: Any) -> None:
        raise OSError("no console")

    failing.send_signal = no_console  # type: ignore[method-assign]
    dpx.request_slave_exit(failing)
    assert failing.signals == ["terminate"]

    # 只有 terminate 的进程对象（测试替身等）同样退回直接结束
    class TerminateOnly:
        def __init__(self) -> None:
            self.signals: list[str] = []

        def terminate(self) -> None:
            self.signals.append("terminate")

    bare = TerminateOnly()
    dpx.request_slave_exit(bare)  # type: ignore[arg-type]
    assert bare.signals == ["terminate"]


def test_posix_stop_sends_sigterm_first(service, monkeypatch) -> None:
    monkeypatch.setattr(dpx, "_CTRL_BREAK_EVENT", None)
    proc = _start(service, monkeypatch, "hostlink")
    service.stop(service.list()[0]["id"], wait=True)
    assert proc.signals == ["terminate"]
