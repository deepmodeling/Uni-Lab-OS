"""dora 通信性能基准 —— host 节点。

作为 dataflow 中的控制节点，对所有设备节点执行三类通信测量并把结果写入 JSON：

  1) echo RTT       —— 命令往返时延（host 时钟，逐个 ping-pong，纯传输开销）
  2) status latency —— 设备定时发布状态的单向时延与聚合速率（同机单调时钟可比）
  3) burst 吞吐     —— 令所有设备尽快连发定长消息，统计聚合吞吐（msg/s、MB/s）与单向时延

环境变量：
  UNILAB_DORA_DEVICES : 设备 id 列表 JSON
  UNILAB_DORA_RESULTS : 结果 JSON 输出路径
  UNILAB_DORA_BENCH   : 可选，基准参数 JSON（覆盖默认值）
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List

from unilabos.backend.dora.serialization import decode, encode, now_ns

log = logging.getLogger("unilabos.backend.dora.bench")

DEFAULTS = {
    "warmup": 200,
    "echo_count": 3000,
    "status_window_s": 3.0,
    "burst_sizes": [64, 1024, 16384],
    "burst_count": 4000,
    "burst_chunk": 500,
    "timeout_s": 30.0,
}


def _percentiles(values: List[float]) -> Dict[str, float]:
    if not values:
        return {}
    s = sorted(values)
    n = len(s)

    def pct(p: float) -> float:
        idx = min(n - 1, int(p * n))
        return s[idx]

    return {
        "n": n,
        "min": s[0],
        "p50": pct(0.50),
        "p90": pct(0.90),
        "p99": pct(0.99),
        "max": s[-1],
        "mean": sum(s) / n,
    }


class Benchmark:
    def __init__(self, node, devices: List[str], params: Dict[str, Any]):
        self.node = node
        self.devices = devices
        self.p = params
        self.results: Dict[str, Any] = {"devices": len(devices), "params": params}

        self.phase = "warmup"
        self.rr = 0  # round-robin 指针
        self.count = 0
        self.warmup_left = params["warmup"]

        # echo
        self.echo_send_ns: Dict[int, int] = {}
        self.echo_rtts: List[float] = []
        self.echo_seq = 0

        # status
        self.status_lat: List[float] = []
        self.status_recv = 0
        self.status_deadline_ns = 0

        # burst
        self.burst_sizes = list(params["burst_sizes"])
        self.burst_idx = 0
        self.burst_lat: List[float] = []
        self.burst_recv = 0
        self.burst_done = 0
        self.burst_start_ns = 0
        self.burst_end_ns = 0
        self.burst_grace_ns = 0  # 收齐所有 burst_done 后的宽限截止（容忍丢包/乱序）
        self.results["burst"] = []

        self.finished = False

    # ---------------------------------------------------------------- #
    def send(self, dev: str, msg: Dict[str, Any]) -> None:
        self.node.send_output(f"cmd_{dev}", encode(msg))

    def _next_dev(self) -> str:
        dev = self.devices[self.rr % len(self.devices)]
        self.rr += 1
        return dev

    # ---------------------------------------------------------------- #
    def start(self) -> None:
        log.info(f"基准开始：{len(self.devices)} 台设备，参数={self.p}")
        self._send_warmup()

    def _send_warmup(self) -> None:
        self.echo_seq += 1
        self.send(self._next_dev(), {"op": "echo", "seq": self.echo_seq})

    def _send_echo(self) -> None:
        self.echo_seq += 1
        self.echo_send_ns[self.echo_seq] = now_ns()
        self.send(self._next_dev(), {"op": "echo", "seq": self.echo_seq})

    def _begin_status(self) -> None:
        self.phase = "status"
        self.status_deadline_ns = now_ns() + int(self.p["status_window_s"] * 1e9)
        log.info(f"进入 status 阶段，采集 {self.p['status_window_s']}s")

    def _begin_burst(self) -> None:
        self.phase = "burst"
        if self.burst_idx >= len(self.burst_sizes):
            self._finish()
            return
        size = self.burst_sizes[self.burst_idx]
        count = self.p["burst_count"]
        self.burst_lat = []
        self.burst_recv = 0
        self.burst_done = 0
        self.burst_start_ns = now_ns()
        self.burst_end_ns = 0
        self.burst_grace_ns = 0
        log.info(f"进入 burst 阶段 size={size}B count={count} x {len(self.devices)} 设备")
        chunk = self.p.get("burst_chunk", 500)
        for dev in self.devices:
            self.send(dev, {"op": "burst", "seq": self.burst_idx, "body": {"count": count, "size": size, "chunk": chunk}})

    def _finish(self) -> None:
        self.results["echo_rtt_us"] = _percentiles(self.echo_rtts)
        self.finished = True
        out = os.environ.get("UNILAB_DORA_RESULTS")
        if out:
            with open(out, "w", encoding="utf-8") as f:
                json.dump(self.results, f, ensure_ascii=False, indent=2)
            log.info(f"结果已写入 {out}")
        log.info(f"基准完成：{json.dumps(self.results.get('echo_rtt_us', {}), ensure_ascii=False)}")

    # ---------------------------------------------------------------- #
    def on_reply(self, msg: Dict[str, Any]) -> None:
        op = msg.get("op")
        if self.phase == "warmup":
            self.warmup_left -= 1
            if self.warmup_left > 0:
                self._send_warmup()
            else:
                self.phase = "echo"
                self.count = 0
                self._send_echo()
            return
        if self.phase == "echo":
            seq = msg.get("seq")
            t0 = self.echo_send_ns.pop(seq, None)
            if t0 is not None:
                self.echo_rtts.append((now_ns() - t0) / 1000.0)  # us
            self.count += 1
            if self.count < self.p["echo_count"]:
                self._send_echo()
            else:
                self._begin_status()
            return
        # burst 阶段的完成/流控标记走 stream 通道（见 on_stream），reply 通道此阶段无消息

    def on_stream(self, msg: Dict[str, Any]) -> None:
        if self.phase != "burst":
            return
        op = msg.get("op")
        if op == "burst_end":
            self.burst_done += 1
            if self.burst_done >= len(self.devices) and self.burst_grace_ns == 0:
                self.burst_grace_ns = now_ns() + 200_000_000
            return
        # 普通数据：记录首/末到达时间，用于按「消费端接收窗口」计吞吐
        now = now_ns()
        if self.burst_recv == 0:
            self.burst_start_ns = now  # 首帧到达
        self.burst_recv += 1
        ts = msg.get("t_send_ns")
        if ts:
            self.burst_lat.append((now - ts) / 1000.0)  # us 单向
        self.burst_end_ns = now

    def on_status(self, msg: Dict[str, Any]) -> None:
        if self.phase != "status":
            return
        self.status_recv += 1
        ts = msg.get("t_send_ns")
        if ts:
            self.status_lat.append((now_ns() - ts) / 1000.0)

    def _record_burst(self) -> None:
        size = self.burst_sizes[self.burst_idx]
        end_ns = self.burst_end_ns or now_ns()
        elapsed_s = max(1e-9, (end_ns - self.burst_start_ns) / 1e9)
        total = self.burst_recv
        expected = self.p["burst_count"] * len(self.devices)
        self.results["burst"].append(
            {
                "size_bytes": size,
                "messages": total,
                "expected": expected,
                "loss_pct": round(100.0 * (expected - total) / expected, 3) if expected else 0.0,
                "elapsed_s": elapsed_s,
                "msgs_per_s": total / elapsed_s,
                "mb_per_s": total * size / elapsed_s / 1e6,
                "oneway_us": _percentiles(self.burst_lat),
            }
        )
        log.info(
            f"burst size={size}B: {total}/{expected} 收到, "
            f"{total/elapsed_s:.0f} msg/s, {total*size/elapsed_s/1e6:.1f} MB/s"
        )
        self.burst_idx += 1
        self._begin_burst()

    def on_tick(self) -> None:
        # 处理各阶段的超时/推进
        if self.phase == "status" and now_ns() >= self.status_deadline_ns:
            window = self.p["status_window_s"]
            self.results["status"] = {
                "received": self.status_recv,
                "msgs_per_s": self.status_recv / window,
                "oneway_us": _percentiles(self.status_lat),
            }
            log.info(f"status: 收到 {self.status_recv} 条，{self.status_recv/window:.0f} msg/s")
            self.burst_idx = 0
            self._begin_burst()
        elif self.phase == "burst" and self.burst_grace_ns and now_ns() >= self.burst_grace_ns:
            self._record_burst()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    devices = json.loads(os.environ.get("UNILAB_DORA_DEVICES", "[]"))
    params = dict(DEFAULTS)
    raw = os.environ.get("UNILAB_DORA_BENCH")
    if raw:
        try:
            params.update(json.loads(raw))
        except json.JSONDecodeError:
            pass

    from dora import Node

    node = Node()
    bench = Benchmark(node, devices, params)
    started = False

    for event in node:
        etype = event["type"]
        if etype == "INPUT":
            eid = event["id"]
            if eid == "tick":
                if not started:
                    started = True
                    bench.start()
                else:
                    bench.on_tick()
            elif eid.endswith("__reply"):
                bench.on_reply(decode(event["value"]))
            elif eid.endswith("__stream"):
                bench.on_stream(decode(event["value"]))
            elif eid.endswith("__status"):
                bench.on_status(decode(event["value"]))
            if bench.finished:
                break
        elif etype in ("STOP", "ERROR"):
            break


if __name__ == "__main__":
    main()
