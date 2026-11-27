"""首次配置诊断的离线验收，不使用真实凭据。"""
import json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch,Mock
import manage
from access_guidance import AccessSetupError


class DoctorTest(unittest.TestCase):
    def test_first_use_without_config(self):
        result=manage.doctor()
        self.assertEqual(result['status'],'needs_setup')
        self.assertIn('use-asset-library',result['message'])

    def test_missing_credentials_denied_network_and_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            oss=root/'oss.json';oss.write_text(json.dumps({'endpoint':'example','region':'example','bucket':'example','prefix':'test/','read_only':True}))
            (root/'service.json').write_text(json.dumps({'library_root':str(root/'mirror'),'state_root':str(root/'cache'),'oss_config':str(oss)}))
            with patch.dict(os.environ,{},clear=True),patch('credentials.credential_path',return_value=root/'absent'):
                self.assertEqual(manage.doctor(root)['code'],'oss_credentials_missing')
            store=Mock()
            with patch('credentials.load_credentials'),patch('library.Library') as factory:
                factory.return_value.store=store
                store.get.side_effect=AccessSetupError('oss_access_denied','无权限，请联系资产库维护者')
                self.assertEqual(manage.doctor(root,True)['code'],'oss_access_denied')
                store.get.reset_mock();store.get.side_effect=None;store.get.return_value=b'{}'
                self.assertEqual(manage.doctor(root,True)['status'],'ready')
                self.assertEqual([call.args[0] for call in store.get.call_args_list],['library.json','统一目录/library.json'])

    def test_configured_is_not_claimed_as_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'service.json').write_text(json.dumps({'library_root':tmp,'oss_config':str(root/'oss.json')}))
            with patch('access_guidance.validate_config'),patch('credentials.load_credentials'):
                self.assertEqual(manage.doctor(root)['status'],'configured_unverified')


if __name__=='__main__':unittest.main()
