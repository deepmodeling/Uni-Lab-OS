import traceback
from datetime import datetime
import os
import re
from typing import TypedDict

import pyautogui
from pywinauto import Application
from pywinauto.application import WindowSpecification
from pywinauto.controls.uiawrapper import UIAWrapper
from pywinauto.uia_element_info import UIAElementInfo

from unilabos.app.oss_upload import oss_upload
from unilabos.device_comms import universal_driver as ud
from unilabos.device_comms.universal_driver import UniversalDriver


class DeviceStatusInfo(TypedDict):
    name: str
    name_obj: UIAWrapper
    status: str
    status_obj: UIAWrapper
    open_btn: UIAWrapper
    close_btn: UIAWrapper
    sub_item: UIAWrapper

class DeviceStatus(TypedDict):
    VWD: DeviceStatusInfo
    JinYangQi: DeviceStatusInfo
    Beng: DeviceStatusInfo
    ShouJiQi: DeviceStatusInfo


class HPLCDriver(UniversalDriver):
    # 设备状态
    _device_status: DeviceStatus = None
    _is_running: bool = False
    _success: bool = False
    _finished: int = None
    _total_sample_number: int = None
    _status_text: str = ""
    # 外部传入
    _wf_name: str = ""
    # 暂时用不到，用来支持action name
    gantt: str = ""
    status: str = ""

    @property
    def status_text(self) -> str:
        return self._status_text

    @property
    def device_status(self) -> str:
        return f", ".join([f"{k}:{v.get('status')}" for k, v in self._device_status.items()])
    
    @property
    def could_run(self) -> bool:
        return self.driver_init_ok and all([v.get('status') == "空闲" for v in self._device_status.values()])
    
    @property
    def driver_init_ok(self) -> bool:
        for k, v in self._device_status.items():
            if v.get("open_btn") is None:
                return False
            if v.get("close_btn") is None:
                return False
        return len(self._device_status) == 4
    
    @property
    def is_running(self) -> bool:
        return self._is_running
    
    @property
    def success(self) -> bool:
        return self._success
    
    @property
    def finish_status(self) -> str:
        return f"{self._finished}/{self._total_sample_number}"
    
    def try_open_sub_device(self, device_name: str = None):
        if not self.driver_init_ok:
            self._success = False
            print(f"仪器还没有初始化完成，无法查询设备：{device_name}")
            return
        if device_name is None:
            for k, v in self._device_status.items():
                self.try_open_sub_device(k)
            return
        target_device_status = self._device_status[device_name]
        if target_device_status["status"] == "未就绪":
            print(f"尝试打开{device_name}设备")
            target_device_status["open_btn"].click()
        else:
            print(f"{device_name}设备状态不支持打开：{target_device_status['status']}")

    def try_close_sub_device(self, device_name: str = None):
        if not self.driver_init_ok:
            self._success = False
            print(f"仪器还没有初始化完成，无法查询设备：{device_name}")
            return
        if device_name is None:
            for k, v in self._device_status.items():
                self.try_close_sub_device(k)
            return
        target_device_status = self._device_status[device_name]
        if target_device_status["status"] == "空闲":
            print(f"尝试关闭{device_name}设备")
            target_device_status["close_btn"].click()
        else:
            print(f"{device_name}设备状态不支持关闭：{target_device_status['status']}")

    def _get_resource_sample_id(self, wf_name, idx):
        try:
            root = list(self.resource_info[wf_name].values())[0]
            # print(root)
            plates = root["children"]
            plate_01 = list(plates.values())[0]
            pots = list(plate_01["children"].values())
            return pots[idx]['sample_id']
        except Exception as ex:
            traceback.print_exc()

    def start_sequence(self, wf_name: str, params: str = None, resource: dict = None):
        print("!!!!!! 任务启动")
        self.resource_info[wf_name] = resource
        # 后续workflow_name将同步一下
        if self.is_running:
            print("设备正在运行，无法启动序列")
            self._success = False
            return False
        if not self.driver_init_ok:
            print(f"仪器还没有初始化完成，无法启动序列")
            self._success = False
            return False
        if not self.could_run:
            print(f"仪器不处于空闲状态，无法运行")
            self._success = False
            return False
        # 参考：
        # with UIPath(u"PREP-LC (联机): 方法和运行控制 ||Window"):
        # with UIPath(u"panelNavTabChem||Pane->||Pane->panelControlChemStation||Pane->||Tab->仪器控制||Pane->||Pane->panelChemStation||Pane->PREP-LC (联机): 方法和运行控制 ||Pane->ViewMGR||Pane->MRC view||Pane->||Pane->||Pane->||Pane->||Custom->||Custom"):
        # 	click(u"||Button#[0,1]")
        app = Application(backend='uia').connect(title=u"PREP-LC (联机): 方法和运行控制 ")
        window = app['PREP-LC (联机): 方法和运行控制']
        window.allow_magic_lookup = False
        panel_nav_tab = window.child_window(title="panelNavTabChem", auto_id="panelNavTabChem", control_type="Pane")
        first_pane = panel_nav_tab.child_window(auto_id="uctlNavTabChem1", control_type="Pane")
        panel_control_station = first_pane.child_window(title="panelControlChemStation", auto_id="panelControlChemStation", control_type="Pane")
        instrument_control_tab: WindowSpecification = panel_control_station.\
            child_window(auto_id="tabControlChem", control_type="Tab").\
            child_window(title="仪器控制", auto_id="tabPage1", control_type="Pane").\
            child_window(auto_id="uctrlChemStation", control_type="Pane").\
            child_window(title="panelChemStation", auto_id="panelChemStation", control_type="Pane").\
            child_window(title="PREP-LC (联机): 方法和运行控制 ", control_type="Pane").\
            child_window(title="ViewMGR", control_type="Pane").\
            child_window(title="MRC view", control_type="Pane").\
            child_window(auto_id="mainMrcControlHost", control_type="Pane").\
            child_window(control_type="Pane", found_index=0).\
            child_window(control_type="Pane", found_index=0).\
            child_window(control_type="Custom", found_index=0).\
            child_window(control_type="Custom", found_index=0)
        instrument_control_tab.dump_tree(3)
        btn: UIAWrapper = instrument_control_tab.child_window(auto_id="methodButtonStartSequence", control_type="Button").wrapper_object()
        self.start_time = datetime.now()
        btn.click()
        self._wf_name = wf_name
        self._success = True
        return True

    def check_status(self):
        app = Application(backend='uia').connect(title=u"PREP-LC (联机): 方法和运行控制 ")
        window = app['PREP-LC (联机): 方法和运行控制']
        ui_window = window.child_window(title="靠顶部", control_type="Group").\
            child_window(title="状态", control_type="ToolBar").\
            child_window(title="项目", control_type="Button", found_index=0)
        # 检测pixel的颜色
        element_info: UIAElementInfo = ui_window.element_info
        rectangle = element_info.rectangle
        point_x = int(rectangle.left + rectangle.width() * 0.15)
        point_y = int(rectangle.top + rectangle.height() * 0.15)
        r, g, b = pyautogui.pixel(point_x, point_y)
        if 270 > r > 250 and 200 > g > 180 and b < 10:  # 是黄色
            self._is_running = False
            self._status_text = "Not Ready"
        elif r > 110 and g > 190 and 50 < b < 60:
            self._is_running = False
            self._status_text = "Ready"
        elif 75 > r > 65 and 135 > g > 120 and 240 > b > 230:
            self._is_running = True
            self._status_text = "Running"
        else:
            print(point_x, point_y, "未知的状态", r, g, b)

    def extract_data_from_txt(self, file_path):
        # 打开文件
        print(file_path)
        with open(file_path, mode='r', encoding='utf-16') as f:
            lines = f.readlines()
        # 定义一个标志变量来判断是否已经找到“馏分列表”
        started = False
        data = []

        for line in lines:
            # 查找“馏分列表”，并开始提取后续行
            if line.startswith("-----|-----|-----"):
                started = True
                continue  # 跳过当前行
            if started:
                # 遇到"==="表示结束读取
                if '=' * 80 in line:
                    break
                # 使用正则表达式提取馏分、孔、位置和原因
                res = re.split(r'\s+', line.strip())
                if res:
                    fraction, hole, position, reason = res[0], res[1], res[2], res[-1]
                    data.append({
                        '馏分': fraction,
                        '孔': hole,
                        '位置': position,
                        '原因': reason.strip()
                    })

        return data

    def get_data_file(self, mat_index: str = None, after_time: datetime = None) -> tuple[str, str]:
        """
        获取数据文件
        after_time: 由于HPLC是启动后生成一个带时间的目录，所以会选取after_time后的文件
        """
        if mat_index is None:
            print(f"mat_index不能为空")
            return None
        if after_time is None:
            after_time = self.start_time
        files = [i for i in os.listdir(self.data_file_path) if i.startswith(self.using_method)]
        time_to_files: list[tuple[datetime, str]] = [(datetime.strptime(i.split(" ", 1)[1], "%Y-%m-%d %H-%M-%S"), i) for i in files]
        time_to_files.sort(key=lambda x: x[0])
        choose_folder = None
        for i in time_to_files:
            if i[0] > after_time:
                print(i[0], after_time)
                print(f"选取时间{datetime.strftime(after_time, '%Y-%m-%d %H-%M-%S')}之后的文件夹{i[1]}")
                choose_folder = i[1]
                break
        if choose_folder is None:
            print(f"没有找到{self.using_method} {datetime.strftime(after_time, '%Y-%m-%d %H-%M-%S')}之后的文件夹")
            return None
        current_data_path = os.path.join(self.data_file_path, choose_folder)

        # 需要匹配 数字数字数字-.* 001-P2-E1-DQJ-4-70.D
        target_row = [i for i in os.listdir(current_data_path) if re.match(r"\d{3}-.*", i)]
        index2filepath = {int(k.split("-")[0]): os.path.join(current_data_path, k) for k in target_row}
        print(f"查找文件{mat_index}")
        if int(mat_index) not in index2filepath:
            print(f"没有找到{mat_index}的文件 已找到：{index2filepath}")
            return None
        mat_final_path = index2filepath[int(mat_index)]
        pdf = os.path.join(mat_final_path, "Report.PDF")
        txt = os.path.join(mat_final_path, "Report.TXT")
        fractions = self.extract_data_from_txt(txt)
        print(fractions)
        return pdf, txt

    def __init__(self, driver_debug=False):
        super().__init__()
        self.data_file_path = r"D:\ChemStation\1\Data"
        self.using_method = f"1106-dqj-4-64"
        self.start_time = datetime.now()
        self._device_status = dict()
        self.resource_info: dict[str, dict] = dict()
        # 启动所有监控器
        self.checkers = [
            InstrumentChecker(self, 1),
            RunningChecker(self, 1),
            RunningResultChecker(self, 1),
        ]
        if not driver_debug:
            for checker in self.checkers:
                checker.start_monitoring()


