# -*- coding: utf-8 -*-
import csv
import re
import logging
import pandas as pd
import openpyxl
from openpyxl import Workbook,load_workbook
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.workbook.defined_name import DefinedName
from openpyxl.styles import Font, Alignment, NamedStyle 
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 引入底层的控制器 
from ..controller.station_controller import SynthesisStationController
from ..config.setting import Settings, configure_logging
from ..config.constants import ResourceCode, TRAY_CODE_DISPLAY_NAME, TraySpec
from .synchronizer import EITSynthesisWorkstation

from ..driver.exceptions import ValidationError,ApiError

logger = logging.getLogger("StationManager")

JsonDict = Dict[str, Any]

class SynthesisStationManager(EITSynthesisWorkstation, SynthesisStationController):
    """
    功能:
        上层面向用户的管理器，继承自 SynthesisStationController。
        负责处理 CSV/Excel 文件读取、生成模板，将文件内容转换为中间格式(List/Dict)，
        然后调用父类方法执行具体的业务逻辑。
    """

    def __init__(
        self,
        settings: Optional[Settings] = None,
        config: Optional[Dict[str, Any]] = None,
        deck: Optional[Any] = None,
        **kwargs,
    ):
        settings = settings or Settings.from_env()
        configure_logging(settings.log_level)
        SynthesisStationController.__init__(self, settings)
        EITSynthesisWorkstation.__init__(
            self,
            config=config,
            deck=deck,
            controller=self,
            **kwargs,
        )

    # ---------- 1. 化合物库文件处理 ----------
    def export_chemical_list_to_file(self, output_path: str) -> None:
        """
        功能:
            获取所有化学品并导出到 CSV 文件
        参数:
            output_path: 输出路径
        返回:
            None
        """
        path = Path(output_path)
        chemical_info = self.get_all_chemical_list()
        chemical_list = chemical_info.get("chemical_list", [])

        if not chemical_list:
            logger.warning("化学品列表为空，未写入文件")
            return

        fieldnames = [
            "fid", "name", "sssi", "cas", "element", "state",
            "concentration_str", "chemical_properties", "preparation_method"
        ]
        
        # 确保目录存在
        path.parent.mkdir(parents=True, exist_ok=True)

        with path.open("w", newline="", encoding="utf-8-sig") as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames, extrasaction='ignore')
            writer.writeheader()
            for item in chemical_list:
                writer.writerow(item)
        
        logger.info(f"化学品列表已导出至: {path.resolve()}")

    def sync_chemicals_from_file(self, file_path: str, overwrite: bool = False) -> None:
        """
        功能:
            读取 CSV 文件并通过父类同步化学品到工站
        参数:
            file_path: CSV 文件路径
            overwrite: 是否覆盖更新
        返回:
            None
        """
        path = Path(file_path)
        if not path.exists():
            # 生成模板
            header = ["name", "cas", "element", "state", "concentration_str", "chemical_properties", "preparation_method"]
            with path.open("w", newline="", encoding="utf-8-sig") as f:
                csv.writer(f).writerow(header)
            logger.warning(f"文件不存在，已生成模板: {path}")
            return

        # 读取并清洗数据
        items: List[JsonDict] = []
        with path.open("r", newline="", encoding="utf-8-sig") as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                name = (row.get("name") or "").strip()
                state = (row.get("state") or "").strip()
                if name and state:
                    # 过滤空值键
                    clean_item = {k: v.strip() for k, v in row.items() if v and str(v).strip()}
                    items.append(clean_item)
        
        # 调用父类逻辑处理
        self.sync_chemicals_from_data(items, overwrite=overwrite)

    def check_chemical_library_by_file(self, file_path: str) -> Dict[str, List[str]]:
        """
        功能:
            读取化学品库文件并调用底层校验逻辑，输出校验结果
        参数:
            file_path: str, 化学品库文件路径，支持 Excel/CSV
        返回:
            Dict[str, List[str]], 包含 errors 与 warnings
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"未找到化学品库文件: {path}")

        # 读取文件后交给控制层做校验
        df = pd.read_excel(path) if path.suffix.lower() in [".xlsx", ".xls"] else pd.read_csv(path)
        df = df.fillna("")
        rows = df.to_dict(orient="records")
        headers = [str(col).strip() for col in df.columns]

        result = self.check_chemical_library_data(rows, headers)

        for msg in result.get("warnings", []):
            logger.warning(msg)

        if len(result.get("errors", [])) > 0:
            for msg in result["errors"]:
                logger.error(msg)
            raise ValidationError("化学品库完整性检查未通过，请修复错误后重试")

        return result
    
    def deduplicate_chemical_library_by_file(self, file_path: str, output_path: Optional[str] = None) -> List[JsonDict]:
        """
        功能:
            读取化学品库文件，按 substance 自动去重并回写
        参数:
            file_path: str, 输入文件路径，支持 Excel/CSV
            output_path: Optional[str], 输出文件路径，默认覆盖原文件
        返回:
            List[Dict[str, Any]], 去重后的数据
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"未找到化学品库文件: {path}")

        df = pd.read_excel(path) if path.suffix.lower() in [".xlsx", ".xls"] else pd.read_csv(path)
        df = df.fillna("")
        headers = [str(c).strip() for c in df.columns]
        rows = df.to_dict(orient="records")

        dedup_rows = self.deduplicate_chemical_library_data(rows, headers)

        target_path = Path(output_path) if output_path else path
        out_df = pd.DataFrame(dedup_rows)
        if target_path.suffix.lower() == ".csv":
            out_df.to_csv(target_path, index=False, encoding="utf-8-sig")
        else:
            out_df.to_excel(target_path, index=False)
            self._beautify_excel_database(target_path)  # 保存后再美化

        logger.info("化合物库去重完成，输出文件: %s", target_path.resolve())
        return dedup_rows
    
    def _beautify_excel_database(self, file_path: Path) -> None:
        """
        功能:
            美化去重后的 Excel: 表头加粗、全居中、列宽自适应、按内容选择中英文字体
        参数:
            file_path: Path, 目标 Excel 路径
        返回:
            None
        """
        wb = load_workbook(file_path)
        ws = wb.active
        MAX_WIDTH = 60  # 列宽上限

        align_center = Alignment(horizontal="center", vertical="center")

        def _is_chinese(text: str) -> bool:
            return re.search(r"[\u4e00-\u9fff]", text) is not None

        # 遍历列计算列宽并设置字体/对齐
        for col_cells in ws.iter_cols():
            max_len = 0
            for idx, cell in enumerate(col_cells):
                val_str = "" if cell.value is None else str(cell.value)
                max_len = max(max_len, len(val_str))

                # 按内容切换字体，表头加粗
                if idx == 0:
                    cell.font = Font(name="微软雅黑", bold=True)
                else:
                    cell.font = Font(name="微软雅黑")

                cell.alignment = align_center

            # 列宽留一点边距，最小 10，最大 40
            col_width = max(10, max_len + 2)
            col_width = min(col_width, MAX_WIDTH)
            ws.column_dimensions[col_cells[0].column_letter].width = col_width

        wb.save(file_path)

    def align_chemicals_with_file(self, file_path: str, auto_delete: bool = True) -> None:
        """
        功能:
            读取 Excel/CSV 文件，调用父类对齐逻辑，并将结果(fid)写回文件
        参数:
            file_path: 文件路径
            auto_delete: 是否删除不在文件中的工站化学品
        返回:
            None
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"未找到化学品对齐文件: {path}")

        # 读取文件内容为 List[Dict]
        df = pd.read_excel(path) if path.suffix in ['.xlsx', '.xls'] else pd.read_csv(path)
        # 将 NaN 替换为空字符串
        df = df.fillna("")
        rows = df.to_dict(orient='records')
        header = df.columns.tolist()

        # 调用父类进行对齐，父类会修改 rows 中的数据(如回填 chemical_id)
        updated_rows = self.align_chemicals_from_data(rows, auto_delete=auto_delete)

        # 写回文件
        new_df = pd.DataFrame(updated_rows)
        # 保持原有列顺序，如果增加了新列(如 chemical_id 之前没有)，这会包含它
        if path.suffix == '.csv':
            new_df.to_csv(path, index=False, encoding="utf-8-sig")
        else:
            new_df.to_excel(path, index=False)
            self._beautify_excel_database(path)  # 保存后再美化
        
        logger.info(f"化学品对齐完成并回写文件: {path}")

    # ----------- 2. 上料动作 -------------
    def batch_in_tray_by_file(self, file_path: str) -> JsonDict:
        """
        功能:
            读取上料表格，转换为中间格式，调用父类生成 Payload 并执行上料
        参数:
            file_path: 文件路径
        返回:
            Dict: API 响应
        """
        path = Path(file_path)
        if not path.exists():
            logger.warning(f"未找到{file_path}.自动生成模板文件")
            self._generate_batch_in_tray_template(path.with_suffix(".xlsx"))
            return {}

        rows: List[Tuple[str, str, str]] = []
        
        # 读取文件
        if path.suffix == '.xlsx':
            wb = openpyxl.load_workbook(path)
            ws = wb.active
            for row in ws.iter_rows(min_row=2, values_only=True):
                # 确保取前三列，且处理 None
                pos = str(row[0]) if row[0] is not None else ""
                t_type = str(row[1]) if len(row) > 1 and row[1] is not None else ""
                content = str(row[2]) if len(row) > 2 and row[2] is not None else ""
                rows.append((pos, t_type, content))
        else:
            df = pd.read_csv(path)
            df = df.fillna("")
            for _, row in df.iterrows():
                rows.append((str(row[0]), str(row[1]), str(row[2])))

        # 调用父类生成 Payload
        payload = self.build_batch_in_tray_payload(rows)

        if not payload:
            logger.warning("生成的上料数据为空")
            return {}

        # 执行上料
        resp = self.batch_in_tray(payload)

        return resp

    def _generate_batch_in_tray_template(self, file_path: Path) -> None:
        """
        功能:
            生成批量上料Excel模板, 配置上料点位下拉、托盘类型下拉与内容示例
        参数:
            file_path: Path, 模板输出路径
        返回:
            None
        """
        wb = Workbook()
        ws = wb.active
        ws.title = "batch_in_tray"
        ws.append(["position", "tray_type", "content"])
        ws.column_dimensions["B"].width = 60
        ws.column_dimensions["C"].width = 80

        # 位置下拉，包含 TB 列与 W-1-1~W-1-8 货位
        positions_tb = [f"TB-{row}-{col}" for row in (1, 2) for col in range(1, 5)]
        positions_w = [f"W-1-{index}" for index in range(1, 9)]
        positions = positions_tb + positions_w
        dv_pos = DataValidation(type="list", formula1=f"\"{','.join(positions)}\"")
        ws.add_data_validation(dv_pos)
        dv_pos.add("A2:A101")

        # 托盘下拉，耗材显示数量范围，带物质显示点位范围
        consumable_trays = {
            int(ResourceCode.TIP_TRAY_50UL),
            int(ResourceCode.TIP_TRAY_1ML),
            int(ResourceCode.TIP_TRAY_5ML),
            int(ResourceCode.REACTION_SEAL_CAP_TRAY),
            int(ResourceCode.FLASH_FILTER_INNER_BOTTLE_TRAY),
            int(ResourceCode.FLASH_FILTER_OUTER_BOTTLE_TRAY),
            int(ResourceCode.REACTION_TUBE_TRAY_2ML),
            int(ResourceCode.TEST_TUBE_MAGNET_TRAY_2ML),
        }
        tray_display: List[str] = []
        for code, name in TRAY_CODE_DISPLAY_NAME.items():
            base_text = f"{name}({code})"
            try:
                enum_name = ResourceCode(code).name
                spec = getattr(TraySpec, enum_name, None)
            except Exception:
                spec = None

            if spec is None:
                tray_display.append(base_text)
                continue

            col_count, row_count = spec
            if col_count <= 0 or row_count <= 0:
                tray_display.append(base_text)
                continue

            if code in consumable_trays:
                capacity = col_count * row_count
                tray_display.append(f"{base_text} [1-{capacity}]")
            else:
                end_row_char = chr(ord("A") + row_count - 1)
                tray_display.append(f"{base_text} [A1-{end_row_char}{col_count}]")

        # 用隐藏sheet作为数据源，避免下拉字符串过长
        tray_sheet = wb.create_sheet("validation_meta")
        for idx, option in enumerate(tray_display, start=1):
            tray_sheet.cell(row=idx, column=1).value = option
        tray_sheet.sheet_state = "hidden"

        # 定义命名区域, 避免跨 sheet 验证被 Excel 写成 x14 扩展
        options_name = "tray_type_options"
        options_ref  = f"validation_meta!$A$1:$A${len(tray_display)}"
        wb.defined_names.add(DefinedName(options_name, attr_text=options_ref))

        dv_tray = DataValidation(
            type="list",
            formula1=f"={options_name}",
            showInputMessage=True,
        )
        ws.add_data_validation(dv_tray)
        dv_tray.add("B2:B101")

        ws["C1"] = "content(耗材填数量; 物质填: A1|名称|2mL; B2|名称|5mg)"
        wb.save(file_path)
        logger.info(f"已生成上料模板: {file_path}")

    # ---------- 3. 任务生成文件处理 ----------
    def create_task_by_file(self, template_path: str, chemical_db_path: str) -> JsonDict:
        """
        功能:
            读取任务模板和化学品库，解析为中间数据，调用父类生成任务 Payload 并提交
        参数:
            template_path: 实验模板路径
            chemical_db_path: 化学品库路径
        返回:
            Dict: 任务创建结果
        """
        t_path = Path(template_path)
        c_path = Path(chemical_db_path)

        # 1. 检查并生成模板
        if not t_path.exists():
            self._generate_reaction_template(t_path)
            raise FileNotFoundError(f"已生成模板 {t_path}，请填写后重试")

        if not c_path.exists():
            raise FileNotFoundError(f"未找到化学品库文件: {c_path}")

        # 2. 读取化学品库 -> Dict
        chem_df = pd.read_excel(c_path) if c_path.suffix.lower() in [".xlsx", ".xls"] else pd.read_csv(c_path)
        chem_df.columns = [str(c).strip().lower() for c in chem_df.columns]

        def _pick(row, *keys, default=None):
            for k in keys:
                if k in row and pd.notna(row[k]):
                    return row[k]
            return default

        chemical_db: Dict[str, Dict[str, Any]] = {}
        for _, r in chem_df.iterrows():
            row = {k: r.get(k) for k in chem_df.columns}
            name = str(_pick(row, "substance", "name", "chemical_name", default="") or "").strip()
            if not name:
                continue

            # 小写后的列名
            chemical_db[name] = {
                "chemical_id": _pick(row, "chemical_id"),
                "molecular_weight": _pick(row, "molecular_weight", "mw"),
                "physical_state": str(_pick(row, "physical_state", "state", default="") or "").strip().lower(),
                "density (g/mL)": _pick(row, "density (g/ml)", "density(g/ml)", "density_g_ml", "density", default=None),
                "physical_form": str(_pick(row, "physical_form", default="") or "").strip().lower(),
                "active_content": _pick(row, "active_content","active_content(mmol/ml or wt%)" ,"active_content(mol/l or wt%)", default="" ),
            }

        # 3. 读取任务模板 -> params(Dict), headers(List), data_rows(List[List])
        wb = load_workbook(t_path, data_only=True)
        ws = wb.active

        # 3.1 找到表头行/实验编号列（模板里一般是：row=1, col=3）
        header_row = None
        exp_no_col = None
        for r in range(1, min(ws.max_row, 50) + 1):
            for c in range(1, min(ws.max_column, 50) + 1):
                v = ws.cell(r, c).value
                if isinstance(v, str) and "实验编号" in v:
                    header_row, exp_no_col = r, c
                    break
            if header_row is not None:
                break
        if header_row is None or exp_no_col is None:
            raise ValueError("模板中未找到'实验编号'表头")

        # 3.2 提取全局参数（左侧 A/B）
        # - 实验名称：A1是标签，用户通常填在 B1
        params: Dict[str, Any] = {}
        exp_name = ws.cell(1, 2).value  # B1
        if exp_name is not None and str(exp_name).strip() != "":
            params["实验名称"] = str(exp_name).strip()

        # 扫描 A/B（从第2行开始，遇到“注：”不停止也可以；这里仅跳过“注：”本行）
        for r in range(2, ws.max_row + 1):
            key = ws.cell(r, 1).value
            val = ws.cell(r, 2).value

            if key is None:
                continue
            key_str = str(key).strip()
            if not key_str:
                continue

            # 跳过注释行（不写入 params；否则会污染）
            if key_str.startswith("注：") or key_str.startswith("注:"):
                continue

            # 分类标题行通常是合并单元格，B 为空；这类不要写入 params
            if val is None or (isinstance(val, str) and val.strip() == ""):
                continue

            params[key_str] = val

        # 3.3 生成 headers（从 “实验编号”列开始往右：C..M）
        # 同时把 “试剂_1” -> “试剂名称_1”，让 build_task_payload 能识别
        raw_headers: List[Any] = []
        for c in range(exp_no_col, ws.max_column + 1):
            raw_headers.append(ws.cell(header_row, c).value)

        headers: List[str] = []
        reagent_idx = 0
        for h in raw_headers:
            s = "" if h is None else str(h).strip()

            # 规范化：试剂_1/试剂1 -> 试剂名称_1
            if s.startswith("试剂") and "量" not in s and s != "试剂名称":
                reagent_idx += 1
                headers.append(f"试剂名称_{reagent_idx}")
                continue

            # 规范化：试剂量 -> 试剂量_1/2/...
            if "试剂量" in s:
                # 若前面还没遇到试剂列，给个兜底编号
                idx = reagent_idx if reagent_idx > 0 else (len([x for x in headers if "试剂量" in x]) + 1)
                headers.append(f"试剂量_{idx}")
                continue

            headers.append(s)

        # 3.4 生成 data_rows：从表头下一行开始，按实验编号列读取到最后一列（C..M）
        data_rows: List[List[Any]] = []
        for r in range(header_row + 1, ws.max_row + 1):
            exp_no = ws.cell(r, exp_no_col).value

            # 实验编号为空：认为实验区结束（模板一般后面都是空）
            if exp_no is None or (isinstance(exp_no, str) and exp_no.strip() == ""):
                # 只有在已经读到至少一行实验后才 break，避免中间空行误判
                if data_rows:
                    break
                else:
                    continue

            row_vals: List[Any] = []
            for c in range(exp_no_col, ws.max_column + 1):
                v = ws.cell(r, c).value
                # 这里不要强制 str 化，build_task_payload 内部会 str()；但 None 要变成 ""
                row_vals.append("" if v is None else v)

            data_rows.append(row_vals)

        # 4. 调用父类纯逻辑生成 Payload
        task_payload = self.build_task_payload(params, headers, data_rows, chemical_db)

        # 5. 提交任务信息到工站
        try:
            resp = self.add_task(task_payload)
        except ApiError as exc:
            if getattr(exc, "code", None) == 409:
                task_name = task_payload.get("task_name") or params.get("实验名称")

                dup_msg = (
                    f"任务上传失败，请检查任务名称是否重复: {task_name}"
                    if task_name
                    else "任务名称重复，请修改任务/实验名称后重试"
                )
                logger.error(dup_msg)
                # 重新抛出带提示的 ApiError
                raise ApiError(code=exc.code, msg=dup_msg, payload=exc.payload) from exc
            raise

        # 6. 提交任务信息到工站
        task_id = resp.get("task_id")

        # 7. 回写任务ID到模板(实验ID)
        try:
            task_id_int = int(task_id)
            updated = False
            for r in range(1, ws.max_row + 1):
                key_val = ws.cell(r, 1).value
                if key_val is None:
                    continue
                if str(key_val).strip() == "实验ID":
                    ws.cell(r, 2, value=task_id_int)
                    updated = True
                    break

            if updated:
                wb.save(t_path)
                logger.info("已将任务ID写入模板文件: %s", t_path)
            else:
                logger.warning("未找到“实验ID”位置，未回写任务ID")
        except Exception as exc:
            logger.warning("任务ID回写失败: %s", exc)

        return task_id

    def _generate_reaction_template(self, path: Path) -> None:
        """
        生成与 reeaction_template.xlsx 一致的反应模板
        结构：左侧为参数配置区，右侧为实验试剂填报区
        """
        wb = Workbook()
        ws = wb.active
        ws.title = "Sheet1"

        # 模板默认字体：等线 11
        base_font = Font(name="Microsoft YaHei", charset=134, family=2, scheme="minor", sz=11)
        title_font = Font(name="Microsoft YaHei", charset=134, family=2, scheme="minor", sz=11, bold=True)
        center = Alignment(horizontal="center", vertical="center")

        # 覆盖默认 Normal 样式，保证空白单元格也用微软雅黑
        for style in getattr(wb, "_named_styles", []):
            if getattr(style, "name", "").lower() == "normal":
                style.font = base_font
                break

        # --- 1. 定义左侧参数配置数据 (行2开始, A列和B列) ---
        left_params = [
            ("实验设定", ""),
            ("实验名称", "Auto_task"),
            ("实验ID", 0),
            ("反应设定", ""),
            ("反应规模(mmol)", "0.2"),
            ("反应器类型", "heat"),
            ("反应时间(h)", 8),
            ("反应温度(°C)", 40),
            ("转速(rpm)", 500),
            ("搅拌后⽬标温度(°C)", 30),
            ("等待目标温度", "否"),
            ("称量设定", ""),
            ("称量误差(%)", 3),
            ("最大称量误差(mg)", 1),
            ("加料设定", ""),
            ("固定加料顺序", "否"),
            ("自动加磁子", "是"),
            ("内标设定", ""),
            ("内标种类", "1,3,5-三异丙基苯(溶液,1mol/L in MeCN)"),
            ("内标用量(μL/mg)", 100),
            ("加入内标后搅拌时间(min)", 5),
            ("稀释设定", ""),
            ("稀释液种类", "乙腈"),
            ("稀释量(μL)", 500),
            ("闪滤设定", ""),
            ("闪滤液种类", "乙腈"),
            ("闪滤液用量(μL)", 500),
            ("取样量(μL)", 1),
            ("", ""),  # 空行
        ]
        left_param_rows = len(left_params)

        # --- 2. 设置第一行表头 (Row 1) ---
        ws.cell(row=1, column=3, value="实验编号").font = base_font
        
        reagent_count = 5
        current_col = 4
        for i in range(1, reagent_count + 1):
            ws.cell(row=1, column=current_col, value=f"试剂").font = base_font
            ws.cell(row=1, column=current_col + 1, value="试剂量").font = base_font
            current_col += 2

        # --- 3. 填充左侧参数区 (Row 2 ~ Row 22) ---
        for idx, (param_name, default_val) in enumerate(left_params):
            row_idx = idx + 1  # 从第2行开始

            # 分类标题：模板是 A:B 合并，只写 A 列，且加粗
            if param_name and default_val == "":
                ws.cell(row=row_idx, column=1, value=param_name).font = title_font
                ws.merge_cells(start_row=row_idx, start_column=1, end_row=row_idx, end_column=2)
                continue

            # 空行：保持空
            if param_name == "" and default_val == "":
                continue

            # 普通参数行
            ws.cell(row=row_idx, column=1, value=param_name).font = base_font
            ws.cell(row=row_idx, column=2, value=default_val).font = base_font

        # --- 4. 填充右侧实验编号 (Row 2 ~ Row 25) ---
        for i in range(1, 25):  # 1~24
            row_idx = i + 1
            ws.cell(row=row_idx, column=3, value=i).font = base_font

        # --- 5. 底部注释 (跟随参数行, 预留一行空白) ---
        note_row = left_param_rows + 2
        note_text = "注：试剂量支持单位：(eq,mmol,g,mg,μL,mL）"
        ws.cell(row=note_row, column=1, value=note_text).font = base_font
        ws.merge_cells(start_row=note_row, start_column=1, end_row=note_row, end_column=2)
        ws.cell(row=note_row, column=1).alignment = center  # 合并后的单元格居中

        max_template_row = max(note_row, 25)

        # --- 6. 字体铺满 (A1:M*)  ---
        for r in range(1, max_template_row + 1):
            for c in range(1, 14):  # A..M
                cell = ws.cell(r, c)
                # 标题行的粗体不要覆盖
                if cell.font and cell.font.bold:
                    continue
                cell.font = base_font

        # --- 7. 对齐 ---
        # C~L 整块都居中（含空白）
        for r in range(1, max_template_row + 1):
            for c in range(3, 13):  # C..L
                ws.cell(r, c).alignment = center

        # A 列：参数行和注释行居中
        a_rows = []
        b_rows = []
        for idx, (param_name, default_val) in enumerate(left_params):
            row_idx = idx + 1
            if param_name != "":
                a_rows.append(row_idx)
            if param_name != "" and default_val != "":
                b_rows.append(row_idx)
        for r in a_rows + [note_row]:
            ws.cell(r, 1).alignment = center

        # B 列：只有有值的参数行居中（标题行/空白行/合并后的 B 不处理）
        for r in b_rows:
            ws.cell(r, 2).alignment = center

        # M 列：只有表头 M1 居中
        ws.cell(1, 13).alignment = center

        # 表头 A1/C1 也居中（模板如此）
        ws.cell(1, 1).alignment = center
        ws.cell(1, 3).alignment = center

        # --- 8. 列宽： ---
        widths_map = {
            "A": 26.0,
            "B": 38.0,
            "C": 15.0,
            "D": 14.0,
            "E": 14.0,
            "F": 14.0,
            "G": 14.0,
            "H": 14.0,
            "I": 14.0,
            "J": 14.0,
            "K": 14.0,
            "L": 14.0,
            "M": 14.0,
        }
        for col_letter, w in widths_map.items():
            ws.column_dimensions[col_letter].width = w

        wb.save(path)
        logger.info(f"已生成任务模板: {path}")

    # -------------- 4. 物料核算 -------------
    def check_resource_for_task(self, template_path: str, chemical_db_path: str) -> JsonDict:
        """
        功能:
            读取实验模板与化学品库, 构建任务 Payload, 获取站内资源并比对是否满足实验需求。
        参数:
            template_path: 实验模板文件路径(xlsx/csv)。
            chemical_db_path: 化学品库文件路径(xlsx/csv)。
        返回:
            Dict, analyze_resource_readiness 的结果, 包含需求、库存、缺失与冗余信息。
        """
        t_path = Path(template_path)
        c_path = Path(chemical_db_path)

        if not t_path.exists():
            raise FileNotFoundError(f"未找到实验模板文件: {t_path}")
        if not c_path.exists():
            raise FileNotFoundError(f"未找到化学品库文件: {c_path}")

        chem_df = pd.read_excel(c_path) if c_path.suffix.lower() in [".xlsx", ".xls"] else pd.read_csv(c_path)
        chem_df.columns = [str(c).strip().lower() for c in chem_df.columns]

        def _pick(row, *keys, default=None):
            for k in keys:
                if k in row and pd.notna(row[k]):
                    return row[k]
            return default

        chemical_db: Dict[str, Dict[str, Any]] = {}
        for _, r in chem_df.iterrows():
            row = {k: r.get(k) for k in chem_df.columns}
            name = str(_pick(row, "substance", "name", "chemical_name", default="") or "").strip()
            if not name:
                continue
            chemical_db[name] = {
                "chemical_id": _pick(row, "chemical_id"),
                "molecular_weight": _pick(row, "molecular_weight", "mw"),
                "physical_state": str(_pick(row, "physical_state", "state", default="") or "").strip().lower(),
                "density (g/mL)": _pick(row, "density (g/ml)", "density(g/ml)", "density_g_ml", "density", default=None),
                "physical_form": str(_pick(row, "physical_form", default="") or "").strip().lower(),
                "active_content": _pick(row, "active_content", "active_content(mmol/ml or wt%)", "active_content(mol/l or wt%)", default=""),
            }

        wb = load_workbook(t_path, data_only=True)
        ws = wb.active

        header_row = None
        exp_no_col = None
        for r in range(1, min(ws.max_row, 50) + 1):
            for c in range(1, min(ws.max_column, 50) + 1):
                v = ws.cell(r, c).value
                if isinstance(v, str) and "实验编号" in v:
                    header_row, exp_no_col = r, c
                    break
            if header_row is not None:
                break
        if header_row is None or exp_no_col is None:
            raise ValidationError("模板中未找到'实验编号'表头")

        params: Dict[str, Any] = {}
        exp_name = ws.cell(1, 2).value
        if exp_name is not None and str(exp_name).strip() != "":
            params["实验名称"] = str(exp_name).strip()

        task_id = None  # 用于存储实验ID
        for r in range(2, ws.max_row + 1):
            key = ws.cell(r, 1).value
            val = ws.cell(r, 2).value
            if key is None:
                continue
            key_str = str(key).strip()
            if key_str == "":
                continue
            if key_str.startswith("注：") or key_str.startswith("注"):
                continue
            if val is None or (isinstance(val, str) and val.strip() == ""):
                continue
            
            # 识别实验ID参数并提取整数值
            if key_str == "实验ID":
                try:
                    task_id = int(val)
                    self._logger.info("从模板中读取到实验ID: %d", task_id)
                except (ValueError, TypeError):
                    self._logger.warning("实验ID格式无效: %s, 将跳过二次校验", val)
            
            params[key_str] = val

        raw_headers: List[Any] = []
        for c in range(exp_no_col, ws.max_column + 1):
            raw_headers.append(ws.cell(header_row, c).value)

        headers: List[str] = []
        reagent_idx = 0
        for h in raw_headers:
            s = "" if h is None else str(h).strip()
            if s.startswith("试剂") and "量" not in s and s != "试剂名称":
                reagent_idx += 1
                headers.append(f"试剂名称_{reagent_idx}")
                continue
            if "试剂量" in s:
                idx = reagent_idx if reagent_idx > 0 else (len([x for x in headers if "试剂量" in x]) + 1)
                headers.append(f"试剂量_{idx}")
                continue
            headers.append(s)

        data_rows: List[List[Any]] = []
        for r in range(header_row + 1, ws.max_row + 1):
            exp_no = ws.cell(r, exp_no_col).value
            if exp_no is None or (isinstance(exp_no, str) and exp_no.strip() == ""):
                if data_rows:
                    break
                else:
                    continue
            row_vals: List[Any] = []
            for c in range(exp_no_col, ws.max_column + 1):
                v = ws.cell(r, c).value
                row_vals.append("" if v is None else v)
            data_rows.append(row_vals)

        task_payload = self.build_task_payload(params, headers, data_rows, chemical_db)
        resource_rows = self.get_resource_info()
        result = self.analyze_resource_readiness(task_payload, resource_rows, chemical_db, task_id=task_id)

        # 自动保存物料核算结果
        if self._data_manager and task_id:
            self._data_manager.save_resource_check(str(task_id), result)

        return result

    # ------------5. Unilab 接口（待修改）-------------
    def submit_experiment_task(
        self,
        chemical_db_path: str,
        task_name: str = "Unilab_Auto_Job",
        reaction_type: str = "heat",
        duration: str = "8",
        temperature: str = "40",
        stir_speed: str = "500",
        target_temp: str = "30",
        auto_magnet: bool = True,
        fixed_order: bool = False,
        internal_std_name: str = "",
        stir_time_after_std: str = "",
        diluent_name: str = "",
        rows: list = None
    ) -> JsonDict:
        """
        功能:
            提交 Unilab 流程编排任务, 按行数据动态生成表头, 兼容包含“加磁子”的列.
        参数:
            chemical_db_path: str, 化学品库文件路径.
            task_name: str, 任务名称.
            reaction_type: str, 反应类型.
            duration: str, 反应时间(h).
            temperature: str, 反应温度(°C).
            stir_speed: str, 搅拌速度(rpm).
            target_temp: str, 搅拌后目标温度(°C).
            auto_magnet: bool, 是否自动加磁子.
            fixed_order: bool, 是否固定加料顺序.
            internal_std_name: str, 内标名称.
            stir_time_after_std: str, 内标加入后搅拌时间(min).
            diluent_name: str, 稀释液名称.
            rows: List[List[Any]], 行数据矩阵, 第1列为实验编号, 其余列为试剂或“加磁子”.
        返回:
            Dict[str, Any], 提交成功后返回的任务 ID.
        """
        c_path = Path(chemical_db_path)
        if c_path.exists() is False:
            raise FileNotFoundError(f"未找到化学品库文件: {c_path}")

        chem_df = pd.read_excel(c_path) if c_path.suffix.lower() in [".xlsx", ".xls"] else pd.read_csv(c_path)
        chem_df.columns = [str(c).strip().lower() for c in chem_df.columns]

        def _pick(row, *keys, default=None):
            for k in keys:
                if k in row and pd.notna(row[k]):
                    return row[k]
            return default

        chemical_db: Dict[str, Dict[str, Any]] = {}
        for _, r in chem_df.iterrows():
            row = {k: r.get(k) for k in chem_df.columns}
            name = str(_pick(row, "substance", "name", "chemical_name", default="") or "").strip()
            if name == "":
                continue
            chemical_db[name] = {
                "chemical_id": _pick(row, "chemical_id"),
                "molecular_weight": _pick(row, "molecular_weight", "mw"),
                "physical_state": str(_pick(row, "physical_state", "state", default="") or "").strip().lower(),
                "density (g/mL)": _pick(row, "density (g/ml)", "density(g/ml)", "density_g_ml", "density", default=None),
                "physical_form": str(_pick(row, "physical_form", default="") or "").strip().lower(),
                "active_content": _pick(row, "active_content", "active_content(mmol/ml or wt%)", "active_content(mol/l or wt%)", default=""),
            }

        if auto_magnet is True:
            auto_magnet_text = "是"
        else:
            auto_magnet_text = "否"

        if fixed_order is True:
            fixed_order_text = "是"
        else:
            fixed_order_text = "否"

        params = {
            "实验名称": task_name,
            "反应器类型": reaction_type,
            "反应时间(h)": duration,
            "反应温度(°C)": temperature,
            "转速(rpm)": stir_speed,
            "搅拌后目标温度(°C)": target_temp,
            "自动加磁子": auto_magnet_text,
            "固定加料顺序": fixed_order_text,
            "内标种类": internal_std_name,
            "加入内标后搅拌时间(min)": stir_time_after_std,
            "稀释液种类": diluent_name,
        }

        if rows is None:
            rows = []

        default_pair_count = 5
        cleaned_rows: List[List[Any]] = []
        magnet_columns: set[int] = set()
        max_col_count = 1

        for row in rows:
            if isinstance(row, (list, tuple)) is False:
                logger.warning("行数据格式需要列表或元组, 已跳过一行")
                continue
            row_values = list(row)
            while len(row_values) > 0:
                tail_text = "" if row_values[-1] is None else str(row_values[-1]).strip()
                if tail_text == "":
                    row_values.pop()
                    continue
                break
            if len(row_values) > max_col_count:
                max_col_count = len(row_values)
            for col_index, cell in enumerate(row_values):
                cell_text = "" if cell is None else str(cell).strip()
                if "加磁子" in cell_text:
                    magnet_columns.add(col_index)
            cleaned_rows.append(row_values)

        if len(cleaned_rows) == 0:
            header_count = 1 + default_pair_count * 2
        else:
            header_count = max_col_count

        headers: List[str] = ["实验编号"]
        for col_index in range(1, header_count):
            if col_index in magnet_columns:
                headers.append("加磁子")
            elif col_index % 2 == 1:
                headers.append("试剂")
            else:
                headers.append("试剂量")

        normalized_rows: List[List[Any]] = []
        for row_values in cleaned_rows:
            padded_values = row_values + [""] * (header_count - len(row_values))
            normalized_rows.append(padded_values)

        try:
            task_payload = self.build_task_payload(params, headers, normalized_rows, chemical_db)
        except AttributeError as exc:
            raise Exception("无法找到 build_task_payload 方法, 请检查 StationController 定义") from exc

        try:
            resp = self.add_task(task_payload)
        except ApiError as exc:
            if getattr(exc, "code", None) == 409:
                task_name_val = task_payload.get("task_name") or params.get("实验名称")
                dup_msg = (
                    f"任务上传失败, 请检查任务名称是否重复: {task_name_val}"
                    if task_name_val
                    else "任务名称重复, 请修改任务或实验名称后重试"
                )
                logger.error(dup_msg)
                raise ApiError(code=exc.code, msg=dup_msg, payload=exc.payload) from exc
            raise

        task_id = resp.get("task_id")
        return task_id

    # -------- controller功能接口函数 --------
    def device_init(self, device_id=None, *, poll_interval_s: float = 1.0, timeout_s: float = 600.0):
        return super().device_init(device_id, poll_interval_s=poll_interval_s, timeout_s=timeout_s)

    def start_task(self, task_id: int | None = None, *, check_glovebox_env: bool = True, water_limit_ppm: float = 10.0, oxygen_limit_ppm: float = 10.0):
        return super().start_task(task_id, check_glovebox_env=check_glovebox_env, water_limit_ppm=water_limit_ppm, oxygen_limit_ppm=oxygen_limit_ppm)

    def wait_task_with_ops(self, task_id: int | None = None, *, poll_interval_s: float = 2.0) -> int:
        return super().wait_task_with_ops(task_id, poll_interval_s=poll_interval_s)

    def batch_out_task_and_empty_trays(self, task_id: int | None = None, *, poll_interval_s: float = 1.0, ignore_missing: bool = True, timeout_s: float = 900.0, move_type: str = "main_out"):
        return super().batch_out_task_and_empty_trays(task_id, poll_interval_s=poll_interval_s, ignore_missing=ignore_missing, timeout_s=timeout_s, move_type=move_type)

    def batch_out_task_trays(self, task_id: int | None = None, *, poll_interval_s: float = 1.0, ignore_missing: bool = True, timeout_s: float = 900.0, move_type: str = "main_out"):
        return super().batch_out_task_trays(task_id, poll_interval_s=poll_interval_s, ignore_missing=ignore_missing, timeout_s=timeout_s, move_type=move_type)

    def batch_out_empty_trays(self, *, poll_interval_s: float = 1.0, ignore_missing: bool = True, timeout_s: float = 900.0, move_type: str = "main_out"):
        return super().batch_out_empty_trays(poll_interval_s=poll_interval_s, ignore_missing=ignore_missing, timeout_s=timeout_s, move_type=move_type)

    def batch_out_tray(self, layout_list: list[dict], move_type: str = "main_out", *, task_id: int = None, poll_interval_s: float = 1.0, timeout_s: float = 900.0):
        return super().batch_out_tray(layout_list, move_type=move_type, task_id=task_id, poll_interval_s=poll_interval_s, timeout_s=timeout_s)


