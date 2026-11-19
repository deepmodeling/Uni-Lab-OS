# import numpy as np
# import networkx as nx


# def generate_evacuateandrefill_protocol(
#     G: nx.DiGraph, 
#     vessel: str, 
#     gas: str, 
#     repeats: int = 1
# ) -> list[dict]:
#     """
#     生成泵操作的动作序列。
    
#     :param G: 有向图, 节点为容器和注射泵, 边为流体管道, A→B边的属性为管道接A端的阀门位置
#     :param from_vessel: 容器A
#     :param to_vessel: 容器B
#     :param volume: 转移的体积
#     :param flowrate: 最终注入容器B时的流速
#     :param transfer_flowrate: 泵骨架中转移流速（若不指定，默认与注入流速相同）
#     :return: 泵操作的动作序列
#     """
    
#     # 生成电磁阀、真空泵、气源操作的动作序列
#     vacuum_action_sequence = []
#     nodes = G.nodes(data=True)
    
#     # 找到和 vessel 相连的电磁阀和真空泵、气源
#     vacuum_backbone = {"vessel": vessel}
    
#     for neighbor in G.neighbors(vessel):
#         if nodes[neighbor]["class"].startswith("solenoid_valve"):
#             for neighbor2 in G.neighbors(neighbor):
#                 if neighbor2 == vessel:
#                     continue
#                 if nodes[neighbor2]["class"].startswith("vacuum_pump"):
#                     vacuum_backbone.update({"vacuum_valve": neighbor, "pump": neighbor2})
#                     break
#                 elif nodes[neighbor2]["class"].startswith("gas_source"):
#                     vacuum_backbone.update({"gas_valve": neighbor, "gas": neighbor2})
#                     break
#     # 判断是否设备齐全
#     if len(vacuum_backbone) < 5:
#         print(f"\n\n\n{vacuum_backbone}\n\n\n")
#         raise ValueError("Not all devices are connected to the vessel.")
    
#     # 生成操作的动作序列
#     for i in range(repeats):
#         # 打开真空泵阀门、关闭气源阀门
#         vacuum_action_sequence.append([
#             {
#                 "device_id": vacuum_backbone["vacuum_valve"],
#                 "action_name": "set_valve_position",
#                 "action_kwargs": {
#                     "command": "OPEN"
#                 }
#             },
#             {
#                 "device_id": vacuum_backbone["gas_valve"],
#                 "action_name": "set_valve_position",
#                 "action_kwargs": {
#                     "command": "CLOSED"
#                 }
#             }
#         ])
        
#         # 打开真空泵、关闭气源
#         vacuum_action_sequence.append([
#             {
#                 "device_id": vacuum_backbone["pump"],
#                 "action_name": "set_status",
#                 "action_kwargs": {
#                     "string": "ON"
#                 }
#             },
#             {
#                 "device_id": vacuum_backbone["gas"],
#                 "action_name": "set_status",
#                 "action_kwargs": {
#                     "string": "OFF"
#                 }
#             }
#         ])
#         vacuum_action_sequence.append({"action_name": "wait", "action_kwargs": {"time": 60}})
        
#         # 关闭真空泵阀门、打开气源阀门
#         vacuum_action_sequence.append([
#             {
#                 "device_id": vacuum_backbone["vacuum_valve"],
#                 "action_name": "set_valve_position",
#                 "action_kwargs": {
#                     "command": "CLOSED"
#                 }
#             },
#             {
#                 "device_id": vacuum_backbone["gas_valve"],
#                 "action_name": "set_valve_position",
#                 "action_kwargs": {
#                     "command": "OPEN"
#                 }
#             }
#         ])
        
#         # 关闭真空泵、打开气源
#         vacuum_action_sequence.append([
#             {
#                 "device_id": vacuum_backbone["pump"],
#                 "action_name": "set_status",
#                 "action_kwargs": {
#                     "string": "OFF"
#                 }
#             },
#             {
#                 "device_id": vacuum_backbone["gas"],
#                 "action_name": "set_status",
#                 "action_kwargs": {
#                     "string": "ON"
#                 }
#             }
#         ])
#         vacuum_action_sequence.append({"action_name": "wait", "action_kwargs": {"time": 60}})
        
#         # 关闭气源
#         vacuum_action_sequence.append(
#             {
#                 "device_id": vacuum_backbone["gas"],
#                 "action_name": "set_status",
#                 "action_kwargs": {
#                     "string": "OFF"
#                 }
#             }
#         )
        
#         # 关闭阀门
#         vacuum_action_sequence.append(
#             {
#                 "device_id": vacuum_backbone["gas_valve"],
#                 "action_name": "set_valve_position",
#                 "action_kwargs": {
#                     "command": "CLOSED"
#                 }
#             }
#         )
#     return vacuum_action_sequence
