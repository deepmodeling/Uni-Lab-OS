"""``@workflow`` 已移除的选项：装饰器（``registry/workflows.py``）与注册表 AST 扫描共用的一套规则。

工作流一律按源码静态编译，``compiler`` 参数已删除。旧驱动包里写的 ``compiler="ast"`` 照常加载：
值被忽略，按定义告警一次；其他取值（原来的 ``"builder"`` 等）报错并提示去掉参数。

本模块只依赖标准库：AST 扫描在线程池里逐文件调用它，不能在工作线程里再触发重量级模块的导入。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterable, Mapping
from typing import Any, List

logger = logging.getLogger(__name__)

#: 已移除的 ``@workflow`` 选项（装饰器关键字参数名）。
REMOVED_WORKFLOW_OPTIONS = ("compiler",)

_warned_definitions: set[str] = set()
_warned_lock = threading.Lock()


def ignored_workflow_options(options: Mapping[str, Any]) -> List[str]:
    """校验 ``@workflow`` 写了的已移除选项，返回被忽略的写法（原样用于告警）。

    ``compiler`` 只接受 ``"ast"``（旧写法，值被忽略）；其他取值报错：builder 模式（注册时执行一次
    作者函数）已移除，工作流一律按源码静态编译。
    """

    if "compiler" not in options:
        return []
    value = options["compiler"]
    if value != "ast":
        raise ValueError(
            f"@workflow 不再接受 compiler={value!r}：builder 模式已移除，"
            "工作流一律按源码静态编译，去掉 compiler 参数即可"
        )
    return ['compiler="ast"']


def warn_ignored_workflow_options(definition: str, ignored: Iterable[str]) -> bool:
    """一个 ``@workflow`` 定义写了已移除的选项：按定义告警一次，返回是否首次告警。

    ``definition`` 是模板的稳定身份 ``模块:函数``（装饰器与 AST 扫描同一写法，进程内只告警一次）。
    """

    names = [str(item) for item in ignored if str(item)]
    if not names:
        return False
    with _warned_lock:
        if definition in _warned_definitions:
            return False
        _warned_definitions.add(definition)
    logger.warning(
        "工作流 %s 的 %s 已废弃并被忽略：工作流一律按源码静态编译，请从 @workflow 里删掉这个参数",
        definition,
        "、".join(names),
    )
    return True


__all__ = [
    "REMOVED_WORKFLOW_OPTIONS",
    "ignored_workflow_options",
    "warn_ignored_workflow_options",
]
