from pathlib import Path

from unilabos.server.services.runtime.workflow.convert_from_json import convert_from_json


def test_build_protocol_graph():
    data_path = Path(__file__).with_name("example_bio.json")

    graph = convert_from_json(data_path, workstation_name="PRCXi")

    assert graph.nodes
    assert graph.edges
