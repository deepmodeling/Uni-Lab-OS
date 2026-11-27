import sys
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import pytest

from unilabos.workflow.common import draw_protocol_graph_with_ports
from unilabos.workflow.convert_from_json import (
    convert_from_json,
)
from unilabos.workflow.convert_from_json import (
    normalize_labware as _normalize_labware,
)
from unilabos.workflow.convert_from_json import (
    normalize_steps as _normalize_steps,
)


@pytest.mark.parametrize(
    "protocol_name",
    [
        "example_bio",
    ],
)
def test_build_protocol_graph(protocol_name):
    data_path = Path(__file__).with_name(f"{protocol_name}.json")

    graph = convert_from_json(data_path, workstation_name="example")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    output_path = data_path.with_name(f"{protocol_name}_graph_{timestamp}.png")
    draw_protocol_graph_with_ports(graph, str(output_path))
    print(graph)
