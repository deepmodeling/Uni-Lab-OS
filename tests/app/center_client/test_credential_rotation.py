"""轮换只导出公钥请求；响应丢失后不改写原私密材料。"""

import hashlib
import json
from uuid import uuid4

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from tests.app.center_client.private_file_assertions import assert_private, permission_snapshot, symlink_or_skip
from unilabos.app.center_client import credential_rotation as rotation


def prepare(directory, **overrides):
    args = dict(station_uuid=str(uuid4()), instance_uuid=str(uuid4()),
                replaced_credential_uuid=str(uuid4()), grace_ms=3600000, reason="例行轮换")
    args.update(overrides)
    return rotation.prepare_rotation(directory, **args)


@pytest.mark.parametrize("name", ["new", "新轮换材料"])
def test_public_rotation_request_matches_private_material_without_exposing_it(tmp_path, name):
    parent_permissions = permission_snapshot(tmp_path)
    result = prepare(tmp_path / name)
    public = result.request_file.read_text(encoding="utf-8")
    request = json.loads(public)
    secret = result.machine_secret_file.read_text()
    private = result.private_key_file.read_bytes()
    assert len(secret) == 64 and all(c in "0123456789abcdef" for c in secret)
    assert request["body"]["new_machine_secret_hash"] == "sha256:" + hashlib.sha256(secret.encode()).hexdigest()
    assert secret not in public and private.decode() not in public
    key = serialization.load_pem_private_key(private, password=None)
    csr = x509.load_pem_x509_csr(request["body"]["csr_pem"].encode())
    assert csr.is_signature_valid
    assert csr.public_key().public_numbers() == key.public_key().public_numbers()
    assert_private(result.directory, directory=True)
    for path in result.directory.iterdir():
        assert_private(path)
    assert permission_snapshot(tmp_path) == parent_permissions
    saved = {path.name: path.read_bytes() for path in result.directory.iterdir()}
    with pytest.raises(FileExistsError):
        prepare(result.directory)
    assert saved == {path.name: path.read_bytes() for path in result.directory.iterdir()}


def test_rotation_preparation_does_not_follow_directory_symlinks(tmp_path):
    destination = tmp_path / "existing"
    destination.mkdir()
    link = tmp_path / "new"
    symlink_or_skip(link, destination, directory=True)
    with pytest.raises(FileExistsError):
        prepare(link)
    assert list(destination.iterdir()) == [] and link.is_symlink()


def test_existing_rotation_directory_is_rejected_before_writing_or_cleanup(tmp_path, monkeypatch):
    existing = tmp_path / "existing"
    existing.mkdir()
    marker = existing / "untouched.txt"
    marker.write_text("unrelated contents", encoding="utf-8")
    before = permission_snapshot(existing)

    def forbidden(*args, **kwargs):
        pytest.fail("已有目录不得写入或清理")

    monkeypatch.setattr(rotation, "write_private", forbidden)
    monkeypatch.setattr(rotation.shutil, "rmtree", forbidden)
    with pytest.raises(FileExistsError):
        prepare(existing)
    assert marker.read_text(encoding="utf-8") == "unrelated contents"
    assert permission_snapshot(existing) == before


def test_failed_preparation_removes_only_its_new_directory(tmp_path, monkeypatch):
    original = rotation.write_private
    def fail(path, value):
        if path.name == "rotation-request.json":
            raise OSError("模拟磁盘满")
        original(path, value)
    monkeypatch.setattr(rotation, "write_private", fail)
    with pytest.raises(OSError):
        prepare(tmp_path / "new")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("overrides", [{"grace_ms": -1}, {"grace_ms": True},
                                        {"grace_ms": 86400001}, {"reason": ""}, {"instance_uuid": "bad"}])
def test_invalid_rotation_input_never_creates_material(tmp_path, overrides):
    with pytest.raises(ValueError):
        prepare(tmp_path / "new", **overrides)
    assert list(tmp_path.iterdir()) == []
