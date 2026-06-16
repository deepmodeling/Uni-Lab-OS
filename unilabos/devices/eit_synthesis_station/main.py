# -*- coding: utf-8 -*-
from .manager.station_manager import SynthesisStationManager
from .config.setting import Settings
from .config.constants import TaskStatus, StationState
from pathlib import Path
import json
import logging
import traceback

logger = logging.getLogger("InteractiveCLI")

# ===================== 模块常量 =====================

ROOT = Path(__file__).resolve().parent
DEFAULT_TASK_TPL = ROOT / "sheet" / "reaction_template.xlsx"
DEFAULT_CHEM_DB = ROOT / "sheet" / "chemical_list.xlsx"
DEFAULT_TEMPLATE_IN = ROOT / "sheet" / "batch_in_tray.xlsx"

# 任务状态码 -> 中文名称
_TASK_STATUS_LABEL = {
    TaskStatus.UNSTARTED: "未开始",
    TaskStatus.RUNNING: "运行中",
    TaskStatus.COMPLETED: "已完成",
    TaskStatus.PAUSED: "已暂停",
    TaskStatus.FAILED: "失败",
    TaskStatus.STOPPED: "已停止",
    TaskStatus.PAUSING: "暂停中",
    TaskStatus.STOPPING: "停止中",
    TaskStatus.WAITING: "等待中",
    TaskStatus.HOLDING: "挂起",
}

# 工站状态码 -> 中文名称
_STATION_STATE_LABEL = {
    StationState.IDLE: "空闲/待机",
    StationState.RUNNING: "运行中",
    StationState.PAUSED: "已暂停",
    StationState.PAUSING: "暂停中",
    StationState.STOPPING: "停止中",
    StationState.HOLDING: "挂起/保持",
}

# 运行时缓存: 最近操作的任务ID
_last_task_id = None


# ===================== 工具函数 =====================

def _input_with_default(prompt, default=None):
    """
    功能:
        带默认值的输入提示, 用户直接回车即采用默认值
    参数:
        prompt: str, 提示文本
        default: 默认值, None 时不显示默认值
    返回:
        str, 用户输入或默认值的字符串形式
    """
    if default is not None:
        display = f"{prompt} [默认: {default}]: "
    else:
        display = f"{prompt}: "
    val = input(display).strip()
    if val == "" and default is not None:
        return str(default)
    return val


def _input_file_path(prompt, default_path):
    """
    功能:
        输入文件路径, 带默认值
    参数:
        prompt: str, 提示文本
        default_path: Path 或 str, 默认路径
    返回:
        str, 文件路径字符串
    """
    return _input_with_default(prompt, str(default_path))


def _input_task_id(allow_none=True):
    """
    功能:
        提示用户输入任务ID, 支持默认值和留空
    参数:
        allow_none: bool, 是否允许留空(由系统自动选取)
    返回:
        int | None, 任务ID或None
    """
    global _last_task_id
    hint = "请输入任务ID"
    if allow_none is True:
        hint += "(留空则自动选取)"
    val = _input_with_default(hint, _last_task_id)
    if val == "" or val == "None":
        return None
    try:
        tid = int(val)
        _last_task_id = tid
        return tid
    except ValueError:
        print("输入的任务ID格式不正确, 将自动选取")
        return None


def _input_positive_float(prompt):
    """
    功能:
        提示用户输入大于 0 的数值, 输入非法时循环重试.
    参数:
        prompt: str, 提示文本.
    返回:
        float, 用户输入的正数.
    """
    while True:
        val = input(f"{prompt}: ").strip()
        if val == "":
            print("输入不能为空")
            continue
        try:
            numeric_value = float(val)
        except ValueError:
            print("请输入数字")
            continue
        if numeric_value <= 0:
            print("请输入大于0的数值")
            continue
        return numeric_value


def _pause():
    """按回车键继续"""
    input("\n按回车键继续...")


def _safe_run(func, *args, **kwargs):
    """
    功能:
        安全执行函数, 捕获异常并友好提示
    参数:
        func: 可调用对象
        *args, **kwargs: 透传参数
    返回:
        func 的返回值, 异常时返回 None
    """
    try:
        result = func(*args, **kwargs)
        return result
    except KeyboardInterrupt:
        print("\n操作已被用户中断")
    except Exception as exc:
        logger.debug("异常详情:\n%s", traceback.format_exc())
        print(f"\n操作失败: {exc}")
    _pause()
    return None


