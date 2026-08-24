"""unilab doctor：host-slave 组网分层诊断（TCP 通路 → ROS 收发）。

四个子诊断，逐层定位「设备不上线」到底卡在哪一层：

- ``net``          纯 TCP：连 host:port → hello 握手 → N 次 ping 测 RTT（无 ROS 依赖）
- ``talker``       诊断发布端：套用组网（指定 ip 单播或 host 下发）后周期发探测消息
- ``listener``     诊断接收端：订阅探测消息，统计丢包/时延，给出 OK/FAIL 判定
- ``fake-device``  假设备：以设备身份发探测消息 + 检查 host 的注册服务是否可见，
                   验证「host 能不能看见一台设备」而不动真实硬件

组网来源二选一（可叠加，手动参数优先）：

- ``--hostlink_addr ip[:port]``  连 host 经 hello 拿组网信息（domain/peers/降级档位）
- ``--peer ip[,ip...]``          直接指定对端 ip（ROS_STATIC_PEERS），配合
  ``--discovery OFF`` 即为纯单播定向诊断，完全不依赖组播与 host 在线

典型用法::

    # host 机器（192.168.1.10）上：
    unilab doctor listener --peer 192.168.1.20 --discovery OFF --ros_domain_id 42
    # slave 机器（192.168.1.20）上：
    unilab doctor talker --peer 192.168.1.10 --discovery OFF --ros_domain_id 42
    # 或者两边都直接用 host 下发的组网：
    unilab doctor talker --hostlink_addr 192.168.1.10
"""

from __future__ import annotations

import json
import socket
import statistics
import time
import uuid as uuid_mod
from typing import Any, Dict, List, Optional, Tuple

from unilabos.hostlink.protocol import (
    ActionType,
    LineReader,
    LinkError,
    new_request,
    read_message,
    send_message,
)
from unilabos.hostlink.ros_assist import (
    RosNetworkInfo,
    apply_ros_network_env,
    use_connected_host,
)

DEFAULT_TOPIC = "/unilab_doctor"


# ─────────────────────────────────────────────────────────────
# 第一层：TCP 通路探测（无 ROS 依赖）
# ─────────────────────────────────────────────────────────────


def probe_network(host: str, port: int, ping_count: int = 5, timeout: float = 3.0) -> Dict[str, Any]:
    """对 host:port 做一次分层 TCP 探测，返回结构化报告。

    verdict: ok（全通）/ tcp_fail（连不上：网络/防火墙/host 未启动）/
    hello_fail（TCP 通但协议握手失败：端口被其它服务占用或版本不符）。
    """
    report: Dict[str, Any] = {
        "target": f"{host}:{port}",
        "tcp_connect": {"ok": False, "ms": None},
        "hello": {"ok": False, "ms": None, "host_name": "", "ros": None},
        "ping": {"count": ping_count, "ok": 0, "min_ms": None, "avg_ms": None, "max_ms": None},
        "verdict": "tcp_fail",
    }
    t0 = time.perf_counter()
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        report["error"] = str(exc)
        return report
    report["tcp_connect"] = {"ok": True, "ms": round((time.perf_counter() - t0) * 1000, 2)}

    sock.settimeout(timeout)
    reader = LineReader(sock)  # makefile 与 timeout 不兼容（见 protocol.LineReader）
    try:
        # hello 握手
        t1 = time.perf_counter()
        try:
            send_message(sock, new_request(ActionType.HELLO, data={"machine_name": "doctor", "role": "doctor"}))
            resp = read_message(reader)
        except (OSError, LinkError, socket.timeout) as exc:
            report["error"] = f"hello failed: {exc}"
            report["verdict"] = "hello_fail"
            return report
        if not resp or not resp.get("ok"):
            report["error"] = f"hello rejected: {resp.get('error') if resp else 'no response'}"
            report["verdict"] = "hello_fail"
            return report
        hello_data = resp.get("data") or {}
        report["hello"] = {
            "ok": True,
            "ms": round((time.perf_counter() - t1) * 1000, 2),
            "host_name": hello_data.get("host_name", ""),
            "ros": hello_data.get("ros"),
        }

        # ping RTT
        rtts: List[float] = []
        for _ in range(ping_count):
            t2 = time.perf_counter()
            try:
                send_message(sock, new_request(ActionType.PING))
                pong = read_message(reader)
            except (OSError, LinkError, socket.timeout):
                continue
            if pong and pong.get("ok"):
                rtts.append((time.perf_counter() - t2) * 1000)
        if rtts:
            report["ping"].update(
                ok=len(rtts),
                min_ms=round(min(rtts), 2),
                avg_ms=round(statistics.fmean(rtts), 2),
                max_ms=round(max(rtts), 2),
            )
        report["verdict"] = "ok" if rtts else "hello_fail"
        return report
    finally:
        reader.close()
        try:
            sock.close()
        except OSError:
            pass


