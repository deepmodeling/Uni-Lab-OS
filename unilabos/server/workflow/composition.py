"""工作区本地 Workflow Authority 的进程级组合根。"""

from __future__ import annotations

import threading
from typing import Optional

from unilabos.server.storage.paths import RuntimeStoragePaths
from unilabos.server.storage.profiles import SchedulerAuthorityProfile
from unilabos.server.workflow.service import AuthoringCompiler, WorkflowService
from unilabos.server.workflow.source_monitor import WorkflowSourceMonitor
from unilabos.server.workflow.store import WorkflowStore

_lock = threading.Lock()
_service: Optional[WorkflowService] = None
_database_path = None
_authority_profile: Optional[SchedulerAuthorityProfile] = None
_monitor: Optional[WorkflowSourceMonitor] = None


def compose_workflow_runtime(
    storage_paths: RuntimeStoragePaths,
    *,
    compiler: Optional[AuthoringCompiler] = None,
    authority_profile: SchedulerAuthorityProfile = (
        SchedulerAuthorityProfile.LOCAL_SCHEDULER
    ),
) -> WorkflowService:
    """装配唯一工作流权威（Workflow Authority）与创作监视。"""

    global _authority_profile, _database_path, _monitor, _service
    if not isinstance(storage_paths, RuntimeStoragePaths):
        raise TypeError("storage_paths must be RuntimeStoragePaths")
    database_path = storage_paths.workflow_db
    profile = SchedulerAuthorityProfile.parse(authority_profile)
    with _lock:
        if _service is not None:
            if database_path != _database_path:
                raise RuntimeError(
                    "Workflow authority cannot switch database path at runtime"
                )
            if profile is not _authority_profile:
                raise RuntimeError(
                    "Workflow authority profile cannot switch at runtime"
                )
            return _service
        _service = WorkflowService(
            WorkflowStore(database_path),
            compiler=compiler,
            authority_profile=profile,
        )
        _database_path = database_path
        _authority_profile = profile
        _service.recover_registered_sources()
        _monitor = WorkflowSourceMonitor(_service)
        _monitor.start()
        return _service


def setup_workflow_service(
    storage_paths: RuntimeStoragePaths,
    *,
    compiler: Optional[AuthoringCompiler] = None,
    authority_profile: SchedulerAuthorityProfile = (
        SchedulerAuthorityProfile.LOCAL_SCHEDULER
    ),
) -> WorkflowService:
    """兼容旧装配调用；所有入口统一进入完整运行时组合。"""

    return compose_workflow_runtime(
        storage_paths,
        compiler=compiler,
        authority_profile=authority_profile,
    )


def get_workflow_service() -> Optional[WorkflowService]:
    return _service


def reset_workflow_service_for_test() -> None:
    """停止监视器并关闭测试使用的进程级单例。"""

    global _authority_profile, _database_path, _monitor, _service
    with _lock:
        if _monitor is not None:
            _monitor.stop()
        if _service is not None:
            _service.close()
        _monitor = None
        _service = None
        _database_path = None
        _authority_profile = None


__all__ = [
    "compose_workflow_runtime",
    "get_workflow_service",
    "reset_workflow_service_for_test",
    "setup_workflow_service",
]