def _print_menu(title, options):
    """
    功能:
        打印格式化菜单
    参数:
        title: str, 菜单标题
        options: list[tuple[str, str]], (编号, 描述) 列表
    返回:
        None
    """
    width = 48
    print("\n" + "=" * width)
    print(f"  {title}")
    print("=" * width)
    for num, desc in options:
        print(f"  {num}. {desc}")
    print("=" * width)


def _print_result(result):
    """
    功能:
        格式化打印 API 返回结果
    参数:
        result: 任意可 JSON 序列化的对象
    返回:
        None
    """
    if result is None:
        return
    try:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (TypeError, ValueError):
        print(result)


def _print_chemical_append_summary(result, *, source_label="查询"):
    """
    功能:
        以精简摘要输出化学品追加成功结果, 避免直接打印完整结果字典.
    参数:
        result: dict | None, 化学品追加结果.
        source_label: str, 成功来源标识, 例如 在线查询 或 SMILES 查询.
    返回:
        None
    """
    if isinstance(result, dict) is False:
        return

    row_data = result.get("row_data") or {}
    if isinstance(row_data, dict) is False:
        row_data = {}

    cas_number = str(row_data.get("cas_number") or "").strip()
    display_name = str(
        row_data.get("substance")
        or row_data.get("substance_chinese_name")
        or row_data.get("substance_english_name")
        or ""
    ).strip()
    row_index = result.get("row_index")
    substance = str(
        row_data.get("substance")
        or row_data.get("substance_chinese_name")
        or ""
    ).strip()
    physical_state = str(row_data.get("physical_state") or "").strip()
    physical_form = str(row_data.get("physical_form") or "").strip()

    print(
        f"已成功添加化合物: CAS={cas_number}, 名称={display_name}, "
        f"substance={substance}, physical_state={physical_state}, "
        f"physical_form={physical_form}, 行号={row_index}"
    )


def _print_prepared_chemical_summary(result):
    """
    功能:
        输出溶液或 beads 派生条目的精简摘要与配制结果.
    参数:
        result: dict | None, prepare_solution_or_beads 的返回结果.
    返回:
        None
    """
    if isinstance(result, dict) is False:
        return

    base_row_data = result.get("base_row_data") or {}
    derived_row_data = result.get("derived_row_data") or {}
    recipe = result.get("recipe") or {}

    base_name = str(
        base_row_data.get("base_substance")
        or base_row_data.get("substance")
        or base_row_data.get("substance_chinese_name")
        or base_row_data.get("substance_english_name")
        or ""
    ).strip()
    derived_name = str(
        derived_row_data.get("substance")
        or derived_row_data.get("substance_chinese_name")
        or derived_row_data.get("substance_english_name")
        or ""
    ).strip()
    base_created = bool(result.get("base_created"))
    derived_row_index = result.get("derived_row_index")
    instruction_text = str(recipe.get("instruction_text") or "").strip()

    base_source_text = "新补录母体" if base_created is True else "复用现有母体"
    print(f"母体条目: 名称={base_name}, 来源={base_source_text}")
    print(f"已成功添加派生条目: 名称={derived_name}, 行号={derived_row_index}")
    if instruction_text != "":
        print(f"配制结果: {instruction_text}")

    solute_volume_ml = recipe.get("solute_volume_ml")
    if solute_volume_ml is not None:
        print(f"液体母体估算体积: {solute_volume_ml:.6g} mL")


def _update_task_id(result):
    """
    功能:
        从 create_task 返回值提取 task_id 并缓存
    参数:
        result: dict, create_task_by_file 的返回值
    返回:
        result 原样返回
    """
    global _last_task_id
    if isinstance(result, dict):
        # 尝试从多种可能的返回结构中提取 task_id
        tid = result.get("task_id") or result.get("id")
        if tid is None:
            inner = result.get("result", {})
            if isinstance(inner, dict):
                tid = inner.get("task_id") or inner.get("id")
        if tid is not None:
            try:
                _last_task_id = int(tid)
                print(f"已记录任务ID: {_last_task_id}")
            except (ValueError, TypeError):
                pass
    return result


