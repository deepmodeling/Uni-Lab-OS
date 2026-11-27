"""设备动作结果规范化合同。"""

from __future__ import annotations

from unilabos.app.scheduler.execution_outcome import normalize_executor_outcome


def test_nested_business_failure_overrides_success_transport_status() -> None:
    """设备返回 ``success=false`` 时不得把传输成功解释为业务成功。

    参数：无。返回：无；断言Edge 结果规范化接口返回 ``failed``。
    异常：规范化模块不存在或错误放行时由测试失败暴露。
    """

    assert (
        normalize_executor_outcome(
            "success",
            {"return_value": {"success": False, "message": "目标库位不满足"}},
        )
        == "failed"
    )


def test_explicit_rejected_state_overrides_success_transport_status() -> None:
    """PLC/机器人以 ``state=REJECTED`` 返回时必须结束为明确失败。"""

    assert (
        normalize_executor_outcome(
            "success",
            {
                "return_value": {
                    "state": "REJECTED",
                    "message": "放料目标库位已占用",
                }
            },
        )
        == "failed"
    )


def test_explicit_rejected_status_is_case_insensitive() -> None:
    """不同设备使用 ``status=failed/error`` 时同样按明确失败处理。"""

    assert (
        normalize_executor_outcome(
            "success",
            {"return_value": {"status": "Failed"}},
        )
        == "failed"
    )


def test_cancel_requested_maps_failed_transport_to_canceled() -> None:
    """已请求取消的作业收到失败终态时按执行协议收敛为取消。

    参数：无。返回：无；断言既有取消语义不因统一结果模块而回退。
    异常：未知终态或映射错误时测试失败。
    """

    assert (
        normalize_executor_outcome(
            "failed",
            {"return_value": {"success": False}},
            cancel_requested=True,
        )
        == "canceled"
    )
