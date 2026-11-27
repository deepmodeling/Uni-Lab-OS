"""离线准备轮换材料：私钥与 secret 留在本机，只导出 CSR、哈希与幂等请求。"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from unilabos.app.center_client.credential_files import create_private_directory, sync_directory
from unilabos.app.center_client.enrollment import PRIVATE_KEY_FILE, generate_key_and_csr, write_private


@dataclass(frozen=True)
class RotationPreparation:
    directory: Path
    request_file: Path
    private_key_file: Path
    machine_secret_file: Path


def prepare_rotation(directory: Path, *, station_uuid: str, instance_uuid: str,
                     replaced_credential_uuid: str, grace_ms: int, reason: str) -> RotationPreparation:
    """新建独立目录；任何现存目录均拒绝覆盖，响应丢失时重用已保存的原请求。"""
    for value in (station_uuid, instance_uuid, replaced_credential_uuid):
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError("轮换身份必须为规范 UUID")
    if type(grace_ms) is not int or not 0 <= grace_ms <= 86_400_000:
        raise ValueError("旧凭据宽限必须在 0 至 24 小时内")
    if not isinstance(reason, str) or not reason.strip() or len(reason.encode()) > 500:
        raise ValueError("轮换原因必须为非空且不超过 500 字节的文本")
    directory = Path(directory)
    key = generate_key_and_csr(station_uuid)
    machine_secret = secrets.token_hex(32)
    request = {
        "path": f"/api/v1/stations/{station_uuid}/credentials/rotate",
        "idempotency_key": str(uuid4()),
        "body": {
            "instance_uuid": instance_uuid,
            "replaced_credential_uuid": replaced_credential_uuid,
            "csr_pem": key.csr_pem,
            "new_machine_secret_hash": "sha256:" + hashlib.sha256(machine_secret.encode()).hexdigest(),
            "grace_ms": grace_ms,
            "reason": reason,
        },
    }
    result = RotationPreparation(directory, directory / "rotation-request.json",
                                 directory / PRIVATE_KEY_FILE, directory / "machine-secret.txt")
    # mkdir 必须成功创建新目录后才有清理权；不能删除调用者已有目录或跟随符号链接。
    create_private_directory(directory)
    try:
        write_private(result.private_key_file, key.private_key_pem)
        write_private(result.machine_secret_file, machine_secret)
        write_private(result.request_file, json.dumps(request, ensure_ascii=False, indent=2) + "\n")
        for durable_directory in (directory, directory.parent):
            sync_directory(durable_directory)
    except BaseException:
        shutil.rmtree(directory)
        raise
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="不存在的新材料目录，父目录必须已存在")
    for option in ("station-uuid", "instance-uuid", "replaced-credential-uuid", "reason"):
        parser.add_argument("--" + option, required=True)
    parser.add_argument("--grace-ms", type=int, default=3_600_000)
    args = parser.parse_args()
    result = prepare_rotation(**vars(args))
    print(f"轮换材料已保存；只向管理员提交 {result.request_file}，保留同目录的本地私密材料。")


if __name__ == "__main__":
    main()
