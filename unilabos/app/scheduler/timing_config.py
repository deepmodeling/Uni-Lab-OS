"""生产组合根的估时规则加载：只读 JSON，不执行配置中的代码。"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from unilabos.app.scheduler.estimation import DurationEstimator
from unilabos.app.scheduler.timing import ActionTimingSpec


def load_timing_rules(estimator: DurationEstimator, rules_file: str | None = None, *,
                      environ: Mapping[str, str] | None = None) -> int:
    """读取 action_key → ActionTimingSpec 配置，全部校验后注册。

    路径缺省从 UNILABOS_TIMING_RULES_FILE 读取；未配置则不改变估时器。
    文件不存在、格式错误或旧版本替换失败应阻止启动，不能静默使用不同规则。
    调用时机是生产组合根创建估时器后、启动调度或回放历史之前。
    """
    environment = os.environ if environ is None else environ
    path = rules_file if rules_file is not None else environment.get("UNILABOS_TIMING_RULES_FILE", "")
    if not path or not path.strip():
        return 0
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"估时规则 JSON 存在重复键: {key}")
            result[key] = value
        return result
    with Path(path).expanduser().open("r", encoding="utf-8") as stream:
        raw = json.load(stream, object_pairs_hook=unique_object,
                        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"非法 JSON 数值: {value}")))
    if not isinstance(raw, dict):
        raise ValueError("估时规则 JSON 顶层必须是动作键到规则对象的映射")
    prepared = {}
    for action_key, config in raw.items():
        if not isinstance(action_key, str) or not action_key.strip() or not isinstance(config, dict):
            raise ValueError("每条估时规则必须具有非空动作键和规则对象")
        prepared[action_key] = ActionTimingSpec.from_dict(config)
    # 启动期先统一检查，防止第二条配置错误时已经替换第一条。
    for action_key, spec in prepared.items():
        existing = estimator.timing_specs.get(action_key)
        if existing is not None and existing.version == spec.version:
            raise ValueError(f"替换估时规则必须使用新的 version: {action_key}")
    for action_key, spec in prepared.items():
        estimator.register_timing_spec(action_key, spec)
    return len(prepared)
