from unilabos.resources import materials


class _SubstanceTarget:
    name = "target"

    def __init__(self) -> None:
        self.substances = []

    def set_liquids(self, substances) -> None:
        self.substances = substances


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
