"""工作流图可执行性校验：编译、保存与计划共用的唯一口径。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# 计划层已有公共错误码的规则沿用原码，其余统一为 graph_not_executable。
_PUBLIC_CODES = frozenset({"invalid_control_region", "material_shortage_unhandled"})
# 只由图结构决定的计划规则。动作模板合同、资源计划、执行器与选择器等规则取决于
# 模板目录或运行环境，保存时不拦，由建任务与预检按真实环境判断。
_STRUCTURAL_CODES = frozenset(
    {
        "invalid_control_region",
        "control_nesting_too_deep",
        "workflow_cycle",
        "invalid_execution_graph",
        "edge_node_identity_mismatch",
        "edge_handle_identity_mismatch",
        "invalid_material_io",
        "invalid_manual_confirmation",
        "material_flow_not_linear",
        "invalid_material_source_mount_binding",
        "dynamic_material_source_mount_requires_runtime",
        "runtime_material_source_in_repeat_unsupported",
        "unsupported_material_source_mode",
        "material_shortage_unhandled",
    }
)


class GraphNotExecutable(ValueError):
    """图结构不能生成执行计划。

    ``code`` 是对外公共错误码；``plan_code`` 保留计划层原始规则码，供编译诊断定位。
    """

    def __init__(self, code: str, message: str, *, plan_code: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.plan_code = plan_code or code


def assert_graph_executable(graph: Mapping[str, Any], *, workflow_uuid: str) -> None:
    """用执行计划构建器校验一张完整图的结构。

    参数：``graph`` 是含节点、连线与模板投影的完整图；``workflow_uuid`` 仅用于
    诊断。返回无；空图直接通过。纯图结构规则只在计划层维护一份，编译、保存、
    预检与建任务共用，避免“保存成功、运行才拒绝”并覆盖有效图。异常：结构不可
    执行时抛出带公共错误码和具体原因的 ``GraphNotExecutable``。
    """

    if not graph.get("nodes"):
        return
    # 函数内导入：计划模块在导入期依赖存储模块，而存储模块依赖本模块。
    from unilabos.workflow._execution_plan_graph import ExecutionPlanBuildError
    from unilabos.workflow.execution_plan import ExecutionPlanBuilder

    try:
        ExecutionPlanBuilder().build(dict(graph), run_mode="normal", target_node_uuid=None)
    except ExecutionPlanBuildError as error:
        code = str(getattr(error, "code", "") or "").lower()
        if code not in _STRUCTURAL_CODES:
            return
        raise GraphNotExecutable(
            code if code in _PUBLIC_CODES else "graph_not_executable",
            str(error) or f"工作流 {workflow_uuid} 不能生成执行计划",
            plan_code=str(getattr(error, "code", "") or code),
        ) from error


__all__ = ["GraphNotExecutable", "assert_graph_executable"]
