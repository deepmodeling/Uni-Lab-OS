"""model_upload.py 单元测试（upload_device_model / download_model_from_oss / XOR 加解密）"""

import unittest
import tempfile
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from unilabos.app.model_upload import (
    upload_device_model,
    download_model_from_oss,
    _MODEL_EXTENSIONS,
    _MESH_ENCRYPT_EXTENSIONS,
    _xor_transform,
)


class TestUploadDeviceModel(unittest.TestCase):
    """测试本地模型文件上传到 OSS"""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.mock_client = MagicMock()

    def _create_model_files(self, subdir: str, filenames: list[str]):
        """在临时目录中创建设备模型文件"""
        model_dir = Path(self.tmp_dir) / "devices" / subdir
        model_dir.mkdir(parents=True, exist_ok=True)
        for name in filenames:
            p = model_dir / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("dummy content")
        return model_dir

    @patch("unilabos.app.model_upload._MESH_BASE_DIR")
    def test_upload_success(self, mock_base):
        """正常上传流程"""
        mock_base.__truediv__ = lambda self, x: Path(self.tmp_dir) / x
        # 直接 patch _MESH_BASE_DIR 为 Path(tmp_dir)
        with patch("unilabos.app.model_upload._MESH_BASE_DIR", Path(self.tmp_dir)):
            self._create_model_files("arm_slider", ["macro_device.xacro", "meshes/link1.stl"])

            self.mock_client.get_model_upload_urls.return_value = {
                "files": [
                    {"name": "macro_device.xacro", "upload_url": "https://oss.example.com/put1"},
                    {"name": "meshes/link1.stl", "upload_url": "https://oss.example.com/put2"},
                ]
            }
            self.mock_client.publish_model.return_value = {
                "path": "https://oss.example.com/arm_slider/macro_device.xacro"
            }

            with patch("unilabos.app.model_upload._put_upload") as mock_put:
                result = upload_device_model(
                    http_client=self.mock_client,
                    template_uuid="test-uuid",
                    mesh_name="arm_slider",
                    model_type="device",
                    version="1.0.0",
                )

            self.assertEqual(result, "https://oss.example.com/arm_slider/macro_device.xacro")
            self.mock_client.get_model_upload_urls.assert_called_once()
            self.mock_client.publish_model.assert_called_once()

    @patch("unilabos.app.model_upload._MESH_BASE_DIR")
    def test_upload_dir_not_exists(self, mock_base):
        """本地目录不存在时返回 None"""
        with patch("unilabos.app.model_upload._MESH_BASE_DIR", Path(self.tmp_dir)):
            result = upload_device_model(
                http_client=self.mock_client,
                template_uuid="test-uuid",
                mesh_name="nonexistent",
                model_type="device",
            )
        self.assertIsNone(result)

    @patch("unilabos.app.model_upload._MESH_BASE_DIR")
    def test_upload_no_valid_files(self, mock_base):
        """目录中无有效模型文件时返回 None"""
        with patch("unilabos.app.model_upload._MESH_BASE_DIR", Path(self.tmp_dir)):
            model_dir = Path(self.tmp_dir) / "devices" / "empty_model"
            model_dir.mkdir(parents=True, exist_ok=True)
            (model_dir / "readme.txt").write_text("not a model")

            result = upload_device_model(
                http_client=self.mock_client,
                template_uuid="test-uuid",
                mesh_name="empty_model",
                model_type="device",
            )
        self.assertIsNone(result)

    @patch("unilabos.app.model_upload._MESH_BASE_DIR")
    def test_upload_urls_failure(self, mock_base):
        """获取上传 URL 失败时返回 None"""
        with patch("unilabos.app.model_upload._MESH_BASE_DIR", Path(self.tmp_dir)):
            self._create_model_files("arm", ["device.xacro"])
            self.mock_client.get_model_upload_urls.return_value = None

            result = upload_device_model(
                http_client=self.mock_client,
                template_uuid="test-uuid",
                mesh_name="arm",
                model_type="device",
            )
        self.assertIsNone(result)


