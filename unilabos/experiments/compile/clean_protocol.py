import numpy as np
import networkx as nx


def generate_clean_protocol(
    G: nx.DiGraph, 
    vessel: str,  # Vessel to clean.
    solvent: str, # Solvent to clean vessel with.
    volume: float = 25.0, # Optional. Volume of solvent to clean vessel with.
    temp: float = 25, # Optional. Temperature to heat vessel to while cleaning.
    repeats: int = 1, # Optional. Number of cleaning cycles to perform.
) -> list[dict]:
    """
    Generate a protocol to clean a vessel with a solvent.
    
    :param G: Directed graph. Nodes are containers and pumps, edges are fluidic connections.
    :param vessel: Vessel to clean.
    :param solvent: Solvent to clean vessel with.
    :param volume: Volume of solvent to clean vessel with.
    :param temp: Temperature to heat vessel to while cleaning.
    :param repeats: Number of cleaning cycles to perform.
    :return: List of actions to clean vessel.
    """
    
    # 生成泵操作的动作序列
    pump_action_sequence = []
    from_vessel = f"flask_{solvent}"
    waste_vessel = f"waste_workup"
    
    transfer_flowrate = flowrate = 2.5
    
    # 生成泵操作的动作序列
    for i in range(repeats):
        # 单泵依次执行阀指令、活塞指令，将液体吸入与之相连的第一台泵
        pump_action_sequence.extend([
            {
                "device_id": "", 
                "action_name": "PumpTransferProtocol",
                "action_kwargs": {
                    "from_vessel": from_vessel,
                    "to_vessel": vessel,
                    "volume": volume,
                    "time": volume / flowrate,
                    # "transfer_flowrate": transfer_flowrate,
                }
            }
        ])
        
        pump_action_sequence.extend([
            {
                "device_id": "", 
                "action_name": "PumpTransferProtocol",
                "action_kwargs": {
                    "from_vessel": vessel,
                    "to_vessel": waste_vessel,
                    "volume": volume,
                    "time": volume / flowrate,
                    # "transfer_flowrate": transfer_flowrate,
                }
            }
        ])
    return pump_action_sequence
