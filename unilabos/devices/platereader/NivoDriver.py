import time
import traceback
from typing import Optional

import requests
from pywinauto.application import WindowSpecification
from pywinauto.controls.uiawrapper import UIAWrapper
from pywinauto_recorder import UIApplication
from pywinauto_recorder.player import UIPath, click, focus_on_application, exists, find, FailedSearch

from unilabos.device_comms import universal_driver as ud
from unilabos.device_comms.universal_driver import UniversalDriver, SingleRunningExecutor
from unilabos.utils.pywinauto_util import connect_application, get_process_pid_by_name, \
    get_ui_path_with_window_specification


class NivoDriver(UniversalDriver):
    # 初始指定
    _device_ip: str = None
    # 软件状态检测
    _software_enabled: bool = False
    _software_pid: int = None
    _http_service_available: bool = False

    # 初始化状态
    _is_initialized: bool = False

    # 任务是否成功
    _success: bool = False

    # 运行状态
    _is_executing_run: bool = False
    _executing_ui_path: Optional[UIPath] = None
    _executing_index: Optional[int] = None
    _executing_status: Optional[str] = None
    _total_tasks: Optional[list[str]] = None
    _guide_app: Optional[UIApplication] = None

    @property
    def executing_status(self) -> str:
        if self._total_tasks is None:
            return f"无任务"
        if self._executing_index is None:
            return f"等待任务开始，总计{len(self._total_tasks)}个".encode('utf-8').decode('utf-8')
        else:
            return f"正在执行第{self._executing_index + 1}/{len(self._total_tasks)}个任务，当前状态：{self._executing_status}".encode('utf-8').decode('utf-8')

    @property
    def device_ip(self) -> str:
        return self._device_ip

    @property
    def success(self) -> bool:
        return self._success

    @property
    def status(self) -> str:
        return f"Software: {self._software_enabled}, HTTP: {self._http_service_available} Initialized: {self._is_initialized} Executing: {self._is_executing_run}"

    def set_device_addr(self, device_ip_str):
        self._device_ip = device_ip_str
        print(f"Set device IP to: {self.device_ip}")

    def run_instrument(self):
        if not self._is_initialized:
            print("Instrument is not initialized")
            self._success = False
            return False

        def post_func(res, _):
            self._success = res
            if not res:
                self._is_executing_run = False
        ins: SingleRunningExecutor = SingleRunningExecutor.get_instance(self.execute_run_instrument, post_func)
        if not ins.is_ended and ins.is_started:
            print("Function is running")
            self._success = False
            return False
        elif not ins.is_started:
            print("Function started")
            ins.start()  # 开始执行
        else:
            print("Function reset and started")
            ins.reset()
            ins.start()

    def execute_run_instrument(self):
        process_found, process_pid = get_process_pid_by_name("Guide.exe", min_memory_mb=20)
        if not process_found:
            uiapp = connect_application(process=self._software_pid)
            focus_on_application(uiapp)
            ui_window: WindowSpecification = uiapp.app.top_window()
            button: WindowSpecification = ui_window.child_window(title="xtpBarTop", class_name="XTPDockBar").child_window(
                title="Standard", class_name="XTPToolBar").child_window(title="Protocol", control_type="Button")
            click(button.wrapper_object())
            for _ in range(5):
                time.sleep(1)
                process_found, process_pid = get_process_pid_by_name("Guide.exe", min_memory_mb=20)
                if process_found:
                    break
        if not process_found:
            print("Guide.exe not found")
            self._success = False
            return False
        uiapp = connect_application(process=process_pid)
        self._guide_app = uiapp
        focus_on_application(uiapp)
        wrapper_object = uiapp.app.top_window().wrapper_object()
        ui_path = get_ui_path_with_window_specification(wrapper_object)
        self._executing_ui_path = ui_path
        with ui_path:
            try:
                click(u"||Custom->||RadioButton-> Run||Text-> Run||Text")
            except FailedSearch as e:
                print(f"未找到Run按钮，可能已经在执行了")
            with UIPath(u"WAA.Guide.Guide.RunControlViewModel||Custom->||Custom"):
                click(u"Start||Button")
            with UIPath(u"WAA.Guide.Guide.RunControlViewModel||Custom->||Custom->||Group->WAA.Guide.Guide.StartupControlViewModel||Custom->||Custom->Ok||Button"):
                while self._executing_index is None or self._executing_index == 0:
                    if exists(None, timeout=2):
                        click(u"Ok||Text")
                        print("Run Init Success!")
                        self._is_executing_run = True
                        return True
                    else:
                        print("Wait for Ok button")

    def check_execute_run_status(self):
        if not self._is_executing_run:
            return False
        if self._executing_ui_path is None:
            return False
        total_tasks = []
        executing_index = 0
        executing_status = ""
        procedure_name_found = False
        with self._executing_ui_path:
            with UIPath(u"WAA.Guide.Guide.RunControlViewModel||Custom->||Custom"):
                with UIPath("Progress||Group->||DataGrid"):
                    wrappered_object: UIAWrapper = find(timeout=0.5)  # BUG: 在查找的时候会触发全局锁，建议还是使用Process来检测
                    for custom_wrapper in wrappered_object.children():
                        if len(custom_wrapper.children()) == 1:
                            each_custom_wrapper = custom_wrapper.children()[0]
                            if len(each_custom_wrapper.children()) == 2:
                                if not procedure_name_found:
                                    procedure_name_found = True
                                    continue
                                task_wrapper = each_custom_wrapper.children()[0]
                                total_tasks.append(task_wrapper.window_text())
                                status_wrapper = each_custom_wrapper.children()[1]
                                status = status_wrapper.window_text()
                                if len(status) > 0:
                                    executing_index = len(total_tasks) - 1
                                    executing_status = status
        try:
            if self._guide_app is not None:
                wrapper_object = self._guide_app.app.top_window().wrapper_object()
                ui_path = get_ui_path_with_window_specification(wrapper_object)
                with ui_path:
                    with UIPath("OK||Button"):
                        btn = find(timeout=1)
                        if btn is not None:
                            btn.set_focus()
                            click(btn, timeout=1)
                            self._is_executing_run = False
                            print("运行完成！")
        except:
            pass
        self._executing_index = executing_index
        self._executing_status = executing_status
        self._total_tasks = total_tasks
        return True

    def initialize_instrument(self, force=False):
        if not self._software_enabled:
            print("Software is not opened")
            self._success = False
            return
        if not self._http_service_available:
            print("HTTP Server Not Available")
            self._success = False
            return
        if self._is_initialized and not force:
            print("Already Initialized")
            self._success = True
            return True
        ins: SingleRunningExecutor = SingleRunningExecutor.get_instance(
            self.execute_initialize, lambda res, _: setattr(self, '_success', res))
        if not ins.is_ended and ins.is_started:
            print("Initialize is running")
            self._success = False
            return False
        elif not ins.is_started:
            print("Initialize started")
            ins.start()
        else:  # 可能外面is_initialized被设置为False，又进来重新初始化了
            print("Initialize reset and started")
            ins.reset()
            ins.start()
        return True

    def execute_initialize(self, process=None) -> bool:
        if process is None:
            process = self._software_pid
        try:
            uiapp = connect_application(process=process)
            ui_window: WindowSpecification = uiapp.app.top_window()

            button = ui_window.child_window(title="xtpBarTop", class_name="XTPDockBar").child_window(
                title="Standard", class_name="XTPToolBar").child_window(title="Initialize Instrument", control_type="Button")
            focus_on_application(uiapp)
            click(button.wrapper_object())
            with get_ui_path_with_window_specification(ui_window):
                with UIPath("Regex: (Initializing|Resetting|Perking).*||Window"):
                    # 检测窗口是否存在
                    for i in range(3):
                        try:
                            initializing_windows = exists(None, timeout=2)
                            break
                        except:
                            pass
                    print("window has recovered", initializing_windows)
                    time.sleep(5)  # another wait
            self._is_initialized = True
            return True
        except Exception as e:
            print("An error occurred during initialization:")
            traceback.print_exc()
            return False

    def __init__(self):
        self._device_ip = "192.168.0.2"
        # 启动所有监控器
        self.checkers = [
            ProcessChecker(self, 1),
            HttpServiceChecker(self, 3),
            RunStatusChecker(self, 1),
            OkButtonChecker(self, 2)  # 添加新的Checker
        ]
        for checker in self.checkers:
            checker.start_monitoring()