class TestDownloadModelFromOss(unittest.TestCase):
    """测试从 OSS 下载模型文件到本地"""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()

    def test_skip_no_mesh_name(self):
        """缺少 mesh 名称时跳过"""
        result = download_model_from_oss({"type": "device", "path": "https://x.com/a.xacro"})
        self.assertFalse(result)

    def test_skip_no_oss_path(self):
        """缺少 OSS path 时跳过"""
        result = download_model_from_oss({"mesh": "arm", "type": "device"})
        self.assertFalse(result)

    def test_skip_local_path(self):
        """非 https:// 路径时跳过"""
        result = download_model_from_oss({
            "mesh": "arm",
            "type": "device",
            "path": "file:///local/model.xacro",
        })
        self.assertFalse(result)

    def test_already_exists(self):
        """本地已有文件时跳过下载"""
        device_dir = Path(self.tmp_dir) / "devices" / "arm"
        device_dir.mkdir(parents=True, exist_ok=True)
        (device_dir / "model.xacro").write_text("existing")

        result = download_model_from_oss(
            {"mesh": "arm", "type": "device", "path": "https://oss.example.com/model.xacro"},
            mesh_base_dir=Path(self.tmp_dir),
        )
        self.assertTrue(result)

    @patch("unilabos.app.model_upload._download_file")
    def test_download_device(self, mock_download):
        """下载 device 模型到 devices/ 目录"""
        result = download_model_from_oss(
            {"mesh": "new_arm", "type": "device", "path": "https://oss.example.com/new_arm/macro_device.xacro"},
            mesh_base_dir=Path(self.tmp_dir),
        )
        self.assertTrue(result)
        mock_download.assert_called_once()
        call_args = mock_download.call_args
        self.assertIn("macro_device.xacro", str(call_args[0][1]))

    @patch("unilabos.app.model_upload._download_file")
    def test_download_resource(self, mock_download):
        """下载 resource 模型到 resources/ 目录"""
        result = download_model_from_oss(
            {
                "mesh": "plate_96/meshes/plate_96.stl",
                "type": "resource",
                "path": "https://oss.example.com/plate_96/modal.xacro",
            },
            mesh_base_dir=Path(self.tmp_dir),
        )
        self.assertTrue(result)
        target_dir = Path(self.tmp_dir) / "resources" / "plate_96"
        self.assertTrue(target_dir.exists())

    @patch("unilabos.app.model_upload._download_file")
    def test_download_with_children_mesh(self, mock_download):
        """下载包含 children_mesh 的模型"""
        result = download_model_from_oss(
            {
                "mesh": "tip_rack",
                "type": "device",
                "path": "https://oss.example.com/tip_rack/model.xacro",
                "children_mesh": {
                    "path": "https://oss.example.com/tip_rack/meshes/tip.stl",
                    "format": "stl",
                },
            },
            mesh_base_dir=Path(self.tmp_dir),
        )
        self.assertTrue(result)
        # 应调用两次：入口文件 + children_mesh
        self.assertEqual(mock_download.call_count, 2)

    @patch("unilabos.app.model_upload._download_file", side_effect=Exception("network error"))
    def test_download_failure_graceful(self, mock_download):
        """下载失败时返回 False（不抛异常）"""
        result = download_model_from_oss(
            {"mesh": "broken", "type": "device", "path": "https://oss.example.com/broken.xacro"},
            mesh_base_dir=Path(self.tmp_dir),
        )
        self.assertFalse(result)


class TestModelExtensions(unittest.TestCase):
    """测试支持的模型文件后缀集合"""

    def test_standard_extensions(self):
        """确认标准 3D 格式在支持列表中"""
        expected = {".stl", ".gltf", ".glb", ".xacro", ".urdf", ".obj", ".dae"}
        for ext in expected:
            self.assertIn(ext, _MODEL_EXTENSIONS, f"{ext} should be supported")

    def test_non_model_excluded(self):
        """非模型文件后缀不在列表中"""
        excluded = {".txt", ".json", ".py", ".png", ".jpg"}
        for ext in excluded:
            self.assertNotIn(ext, _MODEL_EXTENSIONS, f"{ext} should not be supported")


