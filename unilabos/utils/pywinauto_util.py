import psutil
import pywinauto
from pywinauto_recorder import UIApplication
from pywinauto_recorder.player import UIPath, click, focus_on_application, exists, find, get_wrapper_path
from pywinauto.controls.uiawrapper import UIAWrapper
from pywinauto.application import WindowSpecification
from pywinauto import findbestmatch
import sys
import codecs
import os
import locale


def connect_application(backend="uia", **kwargs):
    app = pywinauto.Application(backend=backend)
    app.connect(**kwargs)
    top_window = app.top_window().wrapper_object()
    native_window_handle = top_window.handle
    return UIApplication(app, native_window_handle)

def get_ui_path_with_window_specification(obj):
    return UIPath(get_wrapper_path(obj))

def get_process_pid_by_name(process_name: str, min_memory_mb: float = 0) -> tuple[bool, int]:
    """
    通过进程名称和最小内存要求获取进程PID

    Args:
        process_name: 进程名称
        min_memory_mb: 最小内存要求(MB)，默认为0表示不检查内存

    Returns:
        tuple[bool, int]: (是否找到进程, 进程PID)
    """
    process_found = False
    process_pid = None
    min_memory_bytes = min_memory_mb * 1024 * 1024  # 转换为字节

    try:
        for proc in psutil.process_iter(['name', 'pid', 'memory_info']):
            try:
                # 获取进程信息
                proc_info = proc.info
                if process_name in proc_info['name']:
                    # 如果设置了内存限制，则检查内存
                    if min_memory_mb > 0:
                        memory_info = proc_info.get('memory_info')
                        if memory_info and memory_info.rss > min_memory_bytes:
                            process_found = True
                            process_pid = proc_info['pid']
                            break
                    else:
                        # 不检查内存，直接返回找到的进程
                        process_found = True
                        process_pid = proc_info['pid']
                        break

            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue

    except Exception as e:
        print(f"获取进程信息时发生错误: {str(e)}")

    return process_found, process_pid

def print_wrapper_identifiers(wrapper_object, depth=None, filename=None):
    """
    打印控件及其子控件的标识信息

    Args:
        wrapper_object: UIAWrapper对象
        depth: 打印的最大深度,None表示打印全部
        filename: 输出文件名,None表示打印到控制台
    """
    if depth is None:
        depth = sys.maxsize

    # 创建所有控件的列表(当前控件及其所有子代)
    all_ctrls = [wrapper_object, ] + wrapper_object.descendants()

    # 创建所有可见文本控件的列表
    txt_ctrls = [ctrl for ctrl in all_ctrls if ctrl.can_be_label and ctrl.is_visible() and ctrl.window_text()]

    # 构建唯一的控件名称字典
    name_ctrl_id_map = findbestmatch.UniqueDict()
    for index, ctrl in enumerate(all_ctrls):
        ctrl_names = findbestmatch.get_control_names(ctrl, all_ctrls, txt_ctrls)
        for name in ctrl_names:
            name_ctrl_id_map[name] = index

    # 反转映射关系(控件索引到名称列表)
    ctrl_id_name_map = {}
    for name, index in name_ctrl_id_map.items():
        ctrl_id_name_map.setdefault(index, []).append(name)

    def print_identifiers(ctrls, current_depth=1, log_func=print):
        """递归打印控件及其子代的标识信息"""
        if len(ctrls) == 0 or current_depth > depth:
            return

        indent = (current_depth - 1) * u"   | "
        for ctrl in ctrls:
            try:
                ctrl_id = all_ctrls.index(ctrl)
            except ValueError:
                continue

            ctrl_text = ctrl.window_text()
            if ctrl_text:
                # 将多行文本转换为单行
                ctrl_text = ctrl_text.replace('\n', r'\n').replace('\r', r'\r')

            output = indent + u'\n'
            output += indent + u"{class_name} - '{text}'    {rect}\n"\
                "".format(class_name=ctrl.friendly_class_name(),
                          text=ctrl_text,
                          rect=ctrl.rectangle())
            output += indent + u'{}'.format(ctrl_id_name_map[ctrl_id])

            title = ctrl_text
            class_name = ctrl.class_name()
            auto_id = None
            control_type = None
            if hasattr(ctrl.element_info, 'automation_id'):
                auto_id = ctrl.element_info.automation_id
            if hasattr(ctrl.element_info, 'control_type'):
                control_type = ctrl.element_info.control_type
                if control_type:
                    class_name = None  # 如果有control_type就不需要class_name
                else:
                    control_type = None # 如果control_type为空,仍使用class_name

            criteria_texts = []
            recorder_texts = []
            if title:
                criteria_texts.append(u'title="{}"'.format(title))
                recorder_texts.append(f"{title}")
            if class_name:
                criteria_texts.append(u'class_name="{}"'.format(class_name))
            if auto_id:
                criteria_texts.append(u'auto_id="{}"'.format(auto_id))
            if control_type:
                criteria_texts.append(u'control_type="{}"'.format(control_type))
                recorder_texts.append(f"||{control_type}")
            if title or class_name or auto_id:
                output += u'\n' + indent + u'child_window(' + u', '.join(criteria_texts) + u')' + " / " + "".join(recorder_texts)

            log_func(output)
            print_identifiers(ctrl.children(), current_depth + 1, log_func)

    if filename is None:
        print("Control Identifiers:")
        print_identifiers([wrapper_object, ])
    else:
        log_file = codecs.open(filename, "w", locale.getpreferredencoding())
        def log_func(msg):
            log_file.write(str(msg) + os.linesep)
        log_func("Control Identifiers:")
        print_identifiers([wrapper_object, ], log_func=log_func)
        log_file.close()

