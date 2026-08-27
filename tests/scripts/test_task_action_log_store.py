from scripts.task_action_log_store import TaskActionLogStore


def test_task_action_log_store_append_and_query():
    store = TaskActionLogStore()
    first = store.append(
        workflow_path="demo.json",
        instance_id="inst-a",
        node_id="node_001",
        execution_id="exec-1",
        sample_id="Sample A",
        level="info",
        message="开始",
        detail={"type": "opc_wait"},
        category="opc",
        code="opc_wait",
        phase="waiting",
        template_id="template-a",
        device_id="plc-a",
        action_name="wait_until",
    )
    second = store.append(
        workflow_path="demo.json",
        instance_id="inst-b",
        node_id="node_002",
        execution_id="exec-2",
        sample_id="Sample B",
        level="info",
        message="其他",
    )
    assert first == 1
    assert second == 2
    payload = store.list_since("demo.json", after_seq=0, instance_id="inst-a")
    assert payload["latest_seq"] == 2
    assert payload["next_after_seq"] == 1
    assert payload["has_more"] is False
    assert len(payload["entries"]) == 1
    entry = payload["entries"][0]
    assert entry["instance_id"] == "inst-a"
    assert entry["category"] == "opc"
    assert entry["level"] == "info"
    assert entry["code"] == "opc_wait"
    assert entry["phase"] == "waiting"
    assert entry["template_id"] == "template-a"
    assert entry["device_id"] == "plc-a"
    assert entry["action_name"] == "wait_until"


def test_task_action_log_store_normalizes_legacy_contract_defaults():
    store = TaskActionLogStore()
    store.append(
        workflow_path="demo.json",
        instance_id="inst-a",
        node_id="node-1",
        execution_id="exec-1",
        sample_id="Sample A",
        level="UNKNOWN",
        category="UNKNOWN",
        message="旧日志",
    )

    entry = store.list_since("demo.json")["entries"][0]
    assert entry["category"] == "action"
    assert entry["level"] == "info"
    assert entry["code"] == ""
    assert entry["phase"] == ""
    assert entry["template_id"] == ""
    assert entry["device_id"] == ""
    assert entry["action_name"] == ""


def test_task_action_log_store_pages_from_oldest_unread_entry():
    store = TaskActionLogStore()
    for index in range(5):
        store.append(
            workflow_path="demo.json",
            instance_id="inst-a",
            node_id=f"node-{index}",
            execution_id=f"exec-{index}",
            sample_id="Sample A",
            level="info",
            message=f"日志 {index + 1}",
        )

    first = store.list_since("demo.json", after_seq=0, limit=2)
    assert first["latest_seq"] == 5
    assert first["next_after_seq"] == 2
    assert first["has_more"] is True
    assert [entry["seq"] for entry in first["entries"]] == [1, 2]

    second = store.list_since(
        "demo.json", after_seq=first["next_after_seq"], limit=2
    )
    assert second["next_after_seq"] == 4
    assert second["has_more"] is True
    assert [entry["seq"] for entry in second["entries"]] == [3, 4]

    final = store.list_since(
        "demo.json", after_seq=second["next_after_seq"], limit=2
    )
    assert final["next_after_seq"] == 5
    assert final["has_more"] is False
    assert [entry["seq"] for entry in final["entries"]] == [5]


def test_task_action_log_store_keeps_complete_execution_history():
    store = TaskActionLogStore()
    for index in range(250):
        store.append(
            workflow_path="demo.json",
            instance_id="inst-a",
            node_id="node-1",
            execution_id="exec-1",
            sample_id="Sample A",
            level="info",
            message=f"日志 {index + 1}",
        )

    payload = store.list_since(
        "demo.json", after_seq=0, instance_id="inst-a", limit=500
    )
    assert payload["latest_seq"] == 250
    assert payload["next_after_seq"] == 250
    assert payload["has_more"] is False
    assert [entry["seq"] for entry in payload["entries"]] == list(range(1, 251))
