"""中心 SQLite 中的耗时样本账；复用调用方连接和事务，不另建数据库。

调用方负责串行写入、提交和恢复顺序。先把样本与执行事实一起提交，再喂给
内存估时器；重启时按原顺序回放。旧历史缺少真实参数时不能补猜参数入账。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from unilabos.app.scheduler.timing import DEFAULT_BOUNDARY, finite_seconds

if TYPE_CHECKING:
    from unilabos.app.scheduler.estimation import DurationEstimator


@dataclass(frozen=True)
class TimingObservation:
    """可追溯样本；outcome 决定是否能作为完整成功耗时用于学习。"""
    observation_id: str
    device_action_key: str
    actual_s: float
    params: Any
    device_id: str = ""
    action_version: str = ""
    spec_version: str = "1"
    timing_boundary: str = DEFAULT_BOUNDARY
    outcome: str = "success"
    calibration: bool = False

    def payload(self) -> str:
        if not self.observation_id or not self.device_action_key:
            raise ValueError("耗时样本必须有稳定身份和动作键")
        if not self.spec_version or not self.timing_boundary:
            raise ValueError("耗时样本必须记录规则版本及计时边界")
        if finite_seconds(self.actual_s, allow_zero=self.outcome != "success") is None:
            raise ValueError("样本时长必须有限；完整成功样本必须大于零")
        if not isinstance(self.calibration, bool):
            raise ValueError("calibration 必须为布尔值")
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), allow_nan=False)


class TimingObservationStore:
    """只操作注入的连接；创建表和写样本都由现有中心写入口调用。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def ensure_schema(self) -> None:
        ensure_timing_observation_schema(self._connection)

    def append(self, sample: TimingObservation) -> bool:
        """需要调用方已有写事务；相同身份/内容去重，异内容拒绝。"""
        if not self._connection.in_transaction:
            raise RuntimeError("耗时样本须在中心显式写事务中保存")
        payload = sample.payload()
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        found = self._connection.execute(
            "SELECT payload_hash FROM scheduler_timing_observations WHERE observation_id = ?",
            (sample.observation_id,),
        ).fetchone()
        if found is not None:
            if found[0] != digest:
                raise ValueError("同一耗时样本身份已有不同内容")
            return False
        self._connection.execute(
            "INSERT INTO scheduler_timing_observations (observation_id, payload_hash, payload_json) VALUES (?, ?, ?)",
            (sample.observation_id, digest, payload),
        )
        return True

    def get(self, observation_id: str) -> TimingObservation | None:
        """读取已保存的原始样本，重放不能按新的收包时刻重新计时。"""
        found = self._connection.execute(
            "SELECT payload_hash, payload_json FROM scheduler_timing_observations WHERE observation_id = ?",
            (observation_id,),
        ).fetchone()
        if found is None:
            return None
        expected_hash, payload = found
        if hashlib.sha256(payload.encode("utf-8")).hexdigest() != expected_hash:
            raise ValueError("耗时样本内容指纹不一致")
        sample = TimingObservation(**json.loads(payload))
        sample.payload()
        return sample

    def replay_into(self, estimator: DurationEstimator) -> dict[str, int]:
        """启动恢复时回放已提交完整样本，不把旧规则、失败或删失当成功。"""
        if self._connection.in_transaction:
            raise RuntimeError("提交执行事实后再恢复内存估时器")
        result = {"accepted": 0, "skipped": 0}
        cursor = self._connection.execute(
            "SELECT payload_hash, payload_json FROM scheduler_timing_observations ORDER BY sequence"
        )
        for expected_hash, payload in cursor:
            if hashlib.sha256(payload.encode("utf-8")).hexdigest() != expected_hash:
                raise ValueError("耗时样本内容指纹不一致")
            sample = TimingObservation(**json.loads(payload))
            sample.payload()
            spec = estimator._spec_for(sample.device_action_key)
            if (sample.outcome != "success" or sample.spec_version != spec.version
                    or sample.timing_boundary != spec.timing_boundary):
                result["skipped"] += 1
                continue
            accepted = estimator.observe(sample.device_action_key, sample.actual_s, sample.params,
                device_id=sample.device_id, action_version=sample.action_version,
                outcome=sample.outcome, observation_id=sample.observation_id,
                calibration=sample.calibration)
            result["accepted" if accepted else "skipped"] += 1
        return result



def ensure_timing_observation_schema(connection) -> None:
    """幂等创建持久耗时样本表。"""

    connection.execute("""
        CREATE TABLE IF NOT EXISTS scheduler_timing_observations (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            observation_id TEXT NOT NULL UNIQUE,
            payload_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL
        )
    """)
