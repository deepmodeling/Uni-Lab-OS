"""各库结构指纹与 schema_version 绑定：改了 DDL / contract_version 就必须提升版本并写迁移。

0.13 发布线上四个库都以 v1 为基线；runtime v2 起有迁移（见 tables/runtime/migrations.py）。
"""

from __future__ import annotations

import pytest

from unilabos.server.database import DATABASE_SPECS

#: (schema_version, checksum)。DDL 或 contract_version 变化会改变 checksum：
#: 把对应库的 schema_version +1，在其 migrations 里补上迁移，再更新这里。
PINNED = {
    "runtime": (2, "8542e457cc32ffe4334964b33cd57f58dc3a0adc89d06505edd8eb2a32b1c443"),
    "materials": (1, "27f565de0a9ae30fe86f0d0749e89a9819d68e2c086dad1f62f886b520b678a3"),
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
