"""SQLite 项目执行入口补默认值时保留原 SQL 与事务语义。"""

import subprocess
import sys

import pytest

from unilabos.persistence.database import IntegrityError, SQLiteDatabase

VIEW = (
    "CREATE VIEW reagent AS SELECT * FROM container_content;"
    "CREATE TRIGGER content_insert INSTEAD OF INSERT ON reagent BEGIN "
    "INSERT INTO container_content VALUES (NEW.uuid,NEW.note,NEW.quantity,NEW.revision,NEW.meta_data); END;"
)


@pytest.fixture
def database(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "sql.db"))
    database.executescript(
        "CREATE TABLE container_content(uuid TEXT PRIMARY KEY, note TEXT, quantity REAL, "
        "revision INTEGER NOT NULL,meta_data TEXT NOT NULL);" + VIEW
    )
    try:
        yield database
    finally:
        database.close()


@pytest.mark.parametrize("source,count", [
    ("VALUES (:uuid, 'literal;--', :quantity), ('second', 'next', 0)", 2),
    ("SELECT :uuid, 'literal;--', :quantity UNION ALL SELECT 'second', 'next', 0", 2),
    ("WITH inputs AS (SELECT :uuid, 'literal;--', :quantity) SELECT * FROM inputs", 1),
    ("SELECT :uuid, 'literal;--', :quantity FROM (SELECT 1 AS n) a JOIN (SELECT 1 AS n) b ON a.n=b.n", 1),
    ("SELECT :uuid, 'literal;--', :quantity ORDER BY 1 LIMIT 1 -- trailing comment", 1),
])
def test_default_keeps_insert_source_and_bound_parameters(database, source, count):
    """补默认值不能改写参数、注释/字符串，也不能改变 SELECT 的行数。"""

    statement = f'/* caller */ INSERT INTO main."ReAgEnT" ("uuid", note, quantity) {source}'
    database.execute(statement, {"uuid": "first", "quantity": 2})
    rows = database.query_all("SELECT * FROM container_content ORDER BY uuid")
    assert rows[0] == ("first", "literal;--", 2, 1, "{}")
    assert len(rows) == count


def test_default_preserves_cte_numbered_parameters_and_returning(database):
    row = database.execute(
        "WITH inputs AS (SELECT ?2 AS uuid, ?1 AS quantity) "
        "INSERT INTO reagent AS target (uuid,quantity) SELECT * FROM inputs "
        "RETURNING uuid,revision,meta_data,quantity", (0, "returning"),
    ).fetchone()
    assert row == ("returning", 1, "{}", 0)
    assert database.query_one("SELECT * FROM container_content") == ("returning", None, 0, 1, "{}")


def test_temporary_tables_and_replaced_views_do_not_reuse_stale_defaults(database):
    statement = "INSERT INTO reagent(uuid) VALUES (?)"
    database.execute("CREATE TEMP TABLE reagent(uuid TEXT, revision INTEGER DEFAULT 7)")
    database.execute(statement, ("temp",))
    assert database.query_one("SELECT revision FROM temp.reagent") == (7,)
    database.execute("DROP TABLE temp.reagent")
    database.execute(statement, ("view",))
    assert database.query_one("SELECT revision FROM container_content") == (1,)
    database.executescript("DROP VIEW reagent; CREATE TABLE reagent(uuid TEXT, revision INTEGER DEFAULT 9);")
    database.execute(statement, ("physical",))
    assert database.query_one("SELECT revision FROM reagent") == (9,)


def test_script_resolves_views_created_in_the_same_script(database):
    database.execute("DROP VIEW reagent")
    database.executescript(VIEW + "INSERT INTO reagent(uuid) VALUES ('script');")
    assert database.query_one("SELECT revision,meta_data FROM container_content") == (1, "{}")
    assert not database.in_transaction


