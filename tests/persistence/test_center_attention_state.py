"""事项变更检测的小字段 SQL 在 SQLite/PostgreSQL 上保持一致。"""
from tests.app.center_client import test_attention_api as contract
from tests.persistence.test_station_handoff import handoff_stations


def test_handoff_attention_state(database_mode, handoff_stations, monkeypatch):
    contract.check_handoff_attention_state(handoff_stations, monkeypatch)


def test_material_attention_state(database_mode, tmp_path, monkeypatch):
    contract.test_bridge_attention_state_tracks_export_failure_retry_and_resolution(tmp_path, monkeypatch)


def test_native_manual_attention_state(database_mode, tmp_path, monkeypatch):
    contract.test_native_attention_state_filters_local_manual_and_preserves_center_changes(tmp_path, monkeypatch)


def test_native_human_attention_state(database_mode, tmp_path):
    contract.test_native_attention_state_filters_local_human_interactions(tmp_path)
