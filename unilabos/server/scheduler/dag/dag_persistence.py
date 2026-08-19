"""OS 本地 DAG 执行器 — 执行游标持久化（断网/重启 resume）。

游标 = ``{task_id, completed[], inflight[], failed}``，原子写本地文件。
重启恢复：读游标 → 已 completed 节点视作依赖已满足 → 用 ``DagWalk(dag, completed=...)``
重建 in-degree/ready → 未完成从 ready 续跑（I4）。

与现有幂等 job 缓存（(task_id, node_id) 键）叠加：即使游标漏记，幂等缓存兜底
防重复执行（I4 双保险）。本模块只管游标文件本身，不涉及执行。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from unilabos.server.scheduler.dag.dag_model import NodeState


@dataclass
class DagCursor:
    """一个任务的执行游标。inflight 仅作诊断，恢复只信 completed（幂等兜底）。"""

    task_id: str
    completed: list[str] = field(default_factory=list)
    inflight: list[str] = field(default_factory=list)
    failed: bool = False

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "completed": list(self.completed),
            "inflight": list(self.inflight),
            "failed": self.failed,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DagCursor":
        return cls(
            task_id=str(d["task_id"]),
            completed=list(d.get("completed") or []),
            inflight=list(d.get("inflight") or []),
            failed=bool(d.get("failed", False)),
        )


class DagCursorStore:
    """游标的本地文件读写。目录可注入（测试用 tmp_path），实现原子写。

    生产默认目录由调用方给出（例如 OS 的本地状态目录）；本类不假设具体位置，
    保持 hermetic —— 测试传临时目录即可。
    """

    def __init__(self, base_dir: str | os.PathLike) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, task_id: str) -> Path:
        # task_id 是 uuid，无路径分隔符风险；仍做基本清洗防注入
        safe = str(task_id).replace("/", "_").replace("\\", "_")
        return self.base_dir / f"dag_cursor_{safe}.json"

    def load(self, task_id: str) -> Optional[DagCursor]:
        """读游标；不存在或损坏返回 None（首次执行 / 从头跑）。"""
        path = self._path(task_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return DagCursor.from_dict(data)
        except (json.JSONDecodeError, KeyError, OSError):
            return None

    def save(self, cursor: DagCursor) -> None:
        """原子写：写临时文件 + fsync + os.replace，避免半写游标。"""
        path = self._path(cursor.task_id)
        payload = json.dumps(cursor.to_dict(), ensure_ascii=False, indent=2)
        fd, tmp = tempfile.mkstemp(dir=str(self.base_dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)  # 同目录 rename 原子替换
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def record_terminal(self, task_id: str, node_id: str, status: NodeState) -> DagCursor:
        """记录一个节点终态并落盘，返回更新后的游标。

        success -> 追加 completed；failed -> 置 failed 位。可作为 DagExecutor
        on_node_terminal 回调直接使用（配合 functools.partial 绑定 task_id）。
        """
        cursor = self.load(task_id) or DagCursor(task_id=task_id)
        if node_id in cursor.inflight:
            cursor.inflight.remove(node_id)
        if status == NodeState.SUCCESS:
            if node_id not in cursor.completed:
                cursor.completed.append(node_id)
        elif status == NodeState.FAILED:
            cursor.failed = True
        self.save(cursor)
        return cursor

    def clear(self, task_id: str) -> None:
        """任务整体完成后清理游标文件。"""
        path = self._path(task_id)
        try:
            path.unlink()
        except OSError:
            pass