def test_script_preserves_pending_commit_and_explicit_transaction_on_failure(database):
    database.execute("INSERT INTO reagent(uuid) VALUES ('pending')")
    with pytest.raises(IntegrityError):
        database.executescript(
            "BEGIN; INSERT INTO reagent(uuid) VALUES ('rolled-back');"
            "INSERT INTO reagent(uuid,revision) VALUES ('invalid',NULL); COMMIT;"
        )
    assert database.in_transaction
    database.rollback()
    assert database.query_all("SELECT uuid FROM container_content") == [("pending",)]
    # 执行脚本后恢复普通 execute 的隐式事务行为。
    database.execute("INSERT INTO reagent(uuid) VALUES ('next')")
    assert database.in_transaction
    database.rollback()
    assert database.query_all("SELECT uuid FROM container_content") == [("pending",)]


def test_script_finishes_all_result_rows_and_returns_a_usable_cursor(database):
    seen = []
    database.create_function("observe", 1, lambda value: seen.append(value) or value)
    database.executemany("INSERT INTO reagent(uuid) VALUES (?)", [("first",), ("second",)])
    cursor = database.executescript("SELECT observe(uuid) FROM container_content;")
    assert sorted(seen) == ["first", "second"]
    assert cursor.execute("SELECT count(*) FROM container_content").fetchone() == (2,)


def test_unrelated_sqlite_insert_needs_no_inventory_dependencies():
    subprocess.run(
        [sys.executable, "-S", "-c", "\n".join([
            "from unilabos.persistence.database import SQLiteDatabase",
            "db = SQLiteDatabase(':memory:')",
            "db.execute('CREATE TABLE unrelated(id INTEGER)')",
            "db.execute('INSERT INTO unrelated VALUES (1)')",
            "assert db.query_one('SELECT id FROM unrelated') == (1,)",
            "db.close()",
        ])],
        check=True, capture_output=True, text=True,
    )


@pytest.mark.parametrize("source", ["other", "container_content"])
def test_unrelated_temporary_view_with_same_name_keeps_its_own_contract(database, source):
    database.executescript(
        "CREATE TEMP TABLE other(uuid TEXT);"
        f"CREATE TEMP VIEW reagent AS SELECT uuid FROM {source};"
        "CREATE TEMP TRIGGER other_insert INSTEAD OF INSERT ON reagent BEGIN "
        "INSERT INTO other VALUES (NEW.uuid); END;"
    )
    database.execute("INSERT INTO reagent(uuid) VALUES (?)", ("other",))
    assert database.query_all("SELECT * FROM other") == [("other",)]
    assert database.query_all("SELECT * FROM container_content") == []


@pytest.mark.parametrize("entry", ["execute", "executescript"])
def test_sql_file_bom_does_not_hide_insert_defaults(database, entry):
    getattr(database, entry)("\ufeffINSERT INTO reagent(uuid) VALUES ('bom');")
    assert database.query_one("SELECT uuid,revision FROM container_content") == ("bom", 1)


@pytest.mark.parametrize("parameter", [":returning", "@returning", "$returning", "$on::returning(key)"])
def test_named_parameters_are_not_sql_keywords(database, parameter):
    database.execute(
        f"INSERT INTO reagent(uuid) SELECT {parameter}", {parameter[1:]: "parameter"},
    )
    assert database.query_one("SELECT uuid,revision FROM container_content") == ("parameter", 1)


def test_cte_name_replace_is_not_the_insert_keyword(database):
    database.execute(
        "WITH replace AS (SELECT 'cte' AS uuid) INSERT INTO reagent(uuid) SELECT uuid FROM replace"
    )
    assert database.query_one("SELECT uuid,revision FROM container_content") == ("cte", 1)


@pytest.mark.parametrize("schema", ['"Ä"', "中文", "xä"])
def test_attached_schema_names_preserve_unicode(database, schema):
    database.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
    database.executescript(
        f"CREATE TABLE {schema}.container_content(uuid TEXT,revision INTEGER NOT NULL,meta_data TEXT NOT NULL);"
        f"CREATE VIEW {schema}.reagent AS SELECT * FROM container_content;"
        f"CREATE TRIGGER {schema}.content_insert INSTEAD OF INSERT ON reagent BEGIN "
        "INSERT INTO container_content VALUES (NEW.uuid,NEW.revision,NEW.meta_data); END;"
    )
    database.execute(f"INSERT INTO {schema}.reagent(uuid) VALUES ('attached')")
    assert database.query_one(f"SELECT * FROM {schema}.container_content") == ("attached", 1, "{}")
