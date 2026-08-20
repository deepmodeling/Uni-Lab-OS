"""监听前端 Task 编排历史，生成 7 个样品的 S07/S09 测量分析报告。"""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import os
import sqlite3
import statistics
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


DEFAULT_DATABASE = Path.home() / "Documents" / "UniLab_test" / "run-history" / "history.db"


def default_analysis_dir() -> Path:
    configured = os.getenv("SZLAB_ANALYSIS_DIR")
    if configured:
        return Path(configured)
    if os.name == "nt":
        return Path(r"D:\New_Uni_Lab_OS\Data Analysis")
    wsl_path = Path("/mnt/d/New_Uni_Lab_OS/Data Analysis")
    return wsl_path if wsl_path.parent.exists() else Path("workflow_artifacts")


NODE_S07 = "w01_dose_powder_s07"
NODE_S09_LIQUID = "w03_add_liquid_s09"
NODE_S09_DENSITY = "w05_measure_density_s09"
WATCHED_NODES = (NODE_S07, NODE_S09_LIQUID, NODE_S09_DENSITY)


@dataclass
class Comparison:
    action_no: int
    sample_id: str
    item_no: int
    target: float
    actual: float
    unit: str
    source: str = ""


@dataclass
class DensityReading:
    action_no: int
    sample_id: str
    measurement_no: int
    phase: str
    mass_g: float
    volume_ml: float
    density_g_ml: float
    action_mean_g_ml: float
    formula: str


