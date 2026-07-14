"""生物监测实验室调度基准测试.

从图片中的 Assay1/Assay2 出发, 生成一系列接近真实的生物监测实验案例,
运行所有调度算法, 并将甘特图保存为图片.

设备列表:
- Denso_Arm: 转运机械臂 (1台), 负责样品在设备间转移
- Agilent_vWork: 液体处理工作站 (1台), 负责样品制备
- LightCycler_480: PCR 仪 (1台), 运行 qPCR 实验
- Incubation_Carrier: 孵育器 (2台), 恒温孵育
- Centrifuge: 离心机 (1台), 样品离心分离
- Washer: 洗板机 (1台), ELISA 洗涤步骤
- Reader: 酶标仪 (1台), 吸光度/荧光检测
- Sealer: 封膜机 (1台), 封板操作
"""

from __future__ import annotations
from scheduler.core.step_algorithms import (
    ALGORITHM_REGISTRY,
    get_algorithm,
    list_algorithms,
)
from scheduler.models.schedule_result import ScheduledStep
from scheduler.models.resources import DeviceInstance, DevicePool
from scheduler.models.dag import Edge, StepNode, TaskDAG, TimeConstraint
import numpy as np
from matplotlib.patches import Rectangle
import matplotlib.pyplot as plt

import copy
import os
import sys
import time
from dataclasses import dataclass

import matplotlib
matplotlib.use("Agg")

# 确保 src 在路径中
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


# 确保 CP-SAT 和 ExactDP 被注册
import scheduler.core.cp_solver  # noqa: F401
import scheduler.core.dp_solver  # noqa: F401


# ══════════════════════════════════════════════════════════
# 1. 设备与实验定义
# ══════════════════════════════════════════════════════════

DEVICE_CONFIG = {
    "Denso_Arm": 1,        # 转运机械臂
    "Agilent_vWork": 1,    # 液体处理工作站
    "LightCycler_480": 1,  # qPCR 仪
    "Incubation_Carrier": 2,  # 孵育器 (2台)
    "Centrifuge": 1,       # 离心机
    "Washer": 1,           # 洗板机
    "Reader": 1,           # 酶标仪
    "Sealer": 1,           # 封膜机
}


def make_pool() -> DevicePool:
    pool = DevicePool()
    for dtype, count in DEVICE_CONFIG.items():
        pool.add_device_type(dtype, count)
    return pool


# ══════════════════════════════════════════════════════════
# 2. 实验流程 DAG 构建器
# ══════════════════════════════════════════════════════════

def _build_chain_dag(
    task_id: str,
    priority: float,
    steps: list[tuple[str, str, int]],  # (step_id, machine_type, duration_ms)
    time_constraints: list[TimeConstraint] | None = None,
) -> TaskDAG:
    """构建线性链式 DAG: s0 → s1 → s2 → ..."""
    nodes = {}
    edges = []
    for i, (sid, mtype, dur) in enumerate(steps):
        nodes[sid] = StepNode(
            step_id=sid, task_id=task_id, machine_type=mtype, duration=dur,
        )
        if i > 0:
            edges.append(Edge(source=steps[i - 1][0], target=sid))

    dag = TaskDAG(
        task_id=task_id,
        priority=priority,
        nodes=nodes,
        edges=edges,
        time_constraints=time_constraints or [],
    )
    dag.build_adjacency()
    return dag


# ── Assay1: 简单 PCR 流程 (来自图片) ─────────────────────
def make_assay1(suffix: str = "", priority: float = 1.0) -> TaskDAG:
    """Assay1: Shelf → vWork制备 → LightCycler qPCR → Unload

    步骤:
    1. Denso_Arm.transport(Shelf → Agilent_vWork)      20s
    2. Agilent_vWork.runProtocol("Preparation")        180s
    3. Denso_Arm.transport(Agilent_vWork → LightCycler) 20s
    4. LightCycler_480.runExperiment("Test","Macro")   900s
    5. Denso_Arm.transport(LightCycler → Unload)        15s
    """
    tid = f"Assay1_PCR{suffix}"
    return _build_chain_dag(tid, priority, [
        (f"a1{suffix}_transport_to_vwork", "Denso_Arm", 20),
        (f"a1{suffix}_preparation", "Agilent_vWork", 180),
        (f"a1{suffix}_transport_to_pcr", "Denso_Arm", 20),
        (f"a1{suffix}_qpcr_run", "LightCycler_480", 900),
        (f"a1{suffix}_transport_unload", "Denso_Arm", 15),
    ])


