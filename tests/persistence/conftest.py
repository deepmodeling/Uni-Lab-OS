"""同一组业务合同在 SQLite 和一次性 PostgreSQL 数据库中运行。"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

from unilabos.config.config import BasicConfig
from unilabos.persistence.database import PostgreSQLDatabase


@pytest.fixture(scope="session")
def postgres_test_url():
    admin_url = os.environ.get("UNILABOS_TEST_POSTGRES_URL")
    if not admin_url:
        pytest.skip("设置 UNILABOS_TEST_POSTGRES_URL 后运行真实 PostgreSQL 集成测试")
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql

    database = "unilab_test_" + uuid4().hex
    # 只创建和删除本测试自己生成的数据库，不清理调用者连接地址中的原数据库。
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    try:
        from urllib.parse import urlsplit, urlunsplit

        yield urlunsplit(urlsplit(admin_url)._replace(path="/" + database))
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                    sql.Identifier(database)
                )
            )


@pytest.fixture(params=["sqlite", "postgresql"])
def database_mode(request, monkeypatch):
    mode = request.param
    opened = []
    monkeypatch.setattr(BasicConfig, "database_type", mode)
    monkeypatch.setattr(BasicConfig, "database_url", "")
    if mode == "postgresql":
        connection_string = request.getfixturevalue("postgres_test_url")
        import psycopg
        from psycopg import sql

        monkeypatch.setattr(BasicConfig, "database_url", connection_string)
        with psycopg.connect(connection_string, autocommit=True) as connection:
            tables = connection.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='public'"
            ).fetchall()
            if tables:
                connection.execute(
                    sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(
                        sql.SQL(",").join(sql.Identifier(row[0]) for row in tables)
                    )
                )
        original_init = PostgreSQLDatabase.__init__

        def track(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            opened.append(self)

        monkeypatch.setattr(PostgreSQLDatabase, "__init__", track)
    yield mode
    for database in opened:
        database.close()
