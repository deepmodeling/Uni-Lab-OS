"""通过真实 Git blob 验证 Windows CI 的脚本导出边界。"""

import codecs
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

EXPORTER = Path(__file__).resolve().parents[2] / "ci" / "export-control-scripts.py"
PYTHON_SCRIPTS = (
    "upload-oss.py",
    "prepare-release-version.py",
    "check-oss-release.py",
    "notify-feishu-release.py",
    "oss-public-url.py",
)


class TestExportControlScripts(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repo = Path(self.directory.name)
        (self.repo / "ci").mkdir()
        self.git("init", "-q")
        self.git("config", "core.autocrlf", "false")
        self.sources = {
            "build-runtime-windows.ps1": "# 中文注释。\nparam()\nWrite-Output '构建'\n".encode(),
            **{
                name: 'def main():\n    # 本地包。\n    for name in ("包",):\n        path = name\n'.encode()
                for name in PYTHON_SCRIPTS
            },
        }

    def git(self, *args: str) -> bytes:
        return subprocess.check_output(["git", "-C", str(self.repo), *args], stderr=subprocess.STDOUT)

    def commit_sources(self) -> None:
        for name, content in self.sources.items():
            (self.repo / "ci" / name).write_bytes(content)
        self.git("add", "ci")
        self.git("-c", "user.name=CI Test", "-c", "user.email=ci@example.invalid", "commit", "-qm", "fixture")

    def export(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(EXPORTER), "--repo", str(self.repo), "--ref", "HEAD"],
            capture_output=True,
        )

    def test_preserves_python_bytes_and_adds_powershell_bom(self) -> None:
        self.commit_sources()
        result = self.export()
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in PYTHON_SCRIPTS:
            actual = (self.repo / "ci" / name).read_bytes()
            self.assertEqual(actual, self.sources[name])
            compile(actual, name, "exec")
        self.assertEqual(
            (self.repo / "ci" / "build-runtime-windows.ps1").read_bytes(),
            codecs.BOM_UTF8 + self.sources["build-runtime-windows.ps1"],
        )

    def test_preserves_existing_bom_and_crlf(self) -> None:
        self.sources = {name: content.replace(b"\n", b"\r\n") for name, content in self.sources.items()}
        self.sources["build-runtime-windows.ps1"] = codecs.BOM_UTF8 + self.sources["build-runtime-windows.ps1"]
        self.commit_sources()
        self.assertEqual(self.export().returncode, 0)
        for name, content in self.sources.items():
            self.assertEqual((self.repo / "ci" / name).read_bytes(), content)

    def test_invalid_python_fails_before_overwriting_scripts(self) -> None:
        self.sources["prepare-release-version.py"] = b"def main():\n        pass\n    pass\n"
        self.commit_sources()
        path = self.repo / "ci" / "build-runtime-windows.ps1"
        path.write_bytes(b"workspace sentinel")
        result = self.export()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"IndentationError", result.stderr)
        self.assertEqual(path.read_bytes(), b"workspace sentinel")

    def test_missing_blob_fails_before_overwriting_scripts(self) -> None:
        del self.sources["oss-public-url.py"]
        self.commit_sources()
        path = self.repo / "ci" / "build-runtime-windows.ps1"
        path.write_bytes(b"workspace sentinel")
        self.assertNotEqual(self.export().returncode, 0)
        self.assertEqual(path.read_bytes(), b"workspace sentinel")


if __name__ == "__main__":
    unittest.main()
