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
    assert len(payload["entries"]) == 1
    assert payload["entries"][0]["instance_id"] == "inst-a"
