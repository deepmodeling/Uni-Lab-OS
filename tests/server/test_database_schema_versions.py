"""各库结构指纹与 schema_version 绑定：改了 DDL / contract_version 就必须提升版本并写迁移。

0.13 发布线上四个库都以 v1 为基线（没有迁移）；发布前的结构变化直接并进 v1，发布后从 v2 起追加迁移。
main 发布前 runtime / materials 的 contract_version 提到 2，重置基线：之前的开发库不迁移、不修复，按指纹不符
拒绝启动（或按 ``database_rebuild_on_mismatch`` 重建）。
"""

from __future__ import annotations

import dataclasses

import pytest

from unilabos.config.config import BasicConfig
from unilabos.server.database import DATABASE_SPECS, initialize_database
from unilabos.server.database.schema import DatabaseMigrationRequired

#: (schema_version, checksum)。DDL 或 contract_version 变化会改变 checksum：
#: 把对应库的 schema_version +1，在其 migrations 里补上迁移，再更新这里。
PINNED = {
    "runtime": (1, "bea876e21df263c620db697c4eb74a959bb9b8b4f73866f089a223dfe4b5eb63"),
    "materials": (1, "c35dd5def14fe03dcfa7d9355acd0b8d4fb8901b0ba8640fc2c35bae36eaba77"),
    "telemetry": (1, "a0e00118c131bbae1e4a693563977d7ffff49c17c482ebad0e5ad2b224b548af"),
    "history": (1, "5005469f86db3fe747ab1f30ea32d0c0c8f2f2779202a81bedb8d8254217f4cc"),
}


def test_every_database_is_pinned() -> None:
    assert set(PINNED) == set(DATABASE_SPECS)


@pytest.mark.parametrize("key", sorted(PINNED))
def test_schema_changes_come_with_a_version_bump(key: str) -> None:
    spec = DATABASE_SPECS[key]
    version, checksum = PINNED[key]
    assert spec.checksum == checksum or spec.schema_version > version, (
        f"{key} 的 DDL 或 contract_version 变了，但 schema_version 仍是 {spec.schema_version}："
        "提升 schema_version，在 migrations 里补迁移（runtime / materials 没有迁移会拒绝启动），"
        "再更新本文件的 PINNED"
    )
    assert (spec.schema_version, spec.checksum) == PINNED[key], f"{key} 已升级：请同步更新 PINNED"


def test_release_policy_refuses_runtime_and_materials_without_migration() -> None:
    """工作流 / 注册表与物料是实验室的事实，没有迁移路径时拒绝启动；遥测与历史可丢，备份后重建。"""
    assert {key: spec.on_unmigratable for key, spec in DATABASE_SPECS.items()} == {
        "runtime": "refuse", "materials": "refuse", "telemetry": "rebuild", "history": "rebuild",
    }


@pytest.mark.parametrize("key", ["runtime", "materials"])
def test_a_database_from_before_the_baseline_reset_is_refused_or_rebuilt(tmp_path, monkeypatch, key: str) -> None:
    """重置前（contract v1）的开发库：默认拒绝启动、旧库原样保留；允许重建时改名备份后建新库。"""

    spec = DATABASE_SPECS[key]
    old = dataclasses.replace(spec, contract_version=1)
    path = tmp_path / spec.filename
    initialize_database(path, old).close()
    monkeypatch.setattr(BasicConfig, "database_rebuild_on_mismatch", False, raising=False)

    with pytest.raises(DatabaseMigrationRequired, match="需要重建"):
        initialize_database(path, spec)
    assert path.exists()

    monkeypatch.setattr(BasicConfig, "database_rebuild_on_mismatch", True, raising=False)
    connection = initialize_database(path, spec)
    try:
        stored = connection.execute("SELECT checksum FROM schema_identity").fetchone()[0]
    finally:
        connection.close()
    assert stored == spec.checksum
    assert list(tmp_path.glob(f"{spec.filename}.bak-*"))
