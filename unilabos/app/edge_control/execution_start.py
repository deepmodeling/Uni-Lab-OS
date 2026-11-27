"""HostNode 实际开始边界与工站控制之间的明确返回语义。"""


class ExecutionStartDeferred(RuntimeError):
    """工站暂停，尚未开始；保留原身份等待。"""


class ExecutionStartCanceled(RuntimeError):
    """有权威未开始撤销事实，禁止执行。"""


class ExecutionStartUnknown(RuntimeError):
    """开始确认无法证明，禁止自动再次调用设备。"""


def confirm_execution_start(item) -> None:
    """仅在紧邻不可撤回设备调用处执行，一次 START 只授权一次调用。"""
    guard = getattr(item, "start_guard", None)
    if guard is None:
        return
    try:
        result = guard()
    except Exception as error:
        raise ExecutionStartUnknown("开始授权结果未知") from error
    if result == "WAIT":
        raise ExecutionStartDeferred("工站普通调度已暂停")
    if result == "CANCELED":
        raise ExecutionStartCanceled("命令已在开始前撤销")
    if result != "START":
        raise ExecutionStartUnknown("命令已承诺开始，禁止重复调用")
