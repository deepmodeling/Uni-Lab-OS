"""分区合同使用紧凑 JSON，预算按实际落盘字节强制执行。"""

import json

import pytest

from unilabos.workflow import publication_catalog as catalog


def test_contract_storage_is_lossless_and_checks_compact_byte_budget(
    tmp_path, monkeypatch
):
    entry = {
        "contract": {
            "uuid": "60000000-0000-4000-8000-000000000001",
            "nodes": [{"name": "动作", "param": {"position": i}} for i in range(10)],
        }
    }
    compact = (
        json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()
    assert len(compact) < len(json.dumps(entry, ensure_ascii=False, indent=2).encode())
    identity = (tmp_path.stat().st_dev, tmp_path.stat().st_ino)
    monkeypatch.setattr(catalog, "PUBLICATION_CONTRACT_BYTE_LIMIT", len(compact) - 1)
    with pytest.raises(catalog.WorkflowPublicationCatalogError):
        catalog.WorkflowPublicationCatalog._write_contract_file(
            tmp_path, identity, entry
        )
    assert not list(tmp_path.iterdir())
    monkeypatch.setattr(catalog, "PUBLICATION_CONTRACT_BYTE_LIMIT", len(compact))
    catalog.WorkflowPublicationCatalog._write_contract_file(tmp_path, identity, entry)
    path = tmp_path / (entry["contract"]["uuid"] + ".json")
    assert path.read_bytes() == compact
    assert json.loads(path.read_bytes()) == entry


def test_catalog_reads_remain_detached_and_revalidate_changed_files(tmp_path):
    from uuid import uuid4

    contract = {
        key: {}
        for key in (
            "meta_data",
            "input_contract",
            "output_contract",
            "executor_requirements",
            "executor_binding_mapping",
            "boundary_mapping",
            "graph_snapshot",
        )
    }
    contract.update(
        uuid=str(uuid4()),
        workflow_uuid=str(uuid4()),
        node_template_uuid=str(uuid4()),
        create_time=0,
        update_time=0,
        workflow_revision=1,
        version=1,
        name="child",
        tags=[],
        source_hash="sha256:" + "a" * 64,
        contract_digest="sha256:" + "b" * 64,
        node_count=1,
        edge_count=0,
    )
    contract["graph_snapshot"] = {"nodes": [{"param": {"values": [1, 2]}}]}
    authority = catalog.WorkflowPublicationCatalog(
        package_root=tmp_path,
        package_root_identity=(tmp_path.stat().st_dev, tmp_path.stat().st_ino),
    )
    authority.save(source_draft_hash="sha256:" + "c" * 64, contract=contract)
    first, present = authority.list_entries_with_presence()
    assert present
    first[0]["contract"]["graph_snapshot"]["nodes"][0]["param"]["values"].append(3)
    second = authority.list_entries()
    assert second[0]["contract"]["graph_snapshot"]["nodes"][0]["param"]["values"] == [
        1,
        2,
    ]
    path = next((tmp_path / "workflow_publications").glob("*/contracts/*.json"))
    path.write_text("{}")
    with pytest.raises(catalog.WorkflowPublicationCatalogError):
        authority.list_entries()