# ─────────────────────────────────────────────────────────────
# 组网信息解析（手动 --peer 优先，其次 hello 下发）
# ─────────────────────────────────────────────────────────────


def resolve_ros_network(
    hostlink_addr: str = "",
    peers: str = "",
    ros_domain_id: Optional[int] = None,
    discovery: str = "",
    default_port: int = 7302,
) -> Tuple[RosNetworkInfo, str]:
    """汇总诊断用组网信息，返回 (info, 来源说明)。

    优先级：手动参数（--peer/--ros_domain_id/--discovery）覆盖 host 下发；
    两者都没有时返回空 info（沿用进程现有环境 = 常规组播发现）。
    """
    base = RosNetworkInfo()
    source = "environment (no override)"
    if hostlink_addr:
        host, _, port_text = hostlink_addr.partition(":")
        port = int(port_text) if port_text.strip().isdigit() else default_port
        report = probe_network(host, port, ping_count=1)
        if report["hello"]["ok"] and report["hello"].get("ros"):
            base = RosNetworkInfo.from_dict(report["hello"]["ros"])
            if base.discovery_server and base.discovery_server_managed:
                base.discovery_server = use_connected_host(
                    base.discovery_server,
                    host,
                )
            source = f"hostlink {host}:{port}"
        else:
            source = f"hostlink {host}:{port} unreachable ({report['verdict']}), manual/env only"

    manual_peers = [p.strip() for p in peers.replace(";", ",").split(",") if p.strip()]
    if manual_peers:
        base.static_peers = manual_peers
        source += " + manual peers"
    if ros_domain_id is not None:
        base.domain_id = ros_domain_id
    if discovery:
        base.automatic_discovery_range = discovery.strip().upper()
    # 指定了对端但没说降级档位：默认收紧到 OFF（纯单播定向诊断，不受组播环境干扰）
    if manual_peers and not base.automatic_discovery_range:
        base.automatic_discovery_range = "OFF"
    return base, source


# ─────────────────────────────────────────────────────────────
# 探测消息与统计（纯逻辑，可测）
# ─────────────────────────────────────────────────────────────


def make_probe_message(seq: int, sender: str) -> str:
    return json.dumps(
        {"seq": seq, "sender": sender, "sent_at": time.time()},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def parse_probe_message(text: str) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict) or "seq" not in data:
        return None
    return data


class ProbeStats:
    """listener 侧统计：按 sender 跟踪 seq 缺口 / 重复 / 单向时延（受时钟偏差影响仅供参考）。"""

    def __init__(self) -> None:
        self.received = 0
        self.duplicates = 0
        self.lost = 0
        self.latencies_ms: List[float] = []
        self._last_seq: Dict[str, int] = {}
        self._seen: Dict[str, set] = {}

    def add(self, message: Dict[str, Any], now: Optional[float] = None) -> None:
        sender = str(message.get("sender") or "?")
        seq = int(message["seq"])
        seen = self._seen.setdefault(sender, set())
        if seq in seen:
            self.duplicates += 1
            return
        seen.add(seq)
        self.received += 1
        last = self._last_seq.get(sender)
        if last is not None and seq > last + 1:
            self.lost += seq - last - 1
        self._last_seq[sender] = max(seq, last if last is not None else seq)
        sent_at = message.get("sent_at")
        if isinstance(sent_at, (int, float)):
            self.latencies_ms.append(((now or time.time()) - float(sent_at)) * 1000)

    def summary(self) -> Dict[str, Any]:
        lat = self.latencies_ms
        return {
            "received": self.received,
            "lost": self.lost,
            "duplicates": self.duplicates,
            "senders": sorted(self._last_seq),
            "latency_ms": {
                "min": round(min(lat), 2) if lat else None,
                "avg": round(statistics.fmean(lat), 2) if lat else None,
                "max": round(max(lat), 2) if lat else None,
            },
        }

    @property
    def ok(self) -> bool:
        return self.received > 0


# ─────────────────────────────────────────────────────────────
# ROS 侧：talker / listener / fake-device（rclpy 懒加载）
# ─────────────────────────────────────────────────────────────