# ── Assay2: PCR + 孵育流程 (来自图片) ────────────────────
def make_assay2(suffix: str = "", priority: float = 1.0) -> TaskDAG:
    """Assay2: Shelf → vWork制备 → 孵育 → LightCycler qPCR → Unload

    步骤:
    1. Denso_Arm.transport(Shelf → Agilent_vWork)      20s
    2. Agilent_vWork.runProtocol("Preparation")        180s
    3. Denso_Arm.transport(Agilent_vWork → Incubator)   20s
    4. Incubation_Carrier.incubate(500s)               500s
    5. Denso_Arm.transport(Incubator → LightCycler)     20s
    6. LightCycler_480.runExperiment("Test","Macro")   900s
    7. Denso_Arm.transport(LightCycler → Unload)        15s
    """
    tid = f"Assay2_PCR_Inc{suffix}"
    return _build_chain_dag(tid, priority, [
        (f"a2{suffix}_transport_to_vwork", "Denso_Arm", 20),
        (f"a2{suffix}_preparation", "Agilent_vWork", 180),
        (f"a2{suffix}_transport_to_incubator", "Denso_Arm", 20),
        (f"a2{suffix}_incubation", "Incubation_Carrier", 500),
        (f"a2{suffix}_transport_to_pcr", "Denso_Arm", 20),
        (f"a2{suffix}_qpcr_run", "LightCycler_480", 900),
        (f"a2{suffix}_transport_unload", "Denso_Arm", 15),
    ])


# ── Assay3: Direct ELISA 流程 ────────────────────────────
def make_direct_elisa(suffix: str = "", priority: float = 1.0) -> TaskDAG:
    """Direct ELISA: 包被→孵育→洗涤→加样→孵育→洗涤→显色→读板

    步骤:
    1. Denso_Arm.transport(Shelf → vWork)              20s
    2. Agilent_vWork.runProtocol("Coating")            120s   包被
    3. Denso_Arm.transport(vWork → Incubator)           20s
    4. Incubation_Carrier.incubate(600s)               600s   孵育1h
    5. Denso_Arm.transport(Incubator → Washer)          20s
    6. Washer.wash("3x PBS-T")                         90s    洗涤
    7. Denso_Arm.transport(Washer → vWork)              20s
    8. Agilent_vWork.runProtocol("SampleAddition")     150s   加样
    9. Denso_Arm.transport(vWork → Incubator)           20s
    10. Incubation_Carrier.incubate(1800s)            1800s   孵育30min
    11. Denso_Arm.transport(Incubator → Washer)         20s
    12. Washer.wash("5x PBS-T")                        120s   洗涤
    13. Denso_Arm.transport(Washer → vWork)             20s
    14. Agilent_vWork.runProtocol("Chromogen")         100s   显色底物
    15. Denso_Arm.transport(vWork → Reader)             20s
    16. Reader.read("Absorbance_450nm")                60s    读板
    17. Denso_Arm.transport(Reader → Unload)            15s
    """
    tid = f"DirectELISA{suffix}"
    return _build_chain_dag(tid, priority, [
        (f"elisa{suffix}_transport_to_vwork1", "Denso_Arm", 20),
        (f"elisa{suffix}_coating", "Agilent_vWork", 120),
        (f"elisa{suffix}_transport_to_inc1", "Denso_Arm", 20),
        (f"elisa{suffix}_incubation1", "Incubation_Carrier", 600),
        (f"elisa{suffix}_transport_to_washer1", "Denso_Arm", 20),
        (f"elisa{suffix}_wash1", "Washer", 90),
        (f"elisa{suffix}_transport_to_vwork2", "Denso_Arm", 20),
        (f"elisa{suffix}_sample_add", "Agilent_vWork", 150),
        (f"elisa{suffix}_transport_to_inc2", "Denso_Arm", 20),
        (f"elisa{suffix}_incubation2", "Incubation_Carrier", 1800),
        (f"elisa{suffix}_transport_to_washer2", "Denso_Arm", 20),
        (f"elisa{suffix}_wash2", "Washer", 120),
        (f"elisa{suffix}_transport_to_vwork3", "Denso_Arm", 20),
        (f"elisa{suffix}_chromogen", "Agilent_vWork", 100),
        (f"elisa{suffix}_transport_to_reader", "Denso_Arm", 20),
        (f"elisa{suffix}_read", "Reader", 60),
        (f"elisa{suffix}_transport_unload", "Denso_Arm", 15),
    ])


