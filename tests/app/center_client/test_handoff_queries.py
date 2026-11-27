"""交接检测器通过公共查询读取绑定与接收库位，不持有库存权威。"""

from dataclasses import FrozenInstanceError

import pytest

from tests.app.test_station_handoff import handoff_inventory_fixture, prepared
from unilabos.app.center_client.handoff_queries import InventoryHandoffQueries


def test_handoff_queries_return_read_only_snapshots_scoped_to_handoff(tmp_path):
    fixture = handoff_inventory_fixture(tmp_path)
    state = next(fixture)
    try:
        source = InventoryHandoffQueries(state["stores"]["source"], state["ids"]["source"])
        binding = source.container_binding(state["ids"]["container"])
        assert binding.barcode == "BKR-1"
        assert binding.barcode_namespace == "lab"
        assert source.station_uuid == state["ids"]["source"]
        with pytest.raises(FrozenInstanceError):
            binding.barcode = "changed"
        with pytest.raises(TypeError):
            binding.material_metadata["simulation"] = True
        assert source.container_binding("missing") is None
        prepared(state)
        target = InventoryHandoffQueries(state["stores"]["target"], state["ids"]["target"])
        item = target.incoming_item(state["ids"]["handoff"], state["ids"]["item"])
        assert item.site_uuid == state["sites"]["target"]
        assert item.barcode == "BKR-1"
        assert target.incoming_item("other-handoff", state["ids"]["item"]) is None
    finally:
        fixture.close()
