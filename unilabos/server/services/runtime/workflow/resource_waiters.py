"""资源准入的持久等待顺序：老化优先级 FIFO。

准入（:meth:`ResourceIntervals.admit`）先看占用：资源被别人持有就等待。资源此刻空闲时，
还要看是否有排在前面、也需要其中某个资源的等待者——有就让行，把资源留给先到者，
否则多资源等待者会被只要其中一个资源的后到者反复插队而饿死。

排序与 V1 一致：有效优先级（``high`` = 1、``normal`` = 0，每等待
:data:`WAITER_AGING_INTERVAL_S` 秒加 1）降序，再按入队时间、任务创建时间、任务 / 作业
UUID 升序。入队时间持久化在 ``workflow_resource_waiter.enqueued_at``，重启后顺序不变。

让行会形成新的等待边，必须保证不造成死锁：先到者 A 也许正在等后到者 B 所在任务持有的
资源（例如 B 的任务在显式作用域里持有 A 要的样品，B 这一步只需要一台空闲设备）。此时
B 让行给 A 就是环路。判定在每次准入时按当前事实重算：等待图的节点是任务，持有边来自真实
占用；按排名从前往后逐个给等待者加让行边，会闭合环路的让行边不加（后到者直接取空闲资源）。
同一状态下任何调用顺序得到同样的裁决，让行本身永远不会造成死锁。同一任务的等待者之间
不让行，由任务自己的计划顺序约束。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional

from unilabos.protocol.utils.json_codec import decode_json_bytes
from unilabos.server.backend.scheduler.resource_keys import material_lock_key

#: 等待老化周期（秒）：每等待一个完整周期，有效优先级加 1（与 V1 默认值相同）。
WAITER_AGING_INTERVAL_S = 30.0

_PRIORITY_WEIGHT = {"normal": 0.0, "high": 1.0}


def _seconds(value: str) -> float:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


def waiter_rank(*, priority: str, enqueued_at: str, task_create_time: str, task_uuid: str,
                job_uuid: str, now: float) -> tuple[float, float, float, str, str]:
    """升序排名：越小越先。有效优先级 = 基础优先级 + 完整老化周期数。"""

    enqueued = _seconds(enqueued_at)
    effective = _PRIORITY_WEIGHT.get(str(priority), 0.0) + math.floor(max(0.0, now - enqueued) / WAITER_AGING_INTERVAL_S)
    return (-effective, enqueued, _seconds(task_create_time), str(task_uuid), str(job_uuid))


@dataclass(frozen=True)
class _Entry:
    rank: tuple[float, float, float, str, str]
    job_uuid: str
    task_uuid: str
    keys: frozenset[str]


def lock_holders(conn) -> dict[str, set[str]]:
    """lock_key → 当前持有它的任务：区间所有者（借出时算借用者）、动作使用权、样品托管。"""

    holders: dict[str, set[str]] = {}
    loans = {row["lender_owner_uuid"]: row["borrower_task_uuid"] for row in conn.execute(
        "SELECT lender_owner_uuid,borrower_task_uuid FROM workflow_resource_loan WHERE state!='returned'")}
    for row in conn.execute("SELECT owner_uuid,task_uuid,lock_key FROM workflow_resource_interval WHERE state!='released'"):
        holders.setdefault(row["lock_key"], set()).add(loans.get(row["owner_uuid"], row["task_uuid"]))
    for row in conn.execute("SELECT u.lock_key,j.workflow_task_uuid FROM workflow_resource_use u "
                            "JOIN workflow_node_job j ON j.uuid=u.job_uuid"):
        holders.setdefault(row["lock_key"], set()).add(row["workflow_task_uuid"])
    for row in conn.execute("SELECT task_uuid,device_key,material_uuid FROM workflow_device_tenancy WHERE state!='released'"):
        holders.setdefault(row["device_key"], set()).add(row["task_uuid"])
        holders.setdefault(material_lock_key(row["material_uuid"]), set()).add(row["task_uuid"])
    return holders


def active_waiters(conn):
    """仍在等待的当前 attempt：作业 pending、任务未结束且未暂停（暂停任务不占排队位置）。"""

    return conn.execute(
        "SELECT w.job_uuid,w.task_uuid,w.lock_keys,w.enqueued_at,t.create_time,t.priority "
        "FROM workflow_resource_waiter w JOIN workflow_node_job j ON j.uuid=w.job_uuid "
        "JOIN workflow_node_run r ON r.current_job_uuid=j.uuid JOIN workflow_task t ON t.uuid=w.task_uuid "
        "WHERE j.status='pending' AND t.deleted_at IS NULL AND t.status IN ('pending','running') "
        "AND t.control_status='active'"
    ).fetchall()


def _reaches(edges: dict[str, set[str]], source: str, target: str) -> bool:
    todo, seen = [source], set()
    while todo:
        task = todo.pop()
        if task == target:
            return True
        if task not in seen:
            seen.add(task)
            todo.extend(edges.get(task, ()))
    return False


def older_conflicting_waiter(conn, *, job_uuid: str, task_uuid: str, requested_keys: Iterable[str],
                             enqueued_at: str, now_iso: str) -> Optional[str]:
    """返回应先取得资源的等待任务；本请求可以直接取得空闲资源时返回 ``None``。

    ``requested_keys`` 是本请求的全部区间键与实际使用键；本任务已经持有、借入或托管的键
    不参与排队（长期托管的资源必须让所属任务先用完，不能被外部等待者反向阻塞）。
    ``enqueued_at`` 是本请求的入队时间：已在等待时取持久记录，新来的请求取当前时间。
    """

    rows = active_waiters(conn)
    if not any(row["task_uuid"] != task_uuid for row in rows):
        return None
    holders = lock_holders(conn)
    wanted = frozenset(key for key in requested_keys if task_uuid not in holders.get(key, ()))
    if not wanted:
        return None
    now = _seconds(now_iso)
    task = conn.execute("SELECT create_time,priority FROM workflow_task WHERE uuid=?", (task_uuid,)).fetchone()
    mine = _Entry(waiter_rank(priority=task["priority"], enqueued_at=enqueued_at, task_create_time=task["create_time"],
                              task_uuid=task_uuid, job_uuid=job_uuid, now=now), job_uuid, task_uuid, wanted)
    others = [
        _Entry(waiter_rank(priority=row["priority"], enqueued_at=row["enqueued_at"], task_create_time=row["create_time"],
                           task_uuid=row["task_uuid"], job_uuid=row["job_uuid"], now=now),
               row["job_uuid"], row["task_uuid"], frozenset(decode_json_bytes(row["lock_keys"].encode("utf-8"))))
        for row in rows if row["job_uuid"] != job_uuid
    ]
    if not any(entry.task_uuid != task_uuid and entry.keys & wanted for entry in others):
        return None
    # 持有边：等待者的任务 → 持有其所需资源的任务。
    edges: dict[str, set[str]] = {}
    for entry in (*others, mine):
        edges.setdefault(entry.task_uuid, set()).update(
            holder for key in entry.keys for holder in holders.get(key, ()) if holder != entry.task_uuid)
    # 让行边按排名从前往后加入；只需处理到本请求为止（更晚的让行边会给本请求的边让路）。
    placed: list[_Entry] = []
    for entry in sorted((*others, mine), key=lambda item: item.rank):
        yielded_to = None
        for older in placed:
            if older.task_uuid == entry.task_uuid or not older.keys & entry.keys:
                continue
            if _reaches(edges, older.task_uuid, entry.task_uuid):
                continue  # 先到者在等本任务：让行会闭合环路
            edges.setdefault(entry.task_uuid, set()).add(older.task_uuid)
            yielded_to = yielded_to or older.task_uuid
        if entry is mine:
            return yielded_to
        placed.append(entry)
    return None
