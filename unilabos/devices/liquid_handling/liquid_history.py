"""P9 — liquid_history schema v3 与 helper 函数。

独立模块，**不依赖 pylabrobot**，可在 PLR 环境缺失时单独单测。

模块由 ``liquid_handler_abstract.py`` 在 runtime 挂载点（set_liquid / aspirate /
dispense）调用，且由 ``resource_tracker._augment_states_with_liquid_history`` 在
serialize 链路使用。

详见 ``product_designs/protocol_convert/09-liquid-history-unknown-debug.md``。
"""

from __future__ import annotations

import re
from typing import Any, List, Tuple

from typing_extensions import TypedDict


# PLR ``VolumeTracker._get_liquid_name(None)`` 在 add_liquid 缺 name 时会写入
# ``f"Unknown{counter}"`` 并 bump ``unknown_counter``;``patch_unknown_history_last``
# 用此正则识别 PLR 自动占位名,允许就地改写为真实化学名(详见
# ``RESOLUTION-2026-05-28-plr-liquid-history-double-write.md`` 后续 §"PLR 上游
# add_liquid 漏传 liquid name 的兜底改名补丁")。
_UNKNOWN_LIQUID_NAME_RE = re.compile(r"^Unknown\d+$")


# liquid_history 元素 schema v3
# 详见 ``product_designs/protocol_convert/09-liquid-history-unknown-debug.md`` §6.1。
# 旧格式（v2 ``(name, vol)`` 元组、list[str]）由 ``normalize_liquid_history`` 升级。
class LiquidHistoryEntry(TypedDict, total=False):
    name: str        # 液体名（如 "Plasma"；与 P8 reagent.liquid_name 联动；缺省 ""）
    volume: float    # 操作体积（µL；aspirate 为负，dispense / set 为正）
    action: str      # "set" / "aspirate" / "dispense" / "legacy" / "auto_init"
    timestamp: str   # ISO8601 UTC（OS runtime 写入时填，前端写入时可省略）


# liquid_history 单 well 上限：超过则滚动丢弃头部
# 既限制内存（典型 8 通道 transfer 一次产生 ≤16 条），也防止极端 batch 拖慢前端渲染
LIQUID_HISTORY_MAX_ENTRIES = 1000


def well_current_liquid_name(well: Any) -> str:
    """从 ``well.tracker.liquids`` 末项读取当前液体名（PLR ``Liquid`` enum / str / None 兼容）。

    P9：作为 ``aspirate`` 写入 history 时 ``name`` 字段的来源。
    返回 ``""`` 表示未知（不写字面 "unknown"，避免被前端误展示）。
    """
    tracker = getattr(well, "tracker", None)
    if tracker is None:
        return ""
    liquids = getattr(tracker, "liquids", None)
    if not liquids:
        # PLR 提供 get_liquids() 时优先用之（返回 list[(Liquid|None, vol)]）
        try:
            liquids = tracker.get_liquids()  # type: ignore[attr-defined]
        except Exception:
            liquids = None
    if not liquids:
        return ""
    last = liquids[-1]
    if isinstance(last, (list, tuple)) and last:
        candidate = last[0]
    else:
        candidate = last
    if candidate is None:
        return ""
    name = getattr(candidate, "name", None)
    if isinstance(name, str) and name:
        return name
    if isinstance(candidate, str):
        return candidate
    return ""


