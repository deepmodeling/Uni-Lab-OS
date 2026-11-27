"""旧导入兼容；执行权威由后端持有。"""
from unilabos.app.scheduler.execution_authority import ExecutionAuthority as NativeJobGate

__all__ = ["NativeJobGate"]
