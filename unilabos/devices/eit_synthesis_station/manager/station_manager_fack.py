                          # -*- coding: utf-8 -*-
import csv
import re
import logging
import shutil
import time
import pandas as pd
import openpyxl
from datetime import datetime
from openpyxl import Workbook,load_workbook
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.workbook.defined_name import DefinedName
from openpyxl.styles import Font, Alignment, NamedStyle 
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 引入底层的控制器
from ..controller.station_controller import SynthesisStationController
from ..config.setting import Settings, configure_logging
from ..config.constants import (
    CONSUMABLE_ALIAS_TO_CODE,
    CONSUMABLE_CODE_DISPLAY_NAME,
    CONSUMABLE_CODE_TO_TRAY_CODE,
    ResourceCode,
    TRAY_CODE_DISPLAY_NAME,
    TraySpec,
)
from .synchronizer import EITSynthesisWorkstation

from ..driver.exceptions import ValidationError,ApiError
from ..utils.file_utils import safe_excel_write, safe_workbook_save
from ..chem_tools.chemical_append_utils import (
    build_append_row_data,
    build_append_row_data_for_smiles,
    build_prepared_chemical_row_data,
    build_duplicate_check_specs,
    collect_missing_append_headers,
    get_excel_write_value,
    save_chemicalbook_record,
)

logger = logging.getLogger("StationManager")

JsonDict = Dict[str, Any]

