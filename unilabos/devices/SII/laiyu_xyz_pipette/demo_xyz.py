# demo.py
import logging
import time

from .station_simple import Station

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    st = Station(port="/dev/ttyUSB0", baudrate=115200)
    st.connect()

    # 先移动到工件坐标 (x=100, y=100, z=0)
    st.move_xyz(100.0, 100.0, 0.0, speed=500, acc=1000)
    time.sleep(1.0)

    # 再回到工件原点 (x=0, y=0, z=0)
    st.move_xyz(0.0, 0.0, 0.0, speed=500, acc=1000)
    time.sleep(1.0)

    st.disconnect()