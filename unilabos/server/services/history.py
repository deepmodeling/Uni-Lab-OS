"""``history.db`` payload 与 append-only 历史流业务服务。"""

from __future__ import annotations

import hashlib
import sqlite3
import time
import uuid
from typing import Optional

from unilabos.server.database.repositories.history import HistoryRepository
from unilabos.server.database.tables.history import (
    HistoryEventRecord,
    PayloadObjectRecord,
)
from unilabos.server.protocol.history import (
    ExternalPayloadWrite,
    HistoryEventAppend,
    HistoryEventQuery,
    InlinePayloadWrite,
    ManualResultReplacement,
    PayloadWrite,
)


class HistoryServiceError(RuntimeError):
    """history service 的业务错误基类。"""


class HistoryNotFoundError(HistoryServiceError):
    """引用的 payload 或 history event 不存在。"""


class HistoryConflictError(HistoryServiceError):
    """UUID、幂等内容或人工替换链发生冲突。"""


class HistoryValidationError(HistoryServiceError):
    """请求跨记录约束不成立。"""


class HistoryService:
    """新 ``history.db`` 的唯一业务入口，不依赖旧 workflow store。"""

    def __init__(self, repository: HistoryRepository):
        if not isinstance(repository, HistoryRepository):
            raise TypeError("repository must be a HistoryRepository")
        self.repository = repository

    @staticmethod
    def _now_ms() -> int:
        return time.time_ns() // 1_000_000

    @staticmethod
    def _same_payload(
        existing: PayloadObjectRecord, candidate: PayloadObjectRecord
    ) -> bool:
        return (
            existing.sha256 == candidate.sha256
            and existing.byte_length == candidate.byte_length
        )

    def store_payload(self, request: PayloadWrite) -> PayloadObjectRecord:
        """保存或按内容复用 payload；返回的 UUID 才是规范跨库引用。"""

        created_at_ms = (
            request.created_at_ms
            if request.created_at_ms is not None
            else self._now_ms()
        )
        if request.expires_at_ms is not None and request.expires_at_ms < created_at_ms:
            raise HistoryValidationError(
                "expires_at_ms cannot precede effective created_at_ms"
            )

        if isinstance(request, InlinePayloadWrite):
            content = bytes(request.inline_payload)
            byte_length = len(content)
            sha256 = hashlib.sha256(content).hexdigest()
            inline_payload: Optional[bytes] = content
            external_uri: Optional[str] = None
        elif isinstance(request, ExternalPayloadWrite):
            byte_length = request.byte_length
            sha256 = request.sha256.lower()
            inline_payload = None
            external_uri = request.external_uri
        else:
            raise TypeError(f"unsupported payload request: {type(request)!r}")

        candidate = PayloadObjectRecord(
            payload_uuid=request.payload_uuid or str(uuid.uuid4()),
            media_type=request.media_type,
            encoding=request.encoding,
            compression=request.compression,
            byte_length=byte_length,
            sha256=sha256,
            storage_kind=request.storage_kind,
            inline_payload=inline_payload,
            external_uri=external_uri,
            created_at_ms=created_at_ms,
            expires_at_ms=request.expires_at_ms,
        )
        with self.repository.write():
            if request.payload_uuid is not None:
                existing = self.repository.get_payload(request.payload_uuid)
                if existing is not None:
                    if self._same_payload(existing, candidate):
                        return existing
                    raise HistoryConflictError(
                        f"payload_uuid {request.payload_uuid!r} already has other content"
                    )

            duplicate = self.repository.find_payload_by_content(
                candidate.sha256, candidate.byte_length
            )
            if duplicate is not None:
                if (
                    candidate.inline_payload is not None
                    and duplicate.inline_payload is not None
                    and candidate.inline_payload != duplicate.inline_payload
                ):
                    raise HistoryConflictError(
                        "payload SHA-256 and byte length collided with different content"
                    )
                return duplicate

            self.repository.insert_payload(candidate)
            return candidate

    def get_payload(self, payload_uuid: str) -> PayloadObjectRecord:
        payload = self.repository.get_payload(payload_uuid)
        if payload is None:
            raise HistoryNotFoundError(f"payload {payload_uuid!r} was not found")
        return payload

    @staticmethod
    def _event_times(request: HistoryEventAppend) -> tuple[int, int]:
        now_ms = HistoryService._now_ms()
        occurred_at_ms = (
            request.occurred_at_ms if request.occurred_at_ms is not None else now_ms
        )
        recorded_at_ms = (
            request.recorded_at_ms
            if request.recorded_at_ms is not None
            else max(now_ms, occurred_at_ms)
        )
        return occurred_at_ms, recorded_at_ms

    def _validate_replacement(
        self, request: HistoryEventAppend
    ) -> Optional[HistoryEventRecord]:
        target_uuid = request.supersedes_event_uuid
        if target_uuid is None:
            return None
        target = self.repository.get_event(target_uuid)
        if target is None:
            raise HistoryNotFoundError(
                f"superseded event {target_uuid!r} was not found"
            )
        if request.event_type != "job_result" or target.event_type != "job_result":
            raise HistoryValidationError(
                "manual replacement is only valid for job_result events"
            )
        if request.job_uuid != target.job_uuid:
            raise HistoryValidationError(
                "replacement job_uuid must match the superseded result"
            )
        if request.job_uuid is None:
            raise HistoryValidationError("a result replacement requires job_uuid")
        if self.repository.get_superseding_event(target.event_uuid) is not None:
            raise HistoryConflictError(
                f"event {target.event_uuid!r} is not the replacement chain tail"
            )
        if request.event_uuid == target.event_uuid:
            raise HistoryConflictError("an event cannot supersede itself")
        return target

    def _append_event_locked(self, request: HistoryEventAppend) -> HistoryEventRecord:
        event_uuid = request.event_uuid or str(uuid.uuid4())
        if self.repository.get_event(event_uuid) is not None:
            raise HistoryConflictError(f"event_uuid {event_uuid!r} already exists")
        if (
            request.payload_uuid is not None
            and self.repository.get_payload(request.payload_uuid) is None
        ):
            raise HistoryNotFoundError(
                f"payload {request.payload_uuid!r} was not found"
            )

        target = self._validate_replacement(request)
        state_version = request.state_version
        if target is not None:
            job_uuid = request.job_uuid
            if job_uuid is None:  # 已由 _validate_replacement 拒绝，仅用于类型收窄。
                raise HistoryValidationError("a result replacement requires job_uuid")
            expected_version = (
                self.repository.latest_state_version(job_uuid, "job_result") + 1
            )
            if state_version is None:
                state_version = expected_version
            elif state_version != expected_version:
                raise HistoryConflictError(
                    f"replacement state_version must be {expected_version}"
                )

        occurred_at_ms, recorded_at_ms = self._event_times(request)
        record = HistoryEventRecord(
            event_uuid=event_uuid,
            event_type=request.event_type,
            job_uuid=request.job_uuid,
            endpoint_uuid=request.endpoint_uuid,
            device_uuid=request.device_uuid,
            action_name=request.action_name,
            event_key=request.event_key,
            job_sequence=request.job_sequence,
            state_version=state_version,
            payload_uuid=request.payload_uuid,
            summary=request.summary,
            severity=request.severity,
            actor_type=request.actor_type,
            actor_uuid=request.actor_uuid,
            supersedes_event_uuid=request.supersedes_event_uuid,
            occurred_at_ms=occurred_at_ms,
            recorded_at_ms=recorded_at_ms,
        )
        try:
            return self.repository.insert_event(record)
        except sqlite3.IntegrityError as exc:
            raise HistoryConflictError(
                f"history event conflicts with the stream: {exc}"
            ) from exc

    def append_event(self, request: HistoryEventAppend) -> HistoryEventRecord:
        """追加事件；不提供更新或删除已有历史的入口。"""

        with self.repository.write():
            return self._append_event_locked(request)

    def append_replacement(
        self, request: ManualResultReplacement
    ) -> HistoryEventRecord:
        """在当前 ``job_result`` 链尾追加人工替换结果。"""

        with self.repository.write():
            target = self.repository.get_event(request.supersedes_event_uuid)
            if target is None:
                raise HistoryNotFoundError(
                    f"superseded event {request.supersedes_event_uuid!r} was not found"
                )
            event_request = HistoryEventAppend(
                event_uuid=request.event_uuid,
                event_type="job_result",
                job_uuid=target.job_uuid,
                endpoint_uuid=target.endpoint_uuid,
                device_uuid=target.device_uuid,
                action_name=target.action_name,
                event_key=request.event_key or target.event_key,
                state_version=request.state_version,
                payload_uuid=request.payload_uuid,
                summary=request.summary,
                severity=request.severity,
                actor_type=request.actor_type,
                actor_uuid=request.actor_uuid,
                supersedes_event_uuid=target.event_uuid,
                occurred_at_ms=request.occurred_at_ms,
                recorded_at_ms=request.recorded_at_ms,
            )
            return self._append_event_locked(event_request)

    def get_event(self, event_uuid: str) -> HistoryEventRecord:
        event = self.repository.get_event(event_uuid)
        if event is None:
            raise HistoryNotFoundError(f"history event {event_uuid!r} was not found")
        return event

    def query_events(
        self, query: Optional[HistoryEventQuery] = None
    ) -> list[HistoryEventRecord]:
        value = query or HistoryEventQuery()
        return self.repository.query_events(
            after_sequence=value.after_sequence,
            limit=value.limit,
            event_types=value.event_types,
            job_uuid=value.job_uuid,
            endpoint_uuid=value.endpoint_uuid,
            device_uuid=value.device_uuid,
            event_key=value.event_key,
            occurred_from_ms=value.occurred_from_ms,
            occurred_through_ms=value.occurred_through_ms,
        )

    def replacement_chain(self, event_uuid: str) -> list[HistoryEventRecord]:
        """从链上任意事件返回完整、按追加顺序排列的替换链。"""

        current = self.get_event(event_uuid)
        seen = {current.event_uuid}
        while current.supersedes_event_uuid is not None:
            previous = self.get_event(current.supersedes_event_uuid)
            if previous.event_uuid in seen:
                raise HistoryConflictError("replacement chain contains a cycle")
            seen.add(previous.event_uuid)
            current = previous

        chain = [current]
        seen = {current.event_uuid}
        while True:
            replacement = self.repository.get_superseding_event(current.event_uuid)
            if replacement is None:
                return chain
            if replacement.event_uuid in seen:
                raise HistoryConflictError("replacement chain contains a cycle")
            seen.add(replacement.event_uuid)
            chain.append(replacement)
            current = replacement


__all__ = [
    "HistoryConflictError",
    "HistoryNotFoundError",
    "HistoryService",
    "HistoryServiceError",
    "HistoryValidationError",
]
