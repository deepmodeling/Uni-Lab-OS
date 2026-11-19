"""Registry loading for multiple variants that share a class and YAML contract."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from unilabos.registry.registry import Registry

FIX = Path(__file__).parent / "fixtures" / "external_variant_registry"


def test_registry_loads_multiple_variants_sharing_same_class():
    reg = Registry()
    if reg._startup_executor is None:
        reg._startup_executor = ThreadPoolExecutor(max_workers=2)

    reg.load_device_types(FIX, complete_registry=False)

    a = reg.device_type_registry["vendor.lh.model_a"]
    b = reg.device_type_registry["vendor.lh.model_b"]

    assert a["class"]["module"].endswith(":SharedDevice")
    assert b["class"]["module"].endswith(":SharedDevice")
    assert a["implementation"]["variant"] == "model_a"
    assert b["implementation"]["variant"] == "model_b"
    # Normalization preserves variant-specific class.init values.
    assert a["class"]["init"]["kwargs"]["channels"] == 8
    assert b["class"]["init"]["kwargs"]["channels"] == 96
    # The shared contract is expanded from $ref.
    assert "setup" in a["class"]["action_value_mappings"]
    assert "initialized" in b["class"]["status_types"]
