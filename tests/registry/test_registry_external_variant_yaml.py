"""Plan 09 Task 4: registry loads multiple variants sharing one class, with $ref.

Adapted to real Registry: @singleton + load_device_types(DIR) + needs executor +
device_type_registry stores runtime data (status_types may become class objects).
"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from unilabos.registry.registry import Registry

FIX = Path(__file__).parent / "fixtures" / "external_variant_registry"


def test_registry_loads_multiple_variants_sharing_same_class():
    reg = Registry()  # singleton (needs unilabos_msgs -> run on full env / 4090)
    if reg._startup_executor is None:
        reg._startup_executor = ThreadPoolExecutor(max_workers=2)

    reg.load_device_types(FIX, complete_registry=False)  # DIR, not a single file

    a = reg.device_type_registry["vendor.lh.model_a"]
    b = reg.device_type_registry["vendor.lh.model_b"]

    assert a["class"]["module"].endswith(":SharedDevice")
    assert b["class"]["module"].endswith(":SharedDevice")
    assert a["implementation"]["variant"] == "model_a"
    assert b["implementation"]["variant"] == "model_b"
    assert a["init_param_enforce"] == {"deck_name": "model-a-deck", "channels": 8}
    assert b["init_param_enforce"] == {"deck_name": "model-b-deck", "channels": 96}
    # $ref expanded into the shared contract
    assert "setup" in a["class"]["action_value_mappings"]
    assert "initialized" in b["class"]["status_types"]
