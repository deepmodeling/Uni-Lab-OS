# demo_points.py
import logging
import time

from station_simple import Station

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    st = Station(
        port="/dev/ttyUSB1",
        baudrate=115200,
        points_file="points.json",   # 如果不在同目录，改为实际路径
    )
    st.connect()

    try:
        # 移动到 C1 原始高度
        st.move_to_point("C1")

        time.sleep(1.0)

        # 在 C2 上方 10 mm
        st.move_to_point("C2", z_offset=+10.0)

        time.sleep(1.0)

        # 在 C3 点下插 2 mm
        st.move_to_point("C3", z_offset=-2.0)

    finally:
        st.disconnect()