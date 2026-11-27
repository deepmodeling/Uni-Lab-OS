"""任务文件归档：文件独立落盘，数据库保存归属、摘要及清理记录。"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from unilabos.persistence import open_database
from unilabos.utils.managed_files import MAX_FILE_BYTES, _atomic_write


class RunArtifactStore:
    def __init__(self, root: Path, legacy_roots: tuple[Path, ...] = ()):
        self.root = Path(root)
        self.legacy_roots = legacy_roots

    @classmethod
    def configured(cls):
        from unilabos.config.config import BasicConfig

        if not BasicConfig.working_dir:
            raise RuntimeError("运行目录尚未配置")
        working = Path(BasicConfig.working_dir)
        # 同机工作区的 Backend 与各代 Edge 共享归档；独立运行仍使用自己的目录。
        workbench = working.parent.parent
        is_backend = working.name == "local-domain" and working.parent.name == "backend"
        is_edge = working.parent.name == "edge"
        if workbench.name == "workbench" and (is_backend or is_edge):
            legacy = [workbench / "backend" / "local-domain" / "run_artifacts"]
            for generation in sorted((workbench / "edge").glob("*")):
                try:
                    uuid.UUID(generation.name)
                except ValueError:
                    continue
                if not generation.is_symlink():
                    legacy.append(generation / "run_artifacts")
            return cls(
                workbench / "run_artifacts",
                tuple(root for root in legacy if not root.is_symlink() and (root / "index.sqlite3").is_file()),
            )
        return cls(working / "run_artifacts")

    @contextmanager
    def database(self):
        self.root.mkdir(parents=True, exist_ok=True)
        db = open_database(str(self.root / "index.sqlite3"), "run_artifacts", private=True)
        db.row_factory = sqlite3.Row
        try:
            db.execute("""CREATE TABLE IF NOT EXISTS run_artifact (
                id TEXT PRIMARY KEY, task_uuid TEXT NOT NULL, job_uuid TEXT NOT NULL,
                name TEXT NOT NULL, media_type TEXT NOT NULL, kind TEXT NOT NULL,
                source TEXT NOT NULL, sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL,
                created_at TEXT NOT NULL, deleted_at TEXT)""")
            db.execute("CREATE INDEX IF NOT EXISTS run_artifact_task ON run_artifact(task_uuid)")
            db.commit()
            yield db
        finally:
            db.close()

    def archive(
        self, name: str, content: bytes, *, media_type: str, kind: str, source: str, task_uuid: str, job_uuid: str
    ) -> dict:
        task_uuid, job_uuid = str(uuid.UUID(task_uuid)), str(uuid.UUID(job_uuid))
        name = name.replace("\\", "/").split("/")[-1]
        if not name or any(ord(c) < 32 for c in name) or len(name) > 255:
            raise ValueError("文件名无效")
        if not content or len(content) > MAX_FILE_BYTES:
            raise ValueError("归档文件不能为空且不得超过 20 MiB")
        digest = hashlib.sha256(content).hexdigest()
        # 同一节点的重复解析可复用；不同任务/节点始终持有独立副本。
        with self.database() as db:
            # 查重和落盘共享写事务；并发重试只发布一份归档。
            db.begin()
            row = db.execute(
                """SELECT * FROM run_artifact WHERE task_uuid=? AND job_uuid=?
                AND kind=? AND source=? AND sha256=? AND deleted_at IS NULL""",
                (task_uuid, job_uuid, kind, source, digest),
            ).fetchone()
            if row:
                db.commit()
                return self.project(dict(row))
            identifier = str(uuid.uuid4())
            data = dict(
                id=identifier,
                task_uuid=task_uuid,
                job_uuid=job_uuid,
                name=name,
                media_type=media_type,
                kind=kind,
                source=source,
                sha256=digest,
                size_bytes=len(content),
                created_at=datetime.now(timezone.utc).isoformat(),
                deleted_at=None,
            )
            path = self.root / task_uuid / job_uuid / identifier
            _atomic_write(path, content)
            try:
                db.execute(
                    "INSERT INTO run_artifact (id,task_uuid,job_uuid,name,media_type,kind,source,sha256,size_bytes,created_at,deleted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    tuple(data.values()),
                )
                db.commit()
            except Exception:
                path.unlink(missing_ok=True)
                raise
        return self.project(data)

    @staticmethod
    def project(data: dict) -> dict:
        return {**data, "content_url": f"/api/v1/run-artifacts/{data['id']}/content/{quote(data['name'], safe='')}"}

    def list(self, task_uuid: str) -> list[dict]:
        task_uuid = str(uuid.UUID(task_uuid))
        with self.database() as db:
            items = [
                self.project(dict(row))
                for row in db.execute(
                    "SELECT * FROM run_artifact WHERE task_uuid=? ORDER BY created_at", (task_uuid,)
                ).fetchall()
            ]
        by_id = {item["id"]: item for item in items}
        for root in self.legacy_roots:
            for item in RunArtifactStore(root).list(task_uuid):
                by_id.setdefault(item["id"], item)
        return sorted(by_id.values(), key=lambda item: (item["created_at"], item["id"]))

    def _local_metadata(self, identifier: str) -> dict:
        identifier = str(uuid.UUID(identifier))
        with self.database() as db:
            row = db.execute("SELECT * FROM run_artifact WHERE id=?", (identifier,)).fetchone()
        if not row:
            raise FileNotFoundError("归档文件不存在")
        return self.project(dict(row))

    def _locate(self, identifier: str) -> tuple[RunArtifactStore, dict]:
        identifier = str(uuid.UUID(identifier))
        try:
            return self, self._local_metadata(identifier)
        except FileNotFoundError:
            for root in self.legacy_roots:
                store = RunArtifactStore(root)
                try:
                    return store, store._local_metadata(identifier)
                except FileNotFoundError:
                    continue
            raise FileNotFoundError("归档文件不存在") from None

    def metadata(self, identifier: str) -> dict:
        return self._locate(identifier)[1]

    def read(self, identifier: str) -> tuple[dict, bytes]:
        owner, data = self._locate(identifier)
        if data["deleted_at"]:
            raise FileNotFoundError("归档文件已清理")
        path = owner.root / data["task_uuid"] / data["job_uuid"] / data["id"]
        content = path.read_bytes()
        if len(content) != data["size_bytes"] or hashlib.sha256(content).hexdigest() != data["sha256"]:
            raise ValueError("归档文件校验失败")
        return data, content

    def delete(self, identifier: str) -> dict:
        owner, data = self._locate(identifier)
        if owner is not self:
            return owner.delete(identifier)
        # 保留元数据，使历史记录明确展示“已清理”，而非静默丢失。
        with self.database() as db:
            db.execute(
                "UPDATE run_artifact SET deleted_at=? WHERE id=? AND deleted_at IS NULL",
                (datetime.now(timezone.utc).isoformat(), data["id"]),
            )
            db.commit()
        (self.root / data["task_uuid"] / data["job_uuid"] / data["id"]).unlink(missing_ok=True)
        return self.metadata(identifier)


def archive_current_file(name: str, content: bytes, *, media_type: str, kind: str, source: str) -> dict:
    from unilabos.utils.tracing import capture_workflow_execution_identity

    identity = capture_workflow_execution_identity()
    return RunArtifactStore.configured().archive(
        name,
        content,
        media_type=media_type,
        kind=kind,
        source=source,
        task_uuid=identity.get("task_uuid") or identity.get("node_job_uuid", ""),
        job_uuid=identity.get("node_job_uuid", ""),
    )
