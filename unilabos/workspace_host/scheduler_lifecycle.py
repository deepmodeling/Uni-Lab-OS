"""Workspace Host 使用的调度器（Scheduler）排空公开接口客户端。"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class SchedulerLifecycleError(RuntimeError):
    """表示排空接口不可达、响应无效或在预算内未收敛。"""

    code: str
    message: str
    details: object = None

    def __str__(self) -> str:
        """返回适合写入 Workspace Host 操作记录的中文错误。"""

        return self.message


class SchedulerLifecycleClient:
    """通过 HTTP 控制调度器排空，不读取调度器进程内部状态。"""

    def begin_and_wait(
        self,
        address: str,
        *,
        timeout: float,
        poll_interval: float = 0.1,
    ) -> dict[str, Any]:
        """开始排空并等待所有设备侧在途作业收敛。

        参数：``address`` 是本地 Backend 地址；``timeout`` 是整个排空预算；
        ``poll_interval`` 是状态查询间隔。返回：阶段为 ``drained`` 的最终状态。
        异常：接口不可达、响应不是合法排空状态或超时会抛
        :class:`SchedulerLifecycleError`；失败时不会要求 Host 终止任何进程。
        """

        deadline = time.monotonic() + max(timeout, 0.0)
        status = self._request(
            address,
            method="POST",
            path="/api/v1/scheduler/drain",
            timeout=self._request_timeout(deadline),
        )
        while status["phase"] != "drained":
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(poll_interval, remaining))
            # sleep 可能正好耗尽总预算。此处必须再次判定，不能把已经过期的
            # deadline 转回最小 50ms socket timeout，否则稳定的 drain_timeout
            # 会按调度时序偶发变成 drain_unavailable。
            if deadline - time.monotonic() <= 0:
                break
            status = self._request(
                address,
                method="GET",
                path="/api/v1/scheduler/drain",
                timeout=self._request_timeout(deadline),
            )
        if status["phase"] == "drained":
            return status
        raise SchedulerLifecycleError(
            "workspace_drain_timeout",
            "设备作业未在安全停止预算内结束，工作区保持运行",
            details=status,
        )

    def resume(self, address: str, *, timeout: float) -> dict[str, Any]:
        """退出排空状态，使调度器恢复派发。

        参数：``address`` 是本地 Backend 地址，``timeout`` 是单次请求预算。
        返回：调度器恢复后的状态和本轮派发摘要。异常：接口不可达或响应无效时
        抛 :class:`SchedulerLifecycleError`，调用方应关闭式处理启动失败。
        """

        return self._request(
            address,
            method="POST",
            path="/api/v1/scheduler/drain/resume",
            timeout=max(timeout, 0.05),
        )

    def _request(
        self,
        address: str,
        *,
        method: str,
        path: str,
        timeout: float,
    ) -> dict[str, Any]:
        """调用一次排空接口并校验最小响应合同。"""

        url = f"{address.rstrip('/')}{path}"
        data = b"{}" if method == "POST" else None
        try:
            with urlopen(
                Request(
                    url,
                    data=data,
                    method=method,
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    },
                ),
                timeout=max(timeout, 0.05),
            ) as response:
                payload = json.loads(response.read())
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise SchedulerLifecycleError(
                "workspace_drain_rejected",
                f"调度器拒绝排空请求：HTTP {error.code}",
                details=detail,
            ) from error
        except (OSError, URLError, ValueError, json.JSONDecodeError) as error:
            raise SchedulerLifecycleError(
                "workspace_drain_unavailable",
                f"调度器排空接口不可用：{error}",
            ) from error
        if not isinstance(payload, dict) or payload.get("phase") not in {
            "running",
            "draining",
            "drained",
        }:
            raise SchedulerLifecycleError(
                "workspace_drain_invalid_response",
                "调度器排空接口返回了无法识别的状态",
                details=payload,
            )
        active_count = payload.get("active_device_job_count")
        active_ids = payload.get("active_device_job_ids")
        if (
            isinstance(active_count, bool)
            or not isinstance(active_count, int)
            or active_count < 0
            or not isinstance(active_ids, list)
            or any(not isinstance(job_id, str) for job_id in active_ids)
            or active_count != len(active_ids)
        ):
            raise SchedulerLifecycleError(
                "workspace_drain_invalid_response",
                "调度器排空接口返回的在途作业信息无效",
                details=payload,
            )
        return payload

    @staticmethod
    def _request_timeout(deadline: float) -> float:
        """把剩余总预算转换为一次 HTTP 请求的有限超时。"""

        return max(0.05, min(2.0, deadline - time.monotonic()))


__all__ = ["SchedulerLifecycleClient", "SchedulerLifecycleError"]
