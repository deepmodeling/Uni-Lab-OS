"""微后端四库的规范表记录对象。"""

from unilabos.server.models import history as _history
from unilabos.server.models import materials as _materials
from unilabos.server.models import runtime as _runtime
from unilabos.server.models import telemetry as _telemetry
from unilabos.server.models.base import SchemaMigrationRecord, ServerObject
from unilabos.server.models.history import *  # noqa: F403
from unilabos.server.models.materials import *  # noqa: F403
from unilabos.server.models.runtime import *  # noqa: F403
from unilabos.server.models.telemetry import *  # noqa: F403

__all__ = [
    "SchemaMigrationRecord",
    "ServerObject",
    *_history.__all__,
    *_materials.__all__,
    *_runtime.__all__,
    *_telemetry.__all__,
]
