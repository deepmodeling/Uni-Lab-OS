"""甘特图可视化 — 双视图 (DAG + 设备)."""

from pathlib import Path
from matplotlib.patches import Rectangle
from matplotlib import font_manager
from matplotlib import colors as mcolors
import matplotlib.pyplot as plt
import matplotlib
matplotlib.use("Agg")


# 设备类型颜色映射
STEP_COLORS = {
    "Denso_Arm": "#EF5350",
    "Agilent_vWork": "#42A5F5",
    "LightCycler_480": "#66BB6A",
    "Incubation_Carrier": "#FFA726",
    "Centrifuge": "#AB47BC",
    "Washer": "#26C6DA",
    "Reader": "#FFEE58",
    "Sealer": "#8D6E63",
    "PCR_Machine": "#66BB6A",  # alias for LightCycler_480
    "Plate_Reader": "#FFEE58",  # alias for Reader
    "Gel_Electrophoresis": "#9575CD",
}

DAG_CMAP = plt.get_cmap("tab20")
ALL_DT = list(STEP_COLORS.keys())
DEVICE_CMAP = plt.get_cmap("tab20")


def configure_matplotlib_fonts():
    """Register common macOS CJK fonts so Chinese device names render in charts."""
    font_paths = [
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    ]
    font_names = []
    for path in font_paths:
        if Path(path).exists():
            font_manager.fontManager.addfont(path)
            font_names.append(font_manager.FontProperties(fname=path).get_name())
    if font_names:
        plt.rcParams["font.sans-serif"] = font_names + ["DejaVu Sans", "Arial"]
    else:
        plt.rcParams["font.sans-serif"] = ["DejaVu Sans", "Arial"]
    plt.rcParams["axes.unicode_minus"] = False


