
import json
from typing import Sequence, Optional, List, Union, Literal
json_path = "/Users/guangxinzhang/Documents/Deep Potential/opentrons/convert/protocols/enriched_steps/sci-lucif-assay4.json"

with open(json_path, "r") as f:
    data = json.load(f)

transfer_example = data[0]
# print(transfer_example)


temp_protocol = []
TipLocation = "BC1025F"  # Assuming this is a fixed tip location for the transfer
sources = transfer_example["sources"]  # Assuming sources is a list of Container objects
targets = transfer_example["targets"]  # Assuming targets is a list of Container objects
tip_racks = transfer_example["tip_racks"]  # Assuming tip_racks is a list of TipRack objects
asp_vols = transfer_example["asp_vols"]  # Assuming asp_vols is a list of volumes
solvent = "PBS"


def transfer_liquid(
    # self,
    sources,  # : Sequence[Container],
    targets,  # : Sequence[Container],
    tip_racks,  # : Sequence[TipRack],
    TipLocation,
    # *,
    # use_channels: Optional[List[int]] = None,
    asp_vols: Union[List[float], float],
    solvent: Optional[str] = None,
    # dis_vols: Union[List[float], float],
    # asp_flow_rates: Optional[List[Optional[float]]] = None,
    # dis_flow_rates: Optional[List[Optional[float]]] = None,
    # offsets,#: Optional[List[]] = None,
    # touch_tip: bool = False,
    # liquid_height: Optional[List[Optional[float]]] = None,
    # blow_out_air_volume: Optional[List[Optional[float]]] = None,
    # spread: Literal["wide", "tight", "custom"] = "wide",
    # is_96_well: bool = False,
    # mix_stage: Optional[Literal["none", "before", "after", "both"]] = "none",
    # mix_times,#: Optional[list() = None,
    # mix_vol: Optional[int] = None,
    # mix_rate: Optional[int] = None,
    # mix_liquid_height: Optional[float] = None,
    # delays: Optional[List[int]] = None,
    # none_keys: List[str] = []
):
    # -------- Build Biomek transfer step --------
    # 1) Construct default parameter scaffold (values mirror Biomek “Transfer” block).

    transfer_params = {
        "Span8": False,
        "Pod": "Pod1",
        "items": {},                       # to be filled below
        "Wash": False,
        "Dynamic?": True,
        "AutoSelectActiveWashTechnique": False,
        "ActiveWashTechnique": "",
        "ChangeTipsBetweenDests": False,
        "ChangeTipsBetweenSources": False,
        "DefaultCaption": "",              # filled after we know first pair/vol
        "UseExpression": False,
        "LeaveTipsOn": False,
        "MandrelExpression": "",
        "Repeats": "1",
        "RepeatsByVolume": False,
        "Replicates": "1",
        "ShowTipHandlingDetails": False,
        "ShowTransferDetails": True,
        "Solvent": "Water",
        "Span8Wash": False,
        "Span8WashVolume": "2",
        "Span8WasteVolume": "1",
        "SplitVolume": False,
        "SplitVolumeCleaning": False,
        "Stop": "Destinations",
        "TipLocation": "BC1025F",
        "UseCurrentTips": False,
        "UseDisposableTips": True,
        "UseFixedTips": False,
        "UseJIT": True,
        "UseMandrelSelection": True,
        "UseProbes": [True, True, True, True, True, True, True, True],
        "WashCycles": "1",
        "WashVolume": "110%",
        "Wizard": False
    }

    items: dict = {}
    for idx, (src, dst) in enumerate(zip(sources, targets)):
        items[str(idx)] = {
            "Source": str(src),
            "Destination": str(dst),
            "Volume": asp_vols[idx]
        }
    transfer_params["items"] = items
    transfer_params["Solvent"] = solvent if solvent else "Water"
    transfer_params["TipLocation"] = TipLocation

    if len(tip_racks) == 1:
        transfer_params['UseCurrentTips'] = True
    elif len(tip_racks) > 1:
        transfer_params["ChangeTipsBetweenDests"] = True

    return transfer_params


action = transfer_liquid(sources=sources, targets=targets, tip_racks=tip_racks,
                         asp_vols=asp_vols, solvent=solvent, TipLocation=TipLocation)
print(json.dumps(action, indent=2))
# print(action)


"""
      "transfer": {

        "items": {},
        "Wash": false,
        "Dynamic?": true,
        "AutoSelectActiveWashTechnique": false,
        "ActiveWashTechnique": "",
        "ChangeTipsBetweenDests": true,
        "ChangeTipsBetweenSources": false,
        "DefaultCaption": "Transfer 100 µL from P13 to P3",
        "UseExpression": false,
        "LeaveTipsOn": false,
        "MandrelExpression": "",
        "Repeats": "1",
        "RepeatsByVolume": false,
        "Replicates": "1",
        "ShowTipHandlingDetails": true,
        "ShowTransferDetails": true,
        
        "Span8Wash": false,
        "Span8WashVolume": "2",
        "Span8WasteVolume": "1",
        "SplitVolume": false,
        "SplitVolumeCleaning": false,
        "Stop": "Destinations",
        "TipLocation": "BC1025F",
        "UseCurrentTips": false,
        "UseDisposableTips": false,
        "UseFixedTips": false,
        "UseJIT": true,
        "UseMandrelSelection": true,
        "UseProbes": [true, true, true, true, true, true, true, true],
        "WashCycles": "3",
        "WashVolume": "110%",
        "Wizard": false
"""