def _setup_ros(info: RosNetworkInfo, source: str) -> None:
    applied = apply_ros_network_env(info)
    print(f"[doctor] 组网来源: {source}")
    if applied:
        print(f"[doctor] 已套用: {applied}")
    import rclpy

    if not rclpy.ok():
        try:
            rclpy.init(domain_id=info.domain_id)
        except TypeError:  # 旧版 rclpy 无 domain_id 形参
            rclpy.init()


def run_talker(
    info: RosNetworkInfo,
    source: str,
    topic: str = DEFAULT_TOPIC,
    rate_hz: float = 1.0,
    duration_s: float = 0.0,
    sender: str = "",
) -> int:
    """周期发布探测消息；duration_s<=0 表示一直发（Ctrl-C 退出）。"""
    _setup_ros(info, source)
    import rclpy
    from rclpy.node import Node
    from std_msgs.msg import String

    sender = sender or f"talker-{socket.gethostname()}-{uuid_mod.uuid4().hex[:6]}"
    node = Node(f"unilab_doctor_talker_{uuid_mod.uuid4().hex[:6]}")
    publisher = node.create_publisher(String, topic, 10)
    print(f"[doctor] talker 就绪: topic={topic} rate={rate_hz}Hz sender={sender}（Ctrl-C 结束）")
    seq = 0
    deadline = time.time() + duration_s if duration_s > 0 else None
    try:
        while rclpy.ok() and (deadline is None or time.time() < deadline):
            seq += 1
            publisher.publish(String(data=make_probe_message(seq, sender)))
            if seq % max(int(rate_hz * 5), 1) == 0 or seq == 1:
                print(f"[doctor] 已发送 {seq} 条")
            time.sleep(1.0 / max(rate_hz, 0.1))
    except KeyboardInterrupt:
        pass
    finally:
        print(f"[doctor] talker 结束，共发送 {seq} 条")
        node.destroy_node()
    return 0