# ── Assay4: 血常规+生化分析 ──────────────────────────────
def make_blood_panel(suffix: str = "", priority: float = 2.0) -> TaskDAG:
    """Blood Panel: 离心分离 → 液体处理分杯 → PCR检测

    步骤:
    1. Denso_Arm.transport(Shelf → Centrifuge)         20s
    2. Centrifuge.spin(3000rpm, 10min)                 600s
    3. Denso_Arm.transport(Centrifuge → vWork)          20s
    4. Agilent_vWork.runProtocol("Aliquot")            200s   分杯
    5. Denso_Arm.transport(vWork → LightCycler)         20s
    6. LightCycler_480.runExperiment("Panel","Auto")  1200s
    7. Denso_Arm.transport(LightCycler → Unload)        15s
    """
    tid = f"BloodPanel{suffix}"
    return _build_chain_dag(tid, priority, [
        (f"bp{suffix}_transport_to_centrifuge", "Denso_Arm", 20),
        (f"bp{suffix}_centrifuge", "Centrifuge", 600),
        (f"bp{suffix}_transport_to_vwork", "Denso_Arm", 20),
        (f"bp{suffix}_aliquot", "Agilent_vWork", 200),
        (f"bp{suffix}_transport_to_pcr", "Denso_Arm", 20),
        (f"bp{suffix}_pcr_run", "LightCycler_480", 1200),
        (f"bp{suffix}_transport_unload", "Denso_Arm", 15),
    ])


# ── Assay5: 核酸提取+qPCR 病原检测 ──────────────────────
def make_pathogen_detection(suffix: str = "", priority: float = 3.0) -> TaskDAG:
    """病原体核酸检测: 提取 → 封板 → 扩增 → 检测

    步骤:
    1. Denso_Arm.transport(Shelf → vWork)              20s
    2. Agilent_vWork.runProtocol("NucleicAcidExtract") 300s   核酸提取
    3. Denso_Arm.transport(vWork → Sealer)              20s
    4. Sealer.seal("Thermal")                           45s   封膜
    5. Denso_Arm.transport(Sealer → LightCycler)        20s
    6. LightCycler_480.runExperiment("PathogenPCR")    1500s  qPCR扩增
    7. Denso_Arm.transport(LightCycler → Unload)        15s
    """
    tid = f"PathogenDetect{suffix}"
    return _build_chain_dag(tid, priority, [
        (f"pd{suffix}_transport_to_vwork", "Denso_Arm", 20),
        (f"pd{suffix}_extraction", "Agilent_vWork", 300),
        (f"pd{suffix}_transport_to_sealer", "Denso_Arm", 20),
        (f"pd{suffix}_seal", "Sealer", 45),
        (f"pd{suffix}_transport_to_pcr", "Denso_Arm", 20),
        (f"pd{suffix}_pcr_run", "LightCycler_480", 1500),
        (f"pd{suffix}_transport_unload", "Denso_Arm", 15),
    ])