# ===================== 工作流执行器 =====================

def _run_workflow(steps, quick=False):
    """
    功能:
        按顺序执行工作流步骤, 每步可跳过或终止;
        quick=True 时跳过逐步确认, 直接连续执行
    参数:
        steps: list[tuple[str, callable]], (步骤名, 执行函数) 列表
        quick: bool, 是否跳过逐步确认直接执行
    返回:
        None
    """
    for i, (name, action) in enumerate(steps, 1):
        print(f"\n--- 步骤 {i}/{len(steps)}: {name} ---")
        if quick is False:
            confirm = input("继续执行? (y/n/q) [默认: y]: ").strip().lower()
            if confirm == "n":
                print(f"已跳过: {name}")
                continue
            if confirm == "q":
                print("工作流已终止")
                return
        result = _safe_run(action)
        if result is None:
            if quick is True:
                # 快速模式下步骤失败, 询问是否继续
                continue
            else:
                retry = input("该步骤可能未成功, 是否继续后续步骤? (y/n) [默认: n]: ").strip().lower()
            if retry != "y":
                print("工作流已终止")
                return
    print("\n工作流执行完毕!")
    _pause()


# ===================== 1. 快速工作流 =====================

def _menu_quick_workflow(manager):
    """快速工作流子菜单"""
    options = [
        ("1", "完整合成流程(AGV上料)"),
        ("2", "完整合成流程(手动上料)"),
        ("3", "上传任务流程"),
        ("4", "agv上料+提交合成任务+分析执行流程"),
        ("5", "等待任务完成+分析执行流程"),
        ("0", "返回上级菜单"),
    ]

    while True:
        _print_menu("快速工作流", options)
        choice = input("请选择操作: ").strip()

        if choice == "0":
            return

        # 收集所有工作流共用的文件路径
        if choice in ("1", "2", "3"):
            task_tpl = _input_file_path("任务模板路径", DEFAULT_TASK_TPL)
            chem_db = _input_file_path("化学品库路径", DEFAULT_CHEM_DB)

        if choice == "1":
            # 完整合成流程(AGV上料)
            steps = [
                ("对齐化学品库", lambda: manager.align_chemicals_with_file(chem_db)),
                ("上传任务到工站", lambda: _update_task_id(manager.create_task_by_file(task_tpl, chem_db))),
                ("物料核算", lambda: manager.check_resource_for_task(task_tpl, chem_db)),
                ("AGV上料", lambda: manager.batch_in_tray_with_agv_transfer()),
                ("启动任务", lambda: manager.start_task()),
                ("等待任务完成", lambda: manager.wait_task_with_ops()),
                ("下料(任务物料+空托盘)", lambda: manager.batch_out_task_and_empty_trays()),
                ("AGV自动下料", lambda: manager.auto_unload_trays_to_agv()),
                ("谱图数据处理", lambda: manager.poll_analysis_run()),
            ]
            _run_workflow(steps, quick=True)

        elif choice == "2":
            # 完整合成流程(手动上料)
            template_in = _input_file_path("上料文件路径", DEFAULT_TEMPLATE_IN)
            steps = [
                ("对齐化学品库", lambda: manager.align_chemicals_with_file(chem_db)),
                ("上传任务到工站", lambda: _update_task_id(manager.create_task_by_file(task_tpl, chem_db))),
                ("物料核算", lambda: manager.check_resource_for_task(task_tpl, chem_db)),
                ("手动上料", lambda: manager.batch_in_tray_by_file(template_in)),
                ("启动任务", lambda: manager.start_task()),
                ("等待任务完成", lambda: manager.wait_task_with_ops()),
                ("下料(任务物料+空托盘)", lambda: manager.batch_out_task_and_empty_trays()),
                ("AGV自动下料", lambda: manager.auto_unload_trays_to_agv()),
                ("提交分析任务", lambda: manager.run_analysis()),
                ("谱图数据处理", lambda: manager.poll_analysis_run()),
            ]
            _run_workflow(steps, quick=True)

        elif choice == "3":
            # 提交任务流程
            steps = [
                ("对齐化学品库", lambda: manager.align_chemicals_with_file(chem_db)),
                ("上传任务到工站", lambda: _update_task_id(manager.create_task_by_file(task_tpl, chem_db))),
                ("物料核算", lambda: manager.check_resource_for_task(task_tpl, chem_db)),
            ]
            _run_workflow(steps, quick=True)

        elif choice == "4":
            # AGV执行流程
            task_tpl = _input_file_path("任务模板路径", DEFAULT_TASK_TPL)
            chem_db = _input_file_path("化学品库路径", DEFAULT_CHEM_DB)
            steps = [
                ("AGV上料", lambda: manager.batch_in_tray_with_agv_transfer()),
                ("启动任务", lambda: manager.start_task()),
                ("等待任务完成", lambda: manager.wait_task_with_ops()),
                ("下料(任务物料+空托盘)", lambda: manager.batch_out_task_and_empty_trays()),
                ("AGV自动下料", lambda: manager.auto_unload_trays_to_agv()),
                ("谱图数据处理", lambda: manager.poll_analysis_run()),
            ]
            _run_workflow(steps, quick=True)

        elif choice == "5":
            # 执行流程
            steps = [
                ("等待任务完成", lambda: manager.wait_task_with_ops()),
                ("下料(任务物料+空托盘)", lambda: manager.batch_out_task_and_empty_trays()),
                ("AGV自动下料", lambda: manager.auto_unload_trays_to_agv()),
                ("谱图数据处理", lambda: manager.poll_analysis_run()),
            ]
            _run_workflow(steps, quick=True)

        else:
            print("无效选择, 请重新输入")


