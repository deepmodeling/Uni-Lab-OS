from __future__ import annotations

from copy import deepcopy
from typing import Any


STANDARD_OPERATIONS: dict[str, dict[str, Any]] = {
    "Add": {"template": "AddProtocol", "inputs": {"vessel": "Vessel"}, "outputs": {"vessel": "VesselOut"}},
    "AddSolid": {"template": "AddProtocol", "inputs": {"vessel": "Vessel"}, "outputs": {"vessel": "VesselOut"}},
    "Transfer": {
        "template": "TransferProtocol",
        "inputs": {"from_vessel": "FromVessel", "to_vessel": "ToVessel"},
        "outputs": {"from_vessel": "FromVesselOut", "to_vessel": "ToVesselOut"},
    },
    "Stir": {"template": "StirProtocol", "inputs": {"vessel": "Vessel"}, "outputs": {"vessel": "VesselOut"}},
    "EvacuateAndRefill": {"template": "EvacuateAndRefillProtocol", "inputs": {"vessel": "Vessel"}, "outputs": {"vessel": "VesselOut"}},
    "HeatChill": {"template": "HeatChillProtocol", "inputs": {"vessel": "Vessel"}, "outputs": {"vessel": "VesselOut"}},
    "HeatChillToTemp": {"template": "HeatChillProtocol", "inputs": {"vessel": "Vessel"}, "outputs": {"vessel": "VesselOut"}},
    "Separate": {
        "template": "SeparateProtocol",
        "inputs": {"from_vessel": "FromVessel", "to_vessel": "ToVessel"},
        "outputs": {"from_vessel": "FromVesselOut", "to_vessel": "ToVesselOut"},
    },
    "FilterThrough": {
        "template": "FilterThroughProtocol",
        "inputs": {"from_vessel": "FromVessel", "to_vessel": "ToVessel"},
        "outputs": {"from_vessel": "FromVesselOut", "to_vessel": "ToVesselOut"},
        "parameter_aliases": {"through": "filter_through"},
    },
    "Evaporate": {"template": "EvaporateProtocol", "inputs": {"vessel": "Vessel"}, "outputs": {"vessel": "VesselOut"}},
    "Filter": {
        "template": "FilterProtocol",
        "inputs": {"vessel": "Vessel", "filtrate_vessel": "FiltrateVessel"},
        "outputs": {"vessel": "VesselOut", "filtrate_vessel": "FiltrateOut"},
    },
    "WashSolid": {
        "template": "WashSolidProtocol",
        "inputs": {"vessel": "Vessel", "filtrate_vessel": "filtrate_vessel"},
        "outputs": {"vessel": "VesselOut", "filtrate_vessel": "filtrate_vessel_out"},
    },
    "Recrystallize": {
        "template": "RecrystallizeProtocol",
        "inputs": {"vessel": "Vessel"},
        "outputs": {"vessel": "VesselOut"},
        "parameter_aliases": {"solvent": "solvent1", "solvent_volume": "volume"},
        "defaults": {"ratio": "1:0", "solvent2": "ethanol"},
    },
    "Dry": {"template": "DryProtocol", "inputs": {"vessel": "Vessel"}, "outputs": {"vessel": "VesselOut"}},
    "Distill": {
        "template": "EvaporateProtocol",
        "inputs": {"vessel": "Vessel"},
        "outputs": {"vessel": "VesselOut"},
        "parameter_aliases": {"vapour_temp": "temp"},
    },
    "RunColumn": {
        "template": "RunColumnProtocol",
        "inputs": {"from_vessel": "FromVessel", "to_vessel": "ToVessel"},
        "outputs": {"from_vessel": "FromVesselOut", "to_vessel": "ToVesselOut"},
        "parameter_aliases": {"eluting_solvent": "solvent1"},
    },
}


def standard_operations() -> dict[str, dict[str, Any]]:
    return deepcopy(STANDARD_OPERATIONS)