# ── Assay6: Sandwich ELISA (带时间约束) ──────────────────
def make_sandwich_elisa(suffix: str = "", priority: float = 1.5) -> TaskDAG:
    """Sandwich ELISA: 包被→孵育→洗涤→加一抗→孵育→洗涤→加二抗→孵育→洗涤→显色→读板
    带 max_gap 约束: 孵育结束后必须在60s内开始洗涤
    """
    tid = f"SandwichELISA{suffix}"
    steps = [
        (f"se{suffix}_transport_to_vwork1", "Denso_Arm", 20),
        (f"se{suffix}_coating", "Agilent_vWork", 120),
        (f"se{suffix}_transport_to_inc1", "Denso_Arm", 20),
        (f"se{suffix}_incubation1", "Incubation_Carrier", 900),
        (f"se{suffix}_transport_to_washer1", "Denso_Arm", 20),
        (f"se{suffix}_wash1", "Washer", 90),
        (f"se{suffix}_transport_to_vwork2", "Denso_Arm", 20),
        (f"se{suffix}_primary_ab", "Agilent_vWork", 120),
        (f"se{suffix}_transport_to_inc2", "Denso_Arm", 20),
        (f"se{suffix}_incubation2", "Incubation_Carrier", 1200),
        (f"se{suffix}_transport_to_washer2", "Denso_Arm", 20),
        (f"se{suffix}_wash2", "Washer", 120),
        (f"se{suffix}_transport_to_vwork3", "Denso_Arm", 20),
        (f"se{suffix}_secondary_ab", "Agilent_vWork", 120),
        (f"se{suffix}_transport_to_inc3", "Denso_Arm", 20),
        (f"se{suffix}_incubation3", "Incubation_Carrier", 600),
        (f"se{suffix}_transport_to_washer3", "Denso_Arm", 20),
        (f"se{suffix}_wash3", "Washer", 120),
        (f"se{suffix}_transport_to_vwork4", "Denso_Arm", 20),
        (f"se{suffix}_chromogen", "Agilent_vWork", 100),
        (f"se{suffix}_transport_to_reader", "Denso_Arm", 20),
        (f"se{suffix}_read", "Reader", 60),
        (f"se{suffix}_transport_unload", "Denso_Arm", 15),
    ]
    # 孵育结束后到洗涤步骤的 max_gap 约束 (机械臂转运+排队最多60s)
    tcs = [
        TimeConstraint(
            from_step=f"se{suffix}_incubation1",
            to_step=f"se{suffix}_transport_to_washer1",
            max_gap=60,
        ),
        TimeConstraint(
            from_step=f"se{suffix}_incubation2",
            to_step=f"se{suffix}_transport_to_washer2",
            max_gap=60,
        ),
        TimeConstraint(
            from_step=f"se{suffix}_incubation3",
            to_step=f"se{suffix}_transport_to_washer3",
            max_gap=60,
        ),
    ]
    return _build_chain_dag(tid, priority, steps, tcs)


# ══════════════════════════════════════════════════════════
# 3. 测试场景定义
# ══════════════════════════════════════════════════════════

@dataclass
class TestScenario:
    name: str
    description: str
    dags: list[tuple[TaskDAG, int]]  # (dag, submit_time_ms)


def scenario_image_original() -> TestScenario:
    """图片中的原始场景: Assay1 + Assay2 同时提交."""
    return TestScenario(
        name="S1_ImageOriginal",
        description="图片原始: Assay1(简单PCR) + Assay2(PCR+孵育), 同时提交",
        dags=[
            (make_assay1("_A", priority=1.0), 0),
            (make_assay2("_B", priority=1.0), 0),
        ],
    )


def scenario_triple_pcr() -> TestScenario:
    """3个 PCR 检测批次, 间隔提交."""
    return TestScenario(
        name="S2_TriplePCR",
        description="3批PCR检测, 间隔2分钟提交, 测试机械臂争用",
        dags=[
            (make_assay1("_1", priority=2.0), 0),
            (make_assay1("_2", priority=1.0), 120),
            (make_assay1("_3", priority=1.5), 240),
        ],
    )


def scenario_elisa_batch() -> TestScenario:
    """3个 ELISA 批次 (长流程)."""
    return TestScenario(
        name="S3_ELISA_Batch",
        description="3批DirectELISA, 间隔60s, 长孵育争用孵育器",
        dags=[
            (make_direct_elisa("_1", priority=1.0), 0),
            (make_direct_elisa("_2", priority=1.0), 60),
            (make_direct_elisa("_3", priority=1.0), 120),
        ],
    )


def scenario_mixed_urgent() -> TestScenario:
    """混合实验 + 紧急插单: 常规ELISA运行中, 高优先级病原检测插入."""
    return TestScenario(
        name="S4_MixedUrgent",
        description="ELISA运行中, t=200s插入紧急病原检测(priority=3)",
        dags=[
            (make_direct_elisa("_routine", priority=1.0), 0),
            (make_pathogen_detection("_urgent", priority=3.0), 200),
        ],
    )


