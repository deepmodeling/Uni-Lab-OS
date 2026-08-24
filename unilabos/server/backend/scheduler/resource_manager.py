"""Backend Scheduler 的进程内资源锁分配器。

资源申请先以完整集合登记，再以 all-or-nothing 方式获取。前序 Job 可以把
动作预占或物料锁直接交给已登记的后继 Job；转移完成前锁始终由前序持有，
不存在先释放再竞争的空窗。
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock
from typing import Iterable

from unilabos.server.backend.scheduler.models import (
    ResourceLockIdentifier,
    SchedulerLockEvent,
    SchedulerLockHandoffRecord,
    SchedulerLockHandoffRequest,
    SchedulerLockOwnership,
    SchedulerResourceRequest,
    SchedulerResourceRequestRecord,
    SchedulerResourceSnapshot,
)


LockKey = tuple[str, ...]


class SchedulerResourceError(RuntimeError):
    """Backend Scheduler 资源管理错误基类。"""


class ResourceRequestConflict(SchedulerResourceError):
    """幂等键或 owner 已被不同申请占用。"""


class ResourceNotFound(SchedulerResourceError):
    """请求、owner 或 handoff 不存在。"""


class InvalidLockHandoff(SchedulerResourceError):
    """锁转移不满足所有权或工作流约束。"""


@dataclass
class _RequestState:
    value: SchedulerResourceRequest
    identifiers: tuple[ResourceLockIdentifier, ...]
    keys: tuple[LockKey, ...]
    status: str
    created_sequence: int
    updated_sequence: int
    version: int = 1
    release_requested: bool = False


@dataclass
class _OwnershipState:
    identifier: ResourceLockIdentifier
    owner_uuid: str
    acquired_sequence: int
    version: int = 1


@dataclass
class _HandoffState:
    value: SchedulerLockHandoffRequest
    identifiers: tuple[ResourceLockIdentifier, ...]
    keys: tuple[LockKey, ...]
    status: str
    created_sequence: int
    updated_sequence: int
    version: int = 1


class SchedulerResourceManager:
    """分配 Backend Scheduler 的动作锁和物料锁。

    本类只表达调度权威状态，不调用 Edge、HostLink 或 Materials Service。
    调度器负责先登记后继 Job 的完整申请，再提交 handoff，最后结束前序 Job。
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._sequence = 0
        self._requests: "OrderedDict[str, _RequestState]" = OrderedDict()
        self._request_owners: dict[str, str] = {}
        self._ownerships: dict[LockKey, _OwnershipState] = {}
        self._handoffs: "OrderedDict[str, _HandoffState]" = OrderedDict()
        self._events: list[SchedulerLockEvent] = []

    def acquire(
        self,
        request: SchedulerResourceRequest,
    ) -> SchedulerResourceRequestRecord:
        """登记完整资源集合，并尝试原子获取。

        相同 ``request_uuid`` 和完全相同的请求是幂等重放。资源暂不可用时返回
        ``waiting``，不持有部分锁。
        """

        value = request.model_copy(deep=True)
        with self._lock:
            prior_owner = self._request_owners.get(value.request_uuid)
            if prior_owner is not None:
                prior = self._requests[prior_owner]
                if prior.value != value:
                    raise ResourceRequestConflict(
                        f"request_uuid {value.request_uuid!r} has different payload"
                    )
                return self._request_record(prior)

            prior = self._requests.get(value.owner_uuid)
            if prior is not None:
                if prior.value != value:
                    raise ResourceRequestConflict(
                        f"owner {value.owner_uuid!r} already has a resource request"
                    )
                return self._request_record(prior)

            identifiers = self._sorted_identifiers(value.identifiers)
            event_sequence = self._emit(
                "lock.requested",
                owner_uuid=value.owner_uuid,
                identifiers=identifiers,
            )
            state = _RequestState(
                value=value,
                identifiers=identifiers,
                keys=tuple(item.canonical_key for item in identifiers),
                status="waiting",
                created_sequence=event_sequence,
                updated_sequence=event_sequence,
            )
            self._requests[value.owner_uuid] = state
            self._request_owners[value.request_uuid] = value.owner_uuid
            self._promote()
            return self._request_record(state)

    def begin_handoff(
        self,
        request: SchedulerLockHandoffRequest,
    ) -> SchedulerLockHandoffRecord:
        """把前序已持有的 claims 优先、原子地交给后继 Job。

        后继 Job 必须已经通过 :meth:`acquire` 登记完整申请。若后继还在等待
        其他资源，handoff 保持 ``pending``，前序继续持有待转移 claims。
        """

        value = request.model_copy(deep=True)
        with self._lock:
            prior = self._handoffs.get(value.handoff_uuid)
            if prior is not None:
                if prior.value != value:
                    raise ResourceRequestConflict(
                        f"handoff_uuid {value.handoff_uuid!r} has different payload"
                    )
                return self._handoff_record(prior)

            source = self._requests.get(value.from_owner_uuid)
            if source is None:
                raise ResourceNotFound(
                    f"source owner {value.from_owner_uuid!r} is not registered"
                )
            target = self._requests.get(value.to_owner_uuid)
            if target is None:
                raise ResourceNotFound(
                    f"target owner {value.to_owner_uuid!r} is not registered"
                )
            if source.status != "held":
                raise InvalidLockHandoff("source owner must hold its resource set")
            if target.status != "waiting":
                raise InvalidLockHandoff("target owner must be waiting for resources")
            for proof in value.material_handle_proofs:
                if proof.source_task_uuid != source.value.task_uuid:
                    raise InvalidLockHandoff(
                        "material Handle proof source does not match source task"
                    )
                if proof.target_task_uuid != target.value.task_uuid:
                    raise InvalidLockHandoff(
                        "material Handle proof target does not match target task"
                    )

            identifiers = self._sorted_identifiers(value.identifiers)
            keys = tuple(item.canonical_key for item in identifiers)
            target_keys = set(target.keys)
            if not set(keys).issubset(target_keys):
                raise InvalidLockHandoff(
                    "transferred claims must be part of the target's full request"
                )
            for key in keys:
                ownership = self._ownerships.get(key)
                if ownership is None or ownership.owner_uuid != value.from_owner_uuid:
                    raise InvalidLockHandoff(
                        f"source owner does not hold {self._format_key(key)}"
                    )
            omitted_source_keys = {
                key
                for key in target.keys
                if (ownership := self._ownerships.get(key)) is not None
                and ownership.owner_uuid == value.from_owner_uuid
                and key not in keys
            }
            if omitted_source_keys:
                formatted = ", ".join(
                    self._format_key(key) for key in sorted(omitted_source_keys)
                )
                raise InvalidLockHandoff(
                    f"handoff omits target claims already held by source: {formatted}"
                )
            self._validate_pending_handoff_uniqueness(value, set(keys))
            self._validate_no_handoff_cycle(value.from_owner_uuid, value.to_owner_uuid)

            event_sequence = self._emit(
                "lock.handoff_pending",
                owner_uuid=value.from_owner_uuid,
                target_owner_uuid=value.to_owner_uuid,
                handoff_uuid=value.handoff_uuid,
                identifiers=identifiers,
                reason=value.reason,
            )
            state = _HandoffState(
                value=value,
                identifiers=identifiers,
                keys=keys,
                status="pending",
                created_sequence=event_sequence,
                updated_sequence=event_sequence,
            )
            self._handoffs[value.handoff_uuid] = state
            self._promote()
            return self._handoff_record(state)

    def release(
        self,
        owner_uuid: str,
        *,
        reason: str = "",
    ) -> SchedulerResourceRequestRecord:
        """释放 owner 的普通持有；pending handoff claims 保持到转移或取消。"""

        with self._lock:
            state = self._get_request_state(owner_uuid)
            if not state.release_requested:
                state.release_requested = True
                event_sequence = self._emit(
                    "lock.release_requested",
                    owner_uuid=owner_uuid,
                    reason=reason,
                )
                self._touch_request(state, event_sequence)
            protected = self._pending_outbound_keys(owner_uuid)
            released = tuple(
                key
                for key, ownership in self._ownerships.items()
                if ownership.owner_uuid == owner_uuid and key not in protected
            )
            identifiers = tuple(
                self._ownerships[key].identifier for key in sorted(released)
            )
            self._release_keys(state, released, identifiers, reason=reason)
            if not self._keys_owned_by(owner_uuid) and not protected:
                if state.status != "released":
                    state.status = "released"
                    state.version += 1
                    state.updated_sequence = max(
                        state.updated_sequence,
                        self._sequence,
                    )
            self._promote()
            return self._request_record(state)

    def cancel_handoff(
        self,
        handoff_uuid: str,
        *,
        reason: str = "",
    ) -> SchedulerLockHandoffRecord:
        """取消 pending handoff，并释放前序为其保留的 claims。"""

        with self._lock:
            state = self._get_handoff_state(handoff_uuid)
            self._cancel_handoff(state, reason=reason)
            self._promote()
            return self._handoff_record(state)

    def cancel_owner(
        self,
        owner_uuid: str,
        *,
        reason: str = "",
    ) -> SchedulerResourceRequestRecord:
        """取消 Job，撤销相关 pending handoff 并释放其全部持有。"""

        with self._lock:
            state = self._get_request_state(owner_uuid)
            for handoff in self._handoffs.values():
                if handoff.status != "pending":
                    continue
                if owner_uuid in {
                    handoff.value.from_owner_uuid,
                    handoff.value.to_owner_uuid,
                }:
                    self._cancel_handoff(handoff, reason=reason)

            released = tuple(
                ownership.identifier
                for key, ownership in sorted(self._ownerships.items())
                if ownership.owner_uuid == owner_uuid
            )
            for key in self._keys_owned_by(owner_uuid):
                del self._ownerships[key]
            event_sequence = self._emit(
                "lock.owner_canceled",
                owner_uuid=owner_uuid,
                identifiers=released,
                reason=reason,
            )
            state.status = "canceled"
            self._touch_request(state, event_sequence)
            self._promote()
            return self._request_record(state)

    def request_for_owner(self, owner_uuid: str) -> SchedulerResourceRequestRecord:
        with self._lock:
            return self._request_record(self._get_request_state(owner_uuid))

    def request_by_uuid(self, request_uuid: str) -> SchedulerResourceRequestRecord:
        with self._lock:
            owner_uuid = self._request_owners.get(request_uuid)
            if owner_uuid is None:
                raise ResourceNotFound(f"request {request_uuid!r} does not exist")
            return self._request_record(self._requests[owner_uuid])

    def handoff(self, handoff_uuid: str) -> SchedulerLockHandoffRecord:
        with self._lock:
            return self._handoff_record(self._get_handoff_state(handoff_uuid))

    def owner_of(self, identifier: ResourceLockIdentifier) -> str | None:
        with self._lock:
            state = self._ownerships.get(identifier.canonical_key)
            return None if state is None else state.owner_uuid

    def held_by(self, owner_uuid: str) -> list[SchedulerLockOwnership]:
        with self._lock:
            return [
                self._ownership_record(state)
                for _, state in sorted(self._ownerships.items())
                if state.owner_uuid == owner_uuid
            ]

    def events(self, *, after_sequence: int = 0) -> list[SchedulerLockEvent]:
        with self._lock:
            return [
                event.model_copy(deep=True)
                for event in self._events
                if event.sequence > after_sequence
            ]

    def snapshot(self) -> SchedulerResourceSnapshot:
        with self._lock:
            return SchedulerResourceSnapshot(
                sequence=self._sequence,
                requests=[
                    self._request_record(state) for state in self._requests.values()
                ],
                ownerships=[
                    self._ownership_record(state)
                    for _, state in sorted(self._ownerships.items())
                ],
                handoffs=[
                    self._handoff_record(state) for state in self._handoffs.values()
                ],
            )

    def _promote(self) -> None:
        while self._promote_one_handoff():
            pass
        self._promote_waiters()

    def _promote_one_handoff(self) -> bool:
        for state in self._handoffs.values():
            if state.status != "pending" or not self._handoff_ready(state):
                continue
            self._complete_handoff(state)
            return True
        return False

    def _handoff_ready(self, state: _HandoffState) -> bool:
        source_uuid = state.value.from_owner_uuid
        target_uuid = state.value.to_owner_uuid
        target = self._requests[target_uuid]
        transferred = set(state.keys)
        for key in target.keys:
            ownership = self._ownerships.get(key)
            if key in transferred:
                if ownership is None or ownership.owner_uuid != source_uuid:
                    return False
            elif ownership is not None and ownership.owner_uuid != target_uuid:
                return False
        return True

    def _complete_handoff(self, state: _HandoffState) -> None:
        source_uuid = state.value.from_owner_uuid
        target_uuid = state.value.to_owner_uuid
        target = self._requests[target_uuid]
        event_sequence = self._emit(
            "lock.transferred",
            owner_uuid=source_uuid,
            target_owner_uuid=target_uuid,
            handoff_uuid=state.value.handoff_uuid,
            identifiers=state.identifiers,
            reason=state.value.reason,
        )
        transferred = set(state.keys)
        for identifier, key in zip(target.identifiers, target.keys):
            ownership = self._ownerships.get(key)
            if key in transferred:
                if ownership is None or ownership.owner_uuid != source_uuid:
                    raise AssertionError("handoff ownership changed while locked")
                ownership.owner_uuid = target_uuid
                ownership.acquired_sequence = event_sequence
                ownership.version += 1
            elif ownership is None:
                self._ownerships[key] = _OwnershipState(
                    identifier=identifier,
                    owner_uuid=target_uuid,
                    acquired_sequence=event_sequence,
                )
        target.status = "held"
        self._touch_request(target, event_sequence)
        source = self._requests[source_uuid]
        if not self._keys_owned_by(source_uuid):
            source.status = "released"
        self._touch_request(source, event_sequence)
        state.status = "completed"
        state.version += 1
        state.updated_sequence = event_sequence

    def _promote_waiters(self) -> None:
        pending_targets = {
            state.value.to_owner_uuid
            for state in self._handoffs.values()
            if state.status == "pending"
        }
        reserved_keys = {
            key
            for state in self._handoffs.values()
            if state.status == "pending"
            for key in self._requests[state.value.to_owner_uuid].keys
        }
        earlier_waiting_keys: set[LockKey] = set()
        for owner_uuid, state in self._requests.items():
            if state.status != "waiting":
                continue
            keys = set(state.keys)
            if owner_uuid in pending_targets:
                earlier_waiting_keys.update(keys)
                continue
            owns_reserved = bool(keys & reserved_keys)
            conflicts_with_waiter = bool(keys & earlier_waiting_keys)
            has_owner = any(key in self._ownerships for key in keys)
            if owns_reserved or conflicts_with_waiter or has_owner:
                earlier_waiting_keys.update(keys)
                continue
            self._acquire_request(state)

    def _acquire_request(self, state: _RequestState) -> None:
        event_sequence = self._emit(
            "lock.acquired",
            owner_uuid=state.value.owner_uuid,
            identifiers=state.identifiers,
        )
        for identifier, key in zip(state.identifiers, state.keys):
            if key in self._ownerships:
                raise AssertionError("request acquired a partially owned resource set")
            self._ownerships[key] = _OwnershipState(
                identifier=identifier,
                owner_uuid=state.value.owner_uuid,
                acquired_sequence=event_sequence,
            )
        state.status = "held"
        self._touch_request(state, event_sequence)

    def _cancel_handoff(self, state: _HandoffState, *, reason: str) -> None:
        if state.status == "completed":
            raise InvalidLockHandoff("completed handoff cannot be canceled")
        if state.status == "canceled":
            return
        source_uuid = state.value.from_owner_uuid
        event_sequence = self._emit(
            "lock.handoff_canceled",
            owner_uuid=source_uuid,
            target_owner_uuid=state.value.to_owner_uuid,
            handoff_uuid=state.value.handoff_uuid,
            identifiers=state.identifiers,
            reason=reason,
        )
        state.status = "canceled"
        state.version += 1
        state.updated_sequence = event_sequence
        source = self._requests[source_uuid]
        if source.release_requested:
            released_keys = tuple(
                key
                for key in state.keys
                if (ownership := self._ownerships.get(key)) is not None
                and ownership.owner_uuid == source_uuid
            )
            released_identifiers = tuple(
                self._ownerships[key].identifier for key in sorted(released_keys)
            )
            self._release_keys(
                source,
                released_keys,
                released_identifiers,
                reason=reason,
            )
        if not self._keys_owned_by(source_uuid):
            source.status = "released"
        if source.updated_sequence < event_sequence:
            self._touch_request(source, event_sequence)

    def _release_keys(
        self,
        state: _RequestState,
        keys: Iterable[LockKey],
        identifiers: Iterable[ResourceLockIdentifier],
        *,
        reason: str,
    ) -> None:
        keys = tuple(keys)
        identifiers = tuple(identifiers)
        for key in keys:
            del self._ownerships[key]
        if identifiers:
            event_sequence = self._emit(
                "lock.released",
                owner_uuid=state.value.owner_uuid,
                identifiers=identifiers,
                reason=reason,
            )
            self._touch_request(state, event_sequence)

    def _validate_pending_handoff_uniqueness(
        self,
        value: SchedulerLockHandoffRequest,
        keys: set[LockKey],
    ) -> None:
        for state in self._handoffs.values():
            if state.status != "pending":
                continue
            if state.value.to_owner_uuid == value.to_owner_uuid:
                raise InvalidLockHandoff(
                    "target owner already has a pending inbound handoff"
                )
            if (
                state.value.from_owner_uuid == value.from_owner_uuid
                and keys.intersection(state.keys)
            ):
                raise InvalidLockHandoff(
                    "source claim is already reserved by a pending handoff"
                )

    def _validate_no_handoff_cycle(
        self,
        source_owner_uuid: str,
        target_owner_uuid: str,
    ) -> None:
        adjacency: dict[str, set[str]] = {}
        for state in self._handoffs.values():
            if state.status == "pending":
                adjacency.setdefault(state.value.from_owner_uuid, set()).add(
                    state.value.to_owner_uuid
                )
        adjacency.setdefault(source_owner_uuid, set()).add(target_owner_uuid)
        pending = [target_owner_uuid]
        visited: set[str] = set()
        while pending:
            owner_uuid = pending.pop()
            if owner_uuid == source_owner_uuid:
                raise InvalidLockHandoff("pending handoffs would form an owner cycle")
            if owner_uuid in visited:
                continue
            visited.add(owner_uuid)
            pending.extend(adjacency.get(owner_uuid, ()))

    def _pending_outbound_keys(self, owner_uuid: str) -> set[LockKey]:
        return {
            key
            for state in self._handoffs.values()
            if state.status == "pending" and state.value.from_owner_uuid == owner_uuid
            for key in state.keys
        }

    def _keys_owned_by(self, owner_uuid: str) -> set[LockKey]:
        return {
            key
            for key, ownership in self._ownerships.items()
            if ownership.owner_uuid == owner_uuid
        }

    def _request_record(
        self,
        state: _RequestState,
    ) -> SchedulerResourceRequestRecord:
        blockers = sorted(
            {
                ownership.owner_uuid
                for key in state.keys
                if (ownership := self._ownerships.get(key)) is not None
                and ownership.owner_uuid != state.value.owner_uuid
            }
        )
        return SchedulerResourceRequestRecord(
            request_uuid=state.value.request_uuid,
            owner_uuid=state.value.owner_uuid,
            task_uuid=state.value.task_uuid,
            identifiers=[item.model_copy(deep=True) for item in state.identifiers],
            status=state.status,
            release_requested=state.release_requested,
            blockers=blockers,
            created_sequence=state.created_sequence,
            updated_sequence=state.updated_sequence,
            version=state.version,
        )

    @staticmethod
    def _ownership_record(state: _OwnershipState) -> SchedulerLockOwnership:
        return SchedulerLockOwnership(
            identifier=state.identifier.model_copy(deep=True),
            owner_uuid=state.owner_uuid,
            acquired_sequence=state.acquired_sequence,
            version=state.version,
        )

    @staticmethod
    def _handoff_record(state: _HandoffState) -> SchedulerLockHandoffRecord:
        return SchedulerLockHandoffRecord(
            handoff_uuid=state.value.handoff_uuid,
            from_owner_uuid=state.value.from_owner_uuid,
            to_owner_uuid=state.value.to_owner_uuid,
            identifiers=[item.model_copy(deep=True) for item in state.identifiers],
            material_handle_proofs=[
                item.model_copy(deep=True)
                for item in state.value.material_handle_proofs
            ],
            reason=state.value.reason,
            status=state.status,
            created_sequence=state.created_sequence,
            updated_sequence=state.updated_sequence,
            version=state.version,
        )

    def _emit(
        self,
        event_type: str,
        *,
        owner_uuid: str | None = None,
        target_owner_uuid: str | None = None,
        handoff_uuid: str | None = None,
        identifiers: Iterable[ResourceLockIdentifier] = (),
        reason: str = "",
    ) -> int:
        self._sequence += 1
        event = SchedulerLockEvent(
            sequence=self._sequence,
            event_type=event_type,
            owner_uuid=owner_uuid,
            target_owner_uuid=target_owner_uuid,
            handoff_uuid=handoff_uuid,
            identifiers=[item.model_copy(deep=True) for item in identifiers],
            reason=reason,
        )
        self._events.append(event)
        return event.sequence

    @staticmethod
    def _touch_request(state: _RequestState, sequence: int) -> None:
        state.version += 1
        state.updated_sequence = sequence

    def _get_request_state(self, owner_uuid: str) -> _RequestState:
        state = self._requests.get(owner_uuid)
        if state is None:
            raise ResourceNotFound(f"owner {owner_uuid!r} is not registered")
        return state

    def _get_handoff_state(self, handoff_uuid: str) -> _HandoffState:
        state = self._handoffs.get(handoff_uuid)
        if state is None:
            raise ResourceNotFound(f"handoff {handoff_uuid!r} does not exist")
        return state

    @staticmethod
    def _sorted_identifiers(
        identifiers: Iterable[ResourceLockIdentifier],
    ) -> tuple[ResourceLockIdentifier, ...]:
        return tuple(
            sorted(
                (item.model_copy(deep=True) for item in identifiers),
                key=lambda item: item.canonical_key,
            )
        )

    @staticmethod
    def _format_key(key: LockKey) -> str:
        return "/".join(key)


__all__ = [
    "InvalidLockHandoff",
    "ResourceNotFound",
    "ResourceRequestConflict",
    "SchedulerResourceError",
    "SchedulerResourceManager",
]
