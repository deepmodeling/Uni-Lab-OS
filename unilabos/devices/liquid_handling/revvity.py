import time
import sys
import io
# sys.path.insert(0, r'C:\kui\winprep_cli\winprep_c_Uni-lab\x64\Debug')

try:
    import winprep_c
except ImportError as e:
    print("Error importing winprep_c:", e)
    print("Please ensure that the winprep_c module is correctly installed and accessible.")
from queue import Queue


class Revvity:
    _success: bool = False
    _status: str = "Idle"
    _status_queue: Queue = Queue()

    def __init__(self):
        self._status = "Idle"
        self._success = False
        self._status_queue = Queue()

    @property
    def success(self) -> bool:
        # print("success")
        return self._success

    @property
    def status(self) -> str:
        if not self._status_queue.empty():
            self._status = self._status_queue.get()
        return self._status

    def _run_script(self, file_path: str):
        output = io.StringIO()
        sys.stdout = output  # 重定向标准输出

        try:
            # 执行 winprep_c.test_mtp_script 并打印结果
            winprep_c.test_mtp_script(file_path)
        except Exception as e:
            # 捕获执行过程中的异常
            print(e)
            self._status_queue.put(f"Error: {str(e)}")

        finally:
            sys.stdout = sys.__stdout__  # 恢复标准输出

        # 获取捕获的输出并逐行更新状态
        for line in output.getvalue().splitlines():
            print(line)
            self._status_queue.put(line)
            self._status = line

    def run(self, file_path: str, params: str, resource: dict = {"AichemecoHiwo": {"id": "AichemecoHiwo"}}):
        # 设置状态为 Running
        self._status = "Running"
        winprep_c.test_mtp_script(file_path)

        # 在一个新的线程中运行 MTP 脚本，避免阻塞主线程
        # thread = threading.Thread(target=self._run_script, args=(file_path,))
        # thread.start()
        # self._run_script(file_path)
#
        # # 在主线程中持续访问状态
        # while thread.is_alive() or self._success == False:
        #     current_status = self.status()  # 获取当前的状态
        #     print(f"Current Status: {current_status}")
        #     time.sleep(0.5)

        # output = io.StringIO()
        # sys.stdout = output  # 重定向标准输出

        # try:
        #     # 执行 winprep_c.test_mtp_script 并打印结果
        #     winprep_c.test_mtp_script(file_path)
        # finally:
        #     sys.stdout = sys.__stdout__  # 恢复标准输出

        # # 获取捕获的输出并逐行更新状态
        # for line in output.getvalue().splitlines():
        #     self._status_queue.put(line)
        # self._success = True
        # 修改物料信息
        workstation = list(resource.values())[0]
        input_plate_wells = list(workstation["children"]["test-GL96-2A02"]["children"].values())
        output_plate_wells = list(workstation["children"]["HPLC_Plate"]["children"].values())

        for j in range(8):
            output_plate_wells[j]["data"]["liquid"] += input_plate_wells[j]["data"]["liquid"]
            output_plate_wells[j]["sample_id"] = input_plate_wells[j]["sample_id"]

        self._status = "Idle"
        self._success = True
