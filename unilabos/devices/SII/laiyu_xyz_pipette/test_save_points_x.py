# laiyu_xyz_pipette/test_save_points_x.py
import os
import json
import time

from laiyu_xyz_pipette.station_simple import Station


def main():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    points_path = os.path.join(base_dir, "points.json")
    origin_path = os.path.join(base_dir, "work_origin.json")

    print("Using points_file:", points_path)
    print("Using origin_file:", origin_path)

    # 初始化 Station（如端口不同，请修改 port）
    st = Station(
        port="/dev/ttyUSB0",
        baudrate=115200,
        points_file=points_path,
        origin_file=origin_path,
    )

    st.connect()

    try:
        # 打印一下当前 X 状态，看看能不能正常读到
        if st.xyz:
            print("\nCurrent X status:", st.xyz.get_status("X"))

        # 1) 移动到 x = 50，y/z 先用 0（你可以按需要改）
        print("\n=== Step 1: move to x = 50, save as x_test1 ===")
        st.move_xyz(x=50, y=0, z=0, speed=500, acc=6000)
        time.sleep(1.0)
        st.save_current_position_as_point("x_test1")

        # 2) 移动到 x = 100，保存为 x_test2
        print("\n=== Step 2: move to x = 100, save as x_test2 ===")
        st.move_xyz(x=100, y=0, z=0, speed=500, acc=6000)
        time.sleep(1.0)
        st.save_current_position_as_point("x_test2")

        # 再打印一次 X 状态
        if st.xyz:
            print("\nAfter moves, X status:", st.xyz.get_status("X"))

    finally:
        st.disconnect()

    # 3) 读取 points.json，对比 x_test1 / x_test2 的 x
    print("\n=== Step 3: load points.json and compare x ===")
    if not os.path.exists(points_path):
        print("points.json 不存在:", points_path)
        return

    with open(points_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    p1 = data.get("x_test1")
    p2 = data.get("x_test2")

    print("x_test1 point:", p1)
    print("x_test2 point:", p2)

    if p1 is None or p2 is None:
        print("x_test1 或 x_test2 未找到，请检查 save_current_position_as_point 是否被正常调用。")
        return

    x1 = float(p1["x"])
    x2 = float(p2["x"])

    print(f"\nSaved x values: x_test1.x = {x1}, x_test2.x = {x2}")
    if x1 == x2:
        print("WARNING: x_test1.x 和 x_test2.x 完全相同，说明 X 轴位置可能没有随实际运动变化，需要检查通信/控制逻辑。")
    else:
        print("OK: x_test1.x 和 x_test2.x 不同，说明 X 轴记录是随位置变化的。")


if __name__ == "__main__":
    main()