def scenario_full_lab() -> TestScenario:
    """满载实验室: 所有类型实验同时运行."""
    return TestScenario(
        name="S5_FullLab",
        description="满载: PCR + ELISA + 血液分析 + 病原检测, 高度竞争",
        dags=[
            (make_assay1("_pcr", priority=1.0), 0),
            (make_assay2("_pcr_inc", priority=1.0), 0),
            (make_direct_elisa("_elisa", priority=1.5), 30),
            (make_blood_panel("_blood", priority=2.0), 60),
            (make_pathogen_detection("_pathogen", priority=3.0), 90),
        ],
    )


def scenario_sandwich_elisa_with_constraints() -> TestScenario:
    """Sandwich ELISA 带时间约束."""
    return TestScenario(
        name="S6_SandwichELISA",
        description="2批SandwichELISA带max_gap约束, 测试时间窗约束处理",
        dags=[
            (make_sandwich_elisa("_1", priority=1.0), 0),
            (make_sandwich_elisa("_2", priority=1.0), 100),
        ],
    )


ALL_SCENARIOS = [
    scenario_image_original,
    scenario_triple_pcr,
    scenario_elisa_batch,
    scenario_mixed_urgent,
    scenario_full_lab,
    scenario_sandwich_elisa_with_constraints,
]


# ══════════════════════════════════════════════════════════
# 4. 甘特图绘制
# ══════════════════════════════════════════════════════════

# 设备分类颜色
DEVICE_COLORS = {
    "Denso_Arm": "#E53935",       # 红色 - 机械臂
    "Agilent_vWork": "#1E88E5",   # 蓝色 - 液体处理
    "LightCycler_480": "#43A047",  # 绿色 - PCR
    "Incubation_Carrier": "#FB8C00",  # 橙色 - 孵育
    "Centrifuge": "#8E24AA",      # 紫色 - 离心
    "Washer": "#00ACC1",          # 青色 - 洗板
    "Reader": "#FFD600",          # 黄色 - 读板
    "Sealer": "#6D4C41",          # 棕色 - 封膜
}

TASK_CMAP = plt.get_cmap("Set3")


def plot_gantt_by_device(
    results: list[ScheduledStep],
    title: str,
    filename: str,
) -> None:
    """绘制按设备分组的甘特图 (类似图片中下半部分)."""
    plt.rcParams["font.sans-serif"] = [
        "PingFang SC", "Heiti SC", "STHeiti", "Microsoft YaHei", "SimHei", "Arial",
    ]
    plt.rcParams["axes.unicode_minus"] = False

    if not results:
        print(f"  [WARN] {title}: 无调度结果, 跳过")
        return

    # 收集所有出现的设备实例
    device_instances = sorted(set(r.device_instance for r in results))
    if not device_instances:
        return

    # task_id → color 映射
    task_ids = sorted(set(r.task_id for r in results))
    task_color = {tid: TASK_CMAP(i % 12) for i, tid in enumerate(task_ids)}

    max_end = max(r.end for r in results)
    n_lanes = len(device_instances)
    lane_map = {did: i for i, did in enumerate(device_instances)}

    fig_h = max(4, n_lanes * 0.6 + 2)
    fig, ax = plt.subplots(figsize=(18, fig_h), dpi=120)

    # 泳道背景
    for i in range(n_lanes):
        color = "#F5F5F5" if i % 2 == 0 else "#FFFFFF"
        ax.axhspan(i - 0.4, i + 0.4, facecolor=color, alpha=0.6)

    # 绘制任务块
    legend_entries = {}
    for r in results:
        y = lane_map[r.device_instance]
        duration = r.end - r.start
        # 根据设备类型获取边框颜色
        dev_type = r.device_instance.rsplit("_", 1)[0]
        # 处理 "Incubation_Carrier_0" → "Incubation_Carrier"
        for dt in DEVICE_COLORS:
            if r.device_instance.startswith(dt):
                dev_type = dt
                break
        edge_color = DEVICE_COLORS.get(dev_type, "#333333")
        fill_color = task_color[r.task_id]

        rect = Rectangle(
            (r.start, y - 0.35), duration, 0.7,
            facecolor=fill_color, edgecolor=edge_color,
            linewidth=1.2, alpha=0.85,
        )
        ax.add_patch(rect)

        # 标签 (只对较宽的块显示)
        if duration > max_end * 0.03:
            label = r.step_id.split("_", 2)[-1] if "_" in r.step_id else r.step_id
            # 截断过长标签
            if len(label) > 15:
                label = label[:13] + ".."
            ax.text(
                r.start + duration / 2, y,
                label, ha="center", va="center",
                fontsize=6, fontweight="bold", color="#333",
            )

        if r.task_id not in legend_entries:
            legend_entries[r.task_id] = plt.Rectangle(
                (0, 0), 1, 1, facecolor=fill_color, edgecolor="#333", linewidth=0.8,
            )

    # 结束线
    ax.axvline(max_end, color="#2E7D32", linestyle="--", linewidth=1.5, alpha=0.7)

    # 坐标轴
    ax.set_yticks(range(n_lanes))
    ax.set_yticklabels(device_instances, fontsize=8)
    ax.set_xlim(-max_end * 0.02, max_end * 1.1)
    ax.set_ylim(-0.6, n_lanes - 0.4)
    ax.invert_yaxis()
    ax.set_xlabel("时间 (秒)", fontsize=10)
    ax.set_title(title, fontsize=11, fontweight="bold", pad=10)
    ax.grid(axis="x", alpha=0.3, linestyle=":")

    # 图例
    if legend_entries:
        ax.legend(
            legend_entries.values(), legend_entries.keys(),
            bbox_to_anchor=(1.01, 1), loc="upper left",
            fontsize=7, title="实验批次", title_fontsize=8,
        )

    plt.tight_layout()
    plt.savefig(filename, bbox_inches="tight", dpi=150)
    plt.close(fig)
    print(f"  甘特图已保存: {filename}")