def append_liquid_history(
    well: Any,
    liquid_name: str,
    volume: float,
    action: str,
) -> None:
    """P9 — 统一写入 ``well.tracker.liquid_history``（PLR 扩展属性）。

    设计要点：
        - 元素为 v3 dict 形态 ``{name, volume, action, timestamp}``，与
          :class:`LiquidHistoryEntry` schema 一致。
        - ``aspirate`` 的 ``volume`` 应为**负数**（与 dispense/set 正数对称，
          ``sum(history.volume)`` ≈ 当前残量）。
        - **0-vol 跳过**（用户 2026-05-28 决策）：``abs(volume) < 1e-9`` 直接 return，
          避免 ``set_liquid(name, 0)`` 等场景把 ``(name, 0.0)`` 噪声塞进审计日志。
          注意：跳过点**位于 history 归一化之后**——即便不 append，也保证 history 已被
          刷成 2-tuple 形态，防止后续 PLR ``current_liquids`` 解 dict / 3-tuple 失败。
          PLR ``tracker.liquids`` 仍由 caller 经 ``set_liquids`` 维护，
          液体身份不丢失（``well_current_liquid_name`` / tip-reuse 仍工作）。
        - ``well`` 无 tracker 或 tracker 不可写时 graceful 静默（避免污染主流程）。
        - 滚动上限 ``LIQUID_HISTORY_MAX_ENTRIES``：超出时丢弃**头部**（保留最近）。

    详见 ``product_designs/protocol_convert/09-liquid-history-unknown-debug.md`` §6.2。
    """
    tracker = getattr(well, "tracker", None)
    if tracker is None:
        return
    history = getattr(tracker, "liquid_history", None)
    if not isinstance(history, list):
        history = []
        try:
            tracker.liquid_history = history  # type: ignore[attr-defined]
        except Exception:
            return  # tracker 拒绝写扩展属性（极少见）；静默放弃
    # 兼容修复：PLR VolumeTracker.current_liquids 依赖 tracker.liquid_history 为
    # list[(name, vol)]；若写入 dict 会在 `for name, vol in liquid_history` 时崩溃。
    # 这里把历史就地归一为 tuple 形态，再 append tuple，避免 unpack ValueError。
    # 注（2026-05-28）：必须在 0-vol skip **之前** 做，否则若调用方第一次写就是
    # ``set_liquid(name, 0)``、history 又来自远端 dict snapshot，会绕过归一化保险，
    # 让后续 PLR ``current_liquids`` 解 dict 失败 → 间接拖垮 transfer / 残留 tip。
    normalized_pairs: List[Tuple[str, float]] = []
    for item in history:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            name_val = str(item[0] or "")
            try:
                vol_val = float(item[1])
            except (TypeError, ValueError):
                vol_val = 0.0
            normalized_pairs.append((name_val, vol_val))
        elif isinstance(item, dict):
            name_val = str(item.get("name", ""))
            try:
                vol_val = float(item.get("volume", 0.0) or 0.0)
            except (TypeError, ValueError):
                vol_val = 0.0
            normalized_pairs.append((name_val, vol_val))
        elif isinstance(item, str):
            normalized_pairs.append((item, 0.0))
    history[:] = normalized_pairs
    # 0-vol 跳过（用户 2026-05-28 决策）：set_liquid("agar", 0) 等场景不写审计条目。
    # 用 abs(...) < 1e-9 防浮点误差；aspirate 的负 vol（如 -50.0）正常通过。
    # 关键时序：放在归一化 **之后**，确保即便本次跳过 append，history 仍被刷新为 2-tuple，
    # 不会让 PLR ``current_liquids`` 后续踩到 dict / 3-tuple 旧形态。
    try:
        if abs(float(volume)) < 1e-9:
            return
    except (TypeError, ValueError):
        # 非数值 volume：让原有路径走（保守，不偷偷跳过；下面 float(volume) 会再 raise 提示）
        pass
    entry = (str(liquid_name or ""), float(volume))
    history.append(entry)
    overflow = len(history) - LIQUID_HISTORY_MAX_ENTRIES
    if overflow > 0:
        del history[:overflow]


# ---------------------------------------------------------------------------
# P10 v2 — Tip 复用 ``tracker.liquids`` 等价 helper（详见
# ``product_designs/protocol_convert/10-tip-reuse-by-liquid-history.md`` §3.2）
#
# 设计原则：
#   - 信号源使用 PLR 原生 ``well.tracker.liquids`` 末项（"well 此刻顶层液体"），
#     而非 P9 扩展属性 ``liquid_history``；P10 v2 因此不依赖 P9 是否落地。
#   - 名称比较使用严格字符串相等；空 / "unknown" / "none" 一律保守视为未知 →
#     不触发 liquids 复用，落回 identity-only 现状（零回归）。
#   - 与 P9 现有 ``liquid_names_before_aspirate`` 同模式：aspirate 之前预读
#     source 当前液体名，避免 PLR 顶层归零时 pop ``liquids`` 拿不到身份。
#   - 4 个 helper 共同居于本 PLR-free 模块，方便单元测试在不安装 pylabrobot
#     的环境下独立运行。
# ---------------------------------------------------------------------------


def is_known_liquid_name(name: Any) -> bool:
    """空字符串 / "unknown" / "none" / None 一律视为未知，不触发 liquids 复用。"""
    if not name:
        return False
    if not isinstance(name, str):
        return False
    return name.strip().lower() not in {"unknown", "none"}


def same_liquid_via_liquids(well: Any, tip_liquid_name: Any) -> bool:
    """tip 残留液体名 vs ``well.tracker.liquids`` 末项 name 严格相等。

    用于 pick_up 决策：判断下一轮要 aspirate 的 well 当前液体是否与 tip 残液同名。
    任一侧未知（空 / "unknown"）→ 返回 ``False``（保守换 tip）。
    """
    if not is_known_liquid_name(tip_liquid_name):
        return False
    well_name = well_current_liquid_name(well)
    if not is_known_liquid_name(well_name):
        return False
    return well_name == tip_liquid_name


def same_liquid_via_liquids_pair(cur_well: Any, next_well: Any) -> bool:
    """两个 source well 当前 ``tracker.liquids`` 末项是否同名（用于决定 drop 时机）。

    注：必须在 cur_well 的 aspirate **之前**调用；aspirate 不改
    ``liquids[-1].name`` 只改顶层 vol（或顶层归零时 pop），故 cur/next 的判等
    以 "将要被抽的那一层" 为准。
    """
    cur_name = well_current_liquid_name(cur_well)
    next_name = well_current_liquid_name(next_well)
    if not is_known_liquid_name(cur_name) or not is_known_liquid_name(next_name):
        return False
    return cur_name == next_name


