import os
import json
import time

from laiyu_xyz_pipette.station_simple import Station


def main():
    # 根据你的 device yaml 默认值，points_file / origin_file 都是相对路径
    # 这里用 __file__ 做一个相对路径到当前包目录的映射
    base_dir = os.path.dirname(os.path.abspath(__file__))
    points_path = os.path.join(base_dir, "points.json")
    origin_path = os.path.join(base_dir, "work_origin.json")

    print("Using points_file:", points_path)
    print("Using origin_file:", origin_path)

    # 初始化 Station
    st = Station(
        port="/dev/ttyUSB0",      # 如有不同串口自行修改
        baudrate=115200,
        points_file=points_path,
        origin_file=origin_path,
    )

    # 连接设备
    st.connect()

    try:
        # 可选：先确保已经有软零点；若不放心，可以先手动跑一次 define_current_as_zero
        # 这里不自动设零点，避免影响你现有标定

        # 1) 移动到 y = 50，x/z 先保持当前
        #    为了简单，这里把 x/z 都设为 0（工件坐标），你根据需求可以改
        print("\n=== Step 1: move to y = 50, save as test1 ===")
        st.move_xyz(x=0, y=50, z=0, speed=500, acc=6000)
        # 稍微等一下电机停止（保险起见）
        time.sleep(1.0)
        st.save_current_position_as_point("test1")

        # 2) 移动到 y = 100，保存为 test2
        print("\n=== Step 2: move to y = 100, save as test2 ===")
        st.move_xyz(x=0, y=100, z=0, speed=500, acc=6000)
        time.sleep(1.0)
        st.save_current_position_as_point("test2")

    finally:
        # 断开设备
        st.disconnect()

    # 3) 读取 points.json，打印 test1/test2 的 y 值
    print("\n=== Step 3: load points.json and compare y ===")
    if not os.path.exists(points_path):
        print("points.json 不存在:", points_path)
        return

    with open(points_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    p1 = data.get("test1")
    p2 = data.get("test2")

    print("test1 point:", p1)
    print("test2 point:", p2)

    if p1 is None or p2 is None:
        print("test1 或 test2 未找到，请检查 save_current_position_as_point 是否被正常调用。")
        return

    y1 = float(p1["y"])
    y2 = float(p2["y"])

    print(f"\nSaved y values: test1.y = {y1}, test2.y = {y2}")
    if y1 == y2:
        print("WARNING: test1.y 和 test2.y 完全相同，说明 y 可能没有随实际位置变化，需检查 save_current_position_as_point 逻辑。")
    else:
        print("OK: test1.y 和 test2.y 不同，说明 y 记录是随位置变化的。")


if __name__ == "__main__":
    main()