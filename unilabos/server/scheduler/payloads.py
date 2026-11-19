"""Backend Scheduler 到执行适配层的 Job 载荷。"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from unilabos.server.backend.execution_queue import JOB_ORIGIN_LOCAL_SCHEDULER


class DispatchPayload(Dict[str, Any]):
    """Backend ``execute_job`` 的执行器下发载荷。"""


def build_job_start_payload(
    job_id: str,
    task_id: str,
    workflow_id: str,
    node_id: str,
    device_id: str,
    action_name: str,
    action_type: str,
    action_args: Any,
    materials_need_lock: Optional[List[str]] = None,
    inventory_requirements: Optional[List[Dict[str, Any]]] = None,
    inventory_reservation_uuid: Optional[str] = None,
    scheduler_revision: int = 0,
    node_run_uuid: str = "",
    attempt_no: int = 1,
    server_info: Optional[Dict[str, Any]] = None,
    retry_of_job_uuid: Optional[str] = None,
    attempt_trigger: str = "initial",
    retry_count: Optional[int] = None,
) -> DispatchPayload:
    """构造执行器消费的 Backend ``execute_job`` 载荷。

    ``job_id`` 是本次 attempt 的 uuid；``node_run_uuid`` 是所属节点运行
    （≡ runtime.v1 ``attempt_group_uuid``），``attempt_no`` 从 1 计。``retry_count`` 是本节点
    已重试的次数（执行器据此在错误决策报告里给出 ``retry_count / max_retries``）；循环体节点
    每轮追加的 ``loop_iteration`` attempt 不算重试，所以不能从 ``attempt_no`` 推。``origin``
    标记生命周期 owner 是本机调度器，执行面据此只把该 job 的生命周期回调路由给本机调度权威。
    """
    # test_latency 的 ping-pong 需要知道 Backend 真正签发该 attempt 的
    # wall-clock。它和 manual_confirm 的元数据一样属于控制面特殊字段，
    # 不应被塞进普通 action_args；普通动作保持没有该字段的旧形状。
    if str(action_name).strip().lower() == "test_latency":
        # 调用方可能已经传入空 dict（例如从旧 TaskDag 载荷透传）；只要
        # 缺少真正的签发时刻，就在控制字段中补齐，而不是让设备端收到
        # 一个无法校时的空 server_info。
        server_info = dict(server_info) if isinstance(server_info, dict) else {}
        if not server_info.get("send_timestamp"):
            server_info["send_timestamp"] = time.time()

    payload = DispatchPayload(
        job_id=job_id,
        task_id=task_id,
        node_id=node_id,
        workflow_id=workflow_id,
        device_id=device_id,
        action=action_name,
        action_type=action_type,
        action_args=action_args,
        materials_need_lock=list(materials_need_lock or []),
        inventory_requirements=list(inventory_requirements or []),
        inventory_reservation_uuid=inventory_reservation_uuid,
        scheduler_revision=scheduler_revision,
        node_run_uuid=node_run_uuid,
        attempt_no=int(attempt_no),
        retry_count=(
            max(int(retry_count), 0) if retry_count is not None else max(int(attempt_no) - 1, 0)
        ),
        attempt_trigger=str(attempt_trigger or "initial"),
        origin=JOB_ORIGIN_LOCAL_SCHEDULER,
        sample_material={},
    )
    if server_info is not None:
        payload["server_info"] = dict(server_info)
    if retry_of_job_uuid:
        # retry 链是 runtime.v1 ExecuteJobContent 的一致性校验字段；初次
        # attempt 不写空值，重试 attempt 才携带上一 attempt 的 job uuid。
        payload["retry_of_job_uuid"] = str(retry_of_job_uuid)
    return payload


__all__ = [
    "DispatchPayload",
    "build_job_start_payload",
]
