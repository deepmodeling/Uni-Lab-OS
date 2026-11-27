"""按 main 原物理表的合同验证内容兼容入口的默认值及主键写入。"""

from __future__ import annotations

import pytest

from tests.persistence.test_container_content_constraints import (
    TABLES,
    _insert,
    _valid_content,
    content_store,
)
from unilabos.persistence.database import IntegrityError

DEFAULT_CASES = [
    pytest.param("reagent", "revision", 1, 3, id="reagent-revision"),
    pytest.param("reagent", "meta_data", "{}", '{"caller":true}', id="reagent-meta-data"),
    pytest.param("sample", "meta_data", "{}", '{"caller":true}', id="sample-meta-data"),
    pytest.param("current_substance", "meta_data", "{}", '{"caller":true}', id="substance-meta-data"),
    pytest.param("current_substance", "composition", "[]", '[{"name":"water"}]', id="substance-composition"),
]


@pytest.mark.parametrize("input_mode", ["omitted", "null", "explicit"])
@pytest.mark.parametrize("table,column,default,explicit", DEFAULT_CASES)
def test_content_defaults_preserve_omitted_and_explicit_null(
    content_store, table, column, default, explicit, input_mode
):
    values = _valid_content(table)
    if input_mode == "omitted":
        values.pop(column)
    else:
        values[column] = None if input_mode == "null" else explicit
    if input_mode == "null":
        with pytest.raises(IntegrityError):
            with content_store.transaction() as connection:
                _insert(connection, table, values)
        assert content_store.query_all(f"SELECT * FROM {table}") == []
    else:
        with content_store.transaction() as connection:
            _insert(connection, table, values)
        saved = content_store.query_one(f"SELECT * FROM {table}")
        assert saved[column] == (default if input_mode == "omitted" else explicit)


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("new_uuid", ["changed", None, "collision"])
def test_content_uuid_updates_are_applied_or_rejected(content_store, table, new_uuid):
    values = _valid_content(table)
    with content_store.transaction() as connection:
        _insert(connection, table, values)
        if new_uuid == "collision":
            _insert(connection, table, {**values, "uuid": "collision", "deleted_at": "2026-02-01"})
    before = content_store.query_all(f"SELECT * FROM {table} ORDER BY uuid")
    if new_uuid == "changed":
        with content_store.transaction() as connection:
            connection.execute(f"UPDATE {table} SET uuid=? WHERE uuid=?", (new_uuid, values["uuid"]))
        assert content_store.query_one(f"SELECT uuid FROM {table}")["uuid"] == "changed"
    else:
        with pytest.raises(IntegrityError):
            with content_store.transaction() as connection:
                connection.execute(f"UPDATE {table} SET uuid=? WHERE uuid=?", (new_uuid, values["uuid"]))
        assert content_store.query_all(f"SELECT * FROM {table} ORDER BY uuid") == before


@pytest.mark.parametrize("entry", ["execute", "executemany", "executescript"])
@pytest.mark.parametrize("explicit_null", [False, True])
def test_reagent_default_through_sql_entry_points(content_store, entry, explicit_null):
    """批量、脚本与普通写入必须有相同的省略字段/显式 NULL 语义。"""

    columns = "uuid,create_time,update_time,material_uuid,reagent_info_uuid,quantity,quantity_unit"
    values = "'entry','2026-01-01','2026-01-01','material-reagent','info',0,'mL'"
    if explicit_null:
        columns += ",revision"
        values += ",NULL"
    statement = f"INSERT INTO reagent ({columns}) VALUES ({values})"

    def write():
        if entry == "executemany":
            content_store.db.executemany(statement, [()])
        else:
            getattr(content_store.db, entry)(statement + ";")

    if explicit_null:
        with pytest.raises(IntegrityError):
            write()
        content_store.db.rollback()
        assert content_store.query_all("SELECT * FROM reagent") == []
    else:
        write()
        content_store.db.commit()
        row = content_store.query_one("SELECT revision,meta_data,quantity FROM reagent")
        assert row == {"revision": 1, "meta_data": "{}", "quantity": 0}