def run_listener(
    info: RosNetworkInfo,
    source: str,
    topic: str = DEFAULT_TOPIC,
    duration_s: float = 0.0,
    quiet: bool = False,
) -> int:
    """订阅探测消息并统计；结束时打印判定。exit code: 0=收到消息, 2=颗粒无收。"""
    _setup_ros(info, source)
    import rclpy
    from rclpy.node import Node
    from std_msgs.msg import String

    node = Node(f"unilab_doctor_listener_{uuid_mod.uuid4().hex[:6]}")
    stats = ProbeStats()

    def on_message(msg: "String") -> None:
        parsed = parse_probe_message(msg.data)
        if parsed is None:
            return
        stats.add(parsed)
        if not quiet:
            print(
                f"[doctor] #{parsed['seq']} from {parsed.get('sender', '?')} "
                f"(累计 {stats.received}, 丢 {stats.lost})"
            )

    node.create_subscription(String, topic, on_message, 10)
    print(f"[doctor] listener 就绪: topic={topic}"
          + (f" 持续 {duration_s}s" if duration_s > 0 else "（Ctrl-C 结束）"))
    deadline = time.time() + duration_s if duration_s > 0 else None
    try:
        while rclpy.ok() and (deadline is None or time.time() < deadline):
            rclpy.spin_once(node, timeout_sec=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
    summary = stats.summary()
    print(f"[doctor] 统计: {json.dumps(summary, ensure_ascii=False)}")
    print(f"[doctor] 判定: {'OK - 双向组网可用' if stats.ok else 'FAIL - 未收到任何探测消息'}")
    if not stats.ok:
        print("[doctor] 排查建议: 1) 两端 domain_id 是否一致 2) --peer 是否互指对方 ip "
              "3) 防火墙 UDP 7400+ 端口 4) 先用 `unilab doctor net` 验证 TCP 层")
    return 0 if stats.ok else 2


def run_fake_device(
    info: RosNetworkInfo,
    source: str,
    device_id: str = "",
    rate_hz: float = 1.0,
    duration_s: float = 0.0,
    check_host_services: bool = True,
) -> int:
    """假设备诊断：以设备身份发探测消息 + 检查 host 注册服务在 ROS 图上是否可见。

    不写入任何注册数据（只读诊断），用于回答「host 能不能看见一台新设备」：
    - /node_info_update、/c2s_update_resource_tree 服务可见 → 服务发现通，真设备
      注册失败应查设备自身配置；
    - 服务不可见但 talker 消息能通 → host 未启动或 host_node 异常；
    - 全部不通 → 组网层问题（回到 doctor net / talker+listener 二分）。
    """
    _setup_ros(info, source)
    import rclpy
    from rclpy.node import Node
    from std_msgs.msg import String
    from unilabos_msgs.srv import SerialCommand  # type: ignore

    device_id = device_id or f"fake_device_{uuid_mod.uuid4().hex[:6]}"
    node = Node(device_id)
    exit_code = 0

    if check_host_services:
        print("[doctor] 检查 host 注册服务可见性…")
        for service_name in ("/node_info_update", "/c2s_update_resource_tree"):
            client = node.create_client(SerialCommand, service_name)
            visible = client.wait_for_service(timeout_sec=5.0)
            print(f"[doctor]   {service_name}: {'可见 ✓' if visible else '不可见 ✗'}")
            if not visible:
                exit_code = 3
            node.destroy_client(client)
        if exit_code == 0:
            print("[doctor] host 服务发现正常：真设备注册失败应排查设备自身（registry/graph 配置）")
        else:
            print("[doctor] host 服务不可见：host 未启动 / 域号不一致 / 组网未通（先跑 talker+listener 二分）")

    topic = f"/devices/{device_id}/doctor_status"
    publisher = node.create_publisher(String, topic, 10)
    probe_pub = node.create_publisher(String, DEFAULT_TOPIC, 10)
    print(f"[doctor] fake-device 就绪: id={device_id}，发布 {topic} 与 {DEFAULT_TOPIC}")
    seq = 0
    deadline = time.time() + duration_s if duration_s > 0 else None
    try:
        while rclpy.ok() and (deadline is None or time.time() < deadline):
            seq += 1
            payload = make_probe_message(seq, device_id)
            publisher.publish(String(data=payload))
            probe_pub.publish(String(data=payload))
            time.sleep(1.0 / max(rate_hz, 0.1))
    except KeyboardInterrupt:
        pass
    finally:
        print(f"[doctor] fake-device 结束，共发送 {seq} 条")
        node.destroy_node()
    return exit_code


# ─────────────────────────────────────────────────────────────
# CLI 入口（unilab doctor <net|talker|listener|fake-device>）
# ─────────────────────────────────────────────────────────────


def run_doctor(args: Dict[str, Any]) -> int:
    """由 unilab CLI 调度；args 来自 argparse（见 app/main.py doctor 子命令）。"""
    role = args.get("doctor_command") or "net"
    hostlink_addr = (args.get("hostlink_addr") or "").strip()
    default_port = 7302

    if role == "net":
        if not hostlink_addr:
            print("[doctor] net 需要 --hostlink_addr host_ip[:port]")
            return 1
        host, _, port_text = hostlink_addr.partition(":")
        port = int(port_text) if port_text.strip().isdigit() else default_port
        report = probe_network(host, port, ping_count=int(args.get("count") or 5))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        verdict_help = {
            "ok": "TCP 通路正常；若 ROS 仍不通，用 talker/listener 二分 DDS 层",
            "tcp_fail": "TCP 连不上：检查 host 是否启动、ip/端口、防火墙",
            "hello_fail": "TCP 通但握手失败：端口被其它服务占用或版本不符",
        }
        print(f"[doctor] {verdict_help.get(report['verdict'], report['verdict'])}")
        return 0 if report["verdict"] == "ok" else 2

    info, source = resolve_ros_network(
        hostlink_addr=hostlink_addr,
        peers=str(args.get("peer") or ""),
        ros_domain_id=args.get("ros_domain_id"),
        discovery=str(args.get("discovery") or ""),
        default_port=default_port,
    )
    topic = str(args.get("topic") or DEFAULT_TOPIC)
    rate = float(args.get("rate") or 1.0)
    duration = float(args.get("duration") or 0.0)

    if role == "talker":
        return run_talker(info, source, topic=topic, rate_hz=rate, duration_s=duration)
    if role == "listener":
        return run_listener(info, source, topic=topic, duration_s=duration,
                            quiet=bool(args.get("quiet")))
    if role == "fake-device":
        return run_fake_device(
            info, source,
            device_id=str(args.get("device_id") or ""),
            rate_hz=rate, duration_s=duration,
            check_host_services=not bool(args.get("no_service_check")),
        )
    print(f"[doctor] 未知子命令: {role}（可用: net / talker / listener / fake-device）")
    return 1


__all__ = [
    "DEFAULT_TOPIC",
    "ProbeStats",
    "make_probe_message",
    "parse_probe_message",
    "probe_network",
    "resolve_ros_network",
    "run_doctor",
    "run_fake_device",
    "run_listener",
    "run_talker",
]