def plot_comparison_summary(
    all_results: dict[str, dict[str, list[ScheduledStep]]],
    output_dir: str,
) -> None:
    """绘制所有算法的 makespan 对比柱状图."""
    plt.rcParams["font.sans-serif"] = [
        "PingFang SC", "Heiti SC", "STHeiti", "Microsoft YaHei", "SimHei", "Arial",
    ]
    plt.rcParams["axes.unicode_minus"] = False

    scenarios = list(all_results.keys())
    algorithms = sorted(
        set(alg for s in all_results.values() for alg in s.keys())
    )

    fig, axes = plt.subplots(
        len(scenarios), 1,
        figsize=(14, 3.5 * len(scenarios)),
        dpi=120,
    )
    if len(scenarios) == 1:
        axes = [axes]

    colors = plt.get_cmap("tab10")

    for idx, (scenario_name, alg_results) in enumerate(all_results.items()):
        ax = axes[idx]
        makespans = []
        alg_names = []
        for alg in algorithms:
            res = alg_results.get(alg)
            if res:
                ms = max(r.end for r in res)
                makespans.append(ms)
                alg_names.append(alg)
            else:
                makespans.append(0)
                alg_names.append(alg)

        x = np.arange(len(alg_names))
        bars = ax.bar(x, makespans, color=[colors(i % 10) for i in range(len(alg_names))],
                      edgecolor="#333", linewidth=0.5)
        ax.set_xticks(x)
        ax.set_xticklabels(alg_names, rotation=30, ha="right", fontsize=8)
        ax.set_ylabel("Makespan (秒)", fontsize=9)
        ax.set_title(scenario_name, fontsize=10, fontweight="bold")
        ax.grid(axis="y", alpha=0.3)

        # 标注数值
        for bar, val in zip(bars, makespans):
            if val > 0:
                ax.text(bar.get_x() + bar.get_width() / 2, val,
                        f"{val}", ha="center", va="bottom", fontsize=7)

    plt.suptitle("各算法 Makespan 对比", fontsize=13, fontweight="bold", y=1.01)
    plt.tight_layout()
    summary_path = os.path.join(output_dir, "00_makespan_comparison.png")
    plt.savefig(summary_path, bbox_inches="tight", dpi=150)
    plt.close(fig)
    print(f"\n对比汇总图已保存: {summary_path}")


# ══════════════════════════════════════════════════════════
# 5. 运行基准测试
# ══════════════════════════════════════════════════════════

