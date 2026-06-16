# -*- coding: utf-8 -*-
"""
功能:
    验证TSC标签打印作业的会话生命周期.
    确保单次作业结束后立即关闭端口, 不会等到脚本退出时才提交打印任务.
"""

import unittest
from unittest.mock import MagicMock, call, patch

from eit_synthesis_station.printer.label_print_service import LabelPrintService
from eit_synthesis_station.printer.print_text import execute_print_job


SAMPLE_CONFIG = {
    "printer": {
        "port": "Test Printer",
        "ppi": 300,
    },
    "paper": {
        "width": 56,
        "height": 10,
        "unit": "mm",
        "columns": 2,
        "column_gap": 3,
        "margin": 1.8,
        "gap": 0,
        "gap_offset": 0,
        "direction": 1,
    },
    "font": {
        "name": "Arial",
        "size": 60,
        "bold": 0,
        "underline": 0,
        "rotation": 0,
    },
    "position": {
        "x": 10,
        "y": 30,
    },
}


class FakeTscLib:
    """
    功能:
        用于测试的TSC动态库桩对象.
        记录端口开关和打印调用次数, 避免依赖真实硬件.
    """

    def __init__(self):
        self.openportW = MagicMock(return_value=1)
        self.sendcommandW = MagicMock()
        self.windowsfontUnicode = MagicMock(return_value=1)
        self.printlabelW = MagicMock()
        self.closeport = MagicMock()


class TestPrintJobSession(unittest.TestCase):
    """
    功能:
        验证底层打印作业会在单次调用内完成提交.
    """

    def test_execute_print_job_closes_port_after_print(self):
        """
        功能:
            验证单次打印完成后会立即关闭端口.
        """
        lib = FakeTscLib()

        execute_print_job(lib, SAMPLE_CONFIG, ["123", "456"])

        self.assertEqual(lib.openportW.call_count, 1)
        self.assertEqual(lib.printlabelW.call_count, 1)
        self.assertEqual(lib.closeport.call_count, 1)

    @patch("eit_synthesis_station.printer.print_text.print_text", side_effect=RuntimeError("boom"))
    def test_execute_print_job_closes_port_when_print_failed(self, mock_print_text):
        """
        功能:
            验证打印异常时仍会关闭端口, 避免下次作业继续挂起.
        """
        lib = FakeTscLib()

        with self.assertRaises(RuntimeError):
            execute_print_job(lib, SAMPLE_CONFIG, ["123", "456"])

        mock_print_text.assert_called_once_with(lib, SAMPLE_CONFIG, ["123", "456"])
        self.assertEqual(lib.openportW.call_count, 1)
        self.assertEqual(lib.closeport.call_count, 1)


class TestLabelPrintService(unittest.TestCase):
    """
    功能:
        验证服务层按单次作业提交打印任务.
    """

    @patch("eit_synthesis_station.printer.label_print_service.load_config", return_value=SAMPLE_CONFIG)
    @patch("eit_synthesis_station.printer.label_print_service.load_dll")
    @patch("eit_synthesis_station.printer.label_print_service.check_printer_ready")
    @patch("eit_synthesis_station.printer.label_print_service.execute_print_job")
    def test_print_label_uses_independent_job_per_copy(
        self,
        mock_execute_print_job,
        mock_check_printer_ready,
        mock_load_dll,
        mock_load_config,
    ):
        """
        功能:
            验证服务层每份标签都会单独提交一次打印作业.
        """
        fake_lib = object()
        mock_load_dll.return_value = fake_lib
        service = LabelPrintService("dummy.yaml")

        result = service.print_label(["123", "456"], copies=2)

        self.assertTrue(result)
        mock_load_config.assert_called_once_with("dummy.yaml")
        mock_check_printer_ready.assert_called_once_with(fake_lib, SAMPLE_CONFIG)
        self.assertEqual(mock_execute_print_job.call_count, 2)
        mock_execute_print_job.assert_has_calls(
            [
                call(fake_lib, SAMPLE_CONFIG, ["123", "456"]),
                call(fake_lib, SAMPLE_CONFIG, ["123", "456"]),
            ]
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