# ===================== 2. 全流程分步进行 =====================

def _menu_step_by_step(manager):
    """全流程分步进行子菜单"""
    options = [
        ("1", "对齐化学品库"),
        ("2", "上传任务到工站"),
        ("3", "物料核算"),
        ("4", "AGV上料"),
        ("5", "二次物料核算"),
        ("6", "启动任务"),
        ("7", "等待任务完成"),
        ("8", "下料(任务物料+空托盘)"),
        ("9", "AGV自动下料"),
        ("10", "提交分析任务"),
        ("11", "谱图数据处理"),
        ("0", "返回上级菜单"),
    ]

    while True:
        _print_menu("全流程分步进行", options)
        choice = input("请选择操作: ").strip()

        if choice == "0":
            return
        elif choice == "1":
            chem_db = _input_file_path("化学品库路径", DEFAULT_CHEM_DB)
            _safe_run(manager.align_chemicals_with_file, chem_db)
        elif choice == "2":
            task_tpl = _input_file_path("任务模板路径", DEFAULT_TASK_TPL)
            chem_db = _input_file_path("化学品库路径", DEFAULT_CHEM_DB)
            result = _safe_run(manager.create_task_by_file, task_tpl, chem_db)
            _update_task_id(result)
        elif choice == "3" or choice == "5":
            task_tpl = _input_file_path("任务模板路径", DEFAULT_TASK_TPL)
            chem_db = _input_file_path("化学品库路径", DEFAULT_CHEM_DB)
            _safe_run(manager.check_resource_for_task, task_tpl, chem_db)
        elif choice == "4":
            _safe_run(manager.batch_in_tray_with_agv_transfer)
        elif choice == "6":
            tid = _input_task_id()
            _safe_run(manager.start_task, tid)
        elif choice == "7":
            tid = _input_task_id()
            _safe_run(manager.wait_task_with_ops, tid)
        elif choice == "8":
            tid = _input_task_id()
            _safe_run(manager.batch_out_task_and_empty_trays, tid)
        elif choice == "9":
            _safe_run(manager.auto_unload_trays_to_agv)
        elif choice == "10":
            tid = _input_task_id()
            _safe_run(manager.run_analysis, tid)
        elif choice == "11":
            tid = _input_task_id()
            _safe_run(manager.poll_analysis_run, tid)
        else:
            print("无效选择, 请重新输入")


# ===================== 3. 工站状态查询 =====================

