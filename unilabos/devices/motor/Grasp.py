import time
from serial import Serial
from threading import Thread

from unilabos.device_comms.universal_driver import UniversalDriver


class EleGripper(UniversalDriver):
    @property
    def status(self) -> str:
        return f"spin_pos: {self.rot_data[0]}, grasp_pos: {self.gri_data[0]}, spin_v: {self.rot_data[1]}, grasp_v: {self.gri_data[1]}, spin_F: {self.rot_data[2]}, grasp_F: {self.gri_data[2]}"

    def __init__(self, port, baudrate=115200, pos_error=-11, id=9):
        self.ser = Serial(port, baudrate)
        self.pos_error = pos_error

        self.success = False

        # [pos, speed, force]
        self.gri_data = [0, 0, 0]
        self.rot_data = [0, 0, 0]

        self.id = id

        self.init_gripper()
        self.wait_for_gripper_init()

        t = Thread(target=self.data_loop)
        t.start()

        self.gripper_move(0, 255, 255)
        self.rotate_move_abs(0, 255, 255)

    def modbus_crc(self, data: bytes) -> bytes:
        crc = 0xFFFF
        for pos in data:
            crc ^= pos
            for _ in range(8):
                if (crc & 0x0001) != 0:
                    crc >>= 1
                    crc ^= 0xA001
                else:
                    crc >>= 1
        return crc.to_bytes(2, byteorder='little')

    def send_cmd(self, id, fun, address, data: str):
        data_len = len(bytes.fromhex(data))
        data_ = f"{id:02X} {fun} {address} {data_len//2:04X} {data_len:02X} {data}"
        data_bytes = bytes.fromhex(data_)
        data_with_checksum = data_bytes + self.modbus_crc(data_bytes)
        self.ser.write(data_with_checksum)
        time.sleep(0.01)
        self.ser.read(8)

    def read_address(self, id, address, data_len):
        data = f"{id:02X} 03 {address} {data_len:04X}"
        data_bytes = bytes.fromhex(data)
        data_with_checksum = data_bytes + self.modbus_crc(data_bytes)
        self.ser.write(data_with_checksum)
        time.sleep(0.01)
        res_len = 5+data_len*2
        res = self.ser.read(res_len)
        return res

    def init_gripper(self):
        self.send_cmd(self.id, '10', '03 E8', '00 01')
        self.send_cmd(self.id, '10', '03 E9', '00 01')

    def gripper_move(self, pos, speed, force):
        self.send_cmd(self.id, '10', '03 EA', f"{speed:02x} {pos:02x} {force:02x} 01")

    def rotate_move_abs(self, pos, speed, force):
        pos += self.pos_error
        if pos < 0:
            pos = (1 << 16) + pos
        self.send_cmd(self.id, '10', '03 EC', f"{(pos):04x} {force:02x} {speed:02x} 0000 00 01")

    # def rotate_move_rel(self, pos, speed, force):
    #     if pos < 0:
    #         pos = (1 << 16) + pos
    #     print(f'{pos:04x}')
    #     self.send_cmd(self.id,'10', '03 EC', f"0000 {force:02x} {speed:02x} {pos:04x} 00 02")

    def wait_for_gripper_init(self):
        res = self.read_address(self.id, "07 D0", 1)
        res_r = self.read_address(self.id, "07 D1", 1)
        while res[4] == 0x08 or res_r[4] == 0x08:
            res = self.read_address(self.id, "07 D0", 1)
            res_r = self.read_address(self.id, "07 D1", 1)
            time.sleep(0.1)

    def wait_for_gripper(self):
        while self.gri_data[1] != 0:
            time.sleep(0.1)

    def wait_for_rotate(self):
        while self.rot_data[1] != 0:
            time.sleep(0.1)

    def data_reader(self):
        res_g = self.read_address(self.id, "07 D2", 2)
        res_r = self.read_address(self.id, "07 D4", 4)
        int32_value = (res_r[3] << 8) | res_r[4]
        if int32_value > 0x7FFF:
            int32_value -= 0x10000
        self.gri_data = (int(res_g[4]), int(res_g[3]), int(res_g[5]))
        self.rot_data = (int32_value, int(res_r[5]), int(res_r[6]))

    def data_loop(self):
        while True:
            self.data_reader()
            time.sleep(0.1)

    def node_gripper_move(self, cmd: str):
        self.success = False

        try:
            cmd_list = [int(x) for x in cmd.replace(' ', '').split(',')]
            self.gripper_move(*cmd_list)
            self.wait_for_gripper()
        except Exception as e:
            raise e

        self.success = True

    def node_rotate_move(self, cmd: str):
        self.success = False

        try:
            cmd_list = [int(x) for x in cmd.replace(' ', '').split(',')]
            self.rotate_move_abs(*cmd_list)
            self.wait_for_rotate()
        except Exception as e:
            raise e

        self.success = True

    def move_and_rotate(self, spin_pos, grasp_pos, spin_v, grasp_v, spin_F, grasp_F):
        self.gripper_move(grasp_pos, grasp_v, grasp_F)
        self.wait_for_gripper()
        self.rotate_move_abs(spin_pos, spin_v, spin_F)
        self.wait_for_rotate()


if __name__ == "__main__":
    gripper = EleGripper("COM12")
    gripper.init_gripper()
    gripper.wait_for_gripper_init()
    gripper.gripper_move(210, 127, 255)
    gripper.wait_for_gripper()
    gripper.rotate_move_abs(135, 10, 255)
    gripper.data_reader()
    print(gripper.rot_data)
