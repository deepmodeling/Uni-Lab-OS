"""Single-action material locking for the Edge execution authority.

The microbackend is the one process which sees every dispatched action, whether
the executor is local HostLink, a HostLink slave, or ROS2.  Material exclusion
therefore lives here instead of in an individual driver process.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Iterable, Mapping
from typing import Any

from unilabos.registry.material_locks import normalize_material_parameter_names


_MATERIAL_UUID_FIELDS = (
    "material_uuid",
    "resource_uuid",
    "unilabos_uuid",
    "uuid",
)


def extract_material_uuids(value: Any) -> set[str]:
    """Extract authoritative material UUIDs from a resolved action argument.

    Supported inputs are UUID strings, resource reference dictionaries, flat
    resource-tree lists, and resolved PLR objects.  ``id``/``name`` are
    deliberately not accepted as lock identities: only authority-issued UUIDs
    may define mutual exclusion.
    """

    if value is None:
        return set()
    if isinstance(value, str):
        stripped = value.strip()
        return {stripped} if stripped else set()
    if isinstance(value, Mapping):
        for field_name in _MATERIAL_UUID_FIELDS:
            field_value = value.get(field_name)
            if isinstance(field_value, str) and field_value.strip():
                return {field_value.strip()}
        for nested_name in ("data", "identity", "material", "resource"):
            nested = value.get(nested_name)
            if isinstance(nested, Mapping):
                nested_ids = extract_material_uuids(nested)
                if nested_ids:
                    return nested_ids
        return set()
    if isinstance(value, (list, tuple, set, frozenset)):
        result: set[str] = set()
        for item in value:
            result.update(extract_material_uuids(item))
        return result

    for field_name in _MATERIAL_UUID_FIELDS:
        field_value = getattr(value, field_name, None)
        if isinstance(field_value, str) and field_value.strip():
            return {field_value.strip()}
    content = getattr(value, "res_content", None)
    if content is not None:
        content_ids = extract_material_uuids(content)
        if content_ids:
            return content_ids
    extra = getattr(value, "unilabos_extra", None)
    if isinstance(extra, Mapping):
        return extract_material_uuids(extra)
    return set()


class MaterialActionLockManager:
    """Atomically reserve multiple material UUIDs for one action.

    Requested UUIDs are canonicalized into lexical order.  Reservation is
    all-or-nothing under one guard, so actions can never deadlock while holding
    only a prefix of their requested materials.  Conflicting waiters retain
    FIFO order; disjoint waiters may progress concurrently.
    """

    def __init__(self) -> None:
        self._guard = threading.RLock()
        self._owners: dict[str, str] = {}
        self._held_by_job: dict[str, tuple[str, ...]] = {}
        self._waiters: "OrderedDict[str, tuple[str, ...]]" = OrderedDict()

    @staticmethod
    def canonicalize(material_uuids: Iterable[str]) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    value.strip()
                    for value in material_uuids
                    if isinstance(value, str) and value.strip()
                }
            )
        )

    def request(self, job_id: str, material_uuids: Iterable[str]) -> bool:
        """Reserve all UUIDs or enqueue the job; return whether it owns them."""

        keys = self.canonicalize(material_uuids)
        with self._guard:
            owned = self._held_by_job.get(job_id)
            if owned is not None:
                if owned != keys:
                    raise ValueError("同一 job 不能改变 materials_need_lock 集合")
                return True
            waiting = self._waiters.get(job_id)
            if waiting is not None:
                if waiting != keys:
                    raise ValueError("排队中的 job 不能改变 materials_need_lock 集合")
                return False
            earlier_waiter_conflict = any(
                set(keys).intersection(waiting_keys)
                for waiting_keys in self._waiters.values()
            )
            if (
                not earlier_waiter_conflict
                and all(key not in self._owners for key in keys)
            ):
                self._reserve(job_id, keys)
                return True
            self._waiters[job_id] = keys
            return False

    def release(self, job_id: str) -> list[str]:
        """Release/cancel one job and return newly eligible waiter job IDs."""

        with self._guard:
            self._waiters.pop(job_id, None)
            for key in self._held_by_job.pop(job_id, ()):
                if self._owners.get(key) == job_id:
                    self._owners.pop(key, None)

            ready: list[str] = []
            # 只允许与所有更早等待者都不冲突的任务越过队列。否则，当释放一个
            # 无关任务时，后来的单物料任务可能抢走前面多物料任务正在等待的
            # 空闲资源，破坏同一冲突域内的 FIFO。
            blocked_by_earlier: set[str] = set()
            for waiter_id, keys in list(self._waiters.items()):
                key_set = set(keys)
                if not key_set.intersection(blocked_by_earlier) and all(
                    key not in self._owners for key in keys
                ):
                    self._waiters.pop(waiter_id, None)
                    self._reserve(waiter_id, keys)
                    ready.append(waiter_id)
                else:
                    blocked_by_earlier.update(key_set)
            return ready

    def held_by(self, job_id: str) -> tuple[str, ...]:
        with self._guard:
            return self._held_by_job.get(job_id, ())

    def waiting_jobs(self) -> tuple[str, ...]:
        with self._guard:
            return tuple(self._waiters)

    def _reserve(self, job_id: str, keys: tuple[str, ...]) -> None:
        for key in keys:
            self._owners[key] = job_id
        self._held_by_job[job_id] = keys


__all__ = [
    "MaterialActionLockManager",
    "extract_material_uuids",
    "normalize_material_parameter_names",
]