class DriverChecker(ud.DriverChecker):
    driver: HPLCDriver

class InstrumentChecker(DriverChecker):
    _instrument_control_tab = None
    _instrument_control_tab_wrapper = None
    def get_instrument_status(self):
        if self._instrument_control_tab is not None:
            return self._instrument_control_tab
        # 连接到目标窗口
        app = Application(backend='uia').connect(title=u"PREP-LC (联机): 方法和运行控制 ")
        window = app['PREP-LC (联机): 方法和运行控制']
        window.allow_magic_lookup = False
        panel_nav_tab = window.child_window(title="panelNavTabChem", auto_id="panelNavTabChem", control_type="Pane")
        first_pane = panel_nav_tab.child_window(auto_id="uctlNavTabChem1", control_type="Pane")
        panel_control_station = first_pane.child_window(title="panelControlChemStation", auto_id="panelControlChemStation", control_type="Pane")
        instrument_control_tab: WindowSpecification = panel_control_station.\
            child_window(auto_id="tabControlChem", control_type="Tab").\
            child_window(title="仪器控制", auto_id="tabPage1", control_type="Pane").\
            child_window(auto_id="uctrlChemStation", control_type="Pane").\
            child_window(title="panelChemStation", auto_id="panelChemStation", control_type="Pane").\
            child_window(title="PREP-LC (联机): 方法和运行控制 ", control_type="Pane").\
            child_window(title="ViewMGR", control_type="Pane").\
            child_window(title="MRC view", control_type="Pane").\
            child_window(auto_id="mainMrcControlHost", control_type="Pane").\
            child_window(control_type="Pane", found_index=0).\
            child_window(control_type="Pane", found_index=0).\
            child_window(control_type="Custom", found_index=0).\
            child_window(best_match="Custom6").\
            child_window(auto_id="ListBox_DashboardPanel", control_type="List")
        if self._instrument_control_tab is None:
            self._instrument_control_tab = instrument_control_tab
            self._instrument_control_tab_wrapper = instrument_control_tab.wrapper_object()
        return self._instrument_control_tab

    
    def check(self):
        self.get_instrument_status()
        if self._instrument_control_tab_wrapper is None or self._instrument_control_tab is None:
            return
        item: UIAWrapper
        index = 0
        keys = list(self.driver._device_status.keys())
        for item in self._instrument_control_tab_wrapper.children():
            info: UIAElementInfo = item.element_info
            if info.control_type == "ListItem" and item.window_text() == "Agilent.RapidControl.StatusDashboard.PluginViewModel":
                sub_item: WindowSpecification = self._instrument_control_tab.\
                    child_window(title="Agilent.RapidControl.StatusDashboard.PluginViewModel", control_type="ListItem", found_index=index).\
                    child_window(control_type="Custom", found_index=0)
                if index < len(keys):
                    deviceStatusInfo = self.driver._device_status[keys[index]]  
                    name = deviceStatusInfo["name"]
                    deviceStatusInfo["status"] = deviceStatusInfo["status_obj"].window_text()
                    print(name, index, deviceStatusInfo["status"], "刷新")
                    if deviceStatusInfo["open_btn"] is not None and deviceStatusInfo["close_btn"] is not None:
                        index += 1
                        continue
                else:
                    name_obj = sub_item.child_window(control_type="Text", found_index=0).wrapper_object()
                    name = name_obj.window_text()
                    self.driver._device_status[name] = dict()
                    self.driver._device_status[name]["name_obj"] = name_obj
                    self.driver._device_status[name]["name"] = name
                    print(name, index)
                    status = sub_item.child_window(control_type="Custom", found_index=0).\
                        child_window(auto_id="TextBlock_StateLabel", control_type="Text")
                    status_obj: UIAWrapper = status.wrapper_object()
                    self.driver._device_status[name]["status_obj"] = status_obj
                    self.driver._device_status[name]["status"] = status_obj.window_text()
                    print(status.window_text())
                sub_item = sub_item.wrapper_object()
                found_index = 0
                open_btn = None
                close_btn = None
                for btn in sub_item.children():
                    if btn.element_info.control_type == "Button":
                        found_index += 1
                        if found_index == 5:
                            open_btn = btn
                        elif found_index == 6:
                            close_btn = btn
                self.driver._device_status[name]["open_btn"] = open_btn
                self.driver._device_status[name]["close_btn"] = close_btn
                index += 1

