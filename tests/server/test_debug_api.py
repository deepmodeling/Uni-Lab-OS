"""``/api/v1/debug`` 四库只读浏览面的契约测试。"""

from __future__ import annotations

import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.api_envelope import error_of, ok_data
from unilabos.server.api import install_server_apis
from unilabos.server.composition import ServerServices
from unilabos.server.database import ServerDatabasePaths


def _open_stack(tmp_path):
    paths = ServerDatabasePaths.resolve(tmp_path)
    services = ServerServices.open(paths)
    app = FastAPI()
    install_server_apis(app, services)
    return services, TestClient(app)


def test_list_databases_reports_files_tables_and_row_counts(tmp_path) -> None:
    services, client = _open_stack(tmp_path)
    try:
        payload = ok_data(client.get("/api/v1/debug/databases"))
        assert payload["root"] == str(services.paths.root)
        by_name = {entry["database"]: entry for entry in payload["databases"]}
        assert set(by_name) == {"runtime", "materials", "telemetry", "history"}
        for entry in by_name.values():
            assert entry["exists"] is True
            assert entry["size_bytes"] > 0
            assert isinstance(entry["tables"], list)
            for table in entry["tables"]:
                assert set(table) == {"name", "rows"}
    finally:
        services.close()


def test_browse_table_returns_columns_and_latest_rows_first(tmp_path) -> None:
    services, client = _open_stack(tmp_path)
    try:
        runtime_tables = next(
            entry["tables"]
            for entry in ok_data(client.get("/api/v1/debug/databases"))["databases"]
            if entry["database"] == "runtime"
        )
        table = runtime_tables[0]["name"]

        # 真实 schema 的兼容性验证；行序断言见下面的独立小库用例。
        payload = ok_data(client.get(
            f"/api/v1/debug/databases/runtime/tables/{table}"
        ))
        assert payload["database"] == "runtime"
        assert payload["table"] == table
        assert payload["total_rows"] == len(payload["rows"]) == 0
        assert payload["columns"] and all(column["name"] for column in payload["columns"])
    finally:
        services.close()


def test_row_order_defaults_to_latest_first_and_blob_is_masked(tmp_path) -> None:
    paths = ServerDatabasePaths.resolve(tmp_path)
    for path in paths.as_mapping().values():
        sqlite3.connect(path).close()
    with sqlite3.connect(paths.runtime_db) as connection:
        connection.execute(
            "CREATE TABLE probe (id INTEGER PRIMARY KEY, note TEXT, raw BLOB)"
        )
        connection.executemany(
            "INSERT INTO probe (note, raw) VALUES (?, ?)",
            [("first", b"\x00\x01"), ("second", None), ("third", b"xyz")],
        )

    app = FastAPI()
    from unilabos.server.api.debug import install_debug_api

    install_debug_api(app, paths)
    client = TestClient(app)

    payload = ok_data(client.get("/api/v1/debug/databases/runtime/tables/probe"))
    assert payload["total_rows"] == 3
    assert [row["note"] for row in payload["rows"]] == ["third", "second", "first"]
    assert payload["rows"][0]["raw"] == "<blob 3 bytes>"
    assert payload["rows"][1]["raw"] is None

    ascending = ok_data(client.get(
        "/api/v1/debug/databases/runtime/tables/probe",
        params={"order": "id", "descending": "false", "limit": 2},
    ))
    assert [row["note"] for row in ascending["rows"]] == ["first", "second"]


def test_unknown_database_table_and_order_column_are_rejected(tmp_path) -> None:
    paths = ServerDatabasePaths.resolve(tmp_path)
    for path in paths.as_mapping().values():
        sqlite3.connect(path).close()
    with sqlite3.connect(paths.runtime_db) as connection:
        connection.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY)")

    app = FastAPI()
    from unilabos.server.api.debug import install_debug_api

    install_debug_api(app, paths)
    client = TestClient(app)

    # 不存在 → 4001，参数非法 → 1000（HTTP 200 信封）
    error_of(client.get("/api/v1/debug/databases/nope/tables/probe"), 4001)
    error_of(client.get("/api/v1/debug/databases/runtime/tables/missing"), 4001)
    error_of(
        client.get(
            "/api/v1/debug/databases/runtime/tables/probe",
            params={"order": "evil"},
        ),
        1000,
    )


def test_missing_database_file_reports_not_exists(tmp_path) -> None:
    paths = ServerDatabasePaths.resolve(tmp_path)

    app = FastAPI()
    from unilabos.server.api.debug import install_debug_api

    install_debug_api(app, paths)
    client = TestClient(app)

    payload = ok_data(client.get("/api/v1/debug/databases"))
    assert all(entry["exists"] is False for entry in payload["databases"])
    error_of(client.get("/api/v1/debug/databases/runtime/tables/anything"), 4001)
