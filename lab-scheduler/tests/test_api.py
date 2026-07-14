"""API endpoint tests using httpx TestClient."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from scheduler.main import app


@pytest.fixture
def client():
    return TestClient(app)


class TestHealth:
    def test_health(self, client):
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


class TestAlgorithms:
    def test_list_algorithms(self, client):
        resp = client.get("/api/v1/algorithms")
        assert resp.status_code == 200
        data = resp.json()
        assert "algorithms" in data
        assert "Greedy" in data["algorithms"]
        assert "CriticalPath" in data["algorithms"]
        assert "WeightedCriticalPath" in data["algorithms"]
        assert "DynamicPriority" in data["algorithms"]
        assert "MultiObjective" in data["algorithms"]
        assert "Realtime" in data["algorithms"]
        assert "HybridCriticality" in data["algorithms"]


class TestSchedule:
    def test_schedule_simple(self, client, simple_schedule_request):
        resp = client.post(
            "/api/v1/schedule",
            json=simple_schedule_request.model_dump(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "schedule_id" in data
        assert data["algorithm"] == "WeightedCriticalPath"
        assert len(data["schedule"]) == 3  # 3 steps
        assert data["objective"]["total_makespan"] > 0

    def test_schedule_multi_task(self, client, multi_task_request):
        resp = client.post(
            "/api/v1/schedule",
            json=multi_task_request.model_dump(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["schedule"]) == 4  # 2 + 2 steps
        assert "task-1" in data["objective"]["task_completions"]
        assert "task-2" in data["objective"]["task_completions"]

    def test_schedule_with_algorithm(self, client, simple_schedule_request):
        req = simple_schedule_request.model_dump()
        req["algorithm"] = "Greedy"
        resp = client.post("/api/v1/schedule", json=req)
        assert resp.status_code == 200
        assert resp.json()["algorithm"] == "Greedy"

    def test_schedule_all_algorithms(self, client, simple_schedule_request):
        """验证所有算法都能正确运行."""
        algos = client.get("/api/v1/algorithms").json()["algorithms"]
        for algo in algos:
            req = simple_schedule_request.model_dump()
            req["algorithm"] = algo
            resp = client.post("/api/v1/schedule", json=req)
            assert resp.status_code == 200, f"Algorithm {algo} failed"
            data = resp.json()
            assert len(data["schedule"]) == 3, f"Algorithm {algo}: wrong step count"

    def test_schedule_invalid_algorithm(self, client, simple_schedule_request):
        req = simple_schedule_request.model_dump()
        req["algorithm"] = "NonExistent"
        resp = client.post("/api/v1/schedule", json=req)
        assert resp.status_code == 500 or resp.status_code == 422


class TestReschedule:
    def test_reschedule_with_completed(self, client, simple_schedule_request):
        # 先执行一次调度
        resp = client.post(
            "/api/v1/schedule",
            json=simple_schedule_request.model_dump(),
        )
        sched_id = resp.json()["schedule_id"]

        # 模拟 s1 完成, 请求重排
        req = simple_schedule_request.model_dump()
        req["schedule_id"] = sched_id
        req["current_time"] = 10
        req["completed_steps"] = [
            {
                "step_id": "s1",
                "task_id": "task-1",
                "status": "success",
                "actual_end": 10,
            }
        ]
        req["in_flight_steps"] = []
        req["current_sample_locations"] = {}
        req["robot_states"] = []

        resp = client.post("/api/v1/reschedule", json=req)
        assert resp.status_code == 200
        data = resp.json()
        assert data["schedule_id"] == sched_id
        # s1 已完成, 不应出现在重排结果中 (s2, s3 还在)
        step_ids = [s["step_id"] for s in data["schedule"] if "step_id" in s]
        assert "s2" in step_ids
        assert "s3" in step_ids


def test_post_schedule_priority_string(client):
    """测试 Priority 字符串 (high) 正确映射到权重 200.0."""
    payload = {
        "lab_id": "L",
        "tasks": [{
            "task_id": "W",
            "priority": "high",
            "steps": [{"step_id": "s1", "machine_type": "PCR", "duration": 10}],
        }],
        "resources": {
            "machines": [{"type": "PCR", "count": 1, "batch_capacity": 1}]
        },
        "algorithm": "WeightedCriticalPath",
    }
    r = client.post("/api/v1/schedule", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["objective"]["priority_weighted_cost"] == 200.0 * 10


def test_post_schedule_priority_float_unchanged(client):
    """测试 Priority 浮点数 (1.0) 保持不变."""
    payload = {
        "lab_id": "L",
        "tasks": [{
            "task_id": "W",
            "priority": 1.0,
            "steps": [{"step_id": "s1", "machine_type": "PCR", "duration": 10}],
        }],
        "resources": {"machines": [{"type": "PCR", "count": 1}]},
    }
    r = client.post("/api/v1/schedule", json=payload)
    assert r.status_code == 200
    assert r.json()["objective"]["priority_weighted_cost"] == 10.0
