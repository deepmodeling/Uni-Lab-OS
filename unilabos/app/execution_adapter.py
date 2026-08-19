"""Backend-neutral device execution adapter registry.

The active adapter is a transport boundary only.  It may execute/cancel a
device action and expose device capability snapshots, but it must not own job
identity, queues, retries, failure decisions, or scheduler state.  Those
belong to the Edge microbackend.
"""

from __future__ import annotations

import threading
from typing import Any, Iterable, Optional


_adapter_condition = threading.Condition()
_active_adapter: Optional[Any] = None


def set_execution_adapter(adapter: Any) -> None:
    """Register the adapter created by the selected runtime backend."""

    global _active_adapter
    with _adapter_condition:
        _active_adapter = adapter
        _adapter_condition.notify_all()


def clear_execution_adapter(adapter: Optional[Any] = None) -> None:
    """Clear the active adapter, optionally only when ``adapter`` owns it."""

    global _active_adapter
    with _adapter_condition:
        if adapter is None or _active_adapter is adapter:
            _active_adapter = None
            _adapter_condition.notify_all()


def get_execution_adapter(timeout: Optional[float] = 0) -> Optional[Any]:
    """Return the selected transport adapter without importing unused stacks.

    HostLink registers explicitly.  ROS2's existing ``HostNode`` already
    implements the small execute/cancel/capability surface, so it is used as a
    lazy adapter until the ROS startup path is converted to explicit
    registration.
    """

    from unilabos.config.config import BasicConfig

    wait_timeout = 0.0 if timeout is None else max(float(timeout), 0.0)
    with _adapter_condition:
        if _active_adapter is not None:
            return _active_adapter
        if BasicConfig.backend == "hostlink":
            if wait_timeout:
                _adapter_condition.wait_for(
                    lambda: _active_adapter is not None,
                    timeout=wait_timeout,
                )
            return _active_adapter

    try:
        from unilabos.ros.nodes.presets.host_node import HostNode
    except ImportError:
        return None
    return HostNode.get_instance(timeout)


def execution_result_bridges(bridges: Iterable[Any]) -> list[Any]:
    """Route raw execution results to the single job-lifecycle owner.

    During migration, runtimes can still receive several application bridges.
    If a microbackend bridge is present, only it receives raw action status;
    it will publish the released/canonical status to downstream bridges.
    """

    values = list(bridges)
    owners = [
        bridge
        for bridge in values
        if bool(getattr(bridge, "owns_job_lifecycle", False))
    ]
    return owners or values


__all__ = [
    "clear_execution_adapter",
    "execution_result_bridges",
    "get_execution_adapter",
    "set_execution_adapter",
]
