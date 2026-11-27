"""合表后，项目 SQL 入口仍保留三类原表的原子 UPSERT 合同。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.persistence.test_container_content_constraints import (
    INVALID_INPUTS,
    TABLES,
    _insert,
    _valid_content,
    content_store,
)
from tests.persistence.test_content_compatibility import DEFAULT_CASES
from unilabos.persistence.database import DatabaseError, IntegrityError, open_database


def _statement(table, values, action="DO UPDATE SET quantity=excluded.quantity", target="(uuid)"):
    return (
        f"INSERT INTO {table} ({','.join(values)}) VALUES ({','.join('?' for _ in values)}) "
        f"ON CONFLICT {target} {action}"
    )


def _upsert(connection, table, values, **options):
    return connection.execute(_statement(table, values, **options), tuple(values.values()))


@pytest.mark.parametrize("table", TABLES)
def test_upsert_inserts_updates_and_obeys_conditional_where(content_store, table):
    values = _valid_content(table)
    with content_store.transaction() as connection:
        _upsert(connection, table, values)
        _upsert(connection, table, {**values, "quantity": 0})
        _upsert(connection, table, {**values, "quantity": 7},
                action="DO UPDATE SET quantity=excluded.quantity WHERE excluded.quantity<quantity")
    assert content_store.query_one(f"SELECT uuid,quantity FROM {table}") == {
        "uuid": values["uuid"], "quantity": 0,
    }


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("target", ["(uuid)", ""])
def test_do_nothing_preserves_existing_rows_and_returns_nothing(content_store, table, target):
    values = _valid_content(table)
    with content_store.transaction() as connection:
        _insert(connection, table, values)
        rows = _upsert(connection, table, {**values, "quantity": 9}, target=target,
                       action="DO NOTHING RETURNING uuid").fetchall()
    assert rows == []
    assert content_store.query_one(f"SELECT quantity FROM {table}")["quantity"] == 1


@pytest.mark.parametrize("source,target", [(a, b) for a in TABLES for b in TABLES if a != b])
@pytest.mark.parametrize("key", ["uuid", "material_uuid"])
def test_upsert_cannot_update_a_different_content_kind(content_store, source, target, key):
    original = _valid_content(source)
    values = {**_valid_content(target), key: original[key], "quantity": 9}
    conflict = "(uuid)" if key == "uuid" else "(material_uuid) WHERE deleted_at IS NULL"
    with content_store.transaction() as connection:
        _insert(connection, source, original)
    before = content_store.query_all("SELECT * FROM container_content")
    with pytest.raises(IntegrityError):
        with content_store.transaction() as connection:
            _upsert(connection, target, values, target=conflict)
    assert content_store.query_all("SELECT * FROM container_content") == before
    assert content_store.query_all(f"SELECT * FROM {target}") == []


@pytest.mark.parametrize("table,changes", INVALID_INPUTS)
def test_upsert_update_cannot_bypass_original_constraints(content_store, table, changes):
    values = _valid_content(table)
    with content_store.transaction() as connection:
        _insert(connection, table, values)
    before = content_store.query_one(f"SELECT * FROM {table}")
    assignments = ",".join(f"{column}=?" for column in changes)
    statement = _statement(table, values, action="DO UPDATE SET " + assignments)
    with pytest.raises(IntegrityError):
        with content_store.transaction() as connection:
            connection.execute(statement, (*values.values(), *changes.values()))
    assert content_store.query_one(f"SELECT * FROM {table}") == before


@pytest.mark.parametrize("input_mode", ["omitted", "null"])
@pytest.mark.parametrize("table,column,default,explicit", DEFAULT_CASES)
def test_upsert_preserves_default_values_and_explicit_null(
    content_store, table, column, default, explicit, input_mode,
):
    values = _valid_content(table)
    if input_mode == "omitted":
        values.pop(column)
        with content_store.transaction() as connection:
            _upsert(connection, table, values)
            _upsert(connection, table, {**values, "quantity": 2},
                    action=f"DO UPDATE SET {column}=excluded.{column},quantity=excluded.quantity")
        saved = content_store.query_one(f"SELECT {column},quantity FROM {table}")
        assert saved == {column: default, "quantity": 2}
    else:
        values[column] = None
        with pytest.raises(IntegrityError):
            with content_store.transaction() as connection:
                _upsert(connection, table, values)
        assert content_store.query_all(f"SELECT * FROM {table}") == []


def test_sample_upsert_matches_original_partial_expression_index(content_store):
    first = _valid_content("sample")
    second = {**first, "uuid": "second", "material_uuid": "material-reagent", "code": "s-1", "quantity": 2}
    with content_store.transaction() as connection:
        _upsert(connection, "sample", first,
                target="(LOWER(code)) WHERE deleted_at IS NULL")
        _upsert(connection, "sample", second,
                target="(LOWER(code)) WHERE deleted_at IS NULL")
    assert content_store.query_all("SELECT uuid,quantity FROM sample") == [
        {"uuid": first["uuid"], "quantity": 2},
    ]


@pytest.mark.parametrize("table", TABLES)
def test_soft_deleted_rows_do_not_trigger_active_material_conflict(content_store, table):
    values = _valid_content(table)
    with content_store.transaction() as connection:
        _insert(connection, table, {**values, "deleted_at": "2026-03-01"})
        _upsert(connection, table, {**values, "uuid": "new"},
                target="(material_uuid) WHERE deleted_at IS NULL")
    assert content_store.query_one(f"SELECT uuid FROM {table} WHERE deleted_at IS NULL") == {"uuid": "new"}
    assert len(content_store.query_all(f"SELECT * FROM {table}")) == 2


@pytest.mark.parametrize("table", TABLES)
def test_returning_star_keeps_the_original_view_projection(content_store, table):
    values = _valid_content(table)
    with content_store.transaction() as connection:
        row = _upsert(connection, table, values,
                      action="DO UPDATE SET quantity=excluded.quantity RETURNING *").fetchone()
    saved = content_store.query_one(f"SELECT * FROM {table}")
    assert dict(row) == saved
    assert "kind" not in row.keys()
    assert "_ulab_rowid" not in row.keys()


@pytest.mark.parametrize("table,column", [
    ("sample", "revision"), ("reagent", "composition"), ("current_substance", "reagent_info_uuid"),
])
@pytest.mark.parametrize("location", ["insert", "update", "returning"])
def test_upsert_does_not_expose_other_content_columns(content_store, table, column, location):
    values = _valid_content(table)
    if location == "insert":
        values[column] = None
    action = "DO UPDATE SET quantity=excluded.quantity"
    if location == "update":
        action = f"DO UPDATE SET {column}=NULL"
    elif location == "returning":
        action += f" RETURNING {column}"
    with pytest.raises(DatabaseError):
        with content_store.transaction() as connection:
            _upsert(connection, table, values, action=action)
    assert content_store.query_all("SELECT * FROM container_content") == []


@pytest.mark.parametrize("entry", ["execute", "executemany", "executescript"])
def test_alias_upsert_works_at_every_sql_entry(content_store, entry):
    statement = (
        "INSERT INTO reagent AS bottle(uuid,create_time,update_time,material_uuid,reagent_info_uuid,quantity,quantity_unit) "
        "VALUES ('entry','2026-01-01','2026-01-01','material-reagent','info',1,'mL') "
        "ON CONFLICT(uuid) DO UPDATE SET quantity=bottle.quantity+excluded.quantity"
    )
    if entry == "executescript":
        # 脚本自行管理完整事务，不嵌套调用方事务。
        content_store.db.executescript("BEGIN; " + statement + "; " + statement + "; COMMIT;")
    else:
        with content_store.transaction() as connection:
            if entry == "executemany":
                connection.executemany(statement, [(), ()])
            else:
                connection.execute(statement)
                connection.execute(statement)
    assert content_store.query_one("SELECT quantity,revision,meta_data FROM reagent") == {
        "quantity": 2, "revision": 1, "meta_data": "{}",
    }


def test_upsert_accepts_insert_select_cte_and_keeps_transaction_reusable(content_store):
    statement = (
        "WITH candidate AS (SELECT ? AS uuid,? AS quantity) "
        "INSERT INTO reagent(uuid,create_time,update_time,material_uuid,reagent_info_uuid,quantity,quantity_unit) "
        "SELECT uuid,'2026-01-01','2026-01-01','material-reagent','info',quantity,'mL' FROM candidate "
        "WHERE 1 ON CONFLICT(uuid) DO UPDATE SET quantity=reagent.quantity+excluded.quantity "
        "RETURNING uuid,quantity,revision,meta_data"
    )
    with content_store.transaction() as connection:
        first = connection.execute(statement, ("cte", 1)).fetchone()
        assert dict(first) == {"uuid": "cte", "quantity": 1, "revision": 1, "meta_data": "{}"}
        with pytest.raises(IntegrityError):
            connection.execute(statement, ("cte", -1))
        second = connection.execute(statement, ("cte", 2)).fetchone()
        assert dict(second)["quantity"] == 3


@pytest.mark.parametrize("table", TABLES)
def test_upsert_without_column_list_uses_the_old_view_column_order(content_store, table):
    values = _valid_content(table)
    with content_store.transaction() as connection:
        _insert(connection, table, values)
    row = content_store.query_one(f"SELECT * FROM {table}")
    row["quantity"] = 3
    with content_store.transaction() as connection:
        connection.execute(
            f"INSERT INTO {table} VALUES ({','.join('?' for _ in row)}) "
            "ON CONFLICT(uuid) DO UPDATE SET quantity=excluded.quantity", tuple(row.values()),
        )
    assert content_store.query_one(f"SELECT quantity FROM {table}")["quantity"] == 3


def test_replace_upsert_cannot_delete_another_content_kind(content_store):
    with content_store.transaction() as connection:
        _insert(connection, "reagent", _valid_content("reagent"))
    values = {**_valid_content("sample"), "uuid": "content-reagent"}
    statement = _statement("sample", values,
                           target="(LOWER(code)) WHERE deleted_at IS NULL").replace("INSERT INTO", "INSERT OR REPLACE INTO", 1)
    before = content_store.query_all("SELECT * FROM container_content")
    with pytest.raises(DatabaseError):
        with content_store.transaction() as connection:
            connection.execute(statement, tuple(values.values()))
    assert content_store.query_all("SELECT * FROM container_content") == before


def test_multirow_upsert_failure_rolls_back_earlier_updates(content_store):
    first = _valid_content("sample")
    other = {**_valid_content("reagent"), "uuid": "other"}
    with content_store.transaction() as connection:
        _insert(connection, "sample", first)
        _insert(connection, "reagent", other)
    before = content_store.query_all("SELECT * FROM container_content ORDER BY uuid")
    rows = [{**first, "quantity": 2}, {**first, "uuid": "other", "code": "new", "material_uuid": "material-current_substance"}]
    statement = (
        f"INSERT INTO sample ({','.join(first)}) VALUES "
        + ",".join("(" + ",".join("?" for _ in first) + ")" for _ in rows)
        + " ON CONFLICT(uuid) DO UPDATE SET quantity=excluded.quantity"
    )
    with pytest.raises(IntegrityError):
        with content_store.transaction() as connection:
            connection.execute(statement, tuple(value for row in rows for value in row.values()))
    assert content_store.query_all("SELECT * FROM container_content ORDER BY uuid") == before


def test_atomic_upsert_does_not_lose_concurrent_increments(content_store, tmp_path):
    values = _valid_content("reagent")
    with content_store.transaction() as connection:
        _insert(connection, "reagent", values)
    statement = _statement("reagent", values,
                           action="DO UPDATE SET quantity=reagent.quantity+excluded.quantity")

    def increment():
        database = open_database(str(tmp_path / "inventory.db"), "inventory")
        try:
            for _ in range(10):
                with database.transaction() as connection:
                    connection.execute(statement, tuple(values.values()))
        finally:
            database.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        list(workers.map(lambda _: increment(), range(2)))
    assert content_store.query_one("SELECT quantity FROM reagent")["quantity"] == 21


@pytest.mark.parametrize("limit,parameters", [
    ("?,?", (1, 2)), ("?2,?1", (2, 1)),
    (":offset,:count", (1, 2)), (":offset,:count", {"offset": 1, "count": 2}),
])
def test_upsert_keeps_original_binding_slots_when_sql_order_changes(content_store, limit, parameters):
    statement = (
        "INSERT INTO reagent(uuid,create_time,update_time,deleted_at,material_uuid,reagent_info_uuid,quantity,quantity_unit) "
        "SELECT 'row-'||column1,'2026-01-01','2026-01-01','2026-03-01','material-reagent','info',1,'mL' "
        f"FROM (VALUES (1),(2),(3)) LIMIT {limit} "
        "ON CONFLICT(uuid) DO UPDATE SET quantity=excluded.quantity"
    )
    with content_store.transaction() as connection:
        connection.execute(statement, parameters)
    assert content_store.query_all("SELECT uuid FROM reagent ORDER BY uuid") == [
        {"uuid": "row-2"}, {"uuid": "row-3"},
    ]


@pytest.mark.parametrize("expression", ["(SELECT kind)", "container_content.kind", "impossible.*"])
def test_returning_cannot_bypass_the_view_projection(content_store, expression):
    values = _valid_content("reagent")
    with pytest.raises(DatabaseError):
        with content_store.transaction() as connection:
            _upsert(connection, "reagent", values,
                    action=f"DO UPDATE SET quantity=excluded.quantity RETURNING {expression}")
    assert content_store.query_all("SELECT * FROM container_content") == []


@pytest.mark.parametrize("key", ["((LOWER(code)))", "LOWER((code))", "LOWER(code) COLLATE BINARY"])
def test_sample_partial_upsert_preserves_legal_index_expressions(content_store, key):
    values = _valid_content("sample")
    with content_store.transaction() as connection:
        _upsert(connection, "sample", values, target=f"({key}) WHERE deleted_at IS NULL")
        _upsert(connection, "sample", {**values, "quantity": 2},
                target=f"({key}) WHERE deleted_at IS NULL")
    assert content_store.query_one("SELECT quantity FROM sample")["quantity"] == 2


def test_or_ignore_keeps_an_explicit_conflict_update(content_store):
    values = _valid_content("reagent")
    with content_store.transaction() as connection:
        _insert(connection, "reagent", values)
        statement = _statement("reagent", values).replace("INSERT INTO", "INSERT OR IGNORE INTO", 1)
        connection.execute(statement, tuple({**values, "quantity": 2}.values()))
    assert content_store.query_one("SELECT quantity FROM reagent")["quantity"] == 2


@pytest.mark.parametrize("predicate", ["CAST(kind AS INTEGER)='sample'", "kind=CAST('sample' AS INTEGER)"])
def test_sqlite_does_not_rewrite_user_views_with_different_filter_semantics(content_store, database_mode, predicate):
    if database_mode != "sqlite":
        pytest.skip("验证 SQLite 对自定义视图的延迟类型检查")
    original = content_store.query_one("SELECT sql FROM sqlite_master WHERE name='sample' AND type='view'")["sql"]
    modified = original.replace("kind = 'sample'", predicate)
    assert modified != original
    content_store.db.execute("DROP VIEW sample")
    content_store.db.execute(modified)
    with pytest.raises(DatabaseError):
        with content_store.transaction() as connection:
            _upsert(connection, "sample", _valid_content("sample"))
    assert content_store.query_all("SELECT * FROM container_content") == []


def test_subquery_can_read_its_own_columns_without_exposing_target_columns(content_store):
    values = _valid_content("reagent")
    with content_store.transaction() as connection:
        _insert(connection, "reagent", values)
        row = _upsert(connection, "reagent", values, action=(
            "DO UPDATE SET description=(SELECT name FROM reagent_info WHERE uuid='info') "
            "RETURNING description, (SELECT reagent.kind FROM (SELECT 'local' AS kind) reagent) AS local_kind"
        )).fetchone()
    assert dict(row) == {"description": "水", "local_kind": "local"}


def test_subquery_alias_without_the_column_cannot_hide_a_target_reference(content_store):
    values = _valid_content("reagent")
    with pytest.raises(DatabaseError):
        with content_store.transaction() as connection:
            _upsert(connection, "reagent", values, action=(
                "DO UPDATE SET quantity=excluded.quantity "
                "RETURNING (SELECT reagent.kind FROM (SELECT 1 AS value) reagent)"
            ))
    assert content_store.query_all("SELECT * FROM container_content") == []


def test_qualified_star_does_not_expose_unselected_columns(content_store):
    values = _valid_content("sample")
    with pytest.raises(DatabaseError):
        with content_store.transaction() as connection:
            _upsert(connection, "sample", values, action=(
                "DO UPDATE SET quantity=excluded.quantity RETURNING "
                "(SELECT kind FROM (SELECT a.* FROM (SELECT 'x' AS x) a "
                "CROSS JOIN (SELECT 'local' AS kind) b) container_content)"
            ))
    assert content_store.query_all("SELECT * FROM container_content") == []


def test_subquery_accepts_multiple_unnamed_local_sources(content_store):
    with content_store.transaction() as connection:
        row = _upsert(connection, "sample", _valid_content("sample"), action=(
            "DO UPDATE SET quantity=excluded.quantity RETURNING "
            "(SELECT kind || name || suffix FROM (SELECT 'K' AS kind), (SELECT 'N' AS name), "
            "(SELECT '!' AS suffix) _ulab_content_local_0) AS label"
        )).fetchone()
    assert dict(row) == {"label": "KN!"}


def test_numbered_reuse_can_name_an_anonymous_parameter_slot(content_store):
    with content_store.transaction() as connection:
        connection.execute(
            "INSERT INTO sample(uuid,create_time,update_time,material_uuid,code,name,quantity,quantity_unit) "
            "VALUES (?,?1,'2026-01-01','material-sample','S-1','样品',1,'g') "
            "ON CONFLICT(uuid) DO UPDATE SET quantity=excluded.quantity", {"1": "shared-slot"},
        )
    assert content_store.query_one("SELECT uuid,create_time FROM sample") == {
        "uuid": "shared-slot", "create_time": "shared-slot",
    }


@pytest.mark.parametrize("alias", ["", " AS items"])
def test_subquery_can_read_json_each_columns(content_store, alias):
    reference = "items.value" if alias else "value"
    with content_store.transaction() as connection:
        row = _upsert(connection, "sample", _valid_content("sample"), action=(
            "DO UPDATE SET quantity=excluded.quantity RETURNING "
            f"(SELECT {reference} FROM json_each('[\"local\"]'){alias}) AS label"
        )).fetchone()
    assert dict(row) == {"label": "local"}


def test_subquery_reads_generated_local_column_instead_of_target(content_store):
    with content_store.transaction() as connection:
        # 原生 DDL 安装生成列；待测 UPSERT 及列绑定仍走项目入口。
        connection._connection.execute(
            "CREATE TABLE lookup (value TEXT, name TEXT GENERATED ALWAYS AS ('local') STORED)"
        )
        connection.execute("INSERT INTO lookup(value) VALUES ('row')")
        row = _upsert(connection, "sample", {**_valid_content("sample"), "name": "target"}, action=(
            "DO UPDATE SET quantity=excluded.quantity RETURNING (SELECT name FROM lookup) AS label"
        )).fetchone()
        connection.execute("DROP TABLE lookup")
    assert dict(row) == {"label": "local"}


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("operation", ["insert", "update"])
def test_upsert_keeps_material_foreign_keys(content_store, table, operation):
    values = _valid_content(table)
    if operation == "update":
        with content_store.transaction() as connection:
            _insert(connection, table, values)
    before = content_store.query_all("SELECT * FROM container_content")
    with pytest.raises(IntegrityError):
        with content_store.transaction() as connection:
            _upsert(connection, table, {**values, "material_uuid": "missing-material"},
                    action="DO UPDATE SET material_uuid=excluded.material_uuid")
    assert content_store.query_all("SELECT * FROM container_content") == before


@pytest.mark.parametrize("table", TABLES)
def test_conflict_update_keeps_global_active_material_uniqueness(content_store, table):
    other_table = TABLES[(TABLES.index(table) + 1) % len(TABLES)]
    values = _valid_content(table)
    other = _valid_content(other_table)
    with content_store.transaction() as connection:
        _insert(connection, table, values)
        _insert(connection, other_table, other)
    before = content_store.query_all("SELECT * FROM container_content ORDER BY uuid")
    with pytest.raises(IntegrityError):
        with content_store.transaction() as connection:
            _upsert(connection, table, {**values, "material_uuid": other["material_uuid"]},
                    action="DO UPDATE SET material_uuid=excluded.material_uuid")
    assert content_store.query_all("SELECT * FROM container_content ORDER BY uuid") == before
