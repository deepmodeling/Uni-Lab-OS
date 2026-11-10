"""发布版本在 Python、ROS 消息和两个 Conda 变体之间保持一致。"""

import ast
from pathlib import Path
import unittest
import tomllib
import xml.etree.ElementTree as ET

import yaml

ROOT = Path(__file__).resolve().parents[2]


def recipe(path):
    return yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))


def python_version():
    tree = ast.parse((ROOT / "unilabos/__init__.py").read_text(encoding="utf-8"))
    return next(ast.literal_eval(node.value) for node in tree.body
                if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "__version__"
                        for target in node.targets))


def message_version():
    return ET.parse(ROOT / "unilabos_msgs/package.xml").findtext("version")


class ReleaseVersionTests(unittest.TestCase):
    def test_python_package_versions_match(self):
        tree = ast.parse((ROOT / "setup.py").read_text(encoding="utf-8"))
        setup_call = next(node for node in ast.walk(tree)
                          if isinstance(node, ast.Call)
                          and isinstance(node.func, ast.Name) and node.func.id == "setup")
        version = next(ast.literal_eval(item.value) for item in setup_call.keywords
                       if item.arg == "version")
        self.assertEqual(version, python_version())
        self.assertEqual(recipe(".conda/base/recipe.yaml")["package"]["version"], version)

    def test_ros_recipe_versions_match_package_xml(self):
        for name in ("msgs", "msgs-humble", "ros-humble-unilabos-msgs"):
            with self.subTest(recipe=name):
                self.assertEqual(recipe(f"recipes/{name}/recipe.yaml")["package"]["version"],
                                 message_version())

    def test_default_package_does_not_depend_on_ros(self):
        core = recipe(".conda/base/recipe.yaml")
        self.assertFalse((ROOT / ".conda/environment/recipe.yaml").exists())
        for dependency in core["requirements"]["run"]:
            self.assertNotIn("ros-", dependency)
            self.assertNotIn("robostack", dependency)
            self.assertNotIn("unilabos-env", dependency)
        self.assertEqual(core["build"]["string"], "py314_0")

    def test_optional_ros_variants_pin_matching_core_and_messages(self):
        for distro, suffix in (("jazzy", ""), ("humble", "-humble")):
            with self.subTest(distro=distro):
                recipes = {kind: recipe(f".conda/{kind}{suffix}/recipe.yaml")
                           for kind in ("ros2", "full")}
                for item in recipes.values():
                    self.assertEqual(item["package"]["version"], python_version())
                    self.assertEqual(item["build"]["string"],
                                     f"{distro}_{item['build']['number']}")
                self.assertIn(
                    f"ros-{distro}-unilabos-msgs =={message_version()}",
                    recipes["ros2"]["requirements"]["run"],
                )
                self.assertIn(f"unilabos =={python_version()} py314_0", recipes["ros2"]["requirements"]["run"])
                self.assertIn(f"unilabos-ros2 =={python_version()} {distro}_1", recipes["full"]["requirements"]["run"])
                for item in recipes.values():
                    # Conda 包的 depends 不能携带构建器专用的 channel 匹配信息。
                    for dep in item["requirements"]["run"]:
                        self.assertNotIn("::", str(dep))

    def test_mcp_security_baseline_is_not_lowered_for_conda(self):
        self.assertIn("mcp>=1.30,<2", (ROOT / "unilabos/utils/requirements.txt").read_text())
        self.assertIn("mcp >=1.30,<2", recipe(".conda/base/recipe.yaml")["requirements"]["run"])
        self.assertEqual(recipe(".conda/mcp/recipe.yaml")["package"]["version"], "1.30.0")

    def test_numpy2_opentrons_is_a_default_material_dependency(self):
        patched = recipe("recipes/opentrons-shared-data/recipe.yaml")
        dependency = (f"opentrons-shared-data =={patched['package']['version']} "
                      f"{patched['build']['string']}")
        self.assertIn(dependency, recipe(".conda/base/recipe.yaml")["requirements"]["run"])
        self.assertIn(dependency, recipe(".conda/pylabrobot/recipe.yaml")["requirements"]["run"])
        for name in ("ros2", "ros2-humble"):
            self.assertNotIn(dependency, recipe(f".conda/{name}/recipe.yaml")["requirements"]["run"])

    def test_pip_and_conda_use_identical_downstream_versions(self):
        packages = tomllib.loads((ROOT / "recipes/wheels.toml").read_text(encoding="utf-8"))["packages"]
        dependencies = (ROOT / "unilabos/utils/requirements.txt").read_text(encoding="utf-8")
        for name, spec in packages.items():
            self.assertIn(f"{name}=={spec['version']}", dependencies)
        plr = recipe(".conda/pylabrobot/recipe.yaml")
        self.assertEqual(plr["package"]["version"], packages["pylabrobot"]["version"])
        self.assertIn(f"pylabrobot-unilab =={plr['package']['version']} {plr['build']['string']}",
                      recipe(".conda/base/recipe.yaml")["requirements"]["run"])


if __name__ == "__main__":
    unittest.main()