class TestXorTransform(unittest.TestCase):
    """XOR 加密/解密核心函数测试。"""

    def test_roundtrip_symmetry(self):
        """XOR 加密后再解密恢复原始数据（对称性）。"""
        original = b"Hello, this is a test model file content."
        encrypted = _xor_transform(original)
        self.assertNotEqual(encrypted, original)
        decrypted = _xor_transform(encrypted)
        self.assertEqual(decrypted, original)

    def test_empty_data(self):
        """空数据加密后仍为空。"""
        result = _xor_transform(b"")
        self.assertEqual(result, b"")

    def test_single_byte(self):
        """单字节数据正确加解密。"""
        original = b"\xff"
        encrypted = _xor_transform(original)
        decrypted = _xor_transform(encrypted)
        self.assertEqual(decrypted, original)

    def test_data_longer_than_key(self):
        """超过密钥长度（32 字节）的数据正确循环 XOR。"""
        original = bytes(range(256)) * 2  # 512 字节
        encrypted = _xor_transform(original)
        self.assertNotEqual(encrypted, original)
        decrypted = _xor_transform(encrypted)
        self.assertEqual(decrypted, original)

    def test_data_exactly_key_length(self):
        """恰好 32 字节（密钥长度）的数据正确处理。"""
        original = bytes(range(32))
        encrypted = _xor_transform(original)
        decrypted = _xor_transform(encrypted)
        self.assertEqual(decrypted, original)

    def test_all_zeros_produces_key(self):
        """全零数据 XOR 后结果应为密钥本身。"""
        zeros = b"\x00" * 32
        result = _xor_transform(zeros)
        key = os.environ.get(
            "UNILAB_MESH_XOR_KEY", "unilab3d-model-protection-key-v1"
        ).encode()
        self.assertEqual(result, key)

    def test_custom_key(self):
        """自定义密钥正确加解密。"""
        custom_key = b"custom-key-12345"
        original = b"test data for custom key"
        encrypted = _xor_transform(original, key=custom_key)
        decrypted = _xor_transform(encrypted, key=custom_key)
        self.assertEqual(decrypted, original)

    def test_different_keys_produce_different_results(self):
        """不同密钥产生不同加密结果。"""
        data = b"same data"
        key1 = b"key-one-is-here!"
        key2 = b"key-two-is-here!"
        self.assertNotEqual(_xor_transform(data, key1), _xor_transform(data, key2))

    def test_binary_stl_header(self):
        """二进制内容（模拟 STL 文件头）正确加解密。"""
        stl_header = b"\x00" * 80 + b"\x03\x00\x00\x00"
        encrypted = _xor_transform(stl_header)
        decrypted = _xor_transform(encrypted)
        self.assertEqual(decrypted, stl_header)

    def test_large_data_roundtrip(self):
        """大数据（1MB）加解密正确性。"""
        original = os.urandom(1024 * 1024)
        encrypted = _xor_transform(original)
        decrypted = _xor_transform(encrypted)
        self.assertEqual(decrypted, original)

    def test_consistency_with_frontend_key(self):
        """验证 Python 端与前端使用相同的默认密钥。"""
        frontend_key = b"unilab3d-model-protection-key-v1"
        data = b"cross-platform test data"
        encrypted = _xor_transform(data, key=frontend_key)
        # 用默认密钥解密（应一致）
        decrypted = _xor_transform(encrypted)
        self.assertEqual(decrypted, data)


class TestEncryptExtensions(unittest.TestCase):
    """加密文件扩展名配置测试。"""

    def test_all_mesh_formats_in_encrypt_set(self):
        """所有 mesh 格式都在加密扩展名集合中。"""
        expected = {".stl", ".dae", ".obj", ".fbx", ".gltf", ".glb"}
        self.assertEqual(_MESH_ENCRYPT_EXTENSIONS, expected)

    def test_xml_formats_not_encrypted(self):
        """XACRO/URDF/YAML 文件不加密。"""
        for ext in {".xacro", ".urdf", ".yaml", ".yml"}:
            self.assertNotIn(ext, _MESH_ENCRYPT_EXTENSIONS)

    def test_encrypt_is_subset_of_model_extensions(self):
        """加密扩展名是模型扩展名的子集。"""
        self.assertTrue(_MESH_ENCRYPT_EXTENSIONS.issubset(_MODEL_EXTENSIONS))


class TestPutUploadEncryption(unittest.TestCase):
    """_put_upload 中的条件加密测试。"""

    @patch("unilabos.app.model_upload.requests.put")
    def test_stl_file_encrypted_before_upload(self, mock_put):
        """STL 文件上传前自动 XOR 加密。"""
        from unilabos.app.model_upload import _put_upload

        original_data = b"solid test\nfacet normal 0 0 1\n"
        with tempfile.NamedTemporaryFile(suffix=".stl", delete=False) as f:
            f.write(original_data)
            f.flush()
            tmp_path = Path(f.name)

        try:
            mock_put.return_value = MagicMock(status_code=200)
            mock_put.return_value.raise_for_status = MagicMock()
            _put_upload(tmp_path, "https://oss.example.com/upload")

            uploaded_data = mock_put.call_args.kwargs.get("data")
            self.assertIsNotNone(uploaded_data)
            self.assertNotEqual(uploaded_data, original_data)
            # 解密后应恢复原始数据
            self.assertEqual(_xor_transform(uploaded_data), original_data)
        finally:
            tmp_path.unlink(missing_ok=True)

    @patch("unilabos.app.model_upload.requests.put")
    def test_xacro_file_not_encrypted(self, mock_put):
        """XACRO 文件上传时不加密。"""
        from unilabos.app.model_upload import _put_upload

        original_data = b'<?xml version="1.0"?><robot></robot>'
        with tempfile.NamedTemporaryFile(suffix=".xacro", delete=False) as f:
            f.write(original_data)
            f.flush()
            tmp_path = Path(f.name)

        try:
            mock_put.return_value = MagicMock(status_code=200)
            mock_put.return_value.raise_for_status = MagicMock()
            _put_upload(tmp_path, "https://oss.example.com/upload")

            uploaded_data = mock_put.call_args.kwargs.get("data")
            self.assertEqual(uploaded_data, original_data)
        finally:
            tmp_path.unlink(missing_ok=True)

    @patch("unilabos.app.model_upload.requests.put")
    def test_all_mesh_formats_encrypted(self, mock_put):
        """所有 mesh 格式上传前都加密。"""
        from unilabos.app.model_upload import _put_upload

        original_data = b"test mesh binary data content"
        for ext in [".stl", ".dae", ".obj", ".fbx", ".gltf", ".glb"]:
            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as f:
                f.write(original_data)
                f.flush()
                tmp_path = Path(f.name)
            try:
                mock_put.reset_mock()
                mock_put.return_value = MagicMock(status_code=200)
                mock_put.return_value.raise_for_status = MagicMock()
                _put_upload(tmp_path, "https://oss.example.com/upload")

                uploaded_data = mock_put.call_args.kwargs.get("data")
                self.assertNotEqual(uploaded_data, original_data, f"{ext} 文件应被加密")
            finally:
                tmp_path.unlink(missing_ok=True)

    @patch("unilabos.app.model_upload.requests.put")
    def test_uppercase_extension_encrypted(self, mock_put):
        """大写扩展名 .STL 也被加密（大小写不敏感）。"""
        from unilabos.app.model_upload import _put_upload

        original_data = b"uppercase ext test"
        with tempfile.NamedTemporaryFile(suffix=".STL", delete=False) as f:
            f.write(original_data)
            f.flush()
            tmp_path = Path(f.name)
        try:
            mock_put.return_value = MagicMock(status_code=200)
            mock_put.return_value.raise_for_status = MagicMock()
            _put_upload(tmp_path, "https://oss.example.com/upload")

            uploaded_data = mock_put.call_args.kwargs.get("data")
            self.assertNotEqual(uploaded_data, original_data)
        finally:
            tmp_path.unlink(missing_ok=True)


