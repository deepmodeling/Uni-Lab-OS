import traceback
from unilabos.utils.log import logger

resource_schema = {
    "workstation": {"type": "object", "properties": {}},
    "work_station.aichemeco_hiwo": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "设备状态"},
            "tasks": {"type": "string", "description": "任务列表"},
        }
    },
    "work_station.revvity": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "设备状态"},
            "tasks": {"type": "string", "description": "任务列表"},
        }
    },
    "syringepump.runze": {
        "type": "object",
        "properties": {
            "max_velocity": {"type": "number", "description": "活塞最大速度"},
            "position": {"type": "number", "description": "活塞当前位置"},
            "status": {"type": "string", "description": "设备状态"},
            "valve_position": {"type": "string", "description": "阀门当前位置"},
        },
        "required": ["max_velocity", "position", "status", "valve_position"],
    },
    "heaterstirrer.dalong": {
        "type": "object",
        "properties": {
            "stir_speed": {"type": "number", "description": "搅拌器转速"},
            "temp": {"type": "number", "description": "搅拌器温度"},
            "temp_target": {"type": "number", "description": "搅拌器温度目标值"},
            "temp_warning": {"type": "number", "description": "搅拌器温度警告值"},
        },
        "required": ["temp"],
    },
    "separator_controller": {
        "type": "object",
        "properties": {
            "sensordata": {"type": "number", "description": "电导传感器数据"},
            "status": {"type": "string", "description": "设备状态"},
        },
        "required": ["sensordata", "status"],
    },
    "rotavap": {
        "type": "object",
        "properties": {
            "temperature": {"type": "number", "description": "蒸发器温度"},
            "rotate_time": {"type": "number", "description": "蒸发器转速"},
            "status": {"type": "string", "description": "设备状态"},
        },
        "required": ["temperature", "rotate_time", "status"],
    },
    "container": {
        "type": "object",
        "properties": {
            "liquid": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "liquid_type": {"type": "string"},
                        "liquid_volume": {"type": "number"},
                    },
                },
            },
            "max_volume": {
                "type": "number",
            },
        },
    },
    "plate": {
        "type": "object",
        "properties": {
            "layout": {
                "type": "object",
                "properties": {
                    "gridCount": "number",
                    "gridColumnNumber": "number"
                },
            }
        },
    },
    "serial": None,
    "gripper.mock": None,
    "solenoid_valve.mock": None,
    "vacuum_pump.mock": None,
    "gas_source.mock": None,
    "zhixing_agv": {
        "type": "object",
        "properties": {

            "status": {"type": "string", "description": "设备状态"},
        },
        "required": ["status"],
    },
    "zhixing_ur_arm": {
        "type": "object",
        "properties": {
            "arm_status": {"type": "string", "description": "机械臂设备状态"},
            "gripper_status": {"type": "string", "description": "夹爪设备状态"},
        },
        "required": ["arm_status"],
    },
    "hplc": {
        "type": "object",
        "properties": {
            "device_status": {"type": "string", "description": "机械臂设备状态"},
            "could_run": {"type": "bool", "description": "机械臂设备状态"},
            "driver_init_ok": {"type": "bool", "description": "机械臂设备状态"},
            "is_running": {"type": "bool", "description": "机械臂设备状态"},
            "finish_status": {"type": "string", "description": "机械臂设备状态"},
            "status_text": {"type": "string", "description": "机械臂设备状态"}
        }
    }
}


def add_schema(resources_config: list[dict]) -> list[dict]:
    for resource in resources_config:
        if "type" not in resource:
            resource["type"] = str(resource["class"])
        if resource["type"].lower() == "container":
            resource["schema"] = resource_schema["container"]
        elif resource["type"].lower() == "device":
            resource["schema"] = resource_schema.get(resource["class"], None)

        if len(resource["children"]) > 0:
            try:
                if type(resource["children"][0]) == dict:
                    resource["children"] = add_schema(resource["children"])
            except Exception as ex:
                logger.error("添加物料schema时出错")
                traceback.print_exc()

    return resources_config
