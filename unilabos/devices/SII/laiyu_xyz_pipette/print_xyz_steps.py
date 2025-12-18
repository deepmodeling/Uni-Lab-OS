from laiyu_xyz_pipette.XYZStepperController import XYZStepperController
from laiyu_xyz_pipette.drivers.SharedRS485Bus import SharedRS485Bus

if __name__ == "__main__":
    # 根据你实际串口和波特率修改
    bus = SharedRS485Bus(port="/dev/ttyUSB0", baudrate=115200)

    ctrl = XYZStepperController(
        bus,
        origin_path="unilabos/devices/laiyu_xyz_pipette/work_origin.json",
    )

    x_steps, *_ = ctrl.get_status("X")
    y_steps, *_ = ctrl.get_status("Y")
    z_steps, *_ = ctrl.get_status("Z")

    print(f"X steps: {x_steps}")
    print(f"Y steps: {y_steps}")
    print(f"Z steps: {z_steps}")