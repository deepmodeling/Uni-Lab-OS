"""带摘要的记录经当前 OS 身份/会话进入公共属性通道，显示端不读取文件。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable, Iterable

from unilabos.runtime.observations import Sample, Selector, StateError


class ReplayProvider:
    def __init__(self, observations, group: str, selectors: list[Selector], *, sources):
        self.observations = observations
        self.sources = sources
        self.group = group
        self.nodes = {node["id"]: node for node in (self.observations.resolve(selector) for selector in selectors)}
        self.sources.configure_group(group, selectors)
        self.sequence = 0
        self.source = None

    @staticmethod
    def verify(path: Path, expected_sha256: str) -> None:
        with path.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != expected_sha256:
            raise StateError("record_digest_mismatch")

    def start(self, *, operation_id: str, source_session: str, quiescent: Callable[[], bool]):
        self.source = self.sources.switch(
            self.group,
            operation_id=operation_id,
            mode="replay",
            provider="replay",
            source_session=source_session,
            prepare=lambda: None,
            quiescent=quiescent,
        )
        for node in self.nodes.values():
            self.sources.ready(self.group, node["uuid"], self.source["epoch"], source_session)
        snapshot = self.observations.snapshot()
        self.versions = {
            node["uuid"]: node["descriptor"]["version"]
            for node in snapshot["nodes"]
            if node["uuid"] in self.source["members"]
        }
        self.sequence = 0

    def feed(
        self,
        sampled_at: float,
        values: dict[str, dict],
        *,
        sample_group: str,
        request_id: str | None = None,
        task_id: str | None = None,
        job_id: str | None = None,
    ):
        if self.source is None:
            raise StateError("group_not_ready")
        samples = []
        for identifier, properties in values.items():
            node = self.nodes[identifier]
            for name, value in properties.items():
                samples.append(
                    Sample(
                        material_uuid=node["uuid"],
                        generation=node["generation"],
                        descriptor_version=self.versions[node["uuid"]],
                        group=self.group,
                        epoch=self.source["epoch"],
                        runtime_session=self.observations.session,
                        source_session=self.source["source_session"],
                        provider="replay",
                        property=name,
                        seq=self.sequence,
                        sampled_at=sampled_at,
                        clock="recording",
                        quality="unknown" if value is None else "good",
                        value=value,
                        sample_group=sample_group,
                        request_id=request_id,
                        task_id=task_id,
                        job_id=job_id,
                    )
                )
        result = self.observations.publish_batch(samples)
        self.sequence += 1
        return result

    def run(self, rows: Iterable[dict], ack: Callable[[dict], None]):
        """离线模式每条等待 worker ACK；调用方控制墙钟播放节奏。"""
        count = 0
        for row in rows:
            self.feed(
                row["sampled_at"],
                row["values"],
                sample_group=row["sample_group"],
                request_id=row.get("request_id"),
                task_id=row.get("task_id"),
                job_id=row.get("job_id"),
            )
            ack(self.observations.snapshot())
            count += 1
        return {"records_applied": count, "source_session": self.source["source_session"]}
