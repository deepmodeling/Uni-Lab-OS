# demo_pipette_station.py
import logging
import time

from station_simple import Station


def wash_pipette(st: Station,
                 wash_volume_ul: float = 200.0,
                 cycles: int = 3,
                 delay_s: float = 0.5):
    """
    简单洗液：
    反复执行 “吸 wash_volume_ul → 等待 → 排 wash_volume_ul → 等待”
    通过 Station 的 aspirate/dispense 间接调用移液枪。
    """
    logging.info(f"开始洗液: 体积={wash_volume_ul} uL, 循环={cycles}")

    for i in range(1, cycles + 1):
        logging.info(f"洗液循环 {i}/{cycles} - 吸液 {wash_volume_ul} uL")
        st.aspirate(wash_volume_ul)
        time.sleep(delay_s)

        logging.info(f"洗液循环 {i}/{cycles} - 排液 {wash_volume_ul} uL")
        st.dispense(wash_volume_ul)
        time.sleep(delay_s)

    logging.info("洗液完成")


def purge_liquid(st: Station,
                 purge_volume_ul: float = 500.0,
                 delay_s: float = 0.5):
    """
    排空：向外排出一大段液体，尽量把枪头里的液体排尽。
    通过 Station 的 dispense 调用底层移液枪。
    """
    logging.info(f"开始排空: 体积={purge_volume_ul} uL")
    st.dispense(purge_volume_ul)
    time.sleep(delay_s)
    logging.info("排空完成")


def eject_tip(st: Station):
    """
    弹枪头：调用 Station 的 eject_tip 方法。
    """
    logging.info("准备弹出枪头")
    st.eject_tip()
    # Station 里已经打印日志，这里不再判断返回值


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # 1. 创建 Station（里面会自己 new SharedRS485Bus、XYZ 和 Pipette）
    st = Station(port="/dev/ttyUSB1", baudrate=115200)

    # 2. 连接 Station（打开总线，初始化 XYZ 和 移液枪）
    st.connect()

    try:
        # 3. 洗液（例如 200 uL 洗 3 次）
        wash_pipette(st, wash_volume_ul=200.0, cycles=3, delay_s=0.5)

        # 4. 排空（例如再排 500 uL，把残液尽量排出）
        purge_liquid(st, purge_volume_ul=500.0, delay_s=0.5)

        # 5. 弹枪头
        eject_tip(st)

    finally:
        # 6. 断开 Station（关闭总线）
        st.disconnect()