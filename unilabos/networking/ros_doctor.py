"""ROS 收发诊断；使用显式对端和本进程配置，不启动设备部署。"""

from __future__ import annotations

import json
import socket
import statistics
import sys
import time
import uuid as uuid_mod
from typing import Any, Dict, List, Optional, Tuple

from unilabos.networking.ros_discovery import (
    RosNetworkInfo,
    apply_ros_network_env,
)

DEFAULT_TOPIC = "/unilab_doctor"


# ─────────────────────────────────────────────────────────────
# 第一层：TCP 通路探测（无 ROS 依赖）
# ─────────────────────────────────────────────────────────────


def resolve_ros_network(
    peers: str = "",
    ros_domain_id: Optional[int] = None,
    discovery: str = "",
) -> Tuple[RosNetworkInfo, str]:
    """汇总诊断用组网信息，返回 (info, 来源说明)。

    手动参数（--peer/--ros_domain_id/--discovery）覆盖本进程环境；
    未指定时沿用进程已有配置。
    """
    base = RosNetworkInfo()
    source = "environment (no override)"
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
        from unilabos.ros.logging import prepare_ros_logging

        ros_args = prepare_ros_logging(sys.argv)
        try:
            rclpy.init(args=ros_args, domain_id=info.domain_id)
        except TypeError:  # 旧版 rclpy 无 domain_id 形参
            rclpy.init(args=ros_args)


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
    - /resource_get、/c2s_update_resource_tree 服务可见 → 服务发现通，真设备
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
        for service_name in ("/resource_get", "/c2s_update_resource_tree"):
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
    role = args.get("doctor_command")
    if role not in {"talker", "listener", "fake-device"}:
        print("[doctor] 请指定 talker、listener 或 fake-device")
        return 1

    info, source = resolve_ros_network(
        peers=str(args.get("peer") or ""),
        ros_domain_id=args.get("ros_domain_id"),
        discovery=str(args.get("discovery") or ""),
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
    print(f"[doctor] 未知子命令: {role}（可用: talker / listener / fake-device）")
    return 1


__all__ = [
    "DEFAULT_TOPIC",
    "ProbeStats",
    "make_probe_message",
    "parse_probe_message",
    "resolve_ros_network",
    "run_doctor",
    "run_fake_device",
    "run_listener",
    "run_talker",
]
