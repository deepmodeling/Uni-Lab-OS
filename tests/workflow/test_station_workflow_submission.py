"""Backend 工站工作流调用规范化合同。"""

from __future__ import annotations

import hashlib

from unilabos.workflow.json_codec import encode_json
from unilabos.workflow.station_workflow_submission import (
    prepare_station_workflow_submission,
)


def test_legacy_name_submission_keeps_original_request_fingerprint() -> None:
    """新增发布修订字段不得改变旧版名称调用的持久幂等指纹。"""

    task_uuid = "61000000-0000-4000-8000-000000000001"
    submission = prepare_station_workflow_submission(
        backend_task_uuid=task_uuid,
        invocation_key="legacy-call",
        workflow_name="legacy workflow",
        input_value={"count": 2},
        priority=3,
        inventory_bindings=[],
        description=None,
        meta_data={},
    )
    legacy_payload = {
        "backend_task_uuid": task_uuid,
        "invocation_key": "legacy-call",
        "workflow_name": "legacy workflow",
        "input": {"count": 2},
        "priority": 3.0,
        "inventory_bindings": [],
        "description": None,
        "meta_data": {},
    }

    assert submission.request_fingerprint == hashlib.sha256(
        encode_json(legacy_payload, sort_keys=True)
    ).hexdigest()
