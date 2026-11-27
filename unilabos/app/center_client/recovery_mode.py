"""工站离线灾备运行模式：先停止执行服务，再进入/核实/退出。"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path

from unilabos.persistence import recovery_mode
from unilabos.persistence.center_schema import ensure_center_schema
from unilabos.persistence.config import DatabaseSettings, configured_settings, use_database_settings
from unilabos.persistence.database import open_database
from unilabos.persistence.inspection import table_exists


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--database-type", choices=("sqlite", "postgresql"))
    parser.add_argument("--database-url-env", help="PostgreSQL 连接的环境变量名，不把凭据放在命令行")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    commands.add_parser("review")
    commands.add_parser("enter").add_argument("--reason", required=True)
    commands.add_parser("resume").add_argument("--verification-file", required=True, type=Path)
    commands.add_parser("sync-inventory").add_argument("--inventory-database", required=True)
    args = parser.parse_args()
    settings = configured_settings()
    if args.database_type is not None:
        if args.database_type == "postgresql" and (not args.database_url_env or not os.environ.get(args.database_url_env)):
            parser.error("PostgreSQL 模式须提供含连接配置的 --database-url-env")
        if args.database_type == "sqlite" and args.database_url_env:
            parser.error("SQLite 模式不能使用 PostgreSQL 连接配置")
        settings = DatabaseSettings(args.database_type, os.environ[args.database_url_env] if args.database_type == "postgresql" else "")
    elif args.database_url_env:
        parser.error("--database-url-env 须显式选择 PostgreSQL 模式")
    with use_database_settings(settings):
        run_command(args, parser)


def run_command(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """所有操作共享明确的数据库选择，维护子进程不能误用默认库。"""
    if configured_settings().database_type == "sqlite" and not Path(args.database).is_file():
        parser.error("原工作流数据库不存在，禁止在错误路径创建新库")
    db = open_database(args.database, "workflow", private=True)
    db.row_factory = sqlite3.Row
    try:
        if args.command == "status":
            result = recovery_mode.status(db)
        elif args.command == "enter":
            with db.transaction():
                ensure_center_schema(db, allow_event_gaps=True)
                result = recovery_mode.enter(db, args.reason)
        elif args.command == "review":
            with db.transaction():
                ensure_center_schema(db, allow_event_gaps=True)
                result = recovery_mode.review(db)
        elif args.command == "sync-inventory":
            from unilabos.persistence.center_epochs import synchronize_inventory_epoch

            if configured_settings().database_type == "sqlite" and not Path(args.inventory_database).is_file():
                parser.error("原库存数据库不存在，禁止创建新库替代恢复")
            inventory = open_database(args.inventory_database, "inventory", private=True)
            inventory.row_factory = sqlite3.Row
            try:
                with db.transaction(), inventory.transaction():
                    result = synchronize_inventory_epoch(db, inventory)
            finally:
                inventory.close()
        else:
            request = json.loads(args.verification_file.read_text())
            checkpoint = None
            if table_exists(db, "center_runtime_state"):
                from unilabos.app.center_client.client import CenterClient, ControlSession
                from unilabos.app.center_client.config import CenterClientConfig

                runtime = db.execute("SELECT * FROM center_runtime_state WHERE singleton='runtime'").fetchone()
                if runtime is None or runtime["session_json"] is None:
                    raise recovery_mode.EvidenceOnlyError("原中心身份或历史会话缺失，无法核实恢复基线")
                client = CenterClient(CenterClientConfig.from_env())
                client.restore_evidence_session(ControlSession(**json.loads(runtime["session_json"])))
                checkpoint = client.recovery_checkpoint(request["checkpoint_uuid"], event_epoch_uuid=runtime["event_epoch_uuid"],
                                                        recovery_uuid=recovery_mode.status(db)["recovery_uuid"])
            with db.transaction():
                ensure_center_schema(db, allow_event_gaps=True)
                result = recovery_mode.resume(db, request, checkpoint=checkpoint)
        print(json.dumps(result, ensure_ascii=False))
    finally:
        db.close()


if __name__ == "__main__":
    main()
