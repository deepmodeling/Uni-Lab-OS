import pytest
import json
from pathlib import Path

from pylabrobot.resources import Resource as ResourcePLR
from unilabos.resources.graphio import resource_bioyond_to_plr
from unilabos.registry.registry import lab_registry

from unilabos.resources.bioyond.decks import BIOYOND_PolymerReactionStation_Deck

lab_registry.setup()

FIXTURE_DIR = Path(__file__).parent


type_mapping = {
    "烧杯": ("BIOYOND_PolymerStation_1FlaskCarrier", "3a14196b-24f2-ca49-9081-0cab8021bf1a"),
    "试剂瓶": ("BIOYOND_PolymerStation_1BottleCarrier", ""),
    "样品板": ("BIOYOND_PolymerStation_6StockCarrier", "3a14196e-b7a0-a5da-1931-35f3000281e9"),
    "分装板": ("YB_6VialCarrier", "3a14196e-5dfe-6e21-0c79-fe2036d052c4"),
    "样品瓶": ("BIOYOND_PolymerStation_Solid_Stock", "3a14196a-cf7d-8aea-48d8-b9662c7dba94"),
    "90%分装小瓶": ("BIOYOND_PolymerStation_Solid_Vial", "3a14196c-cdcf-088d-dc7d-5cf38f0ad9ea"),
    "10%分装小瓶": ("BIOYOND_PolymerStation_Liquid_Vial", "3a14196c-76be-2279-4e22-7310d69aed68"),
}


@pytest.fixture
def bioyond_materials_reaction() -> list[dict]:
    with (FIXTURE_DIR / "bioyond_materials_reaction.json").open("r", encoding="utf-8") as f:
        data = json.load(f)
    return data


@pytest.fixture
def bioyond_materials_liquidhandling_1() -> list[dict]:
    with (FIXTURE_DIR / "bioyond_materials_liquidhandling_1.json").open("r", encoding="utf-8") as f:
        data = json.load(f)
    return data


@pytest.fixture
def bioyond_materials_liquidhandling_2() -> list[dict]:
    with (FIXTURE_DIR / "bioyond_materials_liquidhandling_2.json").open("r", encoding="utf-8") as f:
        data = json.load(f)
    return data


@pytest.mark.parametrize("materials_fixture", [
    "bioyond_materials_reaction",
    "bioyond_materials_liquidhandling_1",
])
def test_bioyond_to_plr(materials_fixture, request) -> list[dict]:
    materials = request.getfixturevalue(materials_fixture)
    deck = BIOYOND_PolymerReactionStation_Deck("test_deck")
    output = resource_bioyond_to_plr(materials, type_mapping=type_mapping, deck=deck)
    assert output
    assert all(isinstance(resource, ResourcePLR) for resource in output)
    assert all(resource.serialize()["name"] for resource in output)
