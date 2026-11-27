"""已有 PG 表缺约束时修复；数据或结构冲突时整体回滚。仅操作随机测试库。"""
from uuid import uuid4

import pytest

from unilabos.persistence import schema


@pytest.fixture
def structure(postgres_test_url, monkeypatch):
    import psycopg
    from psycopg import sql

    connection = psycopg.connect(postgres_test_url, autocommit=True)
    parent = "repair_parent_" + uuid4().hex[:8]
    child = "repair_child_" + uuid4().hex[:8]
    index = "repair_index_" + uuid4().hex[:8]
    objects = [
        {"type": "table", "name": parent, "sql": f"CREATE TABLE {parent}(id TEXT PRIMARY KEY)",
         "foreign_keys": [], "columns": [{"name": "id"}]},
        {"type": "table", "name": child, "sql": f"""CREATE TABLE {child} (
            id TEXT PRIMARY KEY, parent_id TEXT NOT NULL REFERENCES {parent}(id),
            code TEXT NOT NULL UNIQUE, quantity INTEGER NOT NULL DEFAULT 0 CHECK(quantity >= 0)
        )""", "foreign_keys": [{"table": parent}],
         "columns": [{"name": x} for x in ("id", "parent_id", "code", "quantity")]},
        {"type": "index", "name": index,
         "sql": f"CREATE INDEX {index} ON {child}(parent_id)"},
    ]
    monkeypatch.setattr(schema, "catalog", lambda: {"repair": objects})
    connection.execute(sql.SQL("CREATE TABLE {} (id TEXT PRIMARY KEY)").format(sql.Identifier(parent)))
    connection.execute(sql.SQL("CREATE TABLE {} (id TEXT, parent_id TEXT, code TEXT, quantity BIGINT)").format(sql.Identifier(child)))
    yield connection, parent, child, index
    connection.close()


def columns(connection, table):
    return {r[0]: r[1:] for r in connection.execute(
        "SELECT column_name,is_nullable,column_default FROM information_schema.columns WHERE table_schema='public' AND table_name=%s", (table,))}


def test_missing_column_is_added_from_target_definition(structure):
    from psycopg import sql

    connection, parent, child, index = structure
    for obj in schema.catalog()["repair"]:
        if obj["name"] != child:
            continue
        obj["sql"] = obj["sql"].replace(
            "quantity INTEGER NOT NULL DEFAULT 0 CHECK(quantity >= 0)",
            "quantity INTEGER NOT NULL DEFAULT 0 CHECK(quantity >= 0), "
            "note TEXT NOT NULL DEFAULT ''",
        )
        obj["columns"].append({"name": "note"})
    connection.execute(sql.SQL("INSERT INTO {} VALUES ('p')").format(sql.Identifier(parent)))
    connection.execute(
        sql.SQL("INSERT INTO {} VALUES ('c','p','code',1)").format(sql.Identifier(child))
    )
    schema.initialize(connection, "repair")
    fields = columns(connection, child)
    assert fields["note"][0] == "NO"
    assert (
        connection.execute(
            sql.SQL("SELECT note FROM {}").format(sql.Identifier(child))
        ).fetchone()[0]
        == ""
    )
    schema.initialize(connection, "repair")
    assert (
        connection.execute(
            sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(child))
        ).fetchone()[0]
        == 1
    )


def test_missing_constraints_defaults_and_nullability_are_repaired(structure):
    from psycopg import sql
    connection, parent, child, index = structure
    connection.execute(sql.SQL("INSERT INTO {} VALUES ('p')").format(sql.Identifier(parent)))
    connection.execute(sql.SQL("INSERT INTO {} VALUES ('c','p','code',1)").format(sql.Identifier(child)))
    schema.initialize(connection, "repair")
    fields = columns(connection, child)
    assert all(value[0] == "NO" for value in fields.values())
    assert fields["quantity"][1] == "0"
    constraints = {r[0] for r in connection.execute(
        "SELECT contype FROM pg_constraint WHERE conrelid=%s::regclass", (child,))}
    assert constraints >= {"p", "u", "f", "c"}
    schema.initialize(connection, "repair")
    assert connection.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(child))).fetchone()[0] == 1


def test_invalid_existing_data_rejects_startup_and_rolls_back_repairs(structure):
    from psycopg import sql
    connection, parent, child, index = structure
    connection.execute(sql.SQL("INSERT INTO {} VALUES ('bad',NULL,'code',-1)").format(sql.Identifier(child)))
    with pytest.raises(Exception):
        schema.initialize(connection, "repair")
    assert columns(connection, child)["id"][0] == "YES"
    assert connection.execute(sql.SQL("SELECT quantity FROM {}").format(sql.Identifier(child))).fetchone()[0] == -1


def test_existing_wrong_named_index_is_not_silently_accepted(structure):
    from psycopg import sql
    connection, parent, child, index = structure
    connection.execute(sql.SQL("CREATE INDEX {} ON {} (code)").format(sql.Identifier(index),sql.Identifier(child)))
    with pytest.raises(RuntimeError, match="索引.*不一致"):
        schema.initialize(connection, "repair")


def test_existing_wrong_default_is_not_silently_overwritten(structure):
    from psycopg import sql
    connection, parent, child, index = structure
    connection.execute(sql.SQL("ALTER TABLE {} ALTER COLUMN quantity SET DEFAULT 99").format(sql.Identifier(child)))
    with pytest.raises(RuntimeError, match="默认值.*不一致"):
        schema.initialize(connection, "repair")
    assert columns(connection, child)["quantity"][1] == "99"


def test_matching_unvalidated_foreign_key_is_validated(structure):
    from psycopg import sql
    connection, parent, child, index = structure
    connection.execute(sql.SQL("ALTER TABLE {} ADD CONSTRAINT pending_fk FOREIGN KEY(parent_id) REFERENCES {}(id) NOT VALID").format(sql.Identifier(child),sql.Identifier(parent)))
    schema.initialize(connection, "repair")
    assert connection.execute("SELECT convalidated FROM pg_constraint WHERE conrelid=%s::regclass AND conname='pending_fk'", (child,)).fetchone()[0]


def test_conflicting_renamed_check_is_not_kept_alongside_new_check(structure):
    from psycopg import sql
    connection, parent, child, index = structure
    connection.execute(sql.SQL("ALTER TABLE {} ADD CONSTRAINT custom_quantity CHECK (quantity > 10)").format(sql.Identifier(child)))
    with pytest.raises(RuntimeError, match="约束定义不一致"):
        schema.initialize(connection, "repair")


def test_wrong_index_on_an_unrelated_table_is_rejected(structure):
    from psycopg import sql
    connection, parent, child, index = structure
    unrelated = "unrelated_" + uuid4().hex[:8]
    connection.execute(sql.SQL("CREATE TABLE {} (id TEXT)").format(sql.Identifier(unrelated)))
    connection.execute(sql.SQL("CREATE INDEX {} ON {} (id)").format(sql.Identifier(index),sql.Identifier(unrelated)))
    with pytest.raises(RuntimeError, match="索引.*不一致"):
        schema.initialize(connection, "repair")
