"""复用原有业务测试，防止仅验证建表却遗漏事务、回放和通知事实。"""

import pytest

from tests.app.test_device_state import (
    TestDeviceStateApi,
    TestStoreBasics,
)

# 这些用例不依赖 SQLite 文件格式或历史 PRAGMA 迁移。
from tests.app.test_inventory_store_service import (
    TestIdempotency,
    TestLotBasics,
    TestMoveAndPersistence,
    TestParentAndSite,
    svc,
)
from tests.app.test_workflow_history import TestPersistenceAndRecovery
from tests.networking.test_edge_control_store_lifecycle import (
    test_failed_serialization_rolls_back_immediate_transaction,
    test_final_unknown_marker_survives_pending_outcome_races,
    test_only_final_unknown_resolution_ack_retires_job_mirror,
    test_settled_job_rejects_late_terminal_callback,
    test_unknown_outcome_waits_for_final_physical_settlement,
)

pytestmark = pytest.mark.usefixtures("database_mode")
