"""监听 S09 加液完成事件，并把每次加液后的重量追加到 CSV。"""

from __future__ import annotations

import argparse
import csv
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


PROCESS_DONE_VAR = "S09工艺完成"
BALANCE_STABLE_VAR = "S09天平读数稳定"
BALANCE_READING_VAR = "S09天平读数"
DISPENSE_PROCESS = 8
DEFAULT_URL = "opc.tcp://192.168.1.10:4840"
DEFAULT_NODE_PREFIX = "ns=4;s=上位机通讯|"


def wait_for_stable_weight(
    client: Any,
    *,
    timeout: float,
    poll_interval: float,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> float:
    """等待天平稳定并返回当前读数。"""
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if bool(client.read(BALANCE_STABLE_VAR, use_cache=False)):
            return float(client.read(BALANCE_READING_VAR, use_cache=False))
        sleep(poll_interval)
    raise TimeoutError(f"等待 {BALANCE_STABLE_VAR} 超时（{timeout:g} 秒）")


def monitor(
    client: Any,
    writer: csv.DictWriter,
    output_file: Any,
    *,
    sample_count: int,
    additions_per_sample: int,
    poll_interval: float,
    stable_timeout: float,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """记录工艺 8 的完成沿；返回成功写入的记录数。"""
    target_count = sample_count * additions_per_sample
    recorded = 0
    previous_done: int | None = None
    armed = False

    while recorded < target_count:
        done = int(client.read(PROCESS_DONE_VAR, use_cache=False) or 0)
        # 启动时 PLC 可能还保留着上一次的完成值 8；先看到非 8 状态后再记账，
        # 避免把历史动作误认为本轮第一个样品。
        if done != DISPENSE_PROCESS:
            armed = True
        if armed and done == DISPENSE_PROCESS and previous_done != DISPENSE_PROCESS:
            weight = wait_for_stable_weight(
                client,
                timeout=stable_timeout,
                poll_interval=poll_interval,
                sleep=sleep,
            )
            recorded += 1
            sample_no = (recorded - 1) // additions_per_sample + 1
            addition_no = (recorded - 1) % additions_per_sample + 1
            timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
            writer.writerow(
                {
                    "timestamp": timestamp,
                    "sample_no": sample_no,
                    "addition_no": addition_no,
                    "weight_g": weight,
                }
            )
            output_file.flush()
            print(
                f"[{timestamp}] 样品 {sample_no}/{sample_count}，"
                f"第 {addition_no}/{additions_per_sample} 次加液后：{weight:g} g"
            )
        previous_done = done
        if recorded < target_count:
            sleep(poll_interval)
    return recorded


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="记录 15 个样品在 S09 每次加液后的重量")
    parser.add_argument("--url", default=DEFAULT_URL, help="OPC UA 地址")
    parser.add_argument("--samples", type=int, default=15, help="样品数量（默认 15）")
    parser.add_argument(
        "--additions-per-sample",
        type=int,
        default=1,
        help="每个样品的加液次数（默认 1）",
    )
    parser.add_argument("--poll-interval", type=float, default=0.2, help="轮询间隔，秒")
    parser.add_argument("--stable-timeout", type=float, default=30.0, help="等待天平稳定超时，秒")
    parser.add_argument("--username", help="OPC UA 用户名")
    parser.add_argument("--password", help="OPC UA 密码")
    parser.add_argument("--node-prefix", default=DEFAULT_NODE_PREFIX, help="OPC UA NodeId 前缀")
    parser.add_argument("--output", type=Path, help="输出 CSV 路径")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.samples <= 0 or args.additions_per_sample <= 0:
        raise SystemExit("--samples 和 --additions-per-sample 必须大于 0")
    if args.poll_interval <= 0 or args.stable_timeout <= 0:
        raise SystemExit("--poll-interval 和 --stable-timeout 必须大于 0")

    from unilabos.devices.workstation.szlab_poly_studio.plc import SZLabPolyPLCDevice

    output = args.output or Path("unilabos_data/szlab_poly_studio/s09_balance") / (
        f"s09_balance_{datetime.now():%Y%m%d_%H%M%S}.csv"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    variables = (PROCESS_DONE_VAR, BALANCE_STABLE_VAR, BALANCE_READING_VAR)
    node_ids = {name: f"{args.node_prefix}{name}" for name in variables}

    print(f"连接 S09：{args.url}")
    print(f"记录目标：{args.samples} 个样品，每个 {args.additions_per_sample} 次加液")
    print(f"输出文件：{output.resolve()}")
    client = SZLabPolyPLCDevice(
        url=args.url,
        csv_path=False,
        username=args.username,
        password=args.password,
        opcua_node_id_map=node_ids,
        ignore_opcua_token_time_drift=True,
    )
    try:
        with output.open("w", newline="", encoding="utf-8-sig") as output_file:
            writer = csv.DictWriter(
                output_file,
                fieldnames=("timestamp", "sample_no", "addition_no", "weight_g"),
            )
            writer.writeheader()
            output_file.flush()
            count = monitor(
                client,
                writer,
                output_file,
                sample_count=args.samples,
                additions_per_sample=args.additions_per_sample,
                poll_interval=args.poll_interval,
                stable_timeout=args.stable_timeout,
            )
    except KeyboardInterrupt:
        print("\n已停止监听，已写入的数据均已保存。")
        return 130
    finally:
        client.disconnect()
    print(f"记录完成，共 {count} 条：{output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
