"""Verified, on-demand reads of explicit evidence references; never extracts ZIPs."""
from pathlib import PurePosixPath
import zipfile

from core import inside, digest, sha

MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024


def _relative(value):
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value:
        raise ValueError('Invalid evidence relative path')
    parts = value.split('/')
    if PurePosixPath(value).is_absolute() or any(p in ('', '.', '..') or ':' in p for p in parts):
        raise ValueError('Evidence path must remain inside the library')
    return value


def _verified_path(library, key, expected, limit):
    key = _relative(key)
    info = library.files.get(key)
    if not info or not expected or expected != info['sha256']:
        raise ValueError('Evidence reference does not match the library manifest')
    if info['bytes'] > limit:
        raise ValueError('Evidence exceeds interactive read limit; use the on-demand archive')
    path = inside(library.root, key)
    if path.is_file():
        if path.stat().st_size != info['bytes'] or digest(path) != expected:
            raise ValueError('Cached evidence was modified; retained for inspection')
    else:
        data = library.store.get(key)
        if len(data) != info['bytes'] or sha(data) != expected:
            raise ValueError('Downloaded evidence failed size/SHA256 verification')
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + '.evidence-download')
        temporary.write_bytes(data)
        temporary.replace(path)
    return path


def resolve(library, ref):
    if not isinstance(ref, dict):
        raise ValueError('Evidence reference must be an object')
    if ref.get('availability') in ('local_only', 'retained_local_only') or ref.get('retained_local_only'):
        raise FileNotFoundError('Evidence is retained in the conversion-run archive, not this published library')
    with library.lock:
        if 'archive_path' in ref:
            archive = _verified_path(library, ref['archive_path'], ref.get('archive_sha256'), MAX_ARCHIVE_BYTES)
            member = _relative(ref.get('member'))
            expected = ref.get('member_sha256')
            if not expected:
                raise ValueError('Archive member SHA256 is required')
            with zipfile.ZipFile(archive) as package:
                matches = [row for row in package.infolist() if row.filename == member]
                if len(matches) != 1 or matches[0].is_dir():
                    raise ValueError('Archive evidence member is absent or ambiguous')
                info = matches[0]
                if info.file_size > MAX_MEMBER_BYTES:
                    raise ValueError('Evidence member exceeds interactive read limit; use the on-demand archive')
                if 'bytes' in ref and (type(ref['bytes']) is not int or ref['bytes'] != info.file_size):
                    raise ValueError('Evidence member size differs from its reference')
                with package.open(info) as stream:
                    data = stream.read(MAX_MEMBER_BYTES + 1)
                if len(data) != info.file_size or sha(data) != expected:
                    raise ValueError('Evidence archive member failed size/SHA256 verification')
            locator = {key: ref[key] for key in ('archive_path', 'archive_sha256', 'member', 'member_sha256')}
        else:
            path = _verified_path(library, ref.get('path'), ref.get('sha256'), MAX_MEMBER_BYTES)
            data = path.read_bytes()
            if sha(data) != ref['sha256']:
                raise ValueError('Evidence changed during reading')
            locator = {'path': ref['path'], 'sha256': ref['sha256']}
        return {'data': data, 'verified': True, 'locator': locator}
