import networkx as nx


def generate_agv_transfer_protocol(
    G: nx.Graph,
    from_repo: dict,
    from_repo_position: str,
    to_repo: dict = {},
    to_repo_position: str = ""
):
    from_repo_ = list(from_repo.values())[0]
    to_repo_ = list(to_repo.values())[0]
    resource_to_move = from_repo_["children"].pop(from_repo_position)
    resource_to_move["parent"] = to_repo_["id"]
    to_repo_["children"][to_repo_position] = resource_to_move

    from_repo_id = from_repo_["id"]
    to_repo_id = to_repo_["id"]

    wf_list = {
        ("AiChemEcoHiWo", "zhixing_agv"): {"nav_command" : '{"target" : "LM14"}',
                             "arm_command": '{"task_name" : "camera/250111_biaozhi.urp"}'},
        ("AiChemEcoHiWo", "AGV"): {"nav_command" : '{"target" : "LM14"}',
                             "arm_command": '{"task_name" : "camera/250111_biaozhi.urp"}'},

        ("zhixing_agv", "Revvity"): {"nav_command" : '{"target" : "LM13"}',
                             "arm_command": '{"task_name" : "camera/250111_put_board.urp"}'},

        ("AGV", "Revvity"): {"nav_command" : '{"target" : "LM13"}',
                             "arm_command": '{"task_name" : "camera/250111_put_board.urp"}'},

        ("Revvity", "HPLC"): {"nav_command": '{"target" : "LM13"}',
                              "arm_command": '{"task_name" : "camera/250111_hplc.urp"}'},

        ("HPLC", "Revvity"): {"nav_command": '{"target" : "LM13"}',
                              "arm_command": '{"task_name" : "camera/250111_lfp.urp"}'},
    }
    return [
        {
            "device_id": "zhixing_agv",
            "action_name": "send_nav_task",
            "action_kwargs": {
                "command": wf_list[(from_repo_id, to_repo_id)]["nav_command"]
            }
        },
        {
            "device_id": "zhixing_ur_arm",
            "action_name": "move_pos_task",
            "action_kwargs": {
                "command": wf_list[(from_repo_id, to_repo_id)]["arm_command"]
            }
        }
    ]