def capture_tip_liquid_name(source_well: Any) -> "str | None":
    """**aspirate 之前** 把 source well 的当前液体名捕获下来，作为本轮 aspirate
    完成后 tip 上残留液体的身份。

    必须在 ``super().aspirate`` / ``_transfer_base_method`` 调用前读取：PLR
    aspirate 会从顶层扣减体积，体积归零时 PLR ``VolumeTracker`` 会 pop 顶层
    ``(Liquid, vol)``，事后再读 ``liquids[-1]`` 可能拿到 prev layer 或空 list。
    详见 ``liquid_handler_abstract.aspirate`` 中 ``liquid_names_before_aspirate``
    同样的 "预读" 模式。
    """
    name = well_current_liquid_name(source_well)
    return name if is_known_liquid_name(name) else None


def is_placeholder_liquid_name(name: Any) -> bool:
    """判定一个 liquid_history entry 的 name 字段是否是"占位 / 未知"。

    覆盖三种来源:
        - ``None``:PLR ``remove_liquid`` 默认值;
        - 空字符串 ``""``:Uni-Lab 早期 fallback;
        - ``Unknown<digit>``:PLR ``VolumeTracker._get_liquid_name(None)`` 占位。

    设计意图:仅用作"末条改名"补丁的判定门——已经写入真实化学名的 entry
    应一律保留,避免误覆盖业务侧手填值。
    """
    if name is None:
        return True
    if not isinstance(name, str):
        return False
    if name == "":
        return True
    return bool(_UNKNOWN_LIQUID_NAME_RE.match(name))


def patch_unknown_history_last(tracker: Any, expected_name: str) -> bool:
    """把 ``tracker.liquid_history`` 末条的占位 name 就地替换成 ``expected_name``。

    适用场景:PLR ``LiquidHandler.aspirate / dispense`` 调
    ``add_liquid(volume=...)`` 时漏传 liquid 参数,导致 tip / target tracker
    末条记为 ``Unknown<n>``;Uni-Lab 在 super 调用之后用本 helper 把末条 name
    覆盖为真实化学名,**不增减 entry**(与 RESOLUTION B1 修复"PLR 当 history
    单一真相源"原则保持一致)。

    返回值:
        ``True``  —— 末条原本是占位且被改写;
        ``False`` —— 末条不存在 / 已写真名 / tracker 不可写,什么都没动。

    安全保证:
        - ``expected_name`` 为空 / 不是字符串时直接返回(不写空字符串覆盖)。
        - 末条不是 list/tuple、长度 < 2 时返回(避免 IndexError)。
        - 仅当末条 name 命中 ``is_placeholder_liquid_name`` 时改写,否则一律保留。
    """
    if not expected_name or not isinstance(expected_name, str):
        return False
    if tracker is None:
        return False
    history = getattr(tracker, "liquid_history", None)
    if not isinstance(history, list) or not history:
        return False
    last = history[-1]
    if not isinstance(last, (list, tuple)) or len(last) < 2:
        return False
    if not is_placeholder_liquid_name(last[0]):
        return False
    last_vol = last[1]
    # 关键：保持原条目的元组长度（arity）。当前安装的 PLR ``VolumeTracker`` 使用
    # **二元组** ``(name, vol)``（``add_liquid`` / ``remove_liquid`` 均写二元组，
    # ``current_liquids`` 走 ``for name, vol in self.liquid_history`` 解包）。若这里
    # 把二元组升级成三元组 ``(name, vol, "ul")``，下一次 dispense 调
    # ``op.tip.tracker.remove_liquid`` → ``current_liquids`` 解包就会
    # ``ValueError: too many values to unpack (expected 2)``。仅当原条目本就是
    # 旧版 PLR 三元组时，才保留其单位字段以维持兼容。
    if len(last) >= 3:
        history[-1] = (expected_name, last_vol, last[2])
    else:
        history[-1] = (expected_name, last_vol)
    return True


def normalize_liquid_history(raw: Any) -> List[Tuple[str, float]]:
    """P9 — 把任意旧形态的 liquid_history 升级为 v3 dict 列表。

    兼容输入：
        - v3 dict： ``[{name, volume, action, timestamp?}, ...]`` 原样返回（字段补全）
        - v2 tuple： ``[(name, vol), ...]`` → ``action="legacy"``
        - list[str]： ``["A", "B"]`` → ``volume=0, action="legacy"``
        - 其它：丢弃该 entry

    详见 ``product_designs/protocol_convert/09-liquid-history-unknown-debug.md`` §6.4。
    """
    if not isinstance(raw, list):
        return []
    result: List[Tuple[str, float]] = []
    for entry in raw:
        if isinstance(entry, dict):
            try:
                vol_val = float(entry.get("volume", 0.0) or 0.0)
            except (TypeError, ValueError):
                vol_val = 0.0
            result.append((str(entry.get("name", "")), vol_val))
        elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
            try:
                vol_val = float(entry[1])
            except (TypeError, ValueError):
                vol_val = 0.0
            result.append((str(entry[0] or ""), vol_val))
        elif isinstance(entry, str):
            result.append((entry, 0.0))
        # 其它类型静默丢弃
    return result
