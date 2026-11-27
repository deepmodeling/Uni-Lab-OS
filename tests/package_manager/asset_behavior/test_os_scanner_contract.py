"""转发声明与 OS 静态扫描器的合同对照：扫描器从转发类登记的内容必须与原类相同。

扫描规则一旦变化（例如开始登记新的成员形态），这里先失败，生成器随之调整。
"""
import tempfile
import textwrap
import unittest
from pathlib import Path

from tests.package_manager.asset_behavior.test_forwarder import sync_only_options
from unilabos.package_manager.package_distribution.asset_behavior import ForwarderOptions, generate_forwarder
from unilabos.registry.ast_registry_scanner import _parse_file

SOURCE = '''
"""扫描对照用设备。"""
from typing import Literal, TypedDict

from unilabos.registry.decorators import action, device, not_action, topic_config

SITES = [{"index": "nest", "label": "nest"}]
Speed = Literal["low", "high"]


class RunResult(TypedDict):
    ok: bool
    count: int


@device(id="scanner_demo", category=["demo"], displayname="对照设备",
        model={"entry": "models/demo/model.urdf", "format": "urdf"}, available_sites=SITES)
class ScannerDemo:
    def __init__(self, port: str, timeout: float = 1.0, *, retries: int = 2):
        self.port = port

    @action(description="异步运行")
    async def run(self, speed: Speed = "low", count: int = 1) -> RunResult:
        """按给定速度运行。"""
        return {"ok": True, "count": count}

    @action()
    def reset(self) -> None:
        """复位。"""

    @topic_config(period=2.0)
    async def temperature(self) -> float:
        return 25.0

    @property
    def status(self) -> str:
        return "idle"

    def get_door(self) -> str:
        return "closed"

    def calibrate(self, offset: float) -> float:
        return offset

    @not_action
    def internal(self) -> None:
        pass
'''

KEYS = ('device_id', 'model', 'available_sites', 'metadata', 'category', 'actions', 'status_properties',
        'init_params', 'auto_methods')


def view(meta):
    out = {k: meta.get(k) for k in KEYS}
    out['actions'] = {k: {f: v.get(f) for f in ('params', 'return_type', 'is_async', 'docstring', 'schema', 'action_args')}
                      for k, v in meta['actions'].items()}
    return out


class ScannerContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.original_root, self.generated_root = root / 'original', root / 'generated'
        self.source = self.original_root / 'demo_pkg/devices/demo.py'
        self.source.parent.mkdir(parents=True)
        self.source.write_text(textwrap.dedent(SOURCE), 'utf8')

    def scan(self, path, root):
        devices, _ = _parse_file(path, root)
        self.assertEqual(len(devices), 1)
        return view(devices[0])

    def forwarded(self, **kw):
        kw.setdefault('options', ForwarderOptions(allow_registered_implementation=True))
        target = self.generated_root / 'demo_gen/devices/demo.py'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(generate_forwarder(self.source, 'demo_pkg.devices.demo', 'ScannerDemo', **kw), 'utf8')
        return self.scan(target, self.generated_root)

    def test_forwarder_registers_exactly_what_the_original_registers(self):
        original = self.scan(self.source, self.original_root)
        self.assertEqual(set(original['actions']), {'run', 'reset'})
        self.assertTrue(original['actions']['run']['is_async'])
        self.assertEqual(self.forwarded(), original)

    def test_sync_only_forwarding_loses_async_and_status(self):
        contracts = Path(self.temp.name) / 'contracts.py'
        contracts.write_text('from typing import TypedDict\n', 'utf8')
        # 只转发同步动作的旧行为不复制根级字面量，available_sites=SITES 在转发文件里无法展开，这里去掉以单看动作差异。
        self.source.write_text(self.source.read_text('utf8').replace(', available_sites=SITES', ''), 'utf8')
        legacy = self.forwarded(options=sync_only_options(typed_dict_sources=(contracts,)))
        self.assertEqual(set(legacy['actions']), {'reset'})
        self.assertEqual(legacy['init_params'], [])
        self.assertEqual(legacy['status_properties'], {})


if __name__ == '__main__':
    unittest.main()