def deep_copy_dag(dag: TaskDAG) -> TaskDAG:
    """深拷贝 DAG, 重置所有运行时状态."""
    new_dag = copy.deepcopy(dag)
    for node in new_dag.nodes.values():
        node.reset()
    new_dag.build_adjacency()
    return new_dag


def run_heuristic(
    algorithm_name: str, dags: list[tuple[TaskDAG, int]]
) -> tuple[list[ScheduledStep], float]:
    """运行启发式/事件驱动算法."""
    pool = make_pool()
    cls = get_algorithm(algorithm_name)
    scheduler = cls(pool)
    for dag, submit_time in dags:
        fresh = deep_copy_dag(dag)
        scheduler.add_batch(fresh, submit_time=submit_time)
    t0 = time.perf_counter()
    results = scheduler.schedule()
    elapsed = time.perf_counter() - t0
    return results, elapsed


def run_benchmark():
    """运行全部基准测试并输出甘特图."""
    output_dir = os.path.join(os.path.dirname(__file__), "..", "benchmark_output")
    os.makedirs(output_dir, exist_ok=True)

    # 获取所有可用算法 (排除 ExactDP 在大规模场景)
    all_algorithms = list_algorithms()
    print(f"可用算法: {all_algorithms}")
    print(f"测试场景: {len(ALL_SCENARIOS)} 个\n")

    # 收集所有结果用于对比图
    all_results: dict[str, dict[str, list[ScheduledStep]]] = {}

    for scenario_fn in ALL_SCENARIOS:
        scenario = scenario_fn()
        total_steps = sum(len(dag.nodes) for dag, _ in scenario.dags)
        print(f"{'='*60}")
        print(f"场景: {scenario.name}")
        print(f"描述: {scenario.description}")
        print(f"批次数: {len(scenario.dags)}, 总步骤数: {total_steps}")
        print(f"{'='*60}")

        scenario_results: dict[str, list[ScheduledStep]] = {}

        for alg_name in all_algorithms:
            # ExactDP 在步骤数 > 20 时跳过
            if alg_name == "ExactDP" and total_steps > 20:
                print(f"  [{alg_name}] 跳过 (步骤数 {total_steps} > 20)")
                continue

            # Sandwich ELISA 的 time_constraints 只有 CP-SAT 支持
            has_constraints = any(
                len(dag.time_constraints) > 0 for dag, _ in scenario.dags
            )

            try:
                results, elapsed = run_heuristic(alg_name, scenario.dags)
                if results:
                    makespan = max(r.end for r in results)
                    print(f"  [{alg_name:25s}] makespan={makespan:>7d}s  "
                          f"steps={len(results):>3d}  time={elapsed*1000:.1f}ms")
                    scenario_results[alg_name] = results

                    # 每个算法单独的甘特图
                    gantt_file = os.path.join(
                        output_dir,
                        f"{scenario.name}_{alg_name}.png",
                    )
                    plot_gantt_by_device(
                        results,
                        f"{scenario.name} — {alg_name} (makespan={makespan}s)",
                        gantt_file,
                    )
                else:
                    print(f"  [{alg_name:25s}] 无可行解")
            except Exception as e:
                print(f"  [{alg_name:25s}] 错误: {e}")

        all_results[scenario.name] = scenario_results
        print()

    # 汇总对比图
    plot_comparison_summary(all_results, output_dir)

    print(f"\n所有输出已保存至: {output_dir}/")
    return all_results


# ══════════════════════════════════════════════════════════
# 6. pytest 入口
# ══════════════════════════════════════════════════════════

def test_biolab_benchmark():
    """pytest 入口: 运行全部基准测试."""
    results = run_benchmark()
    # 验证每个场景至少有一个算法产出了结果
    for scenario_name, alg_results in results.items():
        assert len(alg_results) > 0, f"场景 {scenario_name} 无任何算法产出结果"
        for alg_name, steps in alg_results.items():
            assert len(steps) > 0, f"{scenario_name}/{alg_name} 结果为空"
            # 验证时间一致性: 每个 step 的 end > start
            for s in steps:
                assert s.end > s.start, f"step {s.step_id}: end <= start"


if __name__ == "__main__":
    run_benchmark()