class RunningChecker(DriverChecker):
    def check(self):
        self.driver.check_status()

class RunningResultChecker(DriverChecker):
    _finished: UIAWrapper = None
    _total_sample_number: UIAWrapper = None

    def check(self):
        if self._finished is None or self._total_sample_number is None:
            app = Application(backend='uia').connect(title=u"PREP-LC (联机): 方法和运行控制 ")
            window = app['PREP-LC (联机): 方法和运行控制']
            window.allow_magic_lookup = False
            panel_nav_tab = window.child_window(title="panelNavTabChem", auto_id="panelNavTabChem", control_type="Pane")
            first_pane = panel_nav_tab.child_window(auto_id="uctlNavTabChem1", control_type="Pane")
            panel_control_station = first_pane.child_window(title="panelControlChemStation", auto_id="panelControlChemStation", control_type="Pane")
            instrument_control_tab: WindowSpecification = panel_control_station.\
                child_window(auto_id="tabControlChem", control_type="Tab").\
                child_window(title="仪器控制", auto_id="tabPage1", control_type="Pane").\
                child_window(auto_id="uctrlChemStation", control_type="Pane").\
                child_window(title="panelChemStation", auto_id="panelChemStation", control_type="Pane").\
                child_window(title="PREP-LC (联机): 方法和运行控制 ", control_type="Pane").\
                child_window(title="ViewMGR", control_type="Pane").\
                child_window(title="MRC view", control_type="Pane").\
                child_window(auto_id="mainMrcControlHost", control_type="Pane").\
                child_window(control_type="Pane", found_index=0).\
                child_window(control_type="Pane", found_index=0).\
                child_window(control_type="Custom", found_index=0).\
                child_window(auto_id="mainControlExpanderSampleInformation", control_type="Group").\
                child_window(auto_id="controlsSampleInfo", control_type="Custom")
            self._finished = instrument_control_tab.child_window(best_match="Static15").wrapper_object()
            self._total_sample_number = instrument_control_tab.child_window(best_match="Static16").wrapper_object()
        try:
            temp = int(self._finished.window_text())
            if self.driver._finished is None or temp > self.driver._finished:
                if self.driver._finished is None:
                    self.driver._finished = 0
                for i in range(self.driver._finished, temp):
                    sample_id = self.driver._get_resource_sample_id(self.driver._wf_name, i)  # 从0开始计数
                    pdf, txt = self.driver.get_data_file(i + 1)
                    # 使用新的OSS上传接口，传入driver_name和exp_type
                    pdf_result = oss_upload(pdf, filename=os.path.basename(pdf), driver_name="HPLC", exp_type="analysis")
                    txt_result = oss_upload(txt, filename=os.path.basename(txt), driver_name="HPLC", exp_type="result")
                    
                    if pdf_result["success"]:
                        print(f"PDF上传成功: {pdf_result['oss_path']}")
                    else:
                        print(f"PDF上传失败: {pdf_result['original_path']}")
                    
                    if txt_result["success"]:
                        print(f"TXT上传成功: {txt_result['oss_path']}")
                    else:
                        print(f"TXT上传失败: {txt_result['original_path']}")
                    # self.driver.extract_data_from_txt()
        except Exception as ex:
            self.driver._finished = 0

            print("转换数字出错", ex)
        try:
            self.driver._total_sample_number = int(self._total_sample_number.window_text())
        except Exception as ex:
            self.driver._total_sample_number = 0
            print("转换数字出错", ex)