class TestDownloadFileDecryption(unittest.TestCase):
    """_download_file 中的条件解密测试。"""

    @patch("unilabos.app.model_upload.requests.get")
    def test_mesh_file_decrypted_on_download(self, mock_get):
        """下载的 mesh 文件自动 XOR 解密后存本地。"""
        from unilabos.app.model_upload import _download_file

        original_data = b"original stl content here"
        encrypted_data = _xor_transform(original_data)

        mock_response = MagicMock()
        mock_response.content = encrypted_data
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        with tempfile.TemporaryDirectory() as tmpdir:
            local_path = Path(tmpdir) / "model.stl"
            _download_file("https://oss.example.com/model.stl", local_path)
            self.assertEqual(local_path.read_bytes(), original_data)

    @patch("unilabos.app.model_upload.requests.get")
    def test_xacro_file_not_decrypted(self, mock_get):
        """下载的 XACRO 文件不做解密处理。"""
        from unilabos.app.model_upload import _download_file

        xml_data = b'<?xml version="1.0"?><robot></robot>'

        mock_response = MagicMock()
        mock_response.content = xml_data
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        with tempfile.TemporaryDirectory() as tmpdir:
            local_path = Path(tmpdir) / "macro.xacro"
            _download_file("https://oss.example.com/macro.xacro", local_path)
            self.assertEqual(local_path.read_bytes(), xml_data)

    @patch("unilabos.app.model_upload.requests.get")
    def test_upload_download_roundtrip(self, mock_get):
        """上传加密 → 下载解密的完整 round-trip。"""
        from unilabos.app.model_upload import _download_file

        original_data = b"binary stl mesh \x00\xff\x80 special bytes"
        encrypted_data = _xor_transform(original_data)

        mock_response = MagicMock()
        mock_response.content = encrypted_data
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        with tempfile.TemporaryDirectory() as tmpdir:
            local_path = Path(tmpdir) / "mesh.stl"
            _download_file("https://oss.example.com/mesh.stl", local_path)
            self.assertEqual(local_path.read_bytes(), original_data)

    @patch("unilabos.app.model_upload.requests.get")
    def test_all_mesh_formats_decrypted(self, mock_get):
        """所有 mesh 格式下载后都解密。"""
        from unilabos.app.model_upload import _download_file

        original_data = b"mesh content for roundtrip"
        encrypted_data = _xor_transform(original_data)

        for ext in [".stl", ".dae", ".obj", ".fbx", ".gltf", ".glb"]:
            mock_response = MagicMock()
            mock_response.content = encrypted_data
            mock_response.raise_for_status = MagicMock()
            mock_get.return_value = mock_response

            with tempfile.TemporaryDirectory() as tmpdir:
                local_path = Path(tmpdir) / f"model{ext}"
                _download_file(f"https://oss.example.com/model{ext}", local_path)
                self.assertEqual(
                    local_path.read_bytes(), original_data, f"{ext} 文件应被解密"
                )


if __name__ == "__main__":
    unittest.main()
