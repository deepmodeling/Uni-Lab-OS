"""验证 V3 批处理调度结果的正确性.

检查项:
1. 同设备多槽位必须同时启动、同时结束 (batch constraint)
2. 同设备不存在时间重叠但不同步的情况
3. 确认允许部分填充 (槽位不满也启动)
"""

from __future__ import annotations
from test_biolab_benchmark_v3 import (
    BATCH_CAPS, HEURISTIC_CLASSES, BATCH_SCHEDULERS,
    make_20_dags, make_pool_baseline, make_pool_independent, make_pool_batch,
    run_one, deep_copy_dag,
)

import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


BATCH_DEVICE_TYPES = set(BATCH_CAPS.keys())


def _dtype(iid: str) -> str:
    """从 instance_id 推断 device_type."""
    for dt in BATCH_DEVICE_TYPES:
        if iid.startswith(dt):
            return dt
    # fallback: 去掉最后的 _N
    return iid.rsplit("_", 1)[0]


def check_batch_violations(results, model_label: str, alg_name: str) -> list[str]:
    """检查批处理约束违反.

    规则: 同一物理设备上时间有重叠的任务, 必须 start 和 end 完全相同.
    """
    violations = []

    # 按 device_instance 分组
    by_device: dict[str, list] = defaultdict(list)
    for r in results:
        dt = _dtype(r.device_instance)
        if dt in BATCH_DEVICE_TYPES:
            by_device[r.device_instance].append(r)

    for dev_id, tasks in by_device.items():
        tasks.sort(key=lambda r: r.start)
        n = len(tasks)
        for i in range(n):
            for j in range(i + 1, n):
                ri, rj = tasks[i], tasks[j]
                # 检查是否时间重叠
                if ri.start < rj.end and rj.start < ri.end:
                    # 重叠的任务必须同步
                    if ri.start != rj.start or ri.end != rj.end:
                        violations.append(
                            f"  VIOLATION on {dev_id}: "
                            f"{ri.step_id}[{ri.start}-{ri.end}] vs "
                            f"{rj.step_id}[{rj.start}-{rj.end}] "
                            f"(overlap but not synced!)"
                        )

    return violations


def check_partial_batches(results, model_label: str, alg_name: str) -> dict[str, list[int]]:
    """统计每个设备上每个批次的大小, 找出部分填充的批次."""
    by_device: dict[str, list] = defaultdict(list)
    for r in results:
        dt = _dtype(r.device_instance)
        if dt in BATCH_DEVICE_TYPES:
            by_device[r.device_instance].append(r)

    batch_sizes: dict[str, list[int]] = {}

    for dev_id, tasks in by_device.items():
        tasks.sort(key=lambda r: r.start)
        # 按 (start, end) 分组 = 一个批次
        batches: dict[tuple[int, int], int] = defaultdict(int)
        for r in tasks:
            batches[(r.start, r.end)] += 1
        batch_sizes[dev_id] = list(batches.values())

    return batch_sizes


def check_device_overlap(results, model_label: str, alg_name: str) -> list[str]:
    """检查同设备是否有超过容量的并发 (非批处理设备也检查)."""
    violations = []
    by_device: dict[str, list] = defaultdict(list)
    for r in results:
        by_device[r.device_instance].append(r)

    for dev_id, tasks in by_device.items():
        dt = _dtype(dev_id)
        if dt in BATCH_DEVICE_TYPES:
            # 批处理设备: 同步任务不算重叠, 只查不同步的重叠
            continue  # 已在 check_batch_violations 中检查

        # 非批处理设备: 不应有任何时间重叠
        tasks.sort(key=lambda r: r.start)
        for i in range(len(tasks) - 1):
            ri, rj = tasks[i], tasks[i + 1]
            if ri.end > rj.start:
                violations.append(
                    f"  OVERLAP on {dev_id}: "
                    f"{ri.step_id}[{ri.start}-{ri.end}] vs "
                    f"{rj.step_id}[{rj.start}-{rj.end}]"
                )

    return violations


def main():
    dags = make_20_dags()
    heuristic_algs = [cls.algorithm_name for cls in HEURISTIC_CLASSES]

    print("=" * 80)
    print("  批处理约束验证")
    print("=" * 80)

    models = {
        "A_Baseline": (make_pool_baseline, None),
        "B_IndepSlot": (make_pool_independent, None),
        "C_BatchSlot": (make_pool_batch, "batch"),
    }

    for model_label, (pool_factory, mode) in models.items():
        print(f"\n{'─' * 70}")
        print(f"  {model_label}")
        print(f"{'─' * 70}")

        total_violations = 0
        total_overlaps = 0

        for alg in heuristic_algs:
            if mode == "batch":
                batch_cls = BATCH_SCHEDULERS.get(f"Batch_{alg}")
                if batch_cls is None:
                    continue
                res, _ = run_one(alg, dags, pool_factory, scheduler_cls=batch_cls)
                display_name = f"Batch_{alg}"
            else:
                res, _ = run_one(alg, dags, pool_factory)
                display_name = alg

            if not res:
                print(f"  [{display_name:30s}] NO RESULTS")
                continue

            # 1. 批处理约束检查
            violations = check_batch_violations(res, model_label, alg)
            total_violations += len(violations)

            # 2. 设备重叠检查
            overlaps = check_device_overlap(res, model_label, alg)
            total_overlaps += len(overlaps)

            # 3. 部分批次统计
            batch_sizes = check_partial_batches(res, model_label, alg)
            partial_count = 0
            full_count = 0
            for dev_id, sizes in batch_sizes.items():
                dt = _dtype(dev_id)
                cap = BATCH_CAPS.get(dt, 1)
                for sz in sizes:
                    if sz < cap:
                        partial_count += 1
                    else:
                        full_count += 1

            status = "OK" if not violations and not overlaps else "FAIL"
            print(f"  [{display_name:30s}] {status}  "
                  f"batch_violations={len(violations):2d}  "
                  f"device_overlaps={len(overlaps):2d}  "
                  f"partial_batches={partial_count:2d}  "
                  f"full_batches={full_count:2d}")

            for v in violations[:5]:
                print(v)
            for v in overlaps[:5]:
                print(v)

            # 详细打印部分批次信息 (仅 C 模型)
            if mode == "batch" and partial_count > 0:
                for dev_id, sizes in sorted(batch_sizes.items()):
                    dt = _dtype(dev_id)
                    cap = BATCH_CAPS.get(dt, 1)
                    partials = [s for s in sizes if s < cap]
                    if partials:
                        print(f"    {dev_id}: cap={cap}  batch_sizes={sizes}  "
                              f"partial={partials}")

        print(f"\n  Summary: violations={total_violations}, overlaps={total_overlaps}")
        if total_violations == 0 and total_overlaps == 0:
            print("  ALL CHECKS PASSED")
        else:
            print("  *** ISSUES FOUND ***")


if __name__ == "__main__":
    main()
