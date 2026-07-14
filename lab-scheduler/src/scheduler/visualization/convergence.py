"""收敛曲线可视化 — GA vs CP-SAT."""

import matplotlib.pyplot as plt
import matplotlib
matplotlib.use("Agg")


def plot_convergence(ga_history, cpsat_history, title, filename):
    """绘制 GA vs CP-SAT 收敛曲线.

    Args:
        ga_history: List[Tuple[elapsed_time, objective]] GA 收敛历史
        cpsat_history: List[Tuple[elapsed_time, objective]] CP-SAT 收敛历史
        title: 图表标题
        filename: 输出文件路径
    """
    plt.rcParams["font.sans-serif"] = ["PingFang SC", "Heiti SC", "STHeiti", "Arial"]
    plt.rcParams["axes.unicode_minus"] = False

    fig, ax = plt.subplots(figsize=(12, 6), dpi=120)

    if ga_history:
        ga_t = [h[0] for h in ga_history]
        ga_obj = [h[1] for h in ga_history]
        ax.step(ga_t, ga_obj, where="post", label="GA", color="#E53935",
                linewidth=2, marker="o", markersize=3)

    if cpsat_history:
        cp_t = [h[0] for h in cpsat_history]
        cp_obj = [h[1] for h in cpsat_history]
        ax.step(cp_t, cp_obj, where="post", label="CP-SAT", color="#1E88E5",
                linewidth=2, marker="s", markersize=3)

    ax.set_xlabel("Elapsed Time (s)", fontsize=11)
    ax.set_ylabel("Objective: Sum(wi * Ci)", fontsize=11)
    ax.set_title(title, fontsize=13, fontweight="bold")
    ax.legend(fontsize=10)
    ax.grid(alpha=0.3)

    # 尝试对数 x 轴 (如果跨度大)
    all_t = ([h[0] for h in ga_history] if ga_history else []) + \
            ([h[0] for h in cpsat_history] if cpsat_history else [])
    if all_t and max(all_t) / max(min(t for t in all_t if t > 0), 0.001) > 50:
        ax.set_xscale("log")

    plt.tight_layout()
    plt.savefig(filename, bbox_inches="tight", dpi=150)
    plt.close(fig)
    print(f"Convergence chart saved: {filename}")


def plot_convergence_multi(results_by_limit, filename):
    """多时间上限的收敛对比.

    Args:
        results_by_limit: Dict[time_limit, Dict[algorithm_name, history]]
            例: {10: {"GA": [(t, obj), ...], "CP-SAT": [(t, obj), ...]}}
        filename: 输出文件路径
    """
    plt.rcParams["font.sans-serif"] = ["PingFang SC", "Heiti SC", "STHeiti", "Arial"]
    plt.rcParams["axes.unicode_minus"] = False

    limits = sorted(results_by_limit.keys())
    n = len(limits)
    fig, axes = plt.subplots(1, n, figsize=(6 * n, 5), dpi=120, sharey=True)
    if n == 1:
        axes = [axes]

    ga_color = "#E53935"
    cp_color = "#1E88E5"

    for ax, tl in zip(axes, limits):
        data = results_by_limit[tl]
        ga_h = data.get("GA", [])
        cp_h = data.get("CP-SAT", [])

        if ga_h:
            ax.step([h[0] for h in ga_h], [h[1] for h in ga_h],
                    where="post", label="GA", color=ga_color, linewidth=2,
                    marker="o", markersize=2)
        if cp_h:
            ax.step([h[0] for h in cp_h], [h[1] for h in cp_h],
                    where="post", label="CP-SAT", color=cp_color, linewidth=2,
                    marker="s", markersize=2)

        ga_final = ga_h[-1][1] if ga_h else None
        cp_final = cp_h[-1][1] if cp_h else None
        info_lines = []
        if ga_final is not None:
            info_lines.append(f"GA: {ga_final:.0f}")
        if cp_final is not None:
            info_lines.append(f"CP: {cp_final:.0f}")
        ax.text(0.98, 0.98, "\n".join(info_lines), transform=ax.transAxes,
                ha="right", va="top", fontsize=8, fontstyle="italic",
                bbox=dict(boxstyle="round,pad=0.3", fc="lightyellow", alpha=0.8))

        ax.set_xlabel("Elapsed (s)", fontsize=10)
        ax.set_title(f"Time Limit = {tl}s", fontsize=11, fontweight="bold")
        ax.legend(fontsize=8, loc="upper right")
        ax.grid(alpha=0.3)

    axes[0].set_ylabel("Objective: Sum(wi * Ci)", fontsize=10)
    plt.suptitle("GA vs CP-SAT Convergence at Different Time Limits",
                 fontsize=14, fontweight="bold")
    plt.tight_layout()
    plt.savefig(filename, bbox_inches="tight", dpi=150)
    plt.close(fig)
    print(f"Multi-convergence chart saved: {filename}")