def load_json(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def mappings(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from mappings(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from mappings(nested)


def number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def raw_to_ml(value: Any, unit: str = "raw") -> float:
    amount = float(value)
    normalized = str(unit or "raw").strip().lower().replace("μ", "u").replace("µ", "u")
    if normalized == "raw":
        return amount / 10_000.0
    if normalized == "ul":
        return amount / 1_000.0
    if normalized == "ml":
        return amount
    raise ValueError(f"不支持的 S09 体积单位: {unit}")


def result_payload(result: Any) -> Any:
    """本地 runner 的结果外层是列表/快照包装，提取真实设备返回值。"""
    if isinstance(result, list) and len(result) == 1 and isinstance(result[0], dict):
        return result[0].get("result", result[0])
    return result


def find_first_mapping(value: Any, required_keys: set[str]) -> dict[str, Any] | None:
    return next((item for item in mappings(value) if required_keys <= item.keys()), None)


def extract_s07(rows: list[sqlite3.Row]) -> list[Comparison]:
    output: list[Comparison] = []
    action_no = 0
    for row in rows:
        if row["node_id"] != NODE_S07 or row["status"] != "completed" or not row["sample_id"]:
            continue
        params = load_json(row["params_json"], {})
        configured_additions = params.get("powder_additions") or []
        payload = result_payload(load_json(row["result_json"], {}))
        powder_results = next(
            (item.get("powder_results") for item in mappings(payload) if isinstance(item.get("powder_results"), list)),
            [],
        )
        for item_no, item in enumerate(powder_results, 1):
            # 编排参数是目标值的权威来源；设备结果中曾出现目标字段被临时
            # PLC 参数覆盖为 40000 的记录，不能用它污染目标量统计。
            configured = configured_additions[item_no - 1] if item_no <= len(configured_additions) else {}
            target = number(configured.get("target_weight", item.get("target_weight")))
            actual = number(item.get("balance_reading"))
            if target is None or actual is None:
                continue
            # 本报告面向实验室小剂量注粉。上一轮 Sample C 的第二条目标被
            # 污染为 40000 g（实际 9.591 g）；保留在 raw_actions.json 审计，
            # 但不让它进入正式统计和图表。
            if target > 100:
                continue
            action_no += 1
            output.append(Comparison(action_no, row["sample_id"], item_no, target, actual, "g"))
    return output


def extract_density(rows: list[sqlite3.Row]) -> tuple[list[DensityReading], dict[str, float]]:
    output: list[DensityReading] = []
    sample_means: dict[str, float] = {}
    action_no = 0
    for row in rows:
        if row["node_id"] != NODE_S09_DENSITY or row["status"] != "completed" or not row["sample_id"]:
            continue
        payload = result_payload(load_json(row["result_json"], {}))
        data = find_first_mapping(
            payload,
            {"density_volume_ml", "aspirate_balance_readings", "dispense_balance_readings"},
        )
        if data is None:
            continue
        volume_ml = number(data.get("density_volume_ml"))
        if volume_ml is None or volume_ml <= 0:
            continue
        pairs: list[tuple[str, float]] = []
        for phase, key in (("抽液", "aspirate_balance_readings"), ("放液", "dispense_balance_readings")):
            for mass in data.get(key) or []:
                parsed = number(mass)
                if parsed is not None:
                    pairs.append((phase, parsed))
        if not pairs:
            continue
        densities = [abs(mass) / volume_ml for _, mass in pairs]
        mean_density = statistics.fmean(densities)
        action_no += 1
        sample_means[row["sample_id"]] = mean_density
        for measurement_no, ((phase, mass), density) in enumerate(zip(pairs, densities), 1):
            output.append(
                DensityReading(
                    action_no=action_no,
                    sample_id=row["sample_id"],
                    measurement_no=measurement_no,
                    phase=phase,
                    mass_g=mass,
                    volume_ml=volume_ml,
                    density_g_ml=density,
                    action_mean_g_ml=mean_density,
                    formula=f"|{mass:.9g}| g / {volume_ml:.9g} mL = {density:.9g} g/mL",
                )
            )
    return output, sample_means


def _dispense_readings(payload: Any) -> list[float]:
    readings: list[float] = []
    seen: set[tuple[int, float]] = set()
    for item in mappings(payload):
        if number(item.get("process")) != 8:
            continue
        reading = number(item.get("balance_reading"))
        if reading is None and isinstance(item.get("balance"), dict):
            reading = number(item["balance"].get("balance_reading"))
        if reading is None:
            continue
        marker = (id(item), reading)
        if marker not in seen:
            seen.add(marker)
            readings.append(reading)
    return readings


def extract_liquid(
    rows: list[sqlite3.Row],
    densities: dict[str, float],
    external_weights: dict[str, list[float]] | None = None,
    external_liquid_density_g_ml: float | None = None,
) -> list[Comparison]:
    output: list[Comparison] = []
    action_no = 0
    for row in rows:
        if row["node_id"] != NODE_S09_LIQUID or row["status"] != "completed" or not row["sample_id"]:
            continue
        params = load_json(row["params_json"], {})
        additions = params.get("liquid_additions") or [params]
        targets = [
            raw_to_ml(item.get("volume", params.get("volume", 0)), item.get("volume_unit", params.get("volume_unit", "raw")))
            for item in additions
        ]
        payload = result_payload(load_json(row["result_json"], {}))
        weights = (external_weights or {}).get(row["sample_id"]) or _dispense_readings(payload)
        density = (
            external_liquid_density_g_ml
            if external_weights and row["sample_id"] in external_weights
            else densities.get(row["sample_id"])
        )
        for item_no, (target_ml, weight_g) in enumerate(zip(targets, weights), 1):
            if density is None or density <= 0:
                continue
            action_no += 1
            output.append(
                Comparison(
                    action_no,
                    row["sample_id"],
                    item_no,
                    target_ml,
                    abs(weight_g) / density,
                    "mL",
                    source=f"|{weight_g:.9g} g| / {density:.9g} g/mL",
                )
            )
    return output


def comparison_stats(items: list[Comparison]) -> dict[str, Any]:
    if not items:
        return {"count": 0}
    errors = [item.actual - item.target for item in items]
    absolute = [abs(value) for value in errors]
    squared = [value * value for value in errors]
    relative = [abs(error) / abs(item.target) * 100 for error, item in zip(errors, items) if item.target]
    total_abs = sum(absolute)
    max_index = max(range(len(items)), key=absolute.__getitem__)
    return {
        "count": len(items),
        "mae": statistics.fmean(absolute),
        "mse": statistics.fmean(squared),
        "rmse": math.sqrt(statistics.fmean(squared)),
        "mean_signed_error": statistics.fmean(errors),
        "max_absolute_error": absolute[max_index],
        "max_error_sample": items[max_index].sample_id,
        "mean_absolute_error_rate_percent": statistics.fmean(relative) if relative else None,
        "total_absolute_error": total_abs,
        "error_share_percent": [value / total_abs * 100 if total_abs else 0.0 for value in absolute],
        "unit": items[0].unit,
    }


def density_stats(items: list[DensityReading]) -> dict[str, Any]:
    if not items:
        return {"count": 0, "reference": "同一动作内重复测量均值"}
    comparisons = [
        Comparison(item.action_no, item.sample_id, item.measurement_no, item.action_mean_g_ml, item.density_g_ml, "g/mL")
        for item in items
    ]
    result = comparison_stats(comparisons)
    result["reference"] = "同一动作内重复测量均值（重复性误差，不是相对标准密度的准确度）"
    result["overall_mean_density_g_ml"] = statistics.fmean(item.density_g_ml for item in items)
    result["sample_standard_deviation_g_ml"] = statistics.stdev(item.density_g_ml for item in items) if len(items) > 1 else 0.0
    return result


def theoretical_density_targets(rows: list[sqlite3.Row], water_density_g_ml: float = 1.0) -> dict[str, float]:
    """按 (目标水质量 + 目标粉末质量) / 目标水体积计算配方目标密度。"""
    powder_g: dict[str, float] = {}
    water_ml: dict[str, float] = {}
    for row in rows:
        sample_id = row["sample_id"]
        if not sample_id:
            continue
        params = load_json(row["params_json"], {})
        if row["node_id"] == NODE_S07:
            additions = params.get("powder_additions") or [params]
            powder_g[sample_id] = sum(
                value for item in additions
                if (value := number(item.get("target_weight"))) is not None and value <= 100
            )
        elif row["node_id"] == NODE_S09_LIQUID:
            additions = params.get("liquid_additions") or [params]
            water_ml[sample_id] = sum(
                raw_to_ml(
                    item.get("volume", params.get("volume", 0)),
                    item.get("volume_unit", params.get("volume_unit", "raw")),
                )
                for item in additions
            )
    return {
        sample_id: (volume_ml * water_density_g_ml + powder_g[sample_id]) / volume_ml
        for sample_id, volume_ml in water_ml.items()
        if volume_ml > 0 and sample_id in powder_g
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def svg_chart(path: Path, title: str, labels: list[str], target: list[float], actual: list[float], unit: str) -> None:
    width, height = 1200, 620
    left, right, top, bottom = 95, 35, 70, 155
    plot_w, plot_h = width - left - right, height - top - bottom
    values = target + actual
    low, high = (min(values), max(values)) if values else (0.0, 1.0)
    padding = max((high - low) * 0.12, abs(high) * 0.02, 1e-6)
    low, high = low - padding, high + padding
    x = lambda i: left + (plot_w * i / max(len(labels) - 1, 1))
    y = lambda value: top + plot_h * (high - value) / (high - low)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        f'<text x="{width/2}" y="34" text-anchor="middle" font-size="22" font-family="sans-serif">{html.escape(title)}</text>',
    ]
    for tick in range(6):
        value = low + (high - low) * tick / 5
        yy = y(value)
        parts += [
            f'<line x1="{left}" y1="{yy:.2f}" x2="{width-right}" y2="{yy:.2f}" stroke="#e5e7eb"/>',
            f'<text x="{left-10}" y="{yy+5:.2f}" text-anchor="end" font-size="12">{value:.5g}</text>',
        ]
    parts.append(f'<text x="22" y="{top+plot_h/2}" transform="rotate(-90 22 {top+plot_h/2})" text-anchor="middle" font-size="15">{html.escape(unit)}</text>')
    for index, label in enumerate(labels):
        xx = x(index)
        parts.append(f'<text x="{xx:.2f}" y="{top+plot_h+22}" transform="rotate(35 {xx:.2f} {top+plot_h+22})" text-anchor="start" font-size="11">{html.escape(label)}</text>')
    for values_line, color, name in ((target, "#2563eb", "目标/基准"), (actual, "#dc2626", "实际/测试")):
        points = " ".join(f"{x(i):.2f},{y(value):.2f}" for i, value in enumerate(values_line))
        parts.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2.5"/>')
        for i, value in enumerate(values_line):
            parts.append(f'<circle cx="{x(i):.2f}" cy="{y(value):.2f}" r="4" fill="{color}"/>')
        legend_x = width - 235 if name.startswith("目标") else width - 115
        parts += [f'<line x1="{legend_x}" y1="52" x2="{legend_x+25}" y2="52" stroke="{color}" stroke-width="3"/>', f'<text x="{legend_x+30}" y="57" font-size="13">{name}</text>']
    parts += [f'<text x="{left+plot_w/2}" y="{height-18}" text-anchor="middle" font-size="15">动作次数 / Sample</text>', "</svg>"]
    path.write_text("\n".join(parts), encoding="utf-8")


def _fmt(value: Any) -> str:
    return "—" if value is None else f"{value:.8g}" if isinstance(value, float) else str(value)


def report_html(
    path: Path,
    run_id: str,
    s07: list[Comparison],
    liquid: list[Comparison],
    density: list[DensityReading],
    density_accuracy: list[Comparison],
    stats: dict[str, Any],
) -> None:
    def table(headers: list[str], rows: list[list[Any]]) -> str:
        head = "".join(f"<th>{html.escape(str(value))}</th>" for value in headers)
        body = "".join("<tr>" + "".join(f"<td>{html.escape(_fmt(value))}</td>" for value in row) + "</tr>" for row in rows)
        return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"

    def metric_cards(category: str) -> str:
        values = stats.get(category, {})
        unit = values.get("unit", "")
        squared_unit = f"{unit}²" if unit else ""
        shares = values.get("error_share_percent") or []
        cards = (
            ("MAE", values.get("mae"), unit),
            ("MSE", values.get("mse"), squared_unit),
            ("最大绝对误差", values.get("max_absolute_error"), unit),
            ("最大误差 Sample", values.get("max_error_sample"), ""),
            ("平均绝对误差率", values.get("mean_absolute_error_rate_percent"), "%"),
            ("最大单点误差占比", max(shares) if shares else None, "%"),
        )
        return '<div class="metrics">' + "".join(
            f'<div class="metric"><div class="metric-label">{html.escape(label)}</div><div class="metric-value">{html.escape(_fmt(value))} {html.escape(suffix)}</div></div>'
            for label, value, suffix in cards
        ) + "</div>"

    stat_rows = []
    for category, values in stats.items():
        shares = values.get("error_share_percent") or []
        stat_rows.append([category, values.get("count"), values.get("mae"), values.get("mse"), values.get("rmse"), values.get("max_absolute_error"), values.get("mean_absolute_error_rate_percent"), max(shares) if shares else None, values.get("total_absolute_error"), values.get("unit", "")])
    density_rows = [[x.action_no, x.sample_id, x.measurement_no, x.phase, x.mass_g, x.volume_ml, x.formula, x.action_mean_g_ml] for x in density]
    document = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>SZLab 7 样品统计分析</title>
<style>body{{font-family:Arial,'Microsoft YaHei',sans-serif;margin:28px;color:#172033}}table{{border-collapse:collapse;width:100%;margin:12px 0 28px}}th,td{{border:1px solid #ccd3df;padding:7px;text-align:right}}th{{background:#edf2f7}}td:nth-child(2),th:nth-child(2){{text-align:left}}img{{max-width:100%;border:1px solid #ddd}}code{{background:#f2f4f7;padding:2px 5px}}.metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:12px 0 22px}}.metric{{border:1px solid #cbd5e1;border-left:5px solid #2563eb;border-radius:7px;padding:12px;background:#f8fafc}}.metric-label{{font-size:13px;color:#475569}}.metric-value{{font-size:21px;font-weight:700;margin-top:5px;color:#0f172a}}</style>
<h1>SZLab 7 样品动作测量统计</h1><p>Run ID：<code>{html.escape(run_id)}</code>；生成时间：{datetime.now().astimezone().isoformat(timespec='seconds')}</p>
<p>误差 = 实际值 − 目标/基准值；误差率 = |误差| / |目标/基准| × 100%；误差占比 = 单条绝对误差 / 本类绝对误差总和 × 100%。目标密度按“(目标水质量 + 目标粉末质量) / 目标水体积”计算，并假设粉末溶解后不引起体积变化。</p>
<h2>汇总统计</h2>{table(['类别','数量','MAE','MSE','RMSE','最大绝对误差','平均绝对误差率(%)','最大单点误差占比(%)','绝对误差总和','单位'], stat_rows)}
<h2>S07 注粉</h2>{metric_cards('S07注粉')}<img src="s07_powder.svg">{table(['动作','Sample','粉末序号','目标(g)','实际(g)','误差(g)','绝对误差(g)','误差率(%)','误差占比(%)'], [[x.action_no,x.sample_id,x.item_no,x.target,x.actual,x.actual-x.target,abs(x.actual-x.target),abs(x.actual-x.target)/abs(x.target)*100 if x.target else None,getattr(x,'error_share_percent',None)] for x in s07])}
<h2>S09 加液</h2>{metric_cards('S09加液')}<img src="s09_liquid.svg"><p>实际体积按明细中的密度换算式计算；本次外部加液数据按水的密度 1.000 g/mL 处理。</p>{table(['动作','Sample','液体序号','目标(mL)','实际(mL)','误差(mL)','绝对误差(mL)','误差率(%)','误差占比(%)','计算式'], [[x.action_no,x.sample_id,x.item_no,x.target,x.actual,x.actual-x.target,abs(x.actual-x.target),abs(x.actual-x.target)/abs(x.target)*100 if x.target else None,getattr(x,'error_share_percent',None),x.source] for x in liquid])}
<h2>S09 密度准确度</h2>{metric_cards('S09密度准确度')}<img src="s09_density.svg"><p>主图比较各 Sample 的配方目标密度与 6 次实测密度均值。</p>{table(['Sample','目标密度','实测平均密度','误差','绝对误差','误差率(%)','误差占比(%)'], [[x.sample_id,x.target,x.actual,x.actual-x.target,abs(x.actual-x.target),abs(x.actual-x.target)/abs(x.target)*100 if x.target else None,getattr(x,'error_share_percent',None)] for x in density_accuracy])}<h3>S09 密度重复性</h3>{metric_cards('S09密度重复性')}<h3>逐次密度列式</h3>{table(['动作','Sample','测量序号','阶段','质量(g)','体积(mL)','列式结果','动作均值(g/mL)'], density_rows)}
</html>"""
    path.write_text(document, encoding="utf-8")


def load_liquid_weight_csv(path: Path) -> dict[str, list[float]]:
    """读取列为 Sample、行为每次加液净重的 CSV。"""
    with path.open(newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        result: dict[str, list[float]] = {str(name).strip(): [] for name in reader.fieldnames or []}
        for row in reader:
            for sample_id in result:
                value = number(row.get(sample_id))
                if value is not None:
                    result[sample_id].append(value)
    return result


def generate(
    output_dir: Path,
    run_id: str,
    rows: list[sqlite3.Row],
    *,
    external_liquid_weights: dict[str, list[float]] | None = None,
    external_liquid_density_g_ml: float | None = None,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    s07 = extract_s07(rows)
    density, density_means = extract_density(rows)
    liquid = extract_liquid(
        rows,
        density_means,
        external_liquid_weights,
        external_liquid_density_g_ml,
    )
    target_densities = theoretical_density_targets(
        rows,
        water_density_g_ml=(external_liquid_density_g_ml or 1.0),
    )
    density_accuracy = [
        Comparison(index, sample_id, 1, target_densities[sample_id], actual, "g/mL")
        for index, (sample_id, actual) in enumerate(density_means.items(), 1)
        if sample_id in target_densities
    ]
    stats = {
        "S07注粉": comparison_stats(s07),
        "S09加液": comparison_stats(liquid),
        "S09密度准确度": comparison_stats(density_accuracy),
        "S09密度重复性": density_stats(density),
    }
    for values, stat in ((s07, stats["S07注粉"]), (liquid, stats["S09加液"])):
        shares = stat.get("error_share_percent", [])
        for item, share in zip(values, shares):
            setattr(item, "error_share_percent", share)
    for item, share in zip(
        density_accuracy,
        stats["S09密度准确度"].get("error_share_percent", []),
    ):
        setattr(item, "error_share_percent", share)
    write_csv(output_dir / "s07_powder.csv", [{**asdict(x), "error": x.actual-x.target, "absolute_error": abs(x.actual-x.target), "error_rate_percent": abs(x.actual-x.target)/abs(x.target)*100 if x.target else None, "error_share_percent": getattr(x, "error_share_percent", None)} for x in s07])
    write_csv(output_dir / "s09_liquid.csv", [{**asdict(x), "error": x.actual-x.target, "absolute_error": abs(x.actual-x.target), "error_rate_percent": abs(x.actual-x.target)/abs(x.target)*100 if x.target else None, "error_share_percent": getattr(x, "error_share_percent", None)} for x in liquid])
    density_errors = [abs(x.density_g_ml - x.action_mean_g_ml) for x in density]
    density_error_total = sum(density_errors)
    write_csv(
        output_dir / "s09_density.csv",
        [
            {
                **asdict(x),
                "repeatability_error": x.density_g_ml - x.action_mean_g_ml,
                "absolute_repeatability_error": error,
                "repeatability_error_rate_percent": error / abs(x.action_mean_g_ml) * 100 if x.action_mean_g_ml else None,
                "error_share_percent": error / density_error_total * 100 if density_error_total else 0.0,
            }
            for x, error in zip(density, density_errors)
        ],
    )
    write_csv(
        output_dir / "s09_density_summary.csv",
        [
            {
                **asdict(item),
                "error": item.actual - item.target,
                "absolute_error": abs(item.actual - item.target),
                "error_rate_percent": abs(item.actual - item.target) / abs(item.target) * 100,
            }
            for item in density_accuracy
        ],
    )
    (output_dir / "raw_actions.json").write_text(
        json.dumps(
            [
                {
                    "run_id": row["run_id"],
                    "execution_id": row["execution_id"],
                    "sample_id": row["sample_id"],
                    "node_id": row["node_id"],
                    "action_name": row["action_name"],
                    "status": row["status"],
                    "started_at": row["started_at"],
                    "finished_at": row["finished_at"],
                    "params": load_json(row["params_json"], {}),
                    "result": load_json(row["result_json"], None),
                }
                for row in rows
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (output_dir / "statistics.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    svg_chart(output_dir / "s07_powder.svg", "S07 注粉：目标量与实际量", [f"{x.action_no}-{x.sample_id}" for x in s07], [x.target for x in s07], [x.actual for x in s07], "重量 (g)")
    svg_chart(output_dir / "s09_liquid.svg", "S09 加液：目标量与实际量", [f"{x.action_no}-{x.sample_id}" for x in liquid], [x.target for x in liquid], [x.actual for x in liquid], "体积 (mL)")
    svg_chart(
        output_dir / "s09_density.svg",
        "S09 密度：配方目标密度与实测平均密度",
        [f"{x.action_no}-{x.sample_id}" for x in density_accuracy],
        [x.target for x in density_accuracy],
        [x.actual for x in density_accuracy],
        "密度 (g/mL)",
    )
    report_html(
        output_dir / "report.html",
        run_id,
        s07,
        liquid,
        density,
        density_accuracy,
        stats,
    )
    report_html(
        output_dir / "report_with_metrics.html",
        run_id,
        s07,
        liquid,
        density,
        density_accuracy,
        stats,
    )
    return {"s07_count": len(s07), "liquid_count": len(liquid), "density_count": len(density), "sample_count": len({row["sample_id"] for row in rows if row["sample_id"]})}


def select_run(
    connection: sqlite3.Connection,
    requested: str | None,
    *,
    minimum_completed_density_samples: int = 0,
) -> str | None:
    if requested:
        exists = connection.execute("SELECT 1 FROM run_sessions WHERE run_id = ?", (requested,)).fetchone()
        return requested if exists else None
    row = connection.execute(
        """SELECT r.run_id FROM run_sessions r
        WHERE EXISTS (SELECT 1 FROM action_records a WHERE a.run_id=r.run_id AND a.node_id IN (?,?,?))
          AND (SELECT COUNT(DISTINCT a.sample_id) FROM action_records a
               WHERE a.run_id=r.run_id AND a.node_id=? AND a.status='completed') >= ?
        ORDER BY r.updated_at DESC LIMIT 1""",
        (*WATCHED_NODES, NODE_S09_DENSITY, minimum_completed_density_samples),
    ).fetchone()
    return str(row[0]) if row else None


def read_rows(connection: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM action_records WHERE run_id=? AND node_id IN (?,?,?) ORDER BY started_at, execution_id",
        (run_id, *WATCHED_NODES),
    ).fetchall()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="监听并分析前端 7 样品 Task 编排的 S07/S09 数据")
    parser.add_argument("--database", type=Path, default=Path(os.getenv("UNILABOS_RUN_HISTORY_DIR", "")) / "history.db" if os.getenv("UNILABOS_RUN_HISTORY_DIR") else DEFAULT_DATABASE)
    parser.add_argument("--run-id", help="指定运行 ID；默认自动选择最新包含目标动作的运行")
    parser.add_argument("--samples", type=int, default=7)
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--timeout", type=float, default=8 * 60 * 60, help="最长等待秒数")
    parser.add_argument("--output", type=Path, help="报告输出目录")
    parser.add_argument(
        "--liquid-weight-csv",
        type=Path,
        help="外部 S09 去皮净重 CSV；列名为 Sample ID，每行为一次加液，单位 g",
    )
    parser.add_argument(
        "--liquid-density",
        type=float,
        help="外部加液净重换算使用的固定密度，单位 g/mL；水取 1.0",
    )
    parser.add_argument("--no-wait", action="store_true", help="立即分析当前已有数据")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.samples <= 0 or args.poll_interval <= 0 or args.timeout <= 0:
        raise SystemExit("samples、poll-interval 和 timeout 必须大于 0")
    if args.liquid_density is not None and args.liquid_density <= 0:
        raise SystemExit("--liquid-density 必须大于 0")
    deadline = time.monotonic() + args.timeout
    run_id: str | None = None
    print(f"只读监听运行历史：{args.database}")
    while time.monotonic() < deadline:
        if not args.database.exists():
            time.sleep(args.poll_interval)
            continue
        try:
            uri = args.database.resolve().as_uri() + "?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=10) as connection:
                connection.row_factory = sqlite3.Row
                # 未显式指定 run-id 时持续选择最近更新的运行；这样脚本先启动、
                # 前端稍后创建新 Task 时，不会被历史运行锁住。
                selected = select_run(
                    connection,
                    args.run_id,
                    minimum_completed_density_samples=(args.samples if args.no_wait else 0),
                )
                if selected is None:
                    time.sleep(args.poll_interval)
                    continue
                if run_id != selected:
                    run_id = selected
                    print(f"监听 Run ID：{run_id}")
                rows = read_rows(connection, run_id)
        except (sqlite3.Error, OSError) as exc:
            print(f"数据库暂不可读，稍后重试：{exc}")
            time.sleep(args.poll_interval)
            continue
        density_samples = {row["sample_id"] for row in rows if row["node_id"] == NODE_S09_DENSITY and row["status"] == "completed"}
        print(f"\r已完成密度动作的样品：{len(density_samples)}/{args.samples}", end="", flush=True)
        if args.no_wait or len(density_samples) >= args.samples:
            break
        time.sleep(args.poll_interval)
    else:
        raise SystemExit("等待 7 个样品完成超时")
    if run_id is None:
        raise SystemExit("没有找到包含 S07/S09 目标动作的运行")
    output = args.output or default_analysis_dir() / f"szlab_7sample_analysis_{datetime.now():%Y%m%d_%H%M%S}"
    external_weights = (
        load_liquid_weight_csv(args.liquid_weight_csv)
        if args.liquid_weight_csv
        else None
    )
    summary = generate(
        output,
        run_id,
        rows,
        external_liquid_weights=external_weights,
        external_liquid_density_g_ml=args.liquid_density,
    )
    print(f"\n分析完成：{output.resolve()}")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
