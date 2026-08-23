"""新 history.db 协议、Repository 与 Service 测试。"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from typing import Iterator

import pytest
from pydantic import ValidationError

from unilabos.server.database.repositories.history import HistoryRepository
from unilabos.server.database.tables.history import INLINE_PAYLOAD_LIMIT_BYTES
from unilabos.server.protocol.history import (
    ExternalPayloadWrite,
    HistoryEventAppend,
    HistoryEventQuery,
    InlinePayloadWrite,
    ManualResultReplacement,
)
from unilabos.server.services.history import (
    HistoryConflictError,
    HistoryService,
    HistoryValidationError,
)


@contextmanager
def _history_service(database) -> Iterator[HistoryService]:
    with HistoryRepository(database) as repository:
        yield HistoryService(repository)


def test_inline_payload_limit_and_external_reference(tmp_path) -> None:
    with pytest.raises(ValidationError, match="use external storage"):
        InlinePayloadWrite(
            media_type="application/octet-stream",
            inline_payload=b"x" * (INLINE_PAYLOAD_LIMIT_BYTES + 1),
        )

    with _history_service(tmp_path / "history.db") as service:
        content = b"{}"
        inline = service.store_payload(
            InlinePayloadWrite(
                payload_uuid="inline",
                media_type="application/json",
                encoding="utf-8",
                inline_payload=content,
                created_at_ms=1,
            )
        )
        external = service.store_payload(
            ExternalPayloadWrite(
                payload_uuid="external",
                media_type="application/octet-stream",
                byte_length=INLINE_PAYLOAD_LIMIT_BYTES + 1,
                sha256="a" * 64,
                external_uri="s3://bucket/result.bin",
                created_at_ms=1,
            )
        )

        assert inline.sha256 == hashlib.sha256(content).hexdigest()
        assert inline.inline_payload == content
        assert external.inline_payload is None
        assert external.external_uri == "s3://bucket/result.bin"


def test_payload_is_deduplicated_by_hash_and_length(tmp_path) -> None:
    with _history_service(tmp_path / "history.db") as service:
        first = service.store_payload(
            InlinePayloadWrite(
                payload_uuid="first",
                media_type="application/json",
                encoding="utf-8",
                inline_payload=b'{"value":1}',
                created_at_ms=1,
            )
        )
        duplicate = service.store_payload(
            InlinePayloadWrite(
                payload_uuid="second",
                media_type="application/json",
                encoding="utf-8",
                inline_payload=b'{"value":1}',
                created_at_ms=2,
            )
        )

        assert duplicate.payload_uuid == first.payload_uuid
        assert (
            service.repository.connection.execute(
                "SELECT COUNT(*) FROM payload_object"
            ).fetchone()[0]
            == 1
        )


def test_history_event_append_and_filtered_sequence_query(tmp_path) -> None:
    with _history_service(tmp_path / "history.db") as service:
        for ordinal, event_type in enumerate(
            ("job_transition", "job_log", "job_feedback"), start=1
        ):
            event = service.append_event(
                HistoryEventAppend(
                    event_uuid=f"event-{ordinal}",
                    event_type=event_type,
                    job_uuid="job-1" if ordinal < 3 else "job-2",
                    summary={"ordinal": ordinal},
                    occurred_at_ms=ordinal,
                    recorded_at_ms=ordinal,
                )
            )
            assert event.sequence == ordinal

        events = service.query_events(
            HistoryEventQuery(
                after_sequence=1,
                job_uuid="job-1",
                event_types=["job_log"],
            )
        )
        assert [event.event_uuid for event in events] == ["event-2"]
        assert events[0].summary == {"ordinal": 2}


def test_manual_result_replacement_is_linear_append_only_chain(tmp_path) -> None:
    with _history_service(tmp_path / "history.db") as service:
        original = service.append_event(
            HistoryEventAppend(
                event_uuid="result-1",
                event_type="job_result",
                job_uuid="job-1",
                endpoint_uuid="endpoint-1",
                action_name="transfer",
                event_key="result",
                state_version=1,
                summary={"result": "original"},
                occurred_at_ms=1,
                recorded_at_ms=1,
            )
        )
        replacement = service.append_replacement(
            ManualResultReplacement(
                supersedes_event_uuid=original.event_uuid,
                event_uuid="result-2",
                actor_uuid="operator-1",
                summary={"result": "corrected"},
                occurred_at_ms=2,
                recorded_at_ms=2,
            )
        )
        second_replacement = service.append_replacement(
            ManualResultReplacement(
                supersedes_event_uuid=replacement.event_uuid,
                event_uuid="result-3",
                actor_uuid="operator-2",
                summary={"result": "final"},
                occurred_at_ms=3,
                recorded_at_ms=3,
            )
        )

        assert replacement.state_version == 2
        assert replacement.supersedes_event_uuid == original.event_uuid
        assert second_replacement.state_version == 3
        assert [
            event.event_uuid for event in service.replacement_chain("result-2")
        ] == ["result-1", "result-2", "result-3"]
        assert service.get_event("result-1").summary == {"result": "original"}

        with pytest.raises(HistoryConflictError, match="chain tail"):
            service.append_replacement(
                ManualResultReplacement(
                    supersedes_event_uuid=original.event_uuid,
                    event_uuid="fork",
                    actor_uuid="operator-3",
                    occurred_at_ms=4,
                    recorded_at_ms=4,
                )
            )


def test_replacement_rejects_non_result_event(tmp_path) -> None:
    with _history_service(tmp_path / "history.db") as service:
        service.append_event(
            HistoryEventAppend(
                event_uuid="log-1",
                event_type="job_log",
                job_uuid="job-1",
                occurred_at_ms=1,
                recorded_at_ms=1,
            )
        )
        with pytest.raises(HistoryValidationError, match="only valid for job_result"):
            service.append_event(
                HistoryEventAppend(
                    event_uuid="log-2",
                    event_type="job_log",
                    job_uuid="job-1",
                    supersedes_event_uuid="log-1",
                    actor_type="human",
                    actor_uuid="operator-1",
                    occurred_at_ms=2,
                    recorded_at_ms=2,
                )
            )
