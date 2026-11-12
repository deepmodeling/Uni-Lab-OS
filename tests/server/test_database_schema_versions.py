"""各库结构指纹与 schema_version 绑定：改了 DDL / contract_version 就必须提升版本并写迁移。"""

from __future__ import annotations

import pytest

from unilabos.server.database import DATABASE_SPECS

#: (schema_version, checksum)。DDL 或 contract_version 变化会改变 checksum：
#: 把对应库的 schema_version +1，在其 migrations 里补上迁移，再更新这里。
PINNED = {
    "runtime": (8, 'd3a0dcc80f865cc58bd45ea9409ac17acdd643f4877a80a400c6518fb08e74d1'),
    "materials": (7, "e331c178acd7a452b8752f1a086dc98662c786fb36d3cb0179e533e48bda7f74"),
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


def test_development_policy_rebuilds_without_migration() -> None:
    """开发期数据可丢：四个库都备份后重建。正式发布前把 runtime / materials 改为 refuse 并更新这里。"""
    assert {key: spec.on_unmigratable for key, spec in DATABASE_SPECS.items()} == {
        "runtime": "rebuild", "materials": "rebuild", "telemetry": "rebuild", "history": "rebuild",
    }
