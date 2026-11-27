"""把中心下发的调用落到工站执行路径上，并保证**同一次调用不会被启动两次**。

这是整个闭环里唯一负责"决定要不要真的开始执行"的地方，因此它的规则必须能被一句话
说清：**同一次中心调用的本地任务身份是确定的，而且这个身份一旦占用就永久占用。**

为什么身份必须确定：工站在断网、重启、重复投递之后会再次处理同一份中心任务包。如果
每次都用新的随机 UUID，工站本地就会认为"这是一次新调用"，于是再执行一遍——即使中心
侧的去重完全正确也无济于事。因此本模块从中心的调用身份**推导**本地任务身份，而不是
生成它。

为什么"已存在"不等于"可以跳过"：`AlreadyStarted` 表示这次调用已经有过本地任务，重投
必须直接返回既有身份并**不**执行。真正需要区分的情况是上一次执行崩在半路，那是恢复
问题，由中心的租约与重投语义处理（中心重新下发时携带同一调用身份，工站据此仍然认出
"这是同一次调用"），不能靠在这里允许重复启动来解决。

永久占用由 `WorkflowStore` 的唯一索引保证，而不是由本模块的查询保证：查询与写入之间
存在竞态，唯一索引是唯一可靠的仲裁者。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping
from uuid import NAMESPACE_URL, uuid5

from unilabos.workflow.store import StoreConflict, WorkflowStore
from unilabos.workflow.task_input import PreparedTaskInput

logger = logging.getLogger(__name__)

#: 调用身份命名空间。改动它会同时改变所有派生身份，因此必须保持稳定。
STATION_INVOCATION_NAMESPACE = NAMESPACE_URL
#: 身份派生的命名空间前缀，避免与仓库里其它 uuid5 用途相撞。
IDENTITY_PREFIX = "station-invocation-identity"
#: 本地任务身份的派生前缀。
LOCAL_TASK_PREFIX = "station-local-task"


class StationInvocationError(RuntimeError):
    """无法为这次调用建立本地身份。"""


class InvocationFingerprintMismatch(StationInvocationError):
    """同一个调用身份收到了内容不同的任务包。

    这不是可以自动处理的情况：中心的冻结摘要变了说明准入事实被改写，或者有人手工
    改了任务包。继续执行会执行一份中心并不认可的输入，因此必须停下来由人工核实。
    """


def local_task_uuid(station_invocation_uuid: str) -> str:
    """从中心调用身份推导本地任务身份。

    确定性是刻意的：重启后必须得到同一个值，否则中心侧的事件去重失效，同一件事会被
    记成两件。
    """
    if not station_invocation_uuid:
        raise StationInvocationError("中心调用身份不能为空")
    return str(uuid5(STATION_INVOCATION_NAMESPACE, f"{LOCAL_TASK_PREFIX}:{station_invocation_uuid}"))


def invocation_key(station_invocation_uuid: str) -> str:
    """本地调用键。与中心调用身份一一对应。"""
    if not station_invocation_uuid:
        raise StationInvocationError("中心调用身份不能为空")
    return f"{IDENTITY_PREFIX}:{station_invocation_uuid}"


@dataclass(frozen=True)
class BeginResult:
    """一次"是否开始执行"的判定结果。"""

    local_task_uuid: str
    #: ``True`` 表示本次调用首次落地，应当执行；``False`` 表示已经存在，**不得**再执行。
    created: bool
    task: Mapping[str, Any]

    @property
    def should_execute(self) -> bool:
        return self.created


def begin_station_invocation(
    store: WorkflowStore,
    *,
    workflow_uuid: str,
    station_invocation_uuid: str,
    global_task_uuid: str,
    payload_digest: str,
    run_mode: str,
    target_node_uuid: str | None,
    description: str | None,
    meta_data: Mapping[str, Any],
    plan_builder,
    applied_graph: Mapping[str, Any] | None = None,
    priority: Any = "normal",
) -> BeginResult:
    """建立（或认领既有的）本地任务身份，返回是否应当执行。

    参数：``store`` 是工站运行事实库；``station_invocation_uuid`` 是中心的调用身份，
    也是本地身份的唯一来源；``payload_digest`` 是中心冻结的任务包摘要，用作请求指纹，
    因此"同一调用换了内容"会被拒绝而不是被当成重放接受；其余参数与
    [`WorkflowStore.create_task_with_jobs`] 相同。

    返回：`BeginResult`。``created`` 为假时调用方**必须**跳过执行。

    异常：同一调用身份携带不同任务包时抛 `InvocationFingerprintMismatch`；中心调用
    身份缺失时抛 `StationInvocationError`。
    """
    if not global_task_uuid:
        raise StationInvocationError("中心全局任务身份不能为空")
    if not payload_digest:
        raise StationInvocationError("任务包摘要不能为空：它是「同一调用同一内容」的凭据")
    local_uuid = local_task_uuid(station_invocation_uuid)
    try:
        task = store.create_task_with_jobs(
            workflow_uuid=workflow_uuid,
            task_uuid=local_uuid,
            run_mode=run_mode,
            target_node_uuid=target_node_uuid,
            description=description,
            meta_data=dict(meta_data),
            plan_builder=plan_builder,
            applied_graph=None if applied_graph is None else dict(applied_graph),
            backend_task_uuid=global_task_uuid,
            invocation_key=invocation_key(station_invocation_uuid),
            priority=priority,
            request_fingerprint=payload_digest,
        )
    except StoreConflict as error:
        message = str(error)
        if "请求内容已变化" in message:
            # 同一调用身份配不同任务包：中心的准入事实被改写，或任务包被改动。
            # 继续执行等于执行一份中心并不认可的输入。
            raise InvocationFingerprintMismatch(
                f"调用 {station_invocation_uuid} 的任务包内容已变化，拒绝执行"
            ) from error
        raise StationInvocationError(f"无法建立本地调用身份：{message}") from error

    created = bool(task.get("_station_submission_created", False))
    if not created:
        logger.info(
            "调用 %s 已有本地任务 %s，跳过执行（重投或重启后的重复递送）",
            station_invocation_uuid,
            local_uuid,
        )
    return BeginResult(local_task_uuid=local_uuid, created=created, task=task)


def prepare_plan_builder(prepared: PreparedTaskInput):
    """把一份已准备好的输入包装成 `plan_builder`。

    存在的意义是让调用方不必在闭包里重复解析逻辑：`create_task_with_jobs` 要求
    `plan_builder` 返回已解析输入，而这里把"解析"与"建立身份"分开，前者由调用方负责。
    """

    def builder(_graph: Mapping[str, Any]) -> PreparedTaskInput:
        return prepared

    return builder