# 示例用法
if __name__ == "__main__":
    # obj = HPLCDriver.__new__(HPLCDriver)
    # obj.start_sequence()

    # obj = HPLCDriver.__new__(HPLCDriver)
    # obj.data_file_path = r"D:\ChemStation\1\Data"
    # obj.using_method = r"1106-dqj-4-64"
    # obj.get_data_file("001", after_time=datetime(2024, 11, 6, 19, 3, 6))

    obj = HPLCDriver.__new__(HPLCDriver)
    obj.data_file_path = r"D:\ChemStation\1\Data"
    obj.using_method = r"1106-dqj-4-64"
    obj._wf_name = "test"
    obj.resource_info = {
        "test": {
            "1": {
                "children": {
                    "11": {
                        "children": {
                            "111": {
                                "sample_id": "sample-1"
                            },
                            "112": {
                                "sample_id": "sample-2"
                            }
                        }
                    }
                }
            }
        }
    }
    sample_id = obj._get_resource_sample_id("test", 0)
    pdf, txt = obj.get_data_file("1", after_time=datetime(2024, 11, 6, 19, 3, 6))
    # 使用新的OSS上传接口，传入driver_name和exp_type
    pdf_result = oss_upload(pdf, filename=os.path.basename(pdf), driver_name="HPLC", exp_type="analysis")
    txt_result = oss_upload(txt, filename=os.path.basename(txt), driver_name="HPLC", exp_type="result")
    
    print(f"PDF上传结果: {pdf_result}")
    print(f"TXT上传结果: {txt_result}")
    # driver = HPLCDriver()
    # for i in range(10000):
    #     print({k: v for k, v in driver._device_status.items() if isinstance(v, str)})
    #     print(driver.device_status)
    #     print(driver.could_run)
    #     print(driver.driver_init_ok)
    #     print(driver.is_running)
    #     print(driver.finish_status)
    #     print(driver.status_text)
    #     time.sleep(5)
