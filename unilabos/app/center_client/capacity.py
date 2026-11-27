"""新领取的容量门控；预留空间是准入预算，不删除或冻结已接受任务的结果。"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from unilabos.app.center_client.config import CapacityLimits

if TYPE_CHECKING:
    from unilabos.app.center_client.durable import DurableCenterStore


def disk_free_bytes(database_path: str | Path) -> int | None:
    try:
        directory = Path.cwd() if str(database_path) == ":memory:" else Path(database_path).absolute().parent
        return shutil.disk_usage(directory).free
    except OSError:
        return None


def snapshot(store: DurableCenterStore, config: CapacityLimits) -> dict[str, Any]:
    backlog = store.capacity_backlog()
    free = disk_free_bytes(store.path)
    # 给每个未结束的已领取调用，以及候选的新调用，分别预留 accepted/outcome 预算。
    reserved_calls = backlog["unfinished_claims"] + 1
    reserved_bytes = reserved_calls * config.result_reserve_bytes
    reserved_events = reserved_calls * 2
    reasons = []
    if backlog["pending_events"] + reserved_events > config.outbox_high_water_events:
        reasons.append("OUTBOX_EVENT_HIGH_WATER")
    if backlog["pending_bytes"] + reserved_bytes > config.outbox_high_water_bytes:
        reasons.append("OUTBOX_BYTE_HIGH_WATER")
    if free is None:
        reasons.append("DISK_CAPACITY_UNAVAILABLE")
    elif free < config.disk_free_reserve_bytes + reserved_bytes:
        reasons.append("DISK_FREE_RESERVE")
    return {
        **backlog, "new_claim_paused": bool(reasons), "pause_reasons": reasons,
        "disk_scope": "station_filesystem", "disk_free_bytes": free,
        "next_claim_reserved_events": reserved_events, "next_claim_reserved_bytes": reserved_bytes,
        "limits": {name: getattr(config, name) for name in (
            "outbox_high_water_events", "outbox_high_water_bytes", "disk_free_reserve_bytes", "result_reserve_bytes",
        )},
    }


def main(argv: list[str] | None = None) -> None:
    """本机运维入口；核实只重开原请求，不删除事件或绕过中心准入。"""
    import argparse
    import json
    import os
    import sqlite3

    from unilabos.app.center_client.durable import DurableCenterStore
    from unilabos.persistence.config import DatabaseSettings, configured_settings, use_database_settings
    from unilabos.persistence.database import open_database

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--database-type", choices=("sqlite", "postgresql"))
    parser.add_argument("--database-url-env", help="数据库连接环境变量名，避免在命令行传递凭据")
    commands = parser.add_subparsers(dest="command", required=True)
    status = commands.add_parser("status")
    status.add_argument("--after-digest", default="")
    retry = commands.add_parser("retry-request")
    retry.add_argument("--request-digest", required=True)
    retry.add_argument("--expected-attempts", required=True, type=int)
    retry.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    settings = configured_settings()
    if args.database_type is not None:
        if args.database_type == "postgresql" and (not args.database_url_env or not os.environ.get(args.database_url_env)):
            parser.error("PostgreSQL 模式须通过 --database-url-env 指定连接配置")
        if args.database_type == "sqlite" and args.database_url_env:
            parser.error("SQLite 模式不能使用 PostgreSQL 连接配置")
        settings = DatabaseSettings(args.database_type, os.environ[args.database_url_env] if args.database_type == "postgresql" else "")
    elif args.database_url_env:
        parser.error("--database-url-env 须显式选择 PostgreSQL 模式")
    with use_database_settings(settings):
        if settings.database_type == "sqlite" and not Path(args.database).is_file():
            parser.error("原工作流数据库不存在，禁止新建替代库")
        db = open_database(args.database, "workflow", private=True)
        db.row_factory = sqlite3.Row
        try:
            identity = db.execute("SELECT * FROM center_runtime_state WHERE singleton='runtime'").fetchone()
            if identity is None:
                parser.error("原中心运行身份缺失")
            station, instance = identity["station_uuid"], identity["instance_uuid"]
        finally:
            db.close()
        store = DurableCenterStore(args.database, station_uuid=station, instance_uuid=instance)
        try:
            if args.command == "retry-request":
                store.request_retry(args.request_digest, expected_attempts=args.expected_attempts, reason=args.reason)
                result = {"request_digest": args.request_digest, "retry_requested": True}
            else:
                holds = store.request_holds(after_digest=args.after_digest)
                result = {"capacity": snapshot(store, CapacityLimits.from_env()),
                          "workload": store.workload_diagnostics(),
                          "connection": {"available": False, "reason": "LIVE_RECEIVER_PROCESS_REQUIRED"},
                          "request_holds": holds,
                          "next_after_digest": holds[-1]["request_digest"] if holds else None}
            print(json.dumps(result, ensure_ascii=False))
        finally:
            store.close()


if __name__ == "__main__":
    main()
