"""用临时 CA 离线验证凭据从生成、轮换到回滚后的真实 TLS 可读性。"""

from __future__ import annotations

import json
import os
import ssl
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from unilabos.app.center_client import credential_rotation, enrollment


def test_credentials_remain_usable_through_rotation_and_publish_failure(tmp_path, monkeypatch):
    issuer_key = ec.generate_private_key(ec.SECP256R1())
    issuer = x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, "本地测试 CA")])

    def issue(csr_pem: str) -> str:
        csr = x509.load_pem_x509_csr(csr_pem.encode("ascii"))
        assert csr.is_signature_valid
        now = datetime.now(timezone.utc)
        return (
            x509.CertificateBuilder()
            .subject_name(csr.subject)
            .issuer_name(issuer)
            .public_key(csr.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=1))
            .sign(issuer_key, hashes.SHA256())
            .public_bytes(serialization.Encoding.PEM)
            .decode("ascii")
        )

    station_uuid = str(uuid4())
    active = tmp_path / "工站凭据 with spaces"
    key_path = active / enrollment.PRIVATE_KEY_FILE
    cert_path = active / enrollment.CERTIFICATE_FILE

    def verify_pair(expected_key: str, expected_certificate: str) -> None:
        assert enrollment.load_private_key(key_path) == expected_key
        assert cert_path.read_text(encoding="utf-8") == expected_certificate
        # OpenSSL 实际打开最终文件并验证证书、私钥匹配，覆盖原生 DACL 下的读取。
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_cert_chain(certfile=cert_path, keyfile=key_path)
        assert set(active.iterdir()) == {key_path, cert_path}

    original = enrollment.generate_key_and_csr(station_uuid)
    original_cert = issue(original.csr_pem)
    enrollment.persist_credentials(active, original_cert, original.private_key_pem)
    verify_pair(original.private_key_pem, original_cert)

    prepared = credential_rotation.prepare_rotation(
        tmp_path / "轮换材料 with spaces",
        station_uuid=station_uuid,
        instance_uuid=str(uuid4()),
        replaced_credential_uuid=str(uuid4()),
        grace_ms=3_600_000,
        reason="离线验证凭据文件完整流程",
    )
    saved_material = {path.name: path.read_bytes() for path in prepared.directory.iterdir()}
    request = json.loads(prepared.request_file.read_text(encoding="utf-8"))
    rotated_key = enrollment.load_private_key(prepared.private_key_file)
    rotated_cert = issue(request["body"]["csr_pem"])
    enrollment.persist_credentials(active, rotated_cert, rotated_key)
    verify_pair(rotated_key, rotated_cert)

    rejected = enrollment.generate_key_and_csr(station_uuid)
    rejected_cert = issue(rejected.csr_pem)
    replace = os.replace

    def fail_certificate_publish(source, destination):
        if Path(destination) == cert_path:
            raise OSError("模拟证书发布失败")
        replace(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(enrollment.os, "replace", fail_certificate_publish)
        with pytest.raises(OSError, match="模拟证书发布失败"):
            enrollment.persist_credentials(active, rejected_cert, rejected.private_key_pem)
    verify_pair(rotated_key, rotated_cert)
    assert saved_material == {path.name: path.read_bytes() for path in prepared.directory.iterdir()}

    enrollment.persist_credentials(active, rejected_cert, rejected.private_key_pem)
    verify_pair(rejected.private_key_pem, rejected_cert)