class DriverChecker(ud.DriverChecker):
    driver: NivoDriver


class ProcessChecker(DriverChecker):
    def check(self):
        process_found, process_pid = get_process_pid_by_name("JANUS.exe", min_memory_mb=20)
        self.driver._software_pid = process_pid
        self.driver._software_enabled = process_found
        if not process_found:
            self.driver._is_initialized = False


class HttpServiceChecker(DriverChecker):
    def check(self):
        http_service_available = False
        if self.driver.device_ip:
            try:
                response = requests.get(f"http://{self.driver.device_ip}", timeout=5)
                http_service_available = response.status_code == 200
            except requests.RequestException:
                pass
        self.driver._http_service_available = http_service_available


class RunStatusChecker(DriverChecker):
    def check(self):
        process_found, process_pid = get_process_pid_by_name("Guide.exe", min_memory_mb=20)
        if not process_found:
            self.driver._is_executing_run = False
            return
        self.driver.check_execute_run_status()


class OkButtonChecker(DriverChecker):
    def check(self):
        if not self.driver._is_executing_run or self.driver._guide_app is None:
            return
        # uiapp = connect_application(process=11276)
        # self.driver._guide_app = uiapp
        try:
            ui_window: UIAWrapper = self.driver._guide_app.app.top_window()
            btn: WindowSpecification = ui_window.child_window(title="OK", auto_id="2", control_type="Button")
            if btn.exists(2):
                click(btn.wrapper_object())
                self.driver._is_executing_run = False
                print("运行完成！")
        except Exception as e:
            # traceback.print_exc()
            pass


# 示例用法
if __name__ == "__main__":
    driver = NivoDriver()
    driver.set_device_addr("192.168.0.2")  # 设置设备 IP 地址
    driver._is_executing_run = True