# 模块根目录, 用于构建相对路径
MODULE_ROOT = Path(__file__).resolve().parent.parent

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

    def login(self) -> tuple:
        """
        功能:
            登录并缓存 token, 登录成功后根据配置自动启动异常通知监控.
        参数:
            无.
        返回:
            Tuple[str, str], (token_type, access_token).
        """
        logger.info("虚假执行 login")
        time.sleep(5)
        return ("Bearer", "fake_token")

    def _read_table_file_with_required_columns(
        self,
        path: Path,
        *,
        required_columns: Optional[List[str]] = None,
        preferred_sheet_name: Optional[str] = None,
    ) -> pd.DataFrame:
        """
        功能:
            读取 CSV/Excel 文件.
            当为 Excel 且存在多工作表时, 根据必需列选择工作表.
        参数:
            path: 文件路径.
            required_columns: 必需列名列表, None 表示直接读取默认表.
            preferred_sheet_name: 优先工作表名.
        返回:
            DataFrame, 读取后的表格数据.
        """
        logger.info("虚假执行 _read_table_file_with_required_columns, path=%s", path)
        time.sleep(5)
        return pd.DataFrame()

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
        logger.info("虚假执行 export_chemical_list_to_file, output_path=%s", output_path)
        time.sleep(5)

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
        logger.info("虚假执行 sync_chemicals_from_file, file_path=%s, overwrite=%s", file_path, overwrite)
        time.sleep(5)

    def check_chemical_library_by_file(self, file_path: str) -> Dict[str, List[str]]:
        """
        功能:
            读取化学品库文件并调用底层校验逻辑，输出校验结果
        参数:
            file_path: str, 化学品库文件路径，支持 Excel/CSV
        返回:
            Dict[str, List[str]], 包含 errors 与 warnings
        """
        logger.info("虚假执行 check_chemical_library_by_file, file_path=%s", file_path)
        time.sleep(5)
        return {"errors": [], "warnings": []}
    
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
        logger.info("虚假执行 deduplicate_chemical_library_by_file, file_path=%s, output_path=%s", file_path, output_path)
        time.sleep(5)
        return []
    
    def _beautify_excel_database(self, file_path: Path) -> None:
        """
        功能:
            美化去重后的 Excel: 表头加粗、全居中、列宽自适应、按内容选择中英文字体
        参数:
            file_path: Path, 目标 Excel 路径
        返回:
            None
        """
        logger.info("虚假执行 _beautify_excel_database, file_path=%s", file_path)
        time.sleep(5)

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
        logger.info("虚假执行 align_chemicals_with_file, file_path=%s, auto_delete=%s", file_path, auto_delete)
        time.sleep(5)

    @staticmethod
    def _has_any_append_core_value(row_data: Dict[str, Any]) -> bool:
        """
        功能:
            判断追加行是否至少包含一个可用于识别化合物的核心字段.
        参数:
            row_data: Dict[str, Any], 准备写入 Excel 的行数据.
        返回:
            bool, True 表示至少包含 CAS, 英文名, 中文名中的一个.
        """
        logger.info("虚假执行 _has_any_append_core_value")
        time.sleep(5)
        return False

    @staticmethod
    def _format_append_row_summary(row_data: Dict[str, Any]) -> str:
        """
        功能:
            提取 chemical list 关键字段, 生成统一的日志摘要文本.
        参数:
            row_data: Dict[str, Any], 追加到 chemical list 的行数据.
        返回:
            str, 包含 substance, physical_state, physical_form 的摘要文本.
        """
        logger.info("虚假执行 _format_append_row_summary")
        time.sleep(5)
        return "虚假摘要"

    def _resolve_append_excel_path(self, excel_path: Optional[str]) -> Path:
        """
        功能:
            解析化学品追加流程使用的目标 Excel 路径.
        参数:
            excel_path: Optional[str], 用户指定路径, None 时使用默认库文件.
        返回:
            Path, 已解析的 Excel 路径.
        异常:
            FileNotFoundError: 目标文件不存在时抛出.
        """
        logger.info("虚假执行 _resolve_append_excel_path, excel_path=%s", excel_path)
        time.sleep(5)
        return MODULE_ROOT / "sheet" / "fake_append.xlsx"

    @staticmethod
    def _build_append_header_map(worksheet: Any) -> Dict[str, int]:
        """
        功能:
            从化学品库工作表首行构建表头到列号的映射.
        参数:
            worksheet: Any, openpyxl 工作表对象.
        返回:
            Dict[str, int], 表头名称到列号的映射.
        """
        logger.info("虚假执行 _build_append_header_map")
        time.sleep(5)
        return {}

    @staticmethod
    def _build_append_row_snapshot(
        worksheet: Any,
        header_map: Dict[str, int],
        row_index: int,
    ) -> Dict[str, Any]:
        """
        功能:
            从化学品库工作表中提取单行数据快照, 统一补齐常用别名字段.
        参数:
            worksheet: Any, openpyxl 工作表对象.
            header_map: Dict[str, int], 表头名称到列号映射.
            row_index: int, 目标行号.
        返回:
            Dict[str, Any], 单行字段字典.
        """
        logger.info("虚假执行 _build_append_row_snapshot")
        time.sleep(5)
        return {}

    def _find_existing_chemical_row(
        self,
        *,
        excel_path: Optional[str] = None,
        cas_number: str = "",
        substance_english_name: str = "",
        substance: str = "",
    ) -> Optional[Dict[str, Any]]:
        """
        功能:
            按 CAS, 英文名, 中文名顺序在化学品库中查找已有条目, 优先返回 neat 行.
        参数:
            excel_path: Optional[str], 化学品库文件路径.
            cas_number: str, 候选 CAS 号.
            substance_english_name: str, 候选英文名.
            substance: str, 候选中文名或展示名.
        返回:
            Optional[Dict[str, Any]], 命中时返回包含 row_data 与 row_index 的结果字典.
        """
        logger.info("虚假执行 _find_existing_chemical_row")
        time.sleep(5)
        return None

    @staticmethod
    def _parse_positive_float(value: Any, field_name: str) -> float:
        """
        功能:
            将输入值解析为大于 0 的浮点数.
        参数:
            value: Any, 待解析的数值.
            field_name: str, 字段中文名, 用于异常提示.
        返回:
            float, 解析后的正数.
        异常:
            ValidationError: 字段为空, 非数字或不大于 0 时抛出.
        """
        logger.info("虚假执行 _parse_positive_float, field_name=%s", field_name)
        time.sleep(5)
        return 0.0

    @staticmethod
    def _format_preparation_number(value: float) -> str:
        """
        功能:
            将配液或称量结果格式化为紧凑展示文本.
        参数:
            value: float, 原始数值.
        返回:
            str, 去除多余尾零后的文本.
        """
        logger.info("虚假执行 _format_preparation_number")
        time.sleep(5)
        return "0"

    def _build_solution_recipe(
        self,
        base_row_data: Dict[str, Any],
        concentration_mol_l: float,
        target_volume_ml: float,
        solvent_name: str,
    ) -> Dict[str, Any]:
        """
        功能:
            根据母体化合物信息生成溶液配置结果.
        参数:
            base_row_data: Dict[str, Any], 母体化合物行数据.
            concentration_mol_l: float, 目标浓度, 单位 mol/L.
            target_volume_ml: float, 目标定容体积, 单位 mL.
            solvent_name: str, 溶剂名称.
        返回:
            Dict[str, Any], 包含质量, 体积与展示文案的结果字典.
        异常:
            ValidationError: 缺少分子量时抛出.
        """
        logger.info("虚假执行 _build_solution_recipe")
        time.sleep(5)
        return {}

    def _build_beads_recipe(
        self,
        base_row_data: Dict[str, Any],
        wt_percent: float,
        target_active_mmol: float,
    ) -> Dict[str, Any]:
        """
        功能:
            根据母体化合物信息生成 beads 称量结果.
        参数:
            base_row_data: Dict[str, Any], 母体化合物行数据.
            wt_percent: float, 有效成分质量分数.
            target_active_mmol: float, 目标活性摩尔数, 单位 mmol.
        返回:
            Dict[str, Any], 包含 beads 质量与展示文案的结果字典.
        异常:
            ValidationError: 缺少分子量时抛出.
        """
        logger.info("虚假执行 _build_beads_recipe")
        time.sleep(5)
        return {}

    def _resolve_prepared_base_chemical(
        self,
        identifier: str,
        excel_path: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        功能:
            为溶液或 beads 配置流程解析母体化合物, 不存在时自动补录 neat 条目.
        参数:
            identifier: str, CAS 或 SMILES.
            excel_path: Optional[str], 化学品库文件路径.
        返回:
            Optional[Dict[str, Any]], 成功时返回母体行数据与来源信息, 失败时返回 None.
        """
        logger.info("虚假执行 _resolve_prepared_base_chemical")
        time.sleep(5)
        return {}

    @staticmethod
    def _find_duplicate_append_row(
        worksheet: Any,
        header_map: Dict[str, int],
        row_data: Dict[str, Any],
    ) -> Optional[Tuple[str, str, str, int]]:
        """
        功能:
            按约定优先级在 Excel 中查找重复化合物.
        参数:
            worksheet: Any, openpyxl 工作表对象.
            header_map: Dict[str, int], 表头名称到列号映射.
            row_data: Dict[str, Any], 准备写入的行数据.
        返回:
            Optional[Tuple[str, str, str, int]], 命中时返回
            (字段标签, 目标值, 表头名, 行号), 否则返回 None.
        """
        logger.info("虚假执行 _find_duplicate_append_row")
        time.sleep(5)
        return None

    def _append_chemical_row_to_excel(
        self,
        row_data: Dict[str, Any],
        excel_path: Optional[str] = None,
    ) -> Optional[int]:
        """
        功能:
            将单条化学品行数据追加到 Excel 末尾, 并执行重复检查.
        参数:
            row_data: Dict[str, Any], 准备写入的行数据.
            excel_path: Optional[str], 目标 Excel 文件路径.
        返回:
            Optional[int], 成功时返回新行行号, 命中重复时返回 None.
        """
        logger.info("虚假执行 _append_chemical_row_to_excel")
        time.sleep(5)

    def _fetch_chemicalbook_append_artifacts(
        self,
        resolved_cas: str,
    ) -> Tuple[Optional[Dict[str, Any]], str, str]:
        """
        功能:
            根据已解析的 CAS 获取 ChemicalBook 结构化结果及 sidecar 路径.
        参数:
            resolved_cas: str, 已解析出的 CAS 号.
        返回:
            Tuple[Optional[Dict[str, Any]], str, str], 依次为
            chemicalbook_record, chemicalbook_status, chemicalbook_record_path.
        """
        logger.info("虚假执行 _fetch_chemicalbook_append_artifacts")
        time.sleep(5)
        return {}

    def lookup_and_append_chemical(
        self, query: str, excel_path: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        功能:
            在线查询化合物信息并追加到化学品库 Excel 文件末尾.
            查询分为两层: 先用多源核心查询获取可用基础信息, 再用 ChemicalBook
            结构化抓取补充全量数据, 并将原始结果保存为 sidecar JSON.
        参数:
            query: str, CAS 号或化合物中英文名称.
            excel_path: Optional[str], 目标 Excel 文件路径, 默认为 sheet/chemical_list.xlsx.
        返回:
            Optional[Dict[str, Any]], 成功返回稳定结果字典, 包含 row_data, row_index,
            chemicalbook_status, chemicalbook_record_path. 查询失败或重复时返回 None.
        """
        logger.info("虚假执行 lookup_and_append_chemical")
        time.sleep(5)
        return {"success": True, "message": "虚假找测并追加完成"}

    def lookup_and_append_chemical_by_smiles(
        self, smiles: str, excel_path: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        功能:
            根据单个完整 SMILES 在线查询化合物信息, 并追加到化学品库 Excel 文件末尾.
            查询顺序固定为 PubChem 结构查询, 拿到 CAS 后再补 Common Chemistry
            与 ChemicalBook.
        参数:
            smiles: str, 单个完整 SMILES 结构式.
            excel_path: Optional[str], 目标 Excel 文件路径, 默认为 sheet/chemical_list.xlsx.
        返回:
            Optional[Dict[str, Any]], 成功返回稳定结果字典, 包含 row_data, row_index,
            chemicalbook_status, chemicalbook_record_path. 查询失败或重复时返回 None.
        """
        logger.info("虚假执行 lookup_and_append_chemical_by_smiles")
        time.sleep(5)
        return {"success": True, "message": "虚假 SMILES 追加完成"}

    def prepare_solution_or_beads(
        self,
        identifier: str,
        prepared_form: str,
        *,
        solvent_name: str = "",
        active_content: Any,
        target_volume_ml: Optional[Any] = None,
        target_active_mmol: Optional[Any] = None,
        excel_path: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        功能:
            根据 CAS 或 SMILES 解析母体化合物, 按指定形态生成溶液或 beads 条目并追加到化学品库.
        参数:
            identifier: str, 母体化合物 CAS 或 SMILES.
            prepared_form: str, 派生形态, 支持 solution 或 beads.
            solvent_name: str, solution 使用的溶剂名称.
            active_content: Any, solution 时表示 mol/L, beads 时表示 wt%.
            target_volume_ml: Optional[Any], solution 目标定容体积, 单位 mL.
            target_active_mmol: Optional[Any], beads 目标活性摩尔数, 单位 mmol.
            excel_path: Optional[str], 目标 Excel 文件路径.
        返回:
            Optional[Dict[str, Any]], 成功返回母体信息, 派生条目信息与配制结果摘要.
            派生条目重复或母体无法解析时返回 None.
        异常:
            ValidationError: 形态或数值参数非法时抛出.
        """
        logger.info("虚假执行 prepare_solution_or_beads")
        time.sleep(5)
        return {"success": True, "message": "虚假配制完成"}

    # ---------- 2. 上料动作 ----------

    def _read_batch_in_records(self, file_path: str) -> List[Dict[str, str]]:
        """
        功能:
            读取上料表格文件(xlsx/csv), 返回标准化记录列表.
        参数:
            file_path: str, 上料文件路径.
        返回:
            List[Dict[str, str]], 包含 position, tray_type, content,
            shelf_position, storage 字段的记录列表.
        异常:
            FileNotFoundError: 文件不存在时自动生成模板并抛出.
        """
        logger.info("虚假执行 _read_batch_in_records, file_path=%s", file_path)
        time.sleep(5)
        return []

    def batch_in_tray_by_file(self, file_path: str) -> JsonDict:
        """
        功能:
            读取上料表格, 转换为中间格式, 调用父类生成 Payload 并执行上料
        参数:
            file_path: 文件路径
        返回:
            Dict: API 响应
        """
        logger.info("虚假执行 batch_in_tray_by_file, file_path=%s", file_path)
        time.sleep(5)
        return {"success": True, "message": "虚假批量入盘完成"}

    def batch_in_tray_with_agv_transfer(
        self,
        file_path: str = None,
        *,
        block: bool = True,
        chamber_capacity: int = 8,
    ) -> JsonDict:
        """
        功能:
            根据 batch_in_tray.xlsx 中的信息, 分轮次(每轮最多 chamber_capacity 个托盘)
            执行: 开过渡舱门 -> AGV 转运 -> 机器人上料.
            全部轮次结束后 AGV 前往充电站.
            当总托盘数 <= chamber_capacity 时, 行为与旧版本一致(单轮).
        参数:
            file_path: 上料文件路径, 默认为 sheet/batch_in_tray.xlsx
            block: 是否阻塞等待 AGV 转运完成
            chamber_capacity: 过渡舱单次最大容纳托盘数, 默认 8
        返回:
            Dict, 包含多轮次的聚合结果:
                - success: bool, 是否全部轮次成功
                - total_trays: int, 总托盘数
                - transferred_trays: int, 成功转运的托盘数
                - loaded_trays: int, 成功上料的托盘数
                - rounds: List[Dict], 每轮次详情
                - charging_result: Dict, AGV 充电结果
                - errors: List[str], 所有错误信息
                - message: str, 结果摘要
        """
        logger.info("虚假执行 batch_in_tray_with_agv_transfer, file_path=%s, block=%s, chamber_capacity=%s", file_path, block, chamber_capacity)
        time.sleep(5)
        return {"success": True, "total_trays": 0, "transferred_trays": 0, "loaded_trays": 0, "rounds": [], "charging_result": None, "errors": [], "message": "虚假 AGV 转运入盘完成"}

    def _validate_agv_transfer_round_records(
        self,
        records: List[Dict[str, str]],
        *,
        round_num: int,
    ) -> List[str]:
        """
        功能:
            校验单轮 AGV 上料记录是否满足唯一性要求.
            同一轮次内 position 与 shelf_position 都必须唯一, 且不能为空.
        参数:
            records: List[Dict[str, str]], 单轮上料记录列表.
            round_num: int, 当前轮次编号.
        返回:
            List[str], 校验失败时的错误信息列表.
        """
        logger.info("虚假执行 _validate_agv_transfer_round_records, round_num=%s", round_num)
        time.sleep(5)
        return []

    def _generate_batch_in_tray_template(self, file_path: Path) -> None:
        """
        功能:
            生成批量上料Excel模板, 配置上料点位下拉、托盘类型下拉与内容示例
        参数:
            file_path: Path, 模板输出路径
        返回:
            None
        """
        logger.info("虚假执行 _generate_batch_in_tray_template, file_path=%s", file_path)
        time.sleep(5)

    def _find_header_in_sheet(self, worksheet: Any, header_keyword: str) -> Tuple[Optional[int], Optional[int]]:
        """
        功能:
            在单个工作表的前 50 行和前 50 列中查找目标表头.
        参数:
            worksheet: openpyxl 工作表对象.
            header_keyword: 需要匹配的表头关键词, 例如"实验编号".
        返回:
            Tuple[Optional[int], Optional[int]], 命中时返回(行号, 列号), 未命中返回(None, None).
        """
        logger.info("虚假执行 _find_header_in_sheet, header_keyword=%s", header_keyword)
        time.sleep(5)
        return (None, None)

    def _select_task_template_sheet(
        self,
        workbook: Workbook,
        header_keyword: str = "实验编号",
    ) -> Tuple[Optional[Any], Optional[int], Optional[int]]:
        """
        功能:
            从任务模板工作簿中选择包含目标表头的工作表.
            选择顺序为: "实验方案设定" -> 当前激活工作表 -> 其余工作表.
        参数:
            workbook: openpyxl Workbook 对象.
            header_keyword: 需要匹配的表头关键词.
        返回:
            Tuple[Optional[Any], Optional[int], Optional[int]], 命中时返回(工作表, 行号, 列号), 未命中返回(None, None, None).
        """
        logger.info("虚假执行 _select_task_template_sheet, header_keyword=%s", header_keyword)
        time.sleep(5)
        return (None, None, None)

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
        logger.info("虚假执行 create_task_by_file, template_path=%s, chemical_db_path=%s", template_path, chemical_db_path)
        time.sleep(5)
        return "fake_task_id"

    def _generate_reaction_template(self, path: Path) -> None:
        """
        生成与 reeaction_template.xlsx 一致的反应模板
        结构：左侧为参数配置区，右侧为实验试剂填报区
        """
        logger.info("虚假执行 _generate_reaction_template, path=%s", path)
        time.sleep(5)

    # ---------- 4. 物料核算 ----------
    def check_resource_for_task(self, template_path: str, chemical_db_path: str, auto_generate_batch_file: bool = True) -> JsonDict:
        """
        功能:
            读取实验模板与化学品库, 构建任务 Payload, 获取站内资源并比对是否满足实验需求。
        参数:
            template_path: 实验模板文件路径(xlsx/csv)。
            chemical_db_path: 化学品库文件路径(xlsx/csv)。
            auto_generate_batch_file: 是否自动生成上料文件, 默认为 True。
        返回:
            Dict, analyze_resource_readiness 的结果, 包含需求、库存、缺失与冗余信息。
        """
        logger.info("虚假执行 check_resource_for_task, template_path=%s, chemical_db_path=%s, auto_generate_batch_file=%s", template_path, chemical_db_path, auto_generate_batch_file)
        time.sleep(5)
        return {"success": True, "resource_ready": True, "errors": [], "warnings": [], "message": "虚假资源检查完成"}

    def auto_generate_batch_in_tray_from_resource_check(self, task_id: Optional[int] = None) -> None:
        """
        功能:
            根据资源核查结果自动修改上料文件, 考虑料盘规格, 优先填满一个料盘再使用下一个
        参数:
            task_id: 任务ID, 如果为None则自动搜索data/tasks中id最大且状态为UNSTARTED的任务
        返回:
            None
        """
        logger.info("虚假执行 auto_generate_batch_in_tray_from_resource_check, task_id=%s", task_id)
        time.sleep(5)

    # ---------- 4.5 标签打印 ----------

    def _get_label_printer(self):
        """
        功能:
            延迟创建并返回标签打印服务实例.
        返回:
            LabelPrintService 实例.
        """
        logger.info("虚假执行 _get_label_printer")
        time.sleep(5)
        return None

    def print_reagent_labels(self) -> None:
        """
        功能:
            从上料文件(batch_in_tray.xlsx)中提取试剂名称, 打印试剂标签.
            只打印试剂(固体/液体), 不打印耗材.
            通过判断content字段是否包含"|"来区分试剂和耗材.
        """
        logger.info("虚假执行 print_reagent_labels")
        time.sleep(5)

    def print_task_number_labels(self, task_id: int) -> None:
        """
        功能:
            根据实验方案文件打印两组编号标签:
            - 反应管标签: R{task_id}-1, R{task_id}-2, ..., R{task_id}-N
            - 检测样品标签: S{task_id}-1, S{task_id}-2, ..., S{task_id}-M
            N 由实验编号最大值决定, M 由闪滤实验编号决定(空=全部).
        参数:
            task_id: int, 任务ID.
        """
        logger.info("虚假执行 print_task_number_labels, task_id=%s", task_id)
        time.sleep(5)

    @staticmethod
    def _parse_number_range(range_str: str) -> List[int]:
        """
        功能:
            解析数字范围字符串, 支持逗号分隔和连字符范围.
            例: "1,3,5" -> [1,3,5], "1-6" -> [1,2,3,4,5,6], "1-3,5,7-9" -> [1,2,3,5,7,8,9]
        参数:
            range_str: str, 数字范围字符串.
        返回:
            List[int], 排序后的编号列表.
        """
        logger.info("虚假执行 _parse_number_range, range_str=%s", range_str)
        time.sleep(5)
        return []

    # ---------- 5. 执行任务 ----------
    def device_init(self, device_id=None, *, poll_interval_s: float = 1.0, timeout_s: float = 600.0):
        logger.info("虚假执行 device_init, device_id=%s, poll_interval_s=%s, timeout_s=%s", device_id, poll_interval_s, timeout_s)
        time.sleep(5)
        return {"success": True, "device_id": device_id, "message": "虚假设备初始化完成"}

    def start_task(self, task_id: int | None = None, *, check_glovebox_env: bool = True, water_limit_ppm: float = 10.0, oxygen_limit_ppm: float = 10.0):
        logger.info("虚假执行 start_task, task_id=%s, check_glovebox_env=%s, water_limit_ppm=%s, oxygen_limit_ppm=%s", task_id, check_glovebox_env, water_limit_ppm, oxygen_limit_ppm)
        time.sleep(5)
        return {"success": True, "task_id": task_id, "message": "虚假任务启动完成"}

    def wait_task_with_ops(self, task_id: int | None = None, *, poll_interval_s: float = 2.0) -> int:
        logger.info("虚假执行 wait_task_with_ops, task_id=%s, poll_interval_s=%s", task_id, poll_interval_s)
        time.sleep(5)
        return 0

    def export_task_report(self, task_id: int, file_type: str = "excel") -> Path:
        logger.info("虚假执行 export_task_report, task_id=%s, file_type=%s", task_id, file_type)
        time.sleep(5)
        return MODULE_ROOT / "data" / "fake_report.xlsx"

    # ---------- 6. 下料动作 ----------
    def batch_out_task_and_empty_trays(self, task_id: int | None = None, *, poll_interval_s: float = 1.0, ignore_missing: bool = True, timeout_s: float = 900.0, move_type: str = "main_out"):
        logger.info("虚假执行 batch_out_task_and_empty_trays, task_id=%s, poll_interval_s=%s, ignore_missing=%s, timeout_s=%s, move_type=%s", task_id, poll_interval_s, ignore_missing, timeout_s, move_type)
        time.sleep(5)
        return {"success": True, "message": "虚假任务盘和空盘出盘完成"}

    def batch_out_task_and_chemical_trays(self, task_id: int | None = None, *, poll_interval_s: float = 1.0, ignore_missing: bool = True, timeout_s: float = 900.0, move_type: str = "main_out"):
        logger.info("虚假执行 batch_out_task_and_chemical_trays, task_id=%s, poll_interval_s=%s, ignore_missing=%s, timeout_s=%s, move_type=%s", task_id, poll_interval_s, ignore_missing, timeout_s, move_type)
        time.sleep(5)
        return {"success": True, "message": "虚假任务盘和化学品盘出盘完成"}

    def batch_out_task_trays(self, task_id: int | None = None, *, poll_interval_s: float = 1.0, ignore_missing: bool = True, timeout_s: float = 900.0, move_type: str = "main_out"):
        logger.info("虚假执行 batch_out_task_trays, task_id=%s, poll_interval_s=%s, ignore_missing=%s, timeout_s=%s, move_type=%s", task_id, poll_interval_s, ignore_missing, timeout_s, move_type)
        time.sleep(5)
        return {"success": True, "message": "虚假任务盘出盘完成"}

    def batch_out_empty_trays(self, *, poll_interval_s: float = 1.0, ignore_missing: bool = True, timeout_s: float = 900.0, move_type: str = "main_out"):
        logger.info("虚假执行 batch_out_empty_trays, poll_interval_s=%s, ignore_missing=%s, timeout_s=%s, move_type=%s", poll_interval_s, ignore_missing, timeout_s, move_type)
        time.sleep(5)
        return {"success": True, "message": "虚假空盘出盘完成"}

    def batch_out_tray(self, layout_list: list[dict], move_type: str = "main_out", *, task_id: int = None, poll_interval_s: float = 1.0, timeout_s: float = 900.0):
        logger.info("虚假执行 batch_out_tray, layout_list=%s, move_type=%s, task_id=%s, poll_interval_s=%s, timeout_s=%s", layout_list, move_type, task_id, poll_interval_s, timeout_s)
        time.sleep(5)
        return {"success": True, "message": "虚假按布局出盘完成"}

    def auto_unload_trays_to_agv(self, batch_out_file: Optional[str] = None, *, block: bool = True, auto_run_analysis: bool = True):
        logger.info("虚假执行 auto_unload_trays_to_agv, batch_out_file=%s, block=%s, auto_run_analysis=%s", batch_out_file, block, auto_run_analysis)
        time.sleep(5)
        return {"success": True, "message": "虚假自动卸盘完成"}

    # ---------- 7. Unilab 接口（待修改） ----------
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
            duration: str, 反应时间, 必须带单位, 如 "8h" 或 "30min".
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
        logger.info("虚假执行 submit_experiment_task, chemical_db_path=%s, task_name=%s", chemical_db_path, task_name)
        time.sleep(5)
        return "fake_task_id"

    # ---------- 分析站对接 ----------
    def run_analysis(self, task_id: Optional[str] = None) -> Dict:
        """
        功能:
            读取指定合成任务(或最新任务)的 xlsx 配置, 自动生成分析任务 CSV
            并通过 AnalysisStationController 提交至对应仪器(当前已实现 GC_MS).
        参数:
            task_id: 合成任务 ID 字符串, 为 None 时自动选取编号最大的最近任务.
        返回:
            Dict: 各仪器提交结果, 格式示例:
                {
                    "gc_ms":      {"success": bool, "return_info": str},
                    "uplc_qtof":  {"success": bool, "return_info": str},
                    "hplc":       {"success": bool, "return_info": str},
                }
        """
        logger.info("虚假执行 run_analysis, task_id=%s", task_id)
        time.sleep(5)
        return {
            "gc_ms": {"success": True, "return_info": "虚假 GC-MS 完成"},
            "uplc_qtof": {"success": True, "return_info": "虚假 UPLC-QTOF 完成"},
            "hplc": {"success": True, "return_info": "虚假 HPLC 完成"},
        }

    def poll_analysis_run(
        self, task_id: Optional[str] = None, poll_interval: float = 30.0
    ) -> Dict:
        """
        功能:
            轮询 GC-MS 分析任务运行状态, 完成后自动触发结果处理(积分+定性+报告).
            内部委托给 AnalysisStationController.poll_analysis_run 执行.
        参数:
            task_id: 合成任务 ID 字符串, 为 None 时自动选取编号最大的最近任务.
            poll_interval: 轮询间隔(秒), 默认 30 秒.
        返回:
            Dict: process_gc_ms_results 的返回值, 包含 success/return_info/report_path.
        """
        logger.info("虚假执行 poll_analysis_run, task_id=%s, poll_interval=%s", task_id, poll_interval)
        time.sleep(5)
        return {"success": True, "return_info": "虚假分析轮询完成", "report_path": ""}
