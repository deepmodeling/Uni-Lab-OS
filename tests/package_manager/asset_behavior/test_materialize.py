"""资产行为构建的完整性门禁：越界、来源漂移和重复注册（自 postproc tests/test_asset_code.py 移植）。"""
import json
import tempfile
import unittest
from pathlib import Path

from unilabos.package_manager.package_distribution.asset_behavior import materialize, sha, verify_generated


class AssetCodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.package = self.root/'station/assets/a'
        self.source = self.package/'simready_usd/behavior'
        self.source.mkdir(parents=True)
        for name,code in {'__init__.py':'', 'device.py':'class Device: pass\n',
                          'usd.py':'class Motion: pass\n','contracts.py':'class Rejected(ValueError): pass\n'}.items():
            (self.source/name).write_text(code,encoding='utf8')
        (self.package/'.metadata').mkdir()
        self.seal()
        self.spec = {'kind':'asset_python','source_root':'simready_usd/behavior',
            'files':['__init__.py','device.py','usd.py','contracts.py'],
            'implementation':'device:Device','runtime':'usd:Motion','rejected':'contracts:Rejected','requires':['numpy']}
        self.assets = {'a':{'package_path':'station/assets/a','metadata':{'behavior_binding':self.spec}}}
        self.locks = [{'path':'station/assets/a','asset_id':'test-a','package_digest':'f'*64}]

    def seal(self):
        rows=[{'relative_path':f.relative_to(self.package).as_posix(),'hash':{'sha256':sha(f)}} for f in self.source.glob('*.py')]
        (self.package/'.metadata/com.nvidia.simready.packaging.bom.json').write_text(json.dumps({'items':rows}),encoding='utf8')

    def build(self):
        return materialize(self.root,'station',self.assets,self.locks)

    def test_materialized_source_is_pinned_and_relocatable(self):
        entries,requirements=self.build()
        self.assertEqual(requirements,['numpy'])
        self.assertTrue(entries['a']['implementation'].startswith('station._asset_code.'))
        verify_generated(self.root,{'dependencies':self.locks})
        for row in self.locks[0]['generated_code']:
            self.assertEqual(sha(self.root/row['path']),row['sha256'])

    def test_changed_source_rejected_by_bom(self):
        (self.source/'device.py').write_text('class Changed: pass\n',encoding='utf8')
        with self.assertRaisesRegex(ValueError,'Unsealed'):self.build()

    def test_generated_code_tampering_rejected(self):
        self.build()
        target=self.root/self.locks[0]['generated_code'][1]['path']
        target.write_text('class Changed: pass\n',encoding='utf8')
        with self.assertRaisesRegex(ValueError,'differs'):verify_generated(self.root,{'dependencies':self.locks})

    def test_extra_executable_rejected(self):
        self.build()
        folder=(self.root/self.locks[0]['generated_code'][0]['path']).parent
        (folder/'extra.py').write_text('print("unexpected")',encoding='utf8')
        with self.assertRaisesRegex(ValueError,'Unexpected'):verify_generated(self.root,{'dependencies':self.locks})

    def test_source_path_escape_rejected(self):
        self.spec['files'].append('../escape.py')
        with self.assertRaisesRegex(ValueError,'Invalid'):self.build()

    def test_double_registration_rejected(self):
        (self.source/'device.py').write_text('@device(id="duplicate")\nclass Device: pass\n',encoding='utf8')
        self.seal()
        with self.assertRaisesRegex(ValueError,'register'):self.build()

    def test_qualified_double_registration_rejected(self):
        (self.source/'device.py').write_text('@registry.device(id="duplicate")\nclass Device: pass\n',encoding='utf8')
        self.seal()
        with self.assertRaisesRegex(ValueError,'register'):self.build()

    def test_undeclared_entry_rejected(self):
        self.spec['runtime']='hidden:Motion'
        with self.assertRaisesRegex(ValueError,'declared module'):self.build()

    def test_multiple_exports_need_no_motion_backend(self):
        for field in ('runtime','rejected','implementation'):self.spec.pop(field)
        self.spec['exports']={'pump':{'implementation':'device:Device','declaration':{'id':'pump'}},
            'needle':{'implementation':'device:Device','declaration':{'id':'needle'},
                      'model_provider':'usd:Motion'}}
        self.spec['exports']['needle']['declaration']['model']={'type':'package_static'}
        entries,_=self.build()
        exported=entries['a']['exports']
        self.assertEqual(set(exported),{'pump','needle'})
        self.assertIn('._asset_code.',exported['needle']['declaration']['model']['provider'])
        verify_generated(self.root,{'dependencies':self.locks})

    def test_missing_behavior_owner_is_not_silently_ignored(self):
        self.assets['reference']={'metadata':{'behavior_binding':{'kind':'asset_reference','asset_id':'missing'}}}
        with self.assertRaisesRegex(ValueError,'selected and pinned'):self.build()

    def test_geometry_only_binding_has_no_behavior(self):
        self.assets['cap']={'package_path':'x','metadata':{'behavior_binding':{'implementation':None,'status':'geometry_only'}}}
        entries,_=self.build()
        self.assertNotIn('cap',entries)

    def test_absent_or_null_binding_has_no_behavior(self):
        self.assets['plain']={'package_path':'x','metadata':{}}
        self.assets['null']={'package_path':'y','metadata':{'behavior_binding':None}}
        entries,_=self.build()
        self.assertEqual(set(entries),{'a'})

    def test_unknown_kind_is_not_silently_skipped(self):
        self.assets['odd']={'package_path':'x','metadata':{'behavior_binding':{'kind':'package','module':'x'}}}
        with self.assertRaisesRegex(ValueError,'Unsupported'):self.build()

    def test_implementation_without_kind_rejected(self):
        self.assets['odd']={'package_path':'x','metadata':{'behavior_binding':{'implementation':'device:Device'}}}
        with self.assertRaisesRegex(ValueError,'no kind'):self.build()

    def test_selected_reference_owner_passes(self):
        self.assets['reference']={'package_path':'r','metadata':{'behavior_binding':{'kind':'asset_reference','asset_id':'test-a'}}}
        entries,_=self.build()
        self.assertEqual(set(entries),{'a'})

    def test_missing_lock_entry_is_explicit(self):
        self.locks[0]['path']='elsewhere'
        with self.assertRaisesRegex(ValueError,'no lock entry'):self.build()


if __name__=='__main__':unittest.main()