def _menu_status_query(manager):
    """工站状态查询子菜单"""
    options = [
        ("1", "查询站内物料信息"),
        ("2", "查询设备状态"),
        ("3", "查询工站运行状态"),
        ("4", "查询手套箱环境"),
        ("0", "返回上级菜单"),
    ]

    while True:
        _print_menu("工站状态查询", options)
        choice = input("请选择操作: ").strip()

        if choice == "0":
            return
        elif choice == "1":
            result = _safe_run(manager.get_resource_info)
            _print_result(result)
            _pause()
        elif choice == "2":
            result = _safe_run(manager.list_device_status)
            _print_result(result)
            _pause()
        elif choice == "3":
            result = _safe_run(manager.station_state)
            if result is not None:
                label = _STATION_STATE_LABEL.get(result, f"未知({result})")
                print(f"工站状态: {label} (代码: {result})")
            _pause()
        elif choice == "4":
            result = _safe_run(manager.get_glovebox_env)
            _print_result(result)
            _pause()
        else:
            print("无效选择, 请重新输入")


# ===================== 4. 化学品库管理 =====================

def _menu_chemical_library(manager):
    """化学品库管理子菜单"""
    options = [
        ("1", "导出化学品到CSV"),
        ("2", "从CSV导入化学品"),
        ("3", "化学品库去重"),
        ("4", "化学品库数据校验"),
        ("5", "在线查询并添加化学品"),
        ("6", "SMILES 在线查询并添加化学品"),
        ("7", "配置溶液或beads并添加到化学品库"),
        ("0", "返回上级菜单"),
    ]

    while True:
        _print_menu("化学品库管理", options)
        choice = input("请选择操作: ").strip()

        if choice == "0":
            return
        elif choice == "1":
            path = _input_with_default("导出文件路径", "chemicals_list_export.csv")
            _safe_run(manager.export_chemical_list_to_file, path)
        elif choice == "2":
            path = _input_with_default("导入CSV文件路径", "add_chemical_list.csv")
            _safe_run(manager.sync_chemicals_from_file, path)
        elif choice == "3":
            path = _input_file_path("化学品库文件路径", DEFAULT_CHEM_DB)
            _safe_run(manager.deduplicate_chemical_library_by_file, path)
        elif choice == "4":
            path = _input_file_path("化学品库文件路径", DEFAULT_CHEM_DB)
            result = _safe_run(manager.check_chemical_library_by_file, path)
            _print_result(result)
            _pause()
        elif choice == "5":
            query = input("请输入 CAS 号或化合物中英文名称: ").strip()
            if query:
                result = _safe_run(
                    manager.lookup_and_append_chemical, query, str(DEFAULT_CHEM_DB),
                )
                if result is None:
                    print("未查询到化合物信息或该化合物已存在, 请检查后重试")
                else:
                    _print_chemical_append_summary(result, source_label="在线查询")
            else:
                print("输入不能为空")
            _pause()
        elif choice == "6":
            smiles = input("请输入 SMILES 结构式: ").strip()
            if smiles:
                result = _safe_run(
                    manager.lookup_and_append_chemical_by_smiles, smiles, str(DEFAULT_CHEM_DB),
                )
                if result is None:
                    print("未查询到 SMILES 对应的化合物信息或该化合物已存在, 请检查后重试")
                else:
                    _print_chemical_append_summary(result, source_label="SMILES 查询")
            else:
                print("输入不能为空")
            _pause()
        elif choice == "7":
            identifier = input("请输入 CAS 或 SMILES: ").strip()
            if identifier == "":
                print("输入不能为空")
                _pause()
                continue

            prepared_form = input("请选择派生形态(solution/beads): ").strip().lower()
            if prepared_form not in {"solution", "beads"}:
                print("派生形态仅支持 solution 或 beads")
                _pause()
                continue

            if prepared_form == "solution":
                solvent_name = input("请输入溶剂名称: ").strip()
                concentration_mol_l = _input_positive_float("请输入目标浓度(mol/L)")
                target_volume_ml = _input_positive_float("请输入目标定容体积(mL)")
                result = _safe_run(
                    manager.prepare_solution_or_beads,
                    identifier,
                    prepared_form,
                    solvent_name=solvent_name,
                    active_content=concentration_mol_l,
                    target_volume_ml=target_volume_ml,
                    excel_path=str(DEFAULT_CHEM_DB),
                )
            else:
                wt_percent = _input_positive_float("请输入载量(wt%)")
                target_active_mmol = _input_positive_float("请输入目标活性(mmol)")
                result = _safe_run(
                    manager.prepare_solution_or_beads,
                    identifier,
                    prepared_form,
                    active_content=wt_percent,
                    target_active_mmol=target_active_mmol,
                    excel_path=str(DEFAULT_CHEM_DB),
                )

            if result is None:
                print("未完成派生条目添加, 请检查输入或确认该条目是否已存在")
            else:
                _print_prepared_chemical_summary(result)
            _pause()
        else:
            print("无效选择, 请重新输入")


