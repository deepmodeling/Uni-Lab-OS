from unilabos.resources import liquids, materials


class _SubstanceTarget:
    name = "target"

    def __init__(self) -> None:
        self.substances = []

    def set_liquids(self, substances) -> None:
        self.substances = substances


def test_liquids_module_keeps_material_helper_compatibility() -> None:
    assert liquids.apply_substances is materials.apply_substances
    assert liquids.resolve_site_spot is materials.resolve_site_spot
    assert liquids.resolve_substance_targets is materials.resolve_substance_targets
    assert liquids.set_substance_on_target is materials.set_substance_on_target


def test_material_helper_writes_solid_substance() -> None:
    target = _SubstanceTarget()

    result = materials.apply_substances(
        target,
        names=["NaCl"],
        amounts=[250.0],
        is_solid=[True],
    )

    assert result == [target]
    assert target.substances == [("NaCl", 250.0, materials.SOLID_UNIT)]
