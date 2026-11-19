"""全量重置：只在默认分离部署停机且关库后移出活动数据，绝不在线清表。"""

from __future__ import annotations

import json
import os
import secrets
import threading
from pathlib import Path
from typing import Callable

from unilabos.server.database import ServerDatabasePaths


class ResetConflict(ValueError):
    """重置前置条件未满足，不能开始停机。"""


class ResetController:
    def __init__(self, paths: ServerDatabasePaths, working_dir: str, preflight: Callable[[], None]):
        self.paths = paths
        self.working_dir = Path(working_dir).resolve()
        self.preflight = preflight
        self.token = secrets.token_urlsafe(24)
        self.pending = False
        self._lock = threading.Lock()
        self.backup = paths.root / "reset-backups" / secrets.token_hex(12)

    def targets(self) -> list[Path]:
        # 禁止自定义越界库路径；调用者不能通过 HTTP 提供文件路径。
        result = []
        for root, databases in (
            (self.paths.root, self.paths.as_mapping()),
            (self.paths.root / "edge", ServerDatabasePaths.resolve(self.paths.root / "edge").as_mapping()),
        ):
            for name, path in databases.items():
                expected = root / f"{name}.db"
                if path != expected or path.resolve() != expected.absolute():
                    raise ResetConflict("全量重置只支持默认库路径，不能有自定义路径或符号链接")
                result.extend(Path(str(path) + suffix) for suffix in ("", "-wal", "-shm"))
        result.extend(self.working_dir / name for name in ("device_processes.json", "device_processes"))
        for path in result:
            if path.is_symlink() or path.resolve() != path.absolute():
                raise ResetConflict(f"拒绝重置符号链接或目录联接：{path.name}")
        return result

    def preview(self) -> dict:
        self.targets()
        return {
            "supported": True,
            "pending": self.pending,
            "confirmation_token": self.token,
            "backup_path": str(self.backup),
            "detail": "清空权威与 Host 四库（含设备、物料、图、工作流、任务、遥测、历史）及受管进程配置；保留驱动包源码。完成后服务退出，请不带旧 -g 重新启动 unilab。",
        }

    def request(self, token: str, confirmation: str) -> dict:
        with self._lock:
            if not secrets.compare_digest(token.encode(), self.token.encode()) or confirmation != "清空全部数据":
                raise ResetConflict("重置确认无效，请重新预览并输入「清空全部数据」")
            if self.pending:
                return self.preview()
            self.targets()
            self.preflight()
            self.pending = True
            return self.preview()

    def finish(self) -> None:
        """仅由进程组合根在 Host 已退出、全部 writer 已关闭后调用。

        每次移动后写清单；中断留下 pending 文件，下一次启动拒绝自动恢复旧任务。
        """
        if not self.pending:
            return
        targets = self.targets()
        if self.backup.parent.is_symlink() or self.backup.parent.resolve() != self.backup.parent.absolute():
            raise ResetConflict("备份目录不能是符号链接或目录联接")
        self.backup.mkdir(parents=True, exist_ok=False)
        marker = self.paths.root / "reset-pending.json"
        report = {"state": "archiving", "backup_path": str(self.backup), "moved": []}
        # 完整、不可变的恢复计划先落盘；后续 journal 写入中断也不会丢原始路径映射。
        plan = [{"source": str(path), "backup": str(self.backup / f"{index:02d}-{path.name}")}
                for index, path in enumerate(targets) if path.exists()]
        (self.backup / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        marker.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            for index, path in enumerate(targets):
                if not path.exists():
                    continue
                destination = self.backup / f"{index:02d}-{path.name}"
                # 移动前持久化意图，即使进程在 rename 后崩溃也能人工恢复。
                report["next"] = {"source": str(path), "backup": str(destination)}
                marker.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(path, destination)
                report["moved"].append(report.pop("next"))
            report["state"] = "completed"
            (self.backup / "manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(marker, self.backup / "journal.json")
        except Exception:
            # 不复活旧执行端；备份与 pending 清单保留，避免半清空后继续执行。
            raise


_controller: ResetController | None = None


def configure_reset(controller: ResetController | None) -> None:
    global _controller
    _controller = controller


def get_reset_controller() -> ResetController | None:
    return _controller
