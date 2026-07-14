"""算法对比可视化 — makespan / objective / time / violations."""

import numpy as np
import matplotlib.pyplot as plt
import matplotlib
matplotlib.use("Agg")


def plot_algorithm_comparison(stats, filename, batch_caps=None):
    """各算法 makespan / Sum(wi*Ci) / 求解时间 / TC violations 对比.

    Args:
        stats: Dict[alg_name, Tuple[makespan, objective, time_ms, n_violations]]
        filename: 输出文件路径
        batch_caps: Dict[device_type, capacity] 批处理容量配置 (可选)
    """
    plt.rcParams["font.sans-serif"] = ["PingFang SC", "Heiti SC", "STHeiti", "Arial"]
    plt.rcParams["axes.unicode_minus"] = False

    algs = list(stats.keys())
    n = len(algs)
    x = np.arange(n)
    w = 0.55
    colors = plt.get_cmap("Set2")(np.linspace(0, 1, n))

    fig, axes = plt.subplots(1, 4, figsize=(30, 6), dpi=120)
    titles = ["Makespan (s)", "Sum(wi*Ci)", "Solve Time (ms)", "TC Violations"]

    for ax, title, mi in zip(axes, titles, [0, 1, 2, 3]):
        vals = [stats[a][mi] for a in algs]
        bars = ax.bar(x, vals, w, color=colors, edgecolor="#333", linewidth=0.5)
        for bar, val in zip(bars, vals):
            txt = f"{val:.0f}" if mi != 2 else f"{val:.1f}"
            ax.text(bar.get_x() + bar.get_width() / 2, val, txt,
                    ha="center", va="bottom", fontsize=7, rotation=30)
        ax.set_xticks(x)
        ax.set_xticklabels(algs, rotation=35, ha="right", fontsize=8)
        ax.set_title(title, fontsize=11, fontweight="bold")
        ax.grid(axis="y", alpha=0.3)

    batch_info = ""
    if batch_caps:
        batch_info = "\n" + ", ".join(f"{k}: cap={v}" for k, v in batch_caps.items())

    plt.suptitle(f"Algorithm Comparison{batch_info}",
                 fontsize=13, fontweight="bold")
    plt.tight_layout()
    plt.savefig(filename, bbox_inches="tight", dpi=150)
    plt.close(fig)
    print(f"Comparison chart saved: {filename}")