def _dtype(iid):
    """从 instance_id (含 _sN 槽位后缀) 提取 device_type."""
    for dt in ALL_DT:
        if iid.startswith(dt):
            return dt
    parts = iid.rsplit("_", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return parts[0]
    if len(parts) == 2 and parts[1].startswith("s") and parts[1][1:].isdigit():
        base = parts[0].rsplit("_", 1)
        if len(base) == 2 and base[1].isdigit():
            return base[0]
    return iid


def _device_color(device_type):
    """Return a stable color for both known and newly named device types."""
    if device_type in STEP_COLORS:
        return STEP_COLORS[device_type]
    color_index = len(STEP_COLORS)
    for known in sorted(set(STEP_COLORS) | {device_type}):
        if known == device_type:
            break
        if known not in STEP_COLORS:
            color_index += 1
    return mcolors.to_hex(DEVICE_CMAP(color_index % DEVICE_CMAP.N))


def _dev_sort_key(iid):
    """设备排序: 按 type → 物理编号 → 槽位号."""
    dt = _dtype(iid)
    rest = iid[len(dt):]  # e.g. "_0_s1" or "_2"
    parts = rest.strip("_").split("_")
    nums = []
    for p in parts:
        p = p.lstrip("s")
        try:
            nums.append(int(p))
        except ValueError:
            nums.append(0)
    return (dt, *nums)


def plot_dual_gantt(results, dags, title, filename, objective=0, task_comps=None):
    """绘制双视图甘特图: 上方按 DAG 分组, 下方按设备分组.

    Args:
        results: List[ScheduledStep] 调度结果
        dags: List[Tuple[TaskDAG, submit_time]] DAG 列表
        title: 图表标题
        filename: 输出文件路径
        objective: 目标函数值 (Sum(wi*Ci))
        task_comps: Dict[task_id, completion_time] 任务完成时间映射
    """
    configure_matplotlib_fonts()

    if not results:
        return

    max_end = max(r.end for r in results)
    task_ids = sorted(set(r.task_id for r in results))
    tidx = {t: i for i, t in enumerate(task_ids)}
    tcol = {t: DAG_CMAP(i % 20) for i, t in enumerate(task_ids)}

    # 提取设备实例 (支持 device_instance 或 resource 字段)
    devs = sorted(
        set(getattr(r, "device_instance", None) or getattr(r, "resource", "") for r in results),
        key=_dev_sort_key
    )
    didx = {d: i for i, d in enumerate(devs)}
    nt, nd = len(task_ids), len(devs)

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(24, max((nt + nd) * 0.32 + 3, 8)), dpi=120,
        gridspec_kw={"height_ratios": [max(nt, 3), max(nd, 3)]}, sharex=True
    )

    # ── 上: DAG 视图 ──
    for i in range(nt):
        ax1.axhspan(i - .45, i + .45, fc="#F5F5F5" if i % 2 == 0 else "#FFF", alpha=.5)

    ld = {}
    for r in results:
        y, dur = tidx[r.task_id], r.end - r.start
        dev = getattr(r, "device_instance", None) or getattr(r, "resource", "")
        dt = _dtype(dev)
        fc = _device_color(dt)
        ax1.add_patch(Rectangle((r.start, y - .35), dur, .7, fc=fc, ec="#555", lw=.6, alpha=.88))

        if dur > max_end * .015:
            lb = r.step_id.split("_", 2)[-1][:10]
            ax1.text(r.start + dur / 2, y, lb, ha="center", va="center",
                     fontsize=5, fontweight="bold", color="#222", clip_on=True)

        if dt not in ld:
            ld[dt] = Rectangle((0, 0), 1, 1, fc=fc, ec="#555", lw=.6)

    # 显示任务权重和完成时间
    tw = {dag.task_id: dag.priority if hasattr(dag, "priority") else dag.weight for dag, _ in dags}
    if task_comps:
        for tid, ci in task_comps.items():
            if tid in tidx:
                wi = tw.get(tid, 1.0)
                ax1.text(max_end * 1.01, tidx[tid], f"C={ci} w={wi:.1f} wC={wi * ci:.0f}",
                         ha="left", va="center", fontsize=5, color="#333", fontstyle="italic")

    ttl = title + (f"  |  Sum(wi*Ci) = {objective:.0f}" if objective else "")
    ax1.set_yticks(range(nt))
    ax1.set_yticklabels(task_ids, fontsize=6)
    ax1.set_ylim(nt - .5, -.5)
    ax1.set_xlim(-max_end * .01, max_end * 1.18)
    ax1.set_ylabel("DAG", fontsize=9, fontweight="bold")
    ax1.set_title(ttl, fontsize=11, fontweight="bold", pad=8)
    ax1.grid(axis="x", alpha=.25, ls=":")
    ax1.axvline(max_end, color="#2E7D32", ls="--", lw=1, alpha=.5)
    ax1.legend(ld.values(), ld.keys(), bbox_to_anchor=(1.005, 1), loc="upper left",
               fontsize=6, title="Device", title_fontsize=7)

    # ── 下: 设备视图 ──
    for i in range(nd):
        ax2.axhspan(i - .45, i + .45, fc="#F5F5F5" if i % 2 == 0 else "#FFF", alpha=.5)

    lt = {}
    for r in results:
        dev = getattr(r, "device_instance", None) or getattr(r, "resource", "")
        y, dur = didx[dev], r.end - r.start
        fc = tcol[r.task_id]
        ax2.add_patch(Rectangle((r.start, y - .35), dur, .7, fc=fc, ec="#444", lw=.5, alpha=.85))

        if dur > max_end * .025:
            ax2.text(r.start + dur / 2, y, r.task_id.split("_")[0], ha="center", va="center",
                     fontsize=4.5, fontweight="bold", color="#222", clip_on=True)

        if r.task_id not in lt:
            lt[r.task_id] = Rectangle((0, 0), 1, 1, fc=fc, ec="#444", lw=.5)

    # 设备类型分隔线
    prev = None
    for i, d in enumerate(devs):
        dt = _dtype(d)
        if prev and dt != prev:
            ax2.axhline(i - .5, color="#999", lw=.8, alpha=.5)
        prev = dt

    ax2.set_yticks(range(nd))
    ax2.set_yticklabels(devs, fontsize=6)
    ax2.set_ylim(nd - .5, -.5)
    ax2.set_xlabel("Time (s)", fontsize=10)
    ax2.set_ylabel("Device", fontsize=9, fontweight="bold")
    ax2.grid(axis="x", alpha=.25, ls=":")
    ax2.axvline(max_end, color="#2E7D32", ls="--", lw=1, alpha=.5)
    nc = 2 if len(lt) > 12 else 1
    ax2.legend(lt.values(), lt.keys(), bbox_to_anchor=(1.005, 1), loc="upper left",
               fontsize=5, title="Batch", title_fontsize=6, ncol=nc)

    plt.tight_layout()
    plt.savefig(filename, bbox_inches="tight", dpi=150)
    plt.close(fig)
    print(f"Gantt chart saved: {filename}")
