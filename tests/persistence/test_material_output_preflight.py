"""物料输出预检在 SQLite 与真实 PostgreSQL 中保持只读和相同结论。"""

import pytest

from tests.app import test_material_output_preflight as original


@pytest.fixture
def runtime(database_mode, tmp_path):
    yield from original.runtime.__wrapped__(tmp_path)


def test_shared_output(database_mode, runtime):
    original.test_frozen_preflight_rejects_shared_material_output_without_reserving(runtime)


@pytest.mark.parametrize("policy", ["shared_source", "task_exclusive"])
def test_current_foreign_custody(database_mode, runtime, policy):
    original.test_known_output_current_foreign_usage_is_blocked_without_reserving(runtime, policy)


def test_child_custody(database_mode, runtime):
    original.test_shared_child_blocks_output_carrier_preflight(runtime)


def test_frozen_definition(database_mode, runtime):
    original.test_preflight_checks_original_published_custody_and_not_latest_graph(runtime)


def test_plain_reservation(database_mode, runtime):
    original.test_plain_inventory_reservation_also_blocks_known_output(runtime)


def test_many_output(database_mode, runtime):
    original.test_shared_source_in_many_output_is_checked(runtime)
