"""OSS consumer transport: object GET only, no bucket-list/version/write permissions."""
import json
from pathlib import Path
import oss2
from oss2.credentials import EnvironmentVariableCredentialsProvider
from requests.exceptions import RequestException
from access_guidance import AccessSetupError, safe_error


class ReadOnlyStore:
    def __init__(self, config):
        self.config=json.loads(Path(config).read_text(encoding='utf-8-sig'))
        c=self.config
        self.prefix=c['prefix']
        if not self.prefix or not self.prefix.endswith('/'):
            raise ValueError('OSS prefix must be explicit and end with /')
        self.bucket=oss2.Bucket(oss2.ProviderAuthV4(EnvironmentVariableCredentialsProvider()),
                               c['endpoint'],c['bucket'],region=c['region'],
                               session=oss2.Session(pool_size=24),connect_timeout=20)

    def get(self, key):
        if not isinstance(key,str) or '\\' in key or ':' in key or any(p in ('','.','..') for p in key.split('/')):
            raise ValueError('Invalid relative OSS key')
        try:
            from minimal_format import retry
            return retry(lambda:self.bucket.get_object(self.prefix+key).read())
        except (oss2.exceptions.OssError, RequestException) as exc:
            # SDK errors can contain request details. Keep credentials and signatures out of jobs/logs.
            problem=safe_error(exc)
            raise AccessSetupError(problem['code'],problem['message']) from None
