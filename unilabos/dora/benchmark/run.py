"""dora 通信性能基准 —— 运行器。

对不同设备数量（并发规模）分别生成 dataflow 并用 `dora run` 启动，
收集 host 写出的结果，汇总为一个 results.json 并打印摘要表。

用法：
    python -m unilabos.dora.benchmark.run --counts 1 4 8 16 --out /tmp/dora_bench
"""

from __future__ import annotations

import argparse
import json
import os
import time
from typing import Any, Dict, List

from unilabos.dora import dataflow as dataflow_mod
from unilabos.dora import runtime

# 仅需 sleep 的纯 Python 虚拟驱动池（非 ROS2 专属），轮流分配给设备节点
DRIVER_POOL = [
    ("stir", "unilabos.devices.virtual.virtual_stirrer:VirtualStirrer"),
    ("heat", "unilabos.devices.virtual.virtual_heatchill:VirtualHeatChill"),
    ("cent", "unilabos.devices.virtual.virtual_centrifuge:VirtualCentrifuge"),
    ("filt", "unilabos.devices.virtual.virtual_filter:VirtualFilter"),
    ("rota", "unilabos.devices.virtual.virtual_rotavap:VirtualRotavap"),
    ("sepa", "unilabos.devices.virtual.virtual_separator:VirtualSeparator"),
]


def make_devices(n: int) -> List[Dict[str, Any]]:
    devices = []
    for i in range(n):
        prefix, driver = DRIVER_POOL[i % len(DRIVER_POOL)]
        devices.append({"id": f"{prefix}{i}", "driver": driver, "config": {}})
    return devices


def run_one(n: int, out_dir: str, bench_params: Dict[str, Any], device_tick_ms: int) -> Dict[str, Any]:
    devices = make_devices(n)
    results_path = os.path.join(out_dir, f"result_n{n}.json")
    if os.path.exists(results_path):
        os.remove(results_path)

    df = dataflow_mod.build_dataflow(
        devices,
        host_module="unilabos.dora.benchmark.host",
        device_tick_ms=device_tick_ms,
        host_tick_ms=20,
        host_env={
            "UNILAB_DORA_RESULTS": results_path,
            "UNILAB_DORA_BENCH": json.dumps(bench_params),
        },
        device_inputs_on_host=["reply", "stream", "status"],
    )
    df_path = os.path.join(out_dir, f"dataflow_n{n}.yml")
    dataflow_mod.write_dataflow(df_path, df)

    log_path = os.path.join(out_dir, f"run_n{n}.log")
    print(f"[run] N={n} 启动 dataflow -> {df_path}")
    with open(log_path, "w") as logf:
        proc = runtime.run_dataflow(df_path, stdout=logf, stderr=logf)
        deadline = time.time() + bench_params.get("timeout_s", 30.0) + 30.0
        while time.time() < deadline:
            if os.path.exists(results_path):
                time.sleep(0.5)  # 等待写盘完成
                break
            if proc.poll() is not None:
                break
            time.sleep(0.5)
        # 收尾：整组回收，避免 daemon 派生的子节点进程泄漏
        try:
            proc.wait(timeout=15)
        except Exception:
            runtime.terminate_process(proc)

    if os.path.exists(results_path):
        with open(results_path, encoding="utf-8") as f:
            return json.load(f)
    print(f"[run] N={n} 未产出结果，日志见 {log_path}")
    return {"devices": n, "error": "no result", "log": log_path}


def print_summary(all_results: List[Dict[str, Any]]) -> None:
    print("\n================ dora 本机通信性能摘要 ================")
    print(f"{'设备数':>6} | {'echo RTT p50/p99 (us)':>24} | {'status msg/s':>12} | {'burst(16KB) msg/s':>18} | {'burst(16KB) MB/s':>16}")
    print("-" * 92)
    for r in all_results:
        n = r.get("devices")
        echo = r.get("echo_rtt_us", {})
        status = r.get("status", {})
        big = next((b for b in r.get("burst", []) if b.get("size_bytes", 0) >= 16384), {})
        print(
            f"{n:>6} | {echo.get('p50', 0):>10.1f}/{echo.get('p99', 0):<12.1f} | "
            f"{status.get('msgs_per_s', 0):>12.0f} | {big.get('msgs_per_s', 0):>18.0f} | {big.get('mb_per_s', 0):>16.1f}"
        )
    print("=" * 92)


def main() -> None:
    ap = argparse.ArgumentParser(description="dora 本机通信性能基准")
    ap.add_argument("--counts", type=int, nargs="+", default=[1, 4, 8, 16])
    ap.add_argument("--out", type=str, default="/tmp/dora_bench")
    ap.add_argument("--echo-count", type=int, default=3000)
    ap.add_argument("--burst-count", type=int, default=4000)
    ap.add_argument("--burst-chunk", type=int, default=500)
    ap.add_argument("--status-window", type=float, default=3.0)
    ap.add_argument("--device-tick-ms", type=int, default=5)
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    bench_params = {
        "warmup": 200,
        "echo_count": args.echo_count,
        "status_window_s": args.status_window,
        "burst_sizes": [64, 1024, 16384],
        "burst_count": args.burst_count,
        "burst_chunk": args.burst_chunk,
        "timeout_s": 40.0,
    }

    diag = runtime.check_available()
    print(f"[dora] 环境自检: binary={diag.get('binary')} cli_ok={diag.get('cli_ok')} version={diag.get('cli_version')}")

    all_results = []
    for n in args.counts:
        res = run_one(n, args.out, bench_params, args.device_tick_ms)
        all_results.append(res)

    summary_path = os.path.join(args.out, "results.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    print(f"\n[done] 汇总结果 -> {summary_path}")
    print_summary(all_results)


if __name__ == "__main__":
    main()
