"""normalize_model_for_upload 单元测试"""

import unittest
import sys
import os

# 添加项目根目录到 sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from unilabos.app.register import normalize_model_for_upload


class TestNormalizeModelForUpload(unittest.TestCase):
    """测试 Registry YAML model 字段标准化"""

    def test_empty_input(self):
        """空 dict 直接返回"""
        self.assertEqual(normalize_model_for_upload({}), {})
        self.assertIsNone(normalize_model_for_upload(None))

    def test_format_infer_xacro(self):
        """自动从 path 后缀推断 format=xacro"""
        model = {
            "path": "https://oss.example.com/devices/arm/macro_device.xacro",
            "mesh": "arm_slider",
            "type": "device",
        }
        result = normalize_model_for_upload(model)
        self.assertEqual(result["format"], "xacro")

    def test_format_infer_urdf(self):
        """自动推断 format=urdf"""
        model = {"path": "https://example.com/robot.urdf", "type": "device"}
        result = normalize_model_for_upload(model)
        self.assertEqual(result["format"], "urdf")

    def test_format_infer_stl(self):
        """自动推断 format=stl"""
        model = {"path": "https://example.com/part.stl"}
        result = normalize_model_for_upload(model)
        self.assertEqual(result["format"], "stl")

    def test_format_infer_gltf(self):
        """自动推断 format=gltf（.gltf 和 .glb）"""
        for ext in [".gltf", ".glb"]:
            model = {"path": f"https://example.com/model{ext}"}
            result = normalize_model_for_upload(model)
            self.assertEqual(result["format"], "gltf", f"failed for {ext}")

    def test_format_not_overwritten(self):
        """已有 format 字段时不覆盖"""
        model = {
            "path": "https://example.com/model.xacro",
            "format": "custom",
        }
        result = normalize_model_for_upload(model)
        self.assertEqual(result["format"], "custom")

    def test_format_no_path(self):
        """没有 path 时不推断 format"""
        model = {"mesh": "arm_slider", "type": "device"}
        result = normalize_model_for_upload(model)
        self.assertNotIn("format", result)

    def test_children_mesh_string_to_struct(self):
        """将 children_mesh 字符串（旧格式）转为结构化对象"""
        model = {
            "path": "https://example.com/rack.xacro",
            "type": "resource",
            "children_mesh": "tip/meshes/tip.stl",
            "children_mesh_tf": [0.0045, 0.0045, 0, 0, 0, 1.57],
            "children_mesh_path": "https://oss.example.com/tip.stl",
        }
        result = normalize_model_for_upload(model)

        cm = result["children_mesh"]
        self.assertIsInstance(cm, dict)
        self.assertEqual(cm["path"], "https://oss.example.com/tip.stl")
        self.assertEqual(cm["format"], "stl")
        self.assertTrue(cm["default_visible"])
        self.assertEqual(cm["local_offset"], [0.0045, 0.0045, 0])
        self.assertEqual(cm["local_rotation"], [0, 0, 1.57])

        self.assertNotIn("children_mesh_tf", result)
        self.assertNotIn("children_mesh_path", result)

    def test_children_mesh_no_oss_fallback(self):
        """children_mesh 无 OSS URL 时 fallback 到本地路径"""
        model = {
            "path": "https://example.com/rack.xacro",
            "children_mesh": "plate_96/meshes/plate_96.stl",
        }
        result = normalize_model_for_upload(model)
        cm = result["children_mesh"]
        self.assertEqual(cm["path"], "plate_96/meshes/plate_96.stl")
        self.assertEqual(cm["format"], "stl")

    def test_children_mesh_gltf_format(self):
        """children_mesh .glb 文件推断 format=gltf"""
        model = {
            "path": "https://example.com/rack.xacro",
            "children_mesh": "meshes/child.glb",
        }
        result = normalize_model_for_upload(model)
        self.assertEqual(result["children_mesh"]["format"], "gltf")

    def test_children_mesh_partial_tf(self):
        """children_mesh_tf 只有 3 个值时只有 offset 无 rotation"""
        model = {
            "path": "https://example.com/rack.xacro",
            "children_mesh": "tip.stl",
            "children_mesh_tf": [0.01, 0.02, 0.03],
        }
        result = normalize_model_for_upload(model)
        cm = result["children_mesh"]
        self.assertEqual(cm["local_offset"], [0.01, 0.02, 0.03])
        self.assertNotIn("local_rotation", cm)

    def test_children_mesh_no_tf(self):
        """children_mesh 无 tf 时不加 offset/rotation"""
        model = {
            "path": "https://example.com/rack.xacro",
            "children_mesh": "tip.stl",
        }
        result = normalize_model_for_upload(model)
        cm = result["children_mesh"]
        self.assertNotIn("local_offset", cm)
        self.assertNotIn("local_rotation", cm)

    def test_children_mesh_already_dict(self):
        """children_mesh 已经是 dict 时不重新映射"""
        model = {
            "path": "https://example.com/rack.xacro",
            "children_mesh": {
                "path": "https://example.com/tip.stl",
                "format": "stl",
                "default_visible": False,
            },
        }
        result = normalize_model_for_upload(model)
        cm = result["children_mesh"]
        self.assertIsInstance(cm, dict)
        self.assertFalse(cm["default_visible"])

    def test_original_not_mutated(self):
        """原始 dict 不被修改"""
        original = {
            "path": "https://example.com/model.xacro",
            "mesh": "arm",
        }
        original_copy = {**original}
        normalize_model_for_upload(original)
        self.assertEqual(original, original_copy)

    def test_preserves_existing_fields(self):
        """所有原始字段都被保留"""
        model = {
            "path": "https://example.com/model.xacro",
            "mesh": "arm_slider",
            "type": "device",
            "mesh_tf": [0, 0, 0, 0, 0, 0],
            "custom_field": "should_survive",
        }
        result = normalize_model_for_upload(model)
        self.assertEqual(result["custom_field"], "should_survive")
        self.assertEqual(result["mesh_tf"], [0, 0, 0, 0, 0, 0])


if __name__ == "__main__":
    unittest.main()
