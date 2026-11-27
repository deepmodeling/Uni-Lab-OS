import networkx as nx


def generate_agv_transfer_protocol(
    G: nx.Graph, from_repo: dict, from_repo_position: str, to_repo: dict | None = None, to_repo_position: str = ""
):
    if not from_repo or not to_repo:
        raise ValueError("AGV transfer requires both source and destination repositories")
    if len(from_repo) != 1 or len(to_repo) != 1:
        raise ValueError("AGV 搬运源和目标必须各自唯一")
    from_repo_ = next(iter(from_repo.values()))
    to_repo_ = next(iter(to_repo.values()))
    if (
        not isinstance(from_repo_, dict)
        or not isinstance(to_repo_, dict)
        or not isinstance(from_repo_.get("children"), dict)
        or not isinstance(to_repo_.get("children", {}), dict)
    ):
        raise ValueError("AGV 搬运库位必须为对象")
    if not isinstance(from_repo_["children"].get(from_repo_position), dict):
        raise ValueError("AGV 源库位缺少可搬运物料")
    if from_repo_ is to_repo_:
        raise ValueError("AGV transfer source and destination must be different repositories")
    if from_repo_position not in from_repo_.get("children", {}):
        raise ValueError(f"Source position does not exist: {from_repo_position}")
    if to_repo_position in to_repo_.get("children", {}):
        raise ValueError(f"Destination position is occupied: {to_repo_position}")

    from_repo_id = from_repo_["id"]
    to_repo_id = to_repo_["id"]

    wf_list = {
        ("AiChemEcoHiWo", "zhixing_agv"): {
            "nav_command": '{"target" : "LM14"}',
            "arm_command": '{"task_name" : "camera/250111_biaozhi.urp"}',
        },
        ("AiChemEcoHiWo", "AGV"): {
            "nav_command": '{"target" : "LM14"}',
            "arm_command": '{"task_name" : "camera/250111_biaozhi.urp"}',
        },
        ("zhixing_agv", "Revvity"): {
            "nav_command": '{"target" : "LM13"}',
            "arm_command": '{"task_name" : "camera/250111_put_board.urp"}',
        },
        ("AGV", "Revvity"): {
            "nav_command": '{"target" : "LM13"}',
            "arm_command": '{"task_name" : "camera/250111_put_board.urp"}',
        },
        ("Revvity", "HPLC"): {
            "nav_command": '{"target" : "LM13"}',
            "arm_command": '{"task_name" : "camera/250111_hplc.urp"}',
        },
        ("HPLC", "Revvity"): {
            "nav_command": '{"target" : "LM13"}',
            "arm_command": '{"task_name" : "camera/250111_lfp.urp"}',
        },
    }
    transfer = wf_list.get((from_repo_id, to_repo_id))
    if transfer is None:
        raise ValueError(f"Unsupported AGV route: {from_repo_id} -> {to_repo_id}")

    # 路线和库位全部校验成功后，才更新编译期物料图。
    resource_to_move = from_repo_["children"].pop(from_repo_position)
    resource_to_move["parent"] = to_repo_["id"]
    to_repo_.setdefault("children", {})[to_repo_position] = resource_to_move

    return [
        {
            "device_id": "zhixing_agv",
            "action_name": "send_nav_task",
            "action_kwargs": {"command": transfer["nav_command"]},
        },
        {
            "device_id": "zhixing_ur_arm",
            "action_name": "move_pos_task",
            "action_kwargs": {"command": transfer["arm_command"]},
        },
    ]
