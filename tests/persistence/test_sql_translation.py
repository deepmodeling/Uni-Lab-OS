"""SQLite JSON 谓词转 PostgreSQL 时保持 NULL 语义。"""

from unilabos.persistence.sql import postgres_sql


def test_json_is_not_one_uses_null_safe_postgresql_comparison() -> None:
    sql = postgres_sql(
        "SELECT 1 WHERE json_extract(meta_data, '$.unilab_edge_placeholder') IS NOT 1"
    )

    assert "IS DISTINCT FROM '1'" in sql
    assert "IS NOT 1" not in sql
