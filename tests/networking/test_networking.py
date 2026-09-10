"""联网测试：两个真实 unilab 进程，ROS 层互相隔离，HostLink TCP 照常联网。

命题：TCP 请求通路独立于 DDS 发现——即使两个实例的 ROS 完全互相不可见
（不同域号 + 关闭组播自动发现 + 不套用 host 下发组网），slave 仍能经
HostLink 完成握手、心跳在线、物料查询。

进程拓扑（全部 127.0.0.1）::

    host  进程: ROS_DOMAIN_ID=D1, 发现=LOCALHOST  ── HostLink server :<port>
    slave 进程: ROS_DOMAIN_ID=D2, 发现=LOCALHOST, ros_assist_apply=False
                └── HostLink client ──► 127.0.0.1:<port>

隔离机制：不同域号（DDS 域隔离是数学性的，跨域不可能互见）+
LOCALHOST 发现范围（不泄漏出本机，CI 并发 job 互不干扰）。

断言：
1. slave 经 HostLink 上线（host /api/v1/hostlink/peers 可见且 online）；
2. 测试进程经同一 TCP 通路查到 host 的真实物料树（host_node）；
3. D1 图上只有 host 侧节点、无 slave 节点；D2 图上只有 slave 侧节点、
   无 host 节点（互相不发现）。

运行门槛：默认跳过（进程级慢测试），显式 ``UNILAB_NETWORKING_TEST=1`` 开启
（CI「联网测试」job 设置该变量）；依赖 rclpy 与可运行的 ``python -m unilabos``。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

import pytest

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("UNILAB_NETWORKING_TEST") != "1",
        reason="联网测试为进程级慢测试：设 UNILAB_NETWORKING_TEST=1 开启（CI 联网测试 job）",
    )
]

REPO_ROOT = Path(__file__).resolve().parents[2]
HOST_GRAPH = REPO_ROOT / "unilabos" / "test" / "experiments" / "empty_devices.json"
SLAVE_GRAPH = (
    REPO_ROOT / "unilabos" / "test" / "experiments" / "mock_devices" / "mock_pump.json"
)
SLAVE_DEVICE_ID = "MockPump1"

HOST_DOMAIN = 61
SLAVE_DOMAIN = 62
STARTUP_TIMEOUT_S = 120.0
DISCOVERY_SETTLE_S = 6.0


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _spawn_unilab(args: List[str], env_extra: Dict[str, str], cwd: Path, log_path: Path):
    """以 ``python -m unilabos`` 启动真实进程（与 CI check 同入口）。"""
    # 最小 local_config：绕过「首次使用工作目录确认」的交互 input()（子进程无 stdin）
    config_path = cwd / "local_config.py"
    if not config_path.exists():
        config_path.write_text("# 联网测试最小配置：全部走默认值\n", encoding="utf-8")
    args = ["--config", str(config_path), *args]
    env = os.environ.copy()
    # 子进程 cwd 是隔离临时目录；显式把当前工作树置于 PYTHONPATH 首位，避免
    # 开发机上另一个 editable install 抢先被导入，导致测试实际跑了旧代码。
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(
        value for value in (str(REPO_ROOT), existing_pythonpath) if value
    )
    # 干净的 ROS 组网基线：由各进程的 env_extra 精确指定
    for key in (
        "ROS_DOMAIN_ID",
        "ROS_AUTOMATIC_DISCOVERY_RANGE",
        "ROS_STATIC_PEERS",
        "ROS_DISCOVERY_SERVER",
        "ROS_SUPER_CLIENT",
    ):
        env.pop(key, None)
    env.update(env_extra)
    log_file = open(log_path, "w", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-m", "unilabos", *args],
        cwd=str(cwd),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    process._unilab_log = log_file  # type: ignore[attr-defined] - 供清理关闭
    return process


def _wait_for(predicate, timeout_s: float, interval_s: float = 1.0, desc: str = ""):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval_s)
    raise AssertionError(f"等待超时（{timeout_s}s）: {desc}")


def _http_json(url: str, timeout: float = 4.0) -> Optional[dict]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read())
    except (OSError, ValueError):
        return None


def _terminate(process) -> None:
    if process is None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)
    log = getattr(process, "_unilab_log", None)
    if log is not None:
        log.close()


def _ros_node_names(
    domain_id: int,
    settle_s: float = DISCOVERY_SETTLE_S,
    expect_substring: str = "",
    max_wait_s: float = 20.0,
) -> List[str]:
    """在指定域上开独立 rclpy Context 采样节点名（含命名空间）。

    被观察进程与探针都用 RANGE=LOCALHOST（本机内可互见、不出网卡）；
    「互相不发现」由域号隔离保证（跨域不可能互见），与发现范围无关。
    expect_substring 非空时最多等 max_wait_s 直到该名字出现（发现是异步的）。
    """
    import rclpy
    from rclpy.context import Context
    from rclpy.node import Node

    saved = {k: os.environ.get(k) for k in ("ROS_AUTOMATIC_DISCOVERY_RANGE", "ROS_STATIC_PEERS")}
    os.environ["ROS_AUTOMATIC_DISCOVERY_RANGE"] = "LOCALHOST"
    os.environ["ROS_STATIC_PEERS"] = "127.0.0.1"  # 主动单播敲门，规避 loopback 组播不可靠（WSL2）
    context = Context()
    rclpy.init(context=context, domain_id=domain_id)
    try:
        probe = Node(f"unilab_net_probe_{domain_id}", context=context)
        try:
            deadline = time.time() + (max_wait_s if expect_substring else settle_s)
            names: List[str] = []
            while time.time() < deadline:
                names = [
                    f"{namespace.rstrip('/')}/{name}"
                    for name, namespace in probe.get_node_names_and_namespaces()
                ]
                if expect_substring and any(expect_substring in n for n in names):
                    time.sleep(1.0)  # 再留一拍，收全同域其它节点
                    names = [
                        f"{namespace.rstrip('/')}/{name}"
                        for name, namespace in probe.get_node_names_and_namespaces()
                    ]
                    break
                time.sleep(0.5)
            return names
        finally:
            probe.destroy_node()
    finally:
        rclpy.shutdown(context=context)
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture(scope="module")
def duo(tmp_path_factory):
    """host + slave 双进程（ROS 隔离、HostLink 互联），module 级复用。"""
    pytest.importorskip("rclpy")
    assert HOST_GRAPH.exists(), f"缺少 Host 空设备图: {HOST_GRAPH}"
    assert SLAVE_GRAPH.exists(), f"缺少 Slave 虚拟设备图: {SLAVE_GRAPH}"

    link_port = _free_port()
    host_web = _free_port()
    slave_web = _free_port()
    work = tmp_path_factory.mktemp("unilab-networking")
    (work / "host").mkdir()
    (work / "slave").mkdir()
    host_log = work / "host.log"
    slave_log = work / "slave.log"

    common_args = [
        "--backend",
        "ros",
        "--action_mode",
        "simulate",
        "--disable_browser",
        "--app_bridges",
        "fastapi",
    ]
    host = _spawn_unilab(
        [
            *common_args,
            "--graph",
            str(HOST_GRAPH),
            "--hostlink_addr",
            f"127.0.0.1:{link_port}",
            "--ros_domain_id",
            str(HOST_DOMAIN),
            "--port",
            str(host_web),
        ],
        env_extra={
            "ROS_AUTOMATIC_DISCOVERY_RANGE": "LOCALHOST",  # 发现不出本机
            # 本测试刻意验证 HostLink 与 ROS 隔离；不启动定向发现服务。
            "UNILABOS_HOSTLINKCONFIG_ROS_DISCOVERY_SERVER": "off",
        },
        cwd=work / "host",
        log_path=host_log,
    )
    slave = None
    try:
        # host 就绪：HostLink 端口可连
        _wait_for(
            lambda: _http_json(f"http://127.0.0.1:{host_web}/api/v1/hostlink/peers") is not None,
            STARTUP_TIMEOUT_S, desc=f"host REST 就绪（日志: {host_log}）",
        )

        slave = _spawn_unilab(
            [
                *common_args,
                "--graph",
                str(SLAVE_GRAPH),
                "--is_slave",
                "--slave_no_host",
                "--hostlink_addr",
                f"127.0.0.1:{link_port}",
                "--port",
                str(slave_web),
            ],
            env_extra={
                "ROS_DOMAIN_ID": str(SLAVE_DOMAIN),           # 与 host 不同域（隔离核心）
                "ROS_AUTOMATIC_DISCOVERY_RANGE": "LOCALHOST",  # 发现不出本机
                # 不套用 host 下发组网（隔离场景开关），HostLink 通路本身不受影响
                "UNILABOS_HOSTLINKCONFIG_ROS_ASSIST_APPLY": "false",
            },
            cwd=work / "slave",
            log_path=slave_log,
        )
        yield {
            "link_port": link_port,
            "host_web": host_web,
            "slave_web": slave_web,
            "host_log": host_log,
            "slave_log": slave_log,
        }
    finally:
        _terminate(slave)
        _terminate(host)


class TestNetworking:
    """联网测试：ROS 互相不发现 + HostLink 联网成功。"""

    def test_slave_reaches_host_over_hostlink(self, duo):
        """slave 经 TCP 上线：host peers 出现 online 的 slave 记录。"""
        def slave_online():
            body = _http_json(f"http://127.0.0.1:{duo['host_web']}/api/v1/hostlink/peers")
            if not body or body.get("role") != "host":
                return None
            online = [p for p in body["peers"] if p.get("role") == "slave" and p.get("online")]
            return online or None

        peers = _wait_for(slave_online, STARTUP_TIMEOUT_S,
                          desc=f"slave 上线（日志: {duo['slave_log']}）")
        assert peers[0]["machine_name"], "hello 应登记 slave 机器名"
        assert peers[0]["node_id"] == f"device:{SLAVE_DEVICE_ID}"
        assert peers[0]["device_ids"] == [SLAVE_DEVICE_ID]
        # slave 侧自证：客户端在线 + 明确跳过了组网套用（REST 比 HostLink 晚就绪，需等待）
        slave_body = _wait_for(
            lambda: _http_json(f"http://127.0.0.1:{duo['slave_web']}/api/v1/hostlink/peers"),
            60.0, desc="slave REST 就绪",
        )
        assert slave_body["role"] == "slave"
        assert slave_body["client"]["online"] is True
        assert slave_body["client"]["node_id"] == f"device:{SLAVE_DEVICE_ID}"
        assert slave_body["client"]["device_ids"] == [SLAVE_DEVICE_ID]
        log_text = Path(duo["slave_log"]).read_text(encoding="utf-8", errors="ignore")
        assert "ros_assist_apply=False" in log_text, "隔离开关应生效（跳过组网套用）"

    def test_material_query_over_tcp(self, duo):
        """物料通路：经同一 TCP 端口查询 host 真实资源树（id 查 + uuid 反查）。"""
        from unilabos.hostlink.client import HostLinkClient

        client = HostLinkClient("127.0.0.1", duo["link_port"], machine_name="net-test")
        try:
            assert client.connect_blocking(timeout=15)
            nodes = client.get_resource(res_id="host_node", with_children=True)
            assert nodes, "应查到 host_node 资源"
            assert nodes[0].get("id") == "host_node"
            uuid = nodes[0].get("uuid")
            assert uuid, "节点应带 uuid"
            again = client.get_resource(uuid=uuid, with_children=False)
            assert again and again[0].get("uuid") == uuid
        finally:
            client.close()

    def test_ros_graphs_are_isolated(self, duo):
        """互相不发现：D1 图无 slave 节点，D2 图无 host 节点。

        前置条件：探针能在各自域内看到本域节点（正向可见性）。个别环境
        （如 WSL2 loopback 组播不可用）探针自身发现失灵时 skip，不误报。
        """
        host_names = _ros_node_names(HOST_DOMAIN, expect_substring="host_node")
        slave_names = _ros_node_names(SLAVE_DOMAIN, expect_substring="slaveMachine")

        if not any("host_node" in name for name in host_names) or not any(
            "slaveMachine" in name for name in slave_names
        ):
            pytest.skip(
                f"本机 DDS loopback 发现受限（探针不可见本域节点），跳过图内省断言；"
                f"D{HOST_DOMAIN}={host_names} D{SLAVE_DOMAIN}={slave_names}"
            )

        assert any("host_node" in name for name in host_names), (
            f"host 应在自己域内可见: {host_names}"
        )
        assert not any("slaveMachine" in name for name in host_names), (
            f"host 域不应看到 slave: {host_names}"
        )
        assert any("slaveMachine" in name for name in slave_names), (
            f"slave 应在自己域内可见: {slave_names}"
        )
        assert not any("host_node" in name for name in slave_names), (
            f"slave 域不应看到 host: {slave_names}"
        )
