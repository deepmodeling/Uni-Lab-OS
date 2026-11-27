"""资产行为转发声明：OS 登记的成员都在类体内重声明，运行时委托到实现，registry ID 默认不变。"""
import ast
import asyncio
import importlib
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from unilabos.package_manager.package_distribution.asset_behavior import (
    ForwarderOptions,
    generate_forwarder,
    relative_model_entry,
    write_forwarder,
)


def sync_only_options(**kw):
    """来源实现的行为：只转发同步、带括号的 @action，不保留 docstring、不带模块符号。"""
    return ForwarderOptions(forward_async=False, forward_bare_action=False, forward_legacy_action=False,
                            forward_status=False, forward_init=False, forward_auto_methods=False,
                            keep_docstrings=False, import_module_symbols=False, copy_literal_constants=False,
                            always_import_typed_dict_and_resource_slot=True, allow_registered_implementation=True, **kw)


IMPL = '''
"""示例实现。"""
from __future__ import annotations
from typing import TYPE_CHECKING, Literal, TypedDict
from unilabos.registry.decorators import action, device, not_action, topic_config
if TYPE_CHECKING:
    from .types_only import Hidden

SITES = [{"index": "nest", "label": "nest", "position_x": 1.0}]
Mode = Literal["fast", "slow"]
DEFAULT_SPEED = 3


class StatusResult(TypedDict):
    ok: bool


@device(id="demo_device", category=["demo"], model={"entry": "models/demo/model.urdf", "format": "urdf"}, available_sites=SITES)
class Demo:
    """演示设备。"""

    def __init__(self, port: str, *, timeout: float = 1.0):
        self.port, self.timeout, self.calls, self._level = port, timeout, [], 0

    @action(description="异步动作")
    async def run(self, mode: Mode = "fast", speed: int = DEFAULT_SPEED) -> StatusResult:
        """运行一次。"""
        self.calls.append(("run", mode, speed))
        return {"ok": True}

    @action()
    def bare(self, x: int) -> int:
        return x + 1

    @action()
    def spread(self, first, *rest, flag=False, **extra):
        return (first, rest, flag, extra)

    @topic_config(period=5.0)
    async def temperature(self) -> float:
        return 37.0

    @property
    def level(self) -> int:
        return self._level

    @level.setter
    def level(self, value: int) -> None:
        self._level = value

    def get_state(self) -> str:
        return "idle"

    def helper(self, hidden: "Hidden" = None) -> str:
        return "auto"

    @not_action
    def internal(self):
        return "internal"

    def _private(self):
        return "private"
'''


class ForwarderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        pkg = root / 'demo_pkg'
        (pkg / 'devices').mkdir(parents=True)
        (pkg / 'gen').mkdir()
        for p in (pkg, pkg / 'devices', pkg / 'gen'):
            (p / '__init__.py').write_text('')
        (pkg / 'devices/types_only.py').write_text('class Hidden: pass\n')
        self.source = pkg / 'devices/demo.py'
        self.source.write_text(textwrap.dedent(IMPL), encoding='utf8')
        self.root, self.pkg = root, pkg
        sys.path.insert(0, str(root))
        self.addCleanup(lambda: sys.path.remove(str(root)))
        self.addCleanup(lambda: [sys.modules.pop(m) for m in list(sys.modules) if m.split('.')[0] == 'demo_pkg'])

    def generate(self, **kw):
        # 静态检查允许实现类已注册；运行时用例改走未装饰实现 + declaration。
        kw.setdefault('options', ForwarderOptions(allow_registered_implementation=True))
        return generate_forwarder(self.source, 'demo_pkg.devices.demo', 'Demo', **kw)

    def load(self, code):
        target = self.pkg / 'gen/demo.py'
        target.write_text(code, encoding='utf8')
        importlib.invalidate_caches()
        sys.modules.pop('demo_pkg.gen.demo', None)
        return importlib.import_module('demo_pkg.gen.demo')

    def members(self, code):
        cls = next(n for n in ast.parse(code).body if isinstance(n, ast.ClassDef) and n.name == 'Demo')
        return {n.name: n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}, cls

    def test_class_body_redeclares_everything_os_registers(self):
        members, cls = self.members(self.generate())
        self.assertEqual(sorted(members), ['__init__', 'bare', 'get_state', 'helper', 'level', 'run', 'spread', 'temperature'])
        self.assertIsInstance(members['run'], ast.AsyncFunctionDef)
        self.assertIsInstance(members['temperature'], ast.AsyncFunctionDef)
        self.assertEqual(ast.get_docstring(members['run']), '运行一次。')
        self.assertNotIn('internal', members)
        self.assertNotIn('_private', members)

    def test_registry_id_kept_by_default(self):
        code = self.generate()
        self.assertIn("id='demo_device'", code)
        self.assertIn("id='station_demo_device'", self.generate(options=ForwarderOptions(registry_id_prefix='station')))

    def test_same_id_as_registered_implementation_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'already registers demo_device'):
            generate_forwarder(self.source, 'demo_pkg.devices.demo', 'Demo')

    def test_runtime_delegation_sync_async_property(self):
        # 资产行为实现本身不注册；转发类按 declaration 唯一注册该 ID。
        text = self.source.read_text('utf8')
        start = text.index('@device(')
        self.source.write_text(text[:start] + text[text.index('\nclass Demo', start) + 1:], 'utf8')
        mod = self.load(generate_forwarder(self.source, 'demo_pkg.devices.demo', 'Demo',
                                           declaration={'id': 'demo_runtime_device', 'category': ['demo']}))
        device = mod.Demo('COM1', timeout=2.0)
        self.assertEqual((device.port, device.timeout), ('COM1', 2.0))
        self.assertEqual(asyncio.run(device.run('slow')), {'ok': True})
        self.assertEqual(device.calls, [('run', 'slow', 3)])
        self.assertEqual(asyncio.run(device.temperature()), 37.0)
        self.assertEqual(device.bare(1), 2)
        self.assertEqual(device.spread(1, 2, 3, flag=True, z=4), (1, (2, 3), True, {'z': 4}))
        device.level = 5
        self.assertEqual(device.level, 5)
        self.assertEqual(device.get_state(), 'idle')
        self.assertEqual(device.internal(), 'internal')
        self.assertEqual(device.helper(), 'auto')

    def test_module_symbols_and_literal_sites(self):
        code = self.generate()
        tree = ast.parse(code)
        assigned = {t.id for n in tree.body if isinstance(n, ast.Assign) for t in n.targets}
        self.assertIn('SITES', assigned)  # OS 只展开同文件根级字面量
        self.assertIn('from demo_pkg.devices.demo import DEFAULT_SPEED, Mode', code)
        self.assertIn('class StatusResult(TypedDict)', code)
        self.assertIn('from demo_pkg.devices.types_only import Hidden', code)

    def test_model_entry_rewrite_and_os_relative_rule(self):
        code = self.generate(options=ForwarderOptions(model_entry='assets/demo/model.urdf', allow_registered_implementation=True))
        self.assertIn("'entry': 'assets/demo/model.urdf'", code)
        model = self.pkg / 'assets/demo/model.urdf'
        self.assertEqual(relative_model_entry(self.pkg / 'x.py', model, self.pkg), 'assets/demo/model.urdf')
        with self.assertRaisesRegex(ValueError, 'below the declaration'):
            relative_model_entry(self.pkg / 'devices/x.py', model, self.pkg)
        with self.assertRaisesRegex(ValueError, 'inside the import package'):
            relative_model_entry(self.root / 'x.py', self.root / 'm.urdf', self.pkg)

    def test_declaration_replaces_decorator(self):
        code = self.generate(declaration={'id': 'exported', 'model': {'type': 'package_static'}})
        _, cls = self.members(code)
        self.assertEqual(len(cls.decorator_list), 1)
        self.assertIn("@device(id='exported'", code)

    def test_static_methods_are_explicitly_rejected(self):
        self.source.write_text(self.source.read_text('utf8').replace(
            '    def get_state(self) -> str:', '    @staticmethod\n    def ping() -> str:\n        return "x"\n\n    def get_state(self) -> str:'), 'utf8')
        with self.assertRaisesRegex(ValueError, 'static/class'):
            self.generate()

    def test_sync_only_options_reproduce_source_behaviour(self):
        contracts = self.root / 'contracts.py'
        contracts.write_text('from typing import TypedDict\nclass ActionResult(TypedDict):\n    witness: str\n')
        code = self.generate(options=sync_only_options(registry_id_prefix='station', typed_dict_sources=(contracts,)))
        members, _ = self.members(code)
        self.assertEqual(sorted(members), ['bare', 'spread'])  # 旧实现只转发同步、带括号的 @action
        self.assertIn("id='station_demo_device'", code)
        self.assertIn('class ActionResult(TypedDict)', code)

    def test_write_forwarder_record(self):
        record = write_forwarder(self.pkg / 'gen/demo.py', self.source, 'demo_pkg.devices.demo', 'Demo', package=self.pkg,
                                 options=ForwarderOptions(registry_id_prefix='station'))
        self.assertEqual(record['project_class'], 'demo_pkg.gen.demo:Demo')
        self.assertEqual(record['class'], 'demo_pkg.devices.demo:Demo')


class BareActionTests(unittest.TestCase):
    def test_bare_action_decorator_is_forwarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'bare.py'
            source.write_text('from unilabos.registry.decorators import action, device\n'
                              '@device(id="bare_device", category=["demo"])\nclass Bare:\n    @action\n    async def go(self) -> None:\n        pass\n', 'utf8')
            code = generate_forwarder(source, 'pkg.bare', 'Bare', options=ForwarderOptions(registry_id_prefix='station'))
            self.assertIn('async def go(self) -> None:', code)
            self.assertIn('return await _Implementation.go(self)', code)


class LayeringTests(unittest.TestCase):
    def test_module_uses_only_standard_library_and_itself(self):
        root = Path(__file__).resolve().parents[3] / 'unilabos/package_manager/package_distribution/asset_behavior'
        allowed = set(sys.stdlib_module_names)
        for path in root.glob('*.py'):
            for node in ast.walk(ast.parse(path.read_text('utf8'))):
                if isinstance(node, ast.ImportFrom) and node.level:
                    continue
                names = [node.module] if isinstance(node, ast.ImportFrom) else [a.name for a in node.names] if isinstance(node, ast.Import) else []
                for name in names:
                    self.assertIn(name.split('.')[0], allowed, f'{path.name} imports {name}')


if __name__ == '__main__':
    unittest.main()
