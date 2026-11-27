"""SQLite 与 PostgreSQL 使用相同的空容器筛选和分页合同。"""

import pytest

from tests.app import test_empty_container_filter as contract


@pytest.mark.parametrize("kind", ["reagents", "samples", "current-substances"])
@pytest.mark.parametrize("quantity", [0, 1])
def test_active_and_removed_content(database_mode, tmp_path, kind, quantity):
    contract.test_empty_container_filter_excludes_active_content_and_restores_removed(tmp_path, kind, quantity)


@pytest.mark.parametrize("tags,expected", [('[" Container "]', 1), ('["notcontainer"]', 0)])
def test_container_template_filter(database_mode, tmp_path, tags, expected):
    contract.test_empty_container_filter_requires_container_template(tmp_path, tags, expected)
