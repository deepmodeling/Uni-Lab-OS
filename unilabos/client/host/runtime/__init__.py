"""Backend-neutral contracts shared by device drivers and runtime adapters."""

from unilabos.backend.runtime.async_utils import schedule_async_func
from unilabos.backend.runtime.definition import (
    DeviceConfigEntry,
    DeviceDefinition,
    iter_device_config_entries,
    resolve_device_definition,
)
from unilabos.backend.runtime.action import (
    ActionCancelled,
    ActionContext,
    DeviceActionRouter,
)
from unilabos.backend.runtime.node import (
    BackendCapabilityError,
    DeviceNode,
    StatusListener,
)
from unilabos.backend.runtime.exception import (
    ActionResultError,
    DeviceActionError,
    DeviceBackendUnsupported,
    DeviceClassInvalid,
)
from unilabos.backend.runtime.resource import (
    AuthorityResourceService,
    MaterialSnapshotObserver,
    ResourceService,
)
__all__ = [
    "ActionCancelled",
    "ActionContext",
    "ActionResultError",
    "BackendCapabilityError",
    "AuthorityResourceService",
    "DeviceActionError",
    "DeviceBackendUnsupported",
    "DeviceClassInvalid",
    "MaterialSnapshotObserver",
    "DeviceNode",
    "DeviceConfigEntry",
    "DeviceDefinition",
    "DeviceActionRouter",
    "iter_device_config_entries",
    "ResourceService",
    "resolve_device_definition",
    "schedule_async_func",
    "StatusListener",
]
