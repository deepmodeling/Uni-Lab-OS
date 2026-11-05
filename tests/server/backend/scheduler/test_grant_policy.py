"""可插拔锁授予策略：参考实现、宿主复核与退回、生成的绑定与头文件一致、原生库与参考实现逐条等价。"""

from __future__ import annotations

import importlib.util
import os
import random
from pathlib import Path

import pytest

from unilabos.server.backend.scheduler.grant import (
    NativeGrantPolicy,
    PythonFifoPolicy,
    WaitingRequest,
    fifo_grants,
    library_filename,
    load_grant_policy,
    validate_grants,
)
from unilabos.server.backend.scheduler.models import ActionLockClaim, SchedulerResourceRequest
from unilabos.server.backend.scheduler.resource_manager import SchedulerResourceManager

ROOT = Path(__file__).resolve().parents[4]


def _generator():
    spec = importlib.util.spec_from_file_location("gen_grant_bindings", ROOT / "scripts" / "gen_grant_bindings.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _native_library() -> Path | None:
    explicit = os.environ.get("UNILABOS_SCHEDULER_GRANT_LIBRARY", "")
    if explicit and Path(explicit).is_file():
        return Path(explicit)
    built = ROOT / "native" / "grant-policy" / "target" / "release" / library_filename()
    return built if built.is_file() else None


def w(*keys: int, target: bool = False) -> WaitingRequest:
    return WaitingRequest(keys=tuple(keys), handoff_target=target)


def test_fifo_grants_disjoint_sets_in_parallel_and_blocks_overlap() -> None:
    assert fifo_grants([w(1, 2), w(3), w(2, 4), w(4)], [], []) == [0, 1]
    assert fifo_grants([w(1), w(7, target=True), w(7, 8), w(5), w(9)], [1], [5]) == [4]


def test_generated_binding_matches_the_committed_header() -> None:
    """_abi.py 由头文件生成；改了 Rust 接口后要重新编译并运行 scripts/gen_grant_bindings.py。"""

    generator = _generator()
    assert generator.main_check() == 0


def test_validate_rejects_unsafe_grants() -> None:
    waiting = [w(1), w(1), w(2, target=True), w(3)]
    assert validate_grants([0, 3], waiting, [], [])
    assert not validate_grants([0, 1], waiting, [], [])
    assert not validate_grants([2], waiting, [], [])
    assert not validate_grants([3], waiting, [3], [])
    assert not validate_grants([9], waiting, [], [])
    assert not validate_grants([0, 0], waiting, [], [])


def _request(owner: str) -> SchedulerResourceRequest:
    return SchedulerResourceRequest(
        request_uuid=f"resource:{owner}",
        owner_uuid=owner,
        task_uuid=f"task-{owner}",
        current_action=ActionLockClaim(device_id="pump", action_name="act"),
    )


class _GreedyUnsafe:
    name = "greedy-unsafe"

    def decide(self, waiting, held, reserved):
        return list(range(len(waiting)))


class _GrantNothing:
    name = "grant-nothing"

    def decide(self, waiting, held, reserved):
        return []


def test_unsafe_policy_output_falls_back_to_fifo() -> None:
    manager = SchedulerResourceManager(grant_policy=_GreedyUnsafe())
    assert manager.acquire(_request("a")).status == "held"
    assert manager.acquire(_request("b")).status == "waiting"


def test_pluggable_policy_decides_when_its_answer_is_safe() -> None:
    manager = SchedulerResourceManager(grant_policy=_GrantNothing())
    assert manager.acquire(_request("a")).status == "waiting"
    assert manager.grant_policy_name == "grant-nothing"


def test_policy_selection(monkeypatch, tmp_path) -> None:
    from unilabos.config.config import BasicConfig

    monkeypatch.delenv("UNILABOS_SCHEDULER_GRANT_LIBRARY", raising=False)
    monkeypatch.setattr(BasicConfig, "scheduler_grant_library", "")
    monkeypatch.setattr(BasicConfig, "working_dir", str(tmp_path))
    missing = str(tmp_path / "missing" / library_filename())
    assert isinstance(load_grant_policy("python"), PythonFifoPolicy)
    assert isinstance(load_grant_policy("auto", library=missing), PythonFifoPolicy)
    with pytest.raises(RuntimeError, match="native"):
        load_grant_policy("native", library=missing)
    with pytest.raises(ValueError):
        load_grant_policy("fastest")
    broken = tmp_path / library_filename()
    broken.write_bytes(b"not a library")
    assert isinstance(load_grant_policy("auto", library=str(broken)), PythonFifoPolicy)
    with pytest.raises(RuntimeError):
        load_grant_policy("native", library=str(broken))


def test_require_native_in_ci() -> None:
    """CI 的原生步骤设置 UNILABOS_REQUIRE_NATIVE_GRANT_POLICY=1：默认加载必须拿到原生库。"""

    if os.environ.get("UNILABOS_REQUIRE_NATIVE_GRANT_POLICY") != "1":
        pytest.skip("只在原生 CI 步骤里要求原生库")
    assert SchedulerResourceManager().grant_policy_name == "native-fifo"


@pytest.mark.skipif(_native_library() is None, reason="原生库未编译（python scripts/build_grant_policy.py）")
def test_native_library_matches_the_reference_on_random_scenarios() -> None:
    native = NativeGrantPolicy(_native_library())
    rng = random.Random(20261105)
    for _ in range(3000):
        universe = rng.randint(1, 12)
        waiting = [
            WaitingRequest(
                keys=tuple(rng.sample(range(universe), rng.randint(1, min(4, universe)))),
                handoff_target=rng.random() < 0.15,
            )
            for _ in range(rng.randint(0, 10))
        ]
        held = rng.sample(range(universe), rng.randint(0, min(3, universe)))
        reserved = rng.sample(range(universe), rng.randint(0, min(2, universe)))
        assert native.decide(waiting, held, reserved) == fifo_grants(waiting, held, reserved)
