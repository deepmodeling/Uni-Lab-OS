"""工站侧的一次性注册：本地生成密钥与 CSR，向中心换取证书与机器密钥。

协议里最重要的一条约束是**私钥永不上传**：工站自己生成密钥对，只把公钥（包在 CSR 里）
交给中心。中心签出证书后，工站把证书与私钥一起保存。因此这里没有任何"把私钥发给中心"
的路径，也不接受中心下发的私钥。

注册码是一次性的，且只在兑换响应里出现一次。它落盘的意义只是让中断后的重试仍能完成
兑换；一旦兑换成功，注册码本身既无用也不该继续留着，所以兑换后立即删除。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from unilabos.app.center_client.credential_files import create_private_temporary

logger = logging.getLogger(__name__)

#: 工站私钥文件名。与证书同目录，POSIX 权限 0600，Windows 使用私密 DACL。
PRIVATE_KEY_FILE = "station-key.pem"
CERTIFICATE_FILE = "station.pem"
#: 注册码落盘文件名。兑换成功后删除。
ENROLLMENT_CODE_FILE = "enrollment-code.txt"


class EnrollmentError(RuntimeError):
    """注册失败。"""


@dataclass(frozen=True)
class StationKeyPair:
    """工站本地密钥对。私钥只以 PEM 文本存在，且只在必要时读取。"""

    private_key_pem: str
    csr_pem: str


def generate_key_and_csr(common_name: str) -> StationKeyPair:
    """在本地生成密钥对与 CSR。

    ``common_name`` 只是展示字段：中心会用自己构造的主体名覆盖它，并把手站身份写进
    证书扩展。因此这里不需要（也不应该）尝试声明身份。
    """
    if not common_name:
        raise EnrollmentError("CSR 主体名不能为空")
    key = ec.generate_private_key(ec.SECP256R1())
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, common_name)]))
        .sign(key, hashes.SHA256())
    )
    return StationKeyPair(
        private_key_pem=key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("ascii"),
        csr_pem=csr.public_bytes(serialization.Encoding.PEM).decode("ascii"),
    )


def write_private(path: Path, contents: str) -> None:
    """创建时即限制临时文件访问，再原子替换；写入失败时保留原文件。"""
    staged = _stage_private(path, contents)
    try:
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)


def _stage_private(path: Path, contents: str) -> Path:
    # 权限在创建时设置，不能先写明文再收紧。临时文件与目标同目录，保证
    # os.replace 不会跨文件系统，也不会跟随目标处的符号链接。
    descriptor, staged = create_private_temporary(path)
    try:
        try:
            # 描述符始终由本层持有；即使文本流构造失败，也不会重复关闭或
            # 误关已被其他线程复用的描述符。先关闭文本流，再关闭描述符。
            with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as stream:
                stream.write(contents)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(descriptor)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    return staged


def persist_credentials(directory: Path, certificate_pem: str, private_key_pem: str) -> None:
    """把中心签出的证书与本地私钥保存到同一目录。

    证书是公开材料，可以用默认权限；私钥必须收紧。两者分开放是刻意的：只有证书被误传
    时不会连带泄漏私钥。
    """
    directory.mkdir(parents=True, exist_ok=True)
    certificate = directory / CERTIFICATE_FILE
    private_key = directory / PRIVATE_KEY_FILE
    # 先完成所有写入再发布，磁盘写满等错误不能截断已经可用的凭据。两个独立
    # 路径无法提供跨文件的崩溃原子性；若第二次替换失败，恢复旧私钥并明确报错。
    old_key = private_key.read_text(encoding="utf-8") if private_key.exists() else None
    staged_key = _stage_private(private_key, private_key_pem)
    staged_certificate: Path | None = None
    try:
        staged_certificate = _stage_private(certificate, certificate_pem)
        os.replace(staged_key, private_key)
        try:
            os.replace(staged_certificate, certificate)
        except OSError:
            if old_key is None:
                private_key.unlink(missing_ok=True)
            else:
                write_private(private_key, old_key)
            raise
    finally:
        staged_key.unlink(missing_ok=True)
        if staged_certificate is not None:
            staged_certificate.unlink(missing_ok=True)


def load_private_key(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise EnrollmentError(f"工站私钥无法读取：{path}（{error}）") from error
