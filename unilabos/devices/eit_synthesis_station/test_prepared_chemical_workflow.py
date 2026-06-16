# -*- coding: utf-8 -*-
"""
功能:
验证溶液与 beads 派生条目流程的关键回归场景.
参数:
无.
返回:
无.
"""

import importlib
import io
import logging
import shutil
import sys
import types
import unittest
import uuid
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import openpyxl

from eit_synthesis_station.chem_tools.chemical_append_utils import (
    build_duplicate_check_specs,
    build_prepared_chemical_row_data,
)
from eit_synthesis_station.controller.station_controller import SynthesisStationController
from eit_synthesis_station.manager.station_manager import SynthesisStationManager


CHEMICAL_HEADERS = [
    "cas_number",
    "chemical_id",
    "substance_english_name",
    "substance",
    "other_name",
    "brand",
    "package_size",
    "storage_location",
    "molecular_weight",
    "density (g/mL)",
    "physical_state",
    "physical_form",
    "active_content(mol/L or wt%)",
]


class PreparedChemicalWorkflowTestCase(unittest.TestCase):
    """
    功能:
    覆盖派生条目 builder, manager, CLI 与校验逻辑.
    参数:
    无.
    返回:
    无.
    """

    @staticmethod
    def _get_workspace_temp_root() -> Path:
        """
        功能:
        返回位于工作区内的临时目录根路径.
        参数:
        无.
        返回:
        Path, 可写入的临时目录根路径.
        """
        temp_root = Path(__file__).resolve().parent / "_tmp"
        temp_root.mkdir(parents=True, exist_ok=True)
        return temp_root

    def _make_workspace_case_dir(self) -> Path:
        """
        功能:
        在工作区内创建唯一测试目录.
        参数:
        无.
        返回:
        Path, 新建的测试目录路径.
        """
        case_dir = self._get_workspace_temp_root() / f"case_{uuid.uuid4().hex}"
        case_dir.mkdir(parents=True, exist_ok=False)
        return case_dir

    @staticmethod
    def _create_manager() -> SynthesisStationManager:
        """
        功能:
        创建不执行完整初始化的管理器实例, 仅用于单元测试.
        参数:
        无.
        返回:
        SynthesisStationManager, 可直接调用目标方法的实例.
        """
        manager = SynthesisStationManager.__new__(SynthesisStationManager)
        manager._logger = logging.getLogger("TestPreparedChemicalWorkflow.Manager")
        return manager

    @staticmethod
    def _create_chemical_workbook(file_path: Path, rows: list[list[object]]) -> None:
        """
        功能:
        创建最小可用的 chemical_list.xlsx 测试文件.
        参数:
        file_path: Path, 目标文件路径.
        rows: list[list[object]], 数据行列表.
        返回:
        无.
        """
        workbook = openpyxl.Workbook()
        worksheet = workbook.active
        worksheet.append(CHEMICAL_HEADERS)
        for row in rows:
            worksheet.append(row)
        workbook.save(file_path)
        workbook.close()

    @staticmethod
    def _load_main_with_stubs():
        """
        功能:
        以最小依赖加载 main 模块, 避免真实设备依赖影响测试.
        参数:
        无.
        返回:
        module, 导入后的 eit_synthesis_station.main 模块.
        """
        fake_station_manager = types.ModuleType("eit_synthesis_station.manager.station_manager")
        fake_station_manager.SynthesisStationManager = object

        fake_setting = types.ModuleType("eit_synthesis_station.config.setting")
        fake_setting.Settings = object

        fake_constants = types.ModuleType("eit_synthesis_station.config.constants")
        fake_constants.TaskStatus = SimpleNamespace(
            UNSTARTED=0,
            RUNNING=1,
            COMPLETED=2,
            PAUSED=3,
            FAILED=4,
            STOPPED=5,
            PAUSING=6,
            STOPPING=7,
            WAITING=8,
            HOLDING=9,
        )
        fake_constants.StationState = SimpleNamespace(
            IDLE=0,
            RUNNING=1,
            PAUSED=2,
            PAUSING=3,
            STOPPING=4,
            HOLDING=5,
        )

        with patch.dict(
            sys.modules,
            {
                "eit_synthesis_station.manager.station_manager": fake_station_manager,
                "eit_synthesis_station.config.setting": fake_setting,
                "eit_synthesis_station.config.constants": fake_constants,
            },
        ):
            sys.modules.pop("eit_synthesis_station.main", None)
            return importlib.import_module("eit_synthesis_station.main")

    def test_build_prepared_chemical_row_data_for_solution_keeps_parent_metadata(self) -> None:
        """
        功能:
        校验 solution 派生行会继承母体核心字段并生成规范名称.
        参数:
        无.
        返回:
        无.
        """
        row_data = build_prepared_chemical_row_data(
            base_row_data={
                "cas_number": "64-17-5",
                "substance_english_name": "ethanol",
                "substance": "乙醇",
                "substance_chinese_name": "乙醇",
                "molecular_weight": 46.07,
                "density (g/mL)": 0.7893,
            },
            prepared_form="solution",
            active_content=0.4,
            solvent_name="CH2Cl2",
        )

        self.assertEqual(row_data["cas_number"], "64-17-5")
        self.assertEqual(row_data["substance"], "乙醇 (溶液, 0.4 M in CH2Cl2)")
        self.assertEqual(row_data["physical_form"], "solution")
        self.assertEqual(row_data["density (g/mL)"], 0.7893)

    def test_build_prepared_chemical_row_data_for_beads_clears_density(self) -> None:
        """
        功能:
        校验 beads 派生行会清空密度并生成 wt% 名称.
        参数:
        无.
        返回:
        无.
        """
        row_data = build_prepared_chemical_row_data(
            base_row_data={
                "cas_number": "7681-65-4",
                "substance_english_name": "Cuprous iodide",
                "substance": "碘化亚铜",
                "substance_chinese_name": "碘化亚铜",
                "molecular_weight": 190.45,
                "density (g/mL)": 5.62,
            },
            prepared_form="beads",
            active_content=1.49,
        )

        self.assertEqual(row_data["substance"], "碘化亚铜 (beads, 1.49%)")
        self.assertEqual(row_data["physical_form"], "beads")
        self.assertEqual(row_data["density (g/mL)"], "")

    def test_build_duplicate_check_specs_for_prepared_row_only_checks_display_name(self) -> None:
        """
        功能:
        校验派生条目仅按展示名查重, 不再按 CAS 与英文名拦截.
        参数:
        无.
        返回:
        无.
        """
        duplicate_specs = build_duplicate_check_specs(
            {
                "cas_number": "64-17-5",
                "substance_english_name": "ethanol",
                "substance": "乙醇 (溶液, 1 M in MeCN)",
                "physical_form": "solution",
            }
        )
        self.assertEqual(
            duplicate_specs,
            [
                (("substance", "substance_chinese_name"), "乙醇 (溶液, 1 M in MeCN)", "中文名"),
            ],
        )

    def test_prepare_solution_or_beads_reuses_existing_neat_parent(self) -> None:
        """
        功能:
        验证母体 neat 已存在时, 可直接追加同 CAS 的 solution 条目.
        参数:
        无.
        返回:
        无.
        """
        case_dir = self._make_workspace_case_dir()
        try:
            workbook_path = case_dir / "chemical_list.xlsx"
            self._create_chemical_workbook(
                workbook_path,
                [
                    [
                        "64-17-5",
                        "",
                        "ethanol",
                        "乙醇",
                        "",
                        "",
                        "",
                        "",
                        46.07,
                        0.7893,
                        "liquid",
                        "neat",
                        "",
                    ],
                ],
            )
            manager = self._create_manager()

            result = manager.prepare_solution_or_beads(
                "64-17-5",
                "solution",
                solvent_name="CH2Cl2",
                active_content=0.4,
                target_volume_ml=10,
                excel_path=str(workbook_path),
            )

            self.assertIsNotNone(result)
            self.assertEqual(result["base_created"], False)
            self.assertEqual(result["derived_row_index"], 3)
            self.assertAlmostEqual(result["recipe"]["solute_mass_g"], 0.18428, places=8)
        finally:
            shutil.rmtree(case_dir, ignore_errors=True)

    def test_prepare_solution_or_beads_appends_missing_parent_for_cas(self) -> None:
        """
        功能:
        验证 CAS 路径在母体缺失时, 会先补母体 neat 再追加 beads 条目.
        参数:
        无.
        返回:
        无.
        """
        case_dir = self._make_workspace_case_dir()
        try:
            workbook_path = case_dir / "chemical_list.xlsx"
            self._create_chemical_workbook(workbook_path, [])
            manager = self._create_manager()

            def _fake_lookup_and_append_chemical(query, excel_path=None):
                row_data = {
                    "cas_number": "7681-65-4",
                    "chemical_id": "",
                    "substance_english_name": "Cuprous iodide",
                    "substance": "碘化亚铜",
                    "substance_chinese_name": "碘化亚铜",
                    "other_name": "",
                    "brand": "",
                    "package_size": "",
                    "storage_location": "",
                    "molecular_weight": 190.45,
                    "density (g/mL)": "",
                    "physical_state": "solid",
                    "physical_form": "neat",
                    "active_content(mol/L or wt%)": "",
                }
                row_index = manager._append_chemical_row_to_excel(row_data=row_data, excel_path=excel_path)
                return {
                    "row_data": row_data,
                    "row_index": row_index,
                    "chemicalbook_status": "",
                    "chemicalbook_record_path": "",
                }

            manager.lookup_and_append_chemical = _fake_lookup_and_append_chemical

            result = manager.prepare_solution_or_beads(
                "7681-65-4",
                "beads",
                active_content=1.49,
                target_active_mmol=0.5,
                excel_path=str(workbook_path),
            )

            self.assertIsNotNone(result)
            self.assertEqual(result["base_created"], True)
            self.assertEqual(result["base_row_index"], 2)
            self.assertEqual(result["derived_row_index"], 3)
        finally:
            shutil.rmtree(case_dir, ignore_errors=True)

    def test_prepare_solution_or_beads_appends_missing_parent_for_smiles(self) -> None:
        """
        功能:
        验证 SMILES 路径在母体缺失时, 会先补母体 neat 再追加 solution 条目.
        参数:
        无.
        返回:
        无.
        """
        case_dir = self._make_workspace_case_dir()
        try:
            workbook_path = case_dir / "chemical_list.xlsx"
            self._create_chemical_workbook(workbook_path, [])
            manager = self._create_manager()

            def _fake_lookup_and_append_by_smiles(smiles, excel_path=None):
                row_data = {
                    "cas_number": "64-17-5",
                    "chemical_id": "",
                    "substance_english_name": "ethanol",
                    "substance": "乙醇",
                    "substance_chinese_name": "乙醇",
                    "other_name": "",
                    "brand": "",
                    "package_size": "",
                    "storage_location": "",
                    "molecular_weight": 46.07,
                    "density (g/mL)": 0.7893,
                    "physical_state": "liquid",
                    "physical_form": "neat",
                    "active_content(mol/L or wt%)": "",
                }
                row_index = manager._append_chemical_row_to_excel(row_data=row_data, excel_path=excel_path)
                return {
                    "row_data": row_data,
                    "row_index": row_index,
                    "chemicalbook_status": "",
                    "chemicalbook_record_path": "",
                }

            manager.lookup_and_append_chemical_by_smiles = _fake_lookup_and_append_by_smiles

            with patch(
                "eit_synthesis_station.chem_tools.chemical_lookup.lookup_chemical_by_smiles",
                return_value=SimpleNamespace(
                    cas_number="64-17-5",
                    substance_english_name="ethanol",
                    substance="乙醇",
                ),
            ):
                result = manager.prepare_solution_or_beads(
                    "CCO",
                    "solution",
                    solvent_name="MeCN",
                    active_content=1.0,
                    target_volume_ml=5,
                    excel_path=str(workbook_path),
                )

            self.assertIsNotNone(result)
            self.assertEqual(result["base_created"], True)
            self.assertEqual(result["base_row_index"], 2)
            self.assertEqual(result["derived_row_index"], 3)
        finally:
            shutil.rmtree(case_dir, ignore_errors=True)

    def test_check_chemical_library_data_allows_prepared_rows_share_english_name(self) -> None:
        """
        功能:
        验证 solution 与 beads 可与 neat 共享英文名而不触发重复错误.
        参数:
        无.
        返回:
        无.
        """
        controller = SynthesisStationController.__new__(SynthesisStationController)
        controller._logger = logging.getLogger("TestPreparedChemicalWorkflow.Controller")

        headers = [
            "cas_number",
            "chemical_id",
            "substance_english_name",
            "substance",
            "molecular_weight",
            "density (g/mL)",
            "physical_state",
            "physical_form",
            "active_content(mol/L or wt%)",
        ]
        rows = [
            {
                "cas_number": "64-17-5",
                "chemical_id": "",
                "substance_english_name": "ethanol",
                "substance": "乙醇",
                "molecular_weight": 46.07,
                "density (g/mL)": 0.7893,
                "physical_state": "liquid",
                "physical_form": "neat",
                "active_content(mol/L or wt%)": "",
            },
            {
                "cas_number": "64-17-5",
                "chemical_id": "",
                "substance_english_name": "ethanol",
                "substance": "乙醇 (溶液, 1 M in MeCN)",
                "molecular_weight": 46.07,
                "density (g/mL)": 0.7893,
                "physical_state": "liquid",
                "physical_form": "solution",
                "active_content(mol/L or wt%)": 1,
            },
            {
                "cas_number": "64-17-5",
                "chemical_id": "",
                "substance_english_name": "ethanol",
                "substance": "乙醇 (beads, 1.5%)",
                "molecular_weight": 46.07,
                "density (g/mL)": "",
                "physical_state": "solid",
                "physical_form": "beads",
                "active_content(mol/L or wt%)": 1.5,
            },
        ]

        result = controller.check_chemical_library_data(rows, headers)

        self.assertEqual(result["errors"], [])
        self.assertEqual(result["warnings"], [])

    def test_menu_chemical_library_prints_prepared_summary(self) -> None:
        """
        功能:
        验证新增派生条目菜单会输出母体来源, 派生行号与配制结果摘要.
        参数:
        无.
        返回:
        无.
        """
        main = self._load_main_with_stubs()
        manager = SimpleNamespace(prepare_solution_or_beads=object())
        result = {
            "base_row_data": {
                "base_substance": "乙醇",
            },
            "base_row_index": 2105,
            "base_created": False,
            "derived_row_data": {
                "substance": "乙醇 (溶液, 0.4 M in MeCN)",
            },
            "derived_row_index": 2106,
            "recipe": {
                "instruction_text": "称取/加入溶质 0.18428 g, 用 MeCN 溶解后定容至 10 mL",
                "solute_volume_ml": 0.233469,
            },
        }
        input_values = iter(["7", "64-17-5", "solution", "MeCN", "0.4", "10", "0"])

        with patch.object(main, "_print_menu"):
            with patch.object(main, "_pause"):
                with patch.object(main, "_safe_run", return_value=result):
                    with patch("builtins.input", side_effect=lambda _prompt="": next(input_values)):
                        captured_stdout = io.StringIO()
                        with redirect_stdout(captured_stdout):
                            main._menu_chemical_library(manager)

        output_text = captured_stdout.getvalue()
        self.assertIn("母体条目: 名称=乙醇, 来源=复用现有母体", output_text)
        self.assertIn("已成功添加派生条目: 名称=乙醇 (溶液, 0.4 M in MeCN), 行号=2106", output_text)
        self.assertIn("配制结果: 称取/加入溶质 0.18428 g, 用 MeCN 溶解后定容至 10 mL", output_text)


if __name__ == "__main__":
    unittest.main()