# ===================== 5. 任务管理 =====================

def _show_all_tasks(manager):
    """
    功能:
        格式化显示所有任务列表, 并更新 _last_task_id 缓存
    参数:
        manager: SynthesisStationManager 实例
    返回:
        None
    """
    global _last_task_id
    resp = manager.get_all_tasks()
    # 兼容多种返回结构
    task_list = resp
    if isinstance(resp, dict):
        task_list = resp.get("task_list") or resp.get("result", {}).get("task_list", [])
    if not task_list:
        print("暂无任务")
        return

    print(f"\n{'任务ID':<10} {'任务名称':<30} {'状态':<10}")
    print("-" * 55)
    for task in task_list:
        tid = task.get("task_id", "?")
        name = task.get("task_name", "未知")
        status_code = task.get("status", -1)
        status_text = _TASK_STATUS_LABEL.get(status_code, f"未知({status_code})")
        print(f"{tid:<10} {name:<30} {status_text:<10}")

    print(f"\n共 {len(task_list)} 个任务")

    # 缓存最新的任务ID
    if task_list:
        latest = task_list[0].get("task_id")
        if latest is not None:
            _last_task_id = int(latest)


def _menu_task_management(manager):
    """任务管理子菜单"""
    options = [
        ("1", "查看所有任务"),
        ("2", "查看任务详情"),
        ("3", "停止任务"),
        ("4", "取消任务"),
        ("5", "删除任务"),
        ("6", "导出任务报告"),
        ("0", "返回上级菜单"),
    ]

    while True:
        _print_menu("任务管理", options)
        choice = input("请选择操作: ").strip()

        if choice == "0":
            return
        elif choice == "1":
            _safe_run(_show_all_tasks, manager)
        elif choice == "2":
            tid = _input_task_id(allow_none=False)
            if tid is not None:
                result = _safe_run(manager.get_task_info, tid)
                _print_result(result)
                _pause()
        elif choice == "3":
            tid = _input_task_id(allow_none=False)
            if tid is not None:
                _safe_run(manager.stop_task, tid)
        elif choice == "4":
            tid = _input_task_id(allow_none=False)
            if tid is not None:
                _safe_run(manager.cancel_task, tid)
        elif choice == "5":
            tid = _input_task_id(allow_none=False)
            if tid is not None:
                confirm = input(f"确认删除任务 {tid}? (y/n) [默认: n]: ").strip().lower()
                if confirm == "y":
                    _safe_run(manager.delete_task, tid)
                else:
                    print("已取消删除")
        elif choice == "6":
            tid = _input_task_id(allow_none=False)
            if tid is not None:
                _safe_run(manager.export_task_report, tid)
        else:
            print("无效选择, 请重新输入")


# ===================== 6. 上料操作 =====================

def _menu_load(manager):
    """上料操作子菜单"""
    options = [
        ("1", "手动上料"),
        ("2", "AGV上料"),
        ("0", "返回上级菜单"),
    ]

    while True:
        _print_menu("上料操作", options)
        choice = input("请选择操作: ").strip()

        if choice == "0":
            return
        elif choice == "1":
            template_in = _input_file_path("上料文件路径", DEFAULT_TEMPLATE_IN)
            _safe_run(manager.batch_in_tray_by_file, template_in)
        elif choice == "2":
            _safe_run(manager.batch_in_tray_with_agv_transfer)
        else:
            print("无效选择, 请重新输入")


# ===================== 7. 下料操作 =====================

