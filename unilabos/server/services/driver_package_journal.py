"""包操作的持久日志及源码/台账崩溃恢复；不重放安装器，不自动确认物理状态。"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

FILENAME = 'driver_package_operations.json'


def atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != 'nt':
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def ledger_data(root: Path) -> dict[str, Any]:
    path = root / 'driver_packages.json'
    if not path.exists():
        return {'version': 2, 'packages': []}
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or not isinstance(data.get('packages'), list):
        raise ValueError('驱动包台账格式损坏，拒绝覆盖')
    return data


def load_operations(root: Path) -> list[dict[str, Any]]:
    path = root / FILENAME
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('version') != 1 or not isinstance(data.get('operations'), list):
        raise ValueError('驱动包操作台账格式损坏')
    return data['operations']


def save_operations(root: Path, operations: list[dict[str, Any]]) -> None:
    atomic_json(root / FILENAME, {'version': 1, 'operations': operations})


def _managed_path(root: Path, value: str, *, backup: bool = False) -> Path:
    path = Path(value)
    boundary = (root / 'driver_packages' / '.staging' if backup else root / 'driver_packages').resolve()
    if path.is_symlink() or boundary not in path.resolve().parents:
        raise ValueError('恢复路径越过驱动包缓存边界')
    return path


def recover_operations(root: Path) -> list[dict[str, Any]]:
    """调用方必须持有跨进程变更锁；按已落盘台账判断提交或回滚。"""
    operations = load_operations(root)
    for operation in operations:
        if operation['status'] != 'running' and not (
            operation['status'] in {'interrupted', 'failed'} and (operation.get('recovery') or {}).get('outcome') == 'manual_repair_required'
        ):
            continue
        recovery = operation.get('recovery') or {}
        try:
            current = ledger_data(root)
            committed = recovery.get('expected_ledger') == current
            previous = recovery.get('previous_ledger')
            if not committed and previous is not None and current != previous:
                raise ValueError('包台账与操作前后快照均不一致，请人工核实，未改动源码')
            source = recovery.get('source')
            if source:
                target = _managed_path(root, source['target'])
                backup = _managed_path(root, source['backup'], backup=True)
                if committed:
                    if backup.exists():
                        shutil.rmtree(backup)
                else:
                    if backup.exists():
                        if target.exists():
                            shutil.rmtree(target)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        backup.rename(target)
                    elif not source['target_existed'] and target.exists():
                        shutil.rmtree(target)
            recovery['outcome'] = 'committed' if committed else 'rolled_back'
            operation['error'] = '操作进程中断；已核对源码与台账，请确认环境后继续'
        except (OSError, ValueError, KeyError) as exc:
            recovery['outcome'] = 'manual_repair_required'
            operation['error'] = str(exc)
        operation.update(status='interrupted', requires_confirmation=True, recovery=recovery,
                         finished_at_ms=int(time.time() * 1000), version=operation.get('version', 1) + 1)
        save_operations(root, operations)
    return operations
