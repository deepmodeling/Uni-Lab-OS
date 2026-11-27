import inspect
import os
from typing import get_type_hints

from pydantic import BaseModel, Field

# 定义你要解析的 pydantic 模型所在的 Python 文件
MODULES = ['my_pydantic_models']  # 替换为你的 Python 模块名

ROS_MSG_DIR = 'msg'  # 消息文件生成目录


def map_python_type_to_ros(python_type):
    type_map = {
        int: 'int32',
        float: 'float64',
        str: 'string',
        bool: 'bool',
        list: '[]',  # List in Pydantic should be handled separately
    }
    return type_map.get(python_type, None)


def generate_ros_msg_from_pydantic(model):
    fields = get_type_hints(model)
    ros_msg_lines = []

    for field_name, field_type in fields.items():
        ros_type = map_python_type_to_ros(field_type)
        if not ros_type:
            raise TypeError(f"Unsupported type {field_type} for field {field_name}")

        ros_msg_lines.append(f"{ros_type} {field_name}\n")

    return ''.join(ros_msg_lines)


def save_ros_msg_file(model_name, ros_msg_definition):
    msg_file_path = os.path.join(ROS_MSG_DIR, f'{model_name}.msg')
    os.makedirs(ROS_MSG_DIR, exist_ok=True)
    with open(msg_file_path, 'w') as msg_file:
        msg_file.write(ros_msg_definition)


def main():
    for module_name in MODULES:
        module = __import__(module_name)
        for name, obj in inspect.getmembers(module):
            if inspect.isclass(obj) and issubclass(obj, BaseModel) and obj != BaseModel:
                print(f"Generating ROS message for Pydantic model: {name}")
                ros_msg_definition = generate_ros_msg_from_pydantic(obj)
                save_ros_msg_file(name, ros_msg_definition)


if __name__ == '__main__':
    main()