def _menu_unload(manager):
    """下料操作子菜单"""
    options = [
        ("1", "下料(任务物料+空托盘)"),
        ("2", "下料(仅任务托盘)"),
        ("3", "下料(任务+化学品托盘)"),
        ("4", "下料(仅空托盘)"),
        ("5", "AGV自动下料"),
        ("6", "手动下料(指定layout)"),
        ("0", "返回上级菜单"),
    ]

    while True:
        _print_menu("下料操作", options)
        choice = input("请选择操作: ").strip()

        if choice == "0":
            return
        elif choice == "1":
            tid = _input_task_id()
            _safe_run(manager.batch_out_task_and_empty_trays, tid)
        elif choice == "2":
            tid = _input_task_id()
            _safe_run(manager.batch_out_task_trays, tid)
        elif choice == "3":
            tid = _input_task_id()
            _safe_run(manager.batch_out_task_and_chemical_trays, tid)
        elif choice == "4":
            _safe_run(manager.batch_out_empty_trays)
        elif choice == "5":
            _safe_run(manager.auto_unload_trays_to_agv)
        elif choice == "6":
            # 手动下料: 逐条输入 layout_code 和 dst_layout_code
            layout_list = []
            print("请逐条输入下料位置对(输入空行结束):")
            while True:
                src = input("  layout_code (源位置, 如 T-1-2): ").strip()
                if src == "":
                    break
                dst = input("  dst_layout_code (目标位置, 如 TB-2-2): ").strip()
                if dst == "":
                    break
                layout_list.append({"layout_code": src, "dst_layout_code": dst})
            if layout_list:
                print(f"将下料 {len(layout_list)} 个位置:")
                for item in layout_list:
                    print(f"  {item['layout_code']} -> {item['dst_layout_code']}")
                confirm = input("确认执行? (y/n) [默认: y]: ").strip().lower()
                if confirm != "n":
                    _safe_run(manager.batch_out_tray, layout_list)
            else:
                print("未输入任何下料位置, 已取消")
        else:
            print("无效选择, 请重新输入")


# ===================== 8. 分析操作 =====================

def _menu_analysis(manager):
    """分析操作子菜单"""
    options = [
        ("1", "提交分析任务"),
        ("2", "谱图数据处理"),
        ("0", "返回上级菜单"),
    ]

    while True:
        _print_menu("分析操作", options)
        choice = input("请选择操作: ").strip()

        if choice == "0":
            return
        elif choice == "1":
            tid = _input_task_id()
            _safe_run(manager.run_analysis, tid)
        elif choice == "2":
            tid = _input_task_id()
            _safe_run(manager.poll_analysis_run, tid)
        else:
            print("无效选择, 请重新输入")


# ===================== 9. 其它操作 =====================

def _menu_other(manager):
    """其它操作子菜单"""
    options = [
        ("1", "设备初始化"),
        ("2", "控制过渡舱门"),
        ("3", "控制W1货架"),
        ("4", "异常通知监控"),
        ("0", "返回上级菜单"),
    ]

    while True:
        _print_menu("其它操作", options)
        choice = input("请选择操作: ").strip()

        if choice == "0":
            return
        elif choice == "1":
            _safe_run(manager.device_init)
        elif choice == "2":
            print("  1. 打开过渡舱门")
            print("  2. 关闭过渡舱门")
            op_choice = input("请选择: ").strip()
            if op_choice == "1":
                _safe_run(manager.open_close_door, "open")
            elif op_choice == "2":
                _safe_run(manager.open_close_door, "close")
            else:
                print("无效选择")
        elif choice == "3":
            print("货架位置:")
            print("  1. W-1-1 (控制 W-1-1 和 W-1-2)")
            print("  2. W-1-3 (控制 W-1-3 和 W-1-4)")
            print("  3. W-1-5 (控制 W-1-5 和 W-1-6)")
            print("  4. W-1-7 (控制 W-1-7 和 W-1-8)")
            pos_choice = input("请选择货架位置: ").strip()
            pos_map = {"1": "W-1-1", "2": "W-1-3", "3": "W-1-5", "4": "W-1-7"}
            position = pos_map.get(pos_choice)
            if position is None:
                print("无效选择")
                continue

            print("动作类型:")
            print("  1. 推出 (outside)")
            print("  2. 复位 (home)")
            act_choice = input("请选择动作: ").strip()
            act_map = {"1": "outside", "2": "home"}
            action = act_map.get(act_choice)
            if action is None:
                print("无效选择")
                continue

            _safe_run(manager.control_w1_shelf, position, action)
        elif choice == "4":
            _menu_notification(manager)
        else:
            print("无效选择, 请重新输入")


# ===================== 10. 异常通知监控 =====================

def _menu_notification(manager):
    """异常通知监控子菜单"""
    import datetime as _dt

    options = [
        ("1", "启动通知监控"),
        ("2", "停止通知监控"),
        ("3", "查看监控状态"),
        ("0", "返回上级菜单"),
    ]

    while True:
        _print_menu("异常通知监控", options)
        choice = input("请选择操作: ").strip()

        if choice == "0":
            return
        elif choice == "1":
            _safe_run(manager.start_notification_monitor)
        elif choice == "2":
            _safe_run(manager.stop_notification_monitor)
        elif choice == "3":
            status = _safe_run(manager.notification_monitor_status)
            if status is not None:
                running_text = "运行中" if status.get("running") else "已停止"
                email_text = "可用" if status.get("email_available") else "未配置"
                last_poll = status.get("last_poll_time")
                if last_poll is not None:
                    last_poll_text = _dt.datetime.fromtimestamp(last_poll).strftime("%Y-%m-%d %H:%M:%S")
                else:
                    last_poll_text = "无"

                print(f"\n  监控状态: {running_text}")
                print(f"  邮件渠道: {email_text}")
                print(f"  已处理通知数: {status.get('total_processed', 0)}")
                print(f"  去重记录数: {status.get('processed_ids_count', 0)}")
                print(f"  上次轮询: {last_poll_text}")
            _pause()
        else:
            print("无效选择, 请重新输入")


# ===================== 10. 标签打印 =====================

def _menu_label_print(manager):
    """标签打印子菜单"""
    options = [
        ("1", "打印上料试剂标签"),
        ("2", "打印任务编号标签"),
        ("3", "交互式自由打印"),
        ("0", "返回上级菜单"),
    ]

    while True:
        _print_menu("标签打印", options)
        choice = input("请选择操作: ").strip()

        if choice == "0":
            return
        elif choice == "1":
            # 从 batch_in_tray.xlsx 提取试剂名并打印
            _safe_run(manager.print_reagent_labels)
        elif choice == "2":
            # 输入任务ID, 打印反应管+样品编号标签
            tid = _input_task_id(allow_none=False)
            if tid is not None:
                _safe_run(manager.print_task_number_labels, tid)
        elif choice == "3":
            # 交互式自由打印 (复用 print_text.py 的 main 函数)
            try:
                from .printer.print_text import main as _print_text_main
                _print_text_main()
            except Exception as exc:
                logger.error("交互式打印启动失败: %s", exc)
                print(f"启动失败: {exc}")
            _pause()
        else:
            print("无效选择, 请重新输入")


# ===================== 主入口 =====================

def interactive():
    """
    功能:
        启动交互式命令行菜单, 作为 main.py 的默认入口
    参数:
        无
    返回:
        无
    """
    settings = Settings.from_env()
    manager = SynthesisStationManager(settings)

    print("\n正在连接合成工站...")
    print(f"  地址: {settings.base_url}")
    print(f"  用户: {settings.username}")

    top_menu = [
        ("1", "快速工作流"),
        ("2", "全流程分步进行"),
        ("3", "工站状态查询"),
        ("4", "化学品库管理"),
        ("5", "任务管理"),
        ("6", "上料操作"),
        ("7", "下料操作"),
        ("8", "分析操作"),
        ("9", "其它操作"),
        ("10", "标签打印"),
        ("0", "退出"),
    ]

    dispatch = {
        "1": _menu_quick_workflow,
        "2": _menu_step_by_step,
        "3": _menu_status_query,
        "4": _menu_chemical_library,
        "5": _menu_task_management,
        "6": _menu_load,
        "7": _menu_unload,
        "8": _menu_analysis,
        "9": _menu_other,
        "10": _menu_label_print,
    }

    while True:
        _print_menu("EIT 合成工站 交互控制台", top_menu)
        choice = input("请选择操作: ").strip()

        if choice == "0" or choice.lower() == "q":
            break

        handler = dispatch.get(choice)
        if handler is None:
            print("无效选择, 请重新输入")
            continue

        handler(manager)


if __name__ == "__main__":
    interactive()
