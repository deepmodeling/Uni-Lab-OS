"""模型文件上传/下载管理。

提供 Edge 端本地模型文件与 OSS 之间的双向同步：
- upload_device_model: 本地模型 → OSS（Edge 首次接入时）
- download_model_from_oss: OSS → 本地（新 Edge 加入已有 Lab 时）
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import requests

from unilabos.utils.log import logger

if TYPE_CHECKING:
    from unilabos.app.web.client import HTTPClient

# 设备 mesh 根目录
_MESH_BASE_DIR = Path(__file__).parent.parent / "device_mesh"

# 支持的模型文件后缀
_MODEL_EXTENSIONS = frozenset({
    ".xacro", ".urdf", ".stl", ".dae", ".obj",
    ".gltf", ".glb", ".fbx", ".yaml", ".yml",
})

# 需要 XOR 加密/解密的 mesh 文件后缀（反爬保护 — 方案 C）
_MESH_ENCRYPT_EXTENSIONS = frozenset({
    ".stl", ".dae", ".obj", ".fbx", ".gltf", ".glb",
})

# XOR 密钥 — 从环境变量读取，与前端 mesh-decrypt.ts 一致
_XOR_KEY = os.environ.get("UNILAB_MESH_XOR_KEY", "unilab3d-model-protection-key-v1").encode()


def _xor_transform(data: bytes, key: bytes = _XOR_KEY) -> bytes:
    """XOR 加密/解密（对称操作）。"""
    key_len = len(key)
    return bytes(b ^ key[i % key_len] for i, b in enumerate(data))


def upload_device_model(
    http_client: "HTTPClient",
    template_uuid: str,
    mesh_name: str,
    model_type: str,
    version: str = "1.0.0",
) -> Optional[str]:
    """上传本地模型文件到 OSS，返回入口文件的 OSS URL。

    Args:
        http_client: HTTPClient 实例
        template_uuid: 设备模板 UUID
        mesh_name: mesh 目录名（如 "arm_slider"）
        model_type: "device" 或 "resource"
        version: 模型版本

    Returns:
        入口文件 OSS URL，上传失败返回 None
    """
    if model_type == "device":
        model_dir = _MESH_BASE_DIR / "devices" / mesh_name
    else:
        model_dir = _MESH_BASE_DIR / "resources" / mesh_name

    if not model_dir.exists():
        logger.warning(f"[模型上传] 本地目录不存在: {model_dir}")
        return None

    # 收集所有需要上传的文件
    files = []
    for f in model_dir.rglob("*"):
        if f.is_file() and f.suffix.lower() in _MODEL_EXTENSIONS:
            files.append({
                "name": str(f.relative_to(model_dir)),
                "size_kb": f.stat().st_size // 1024,
            })

    if not files:
        logger.warning(f"[模型上传] 目录中无可上传的模型文件: {model_dir}")
        return None

    try:
        # 1. 获取预签名上传 URL
        upload_urls_resp = http_client.get_model_upload_urls(
            template_uuid=template_uuid,
            files=[{"name": f["name"], "version": version} for f in files],
        )
        if not upload_urls_resp:
            return None

        url_items = upload_urls_resp.get("files", [])

        # 2. 逐个上传文件
        for file_info, url_info in zip(files, url_items):
            local_path = model_dir / file_info["name"]
            upload_url = url_info.get("upload_url", "")
            if not upload_url:
                continue
            _put_upload(local_path, upload_url)

        # 3. 确认发布
        entry_file = "macro_device.xacro" if model_type == "device" else "modal.xacro"
        # 检查入口文件是否存在，使用实际存在的文件名
        for f in files:
            if f["name"].endswith(".xacro"):
                entry_file = f["name"]
                break

        publish_resp = http_client.publish_model(
            template_uuid=template_uuid,
            version=version,
            entry_file=entry_file,
        )
        return publish_resp.get("path") if publish_resp else None

    except Exception as e:
        logger.error(f"[模型上传] 上传失败 ({mesh_name}): {e}")
        return None


def download_model_from_oss(
    model_config: dict,
    mesh_base_dir: Optional[Path] = None,
) -> bool:
    """检查本地模型文件是否存在，不存在则从 OSS 下载。

    Args:
        model_config: 节点的 model 配置字典
        mesh_base_dir: mesh 根目录，默认使用 device_mesh/

    Returns:
        True 表示本地文件就绪，False 表示下载失败或无需下载
    """
    if mesh_base_dir is None:
        mesh_base_dir = _MESH_BASE_DIR

    mesh_name = model_config.get("mesh", "")
    model_type = model_config.get("type", "")
    oss_path = model_config.get("path", "")

    if not mesh_name or not oss_path or not oss_path.startswith("https://"):
        return False

    # 确定本地目标目录
    if model_type == "device":
        local_dir = mesh_base_dir / "devices" / mesh_name
    elif model_type == "resource":
        resource_name = mesh_name.split("/")[0]
        local_dir = mesh_base_dir / "resources" / resource_name
    else:
        return False

    # 已有本地文件 → 跳过
    if local_dir.exists() and any(local_dir.iterdir()):
        return True

    # 从 OSS 下载
    local_dir.mkdir(parents=True, exist_ok=True)
    try:
        # 下载入口文件（OSS URL 通常直接可访问）
        entry_name = oss_path.rsplit("/", 1)[-1]
        _download_file(oss_path, local_dir / entry_name)

        # 如果有 children_mesh，也下载
        children_mesh = model_config.get("children_mesh")
        if isinstance(children_mesh, dict) and children_mesh.get("path"):
            cm_path = children_mesh["path"]
            if cm_path.startswith("https://"):
                cm_name = cm_path.rsplit("/", 1)[-1]
                meshes_dir = local_dir / "meshes"
                meshes_dir.mkdir(parents=True, exist_ok=True)
                _download_file(cm_path, meshes_dir / cm_name)

        logger.info(f"[模型下载] 成功下载模型到本地: {mesh_name} → {local_dir}")
        return True

    except Exception as e:
        logger.warning(f"[模型下载] 下载失败 ({mesh_name}): {e}")
        return False


def _put_upload(local_path: Path, upload_url: str) -> None:
    """通过预签名 URL 上传文件到 OSS。对 mesh 文件自动 XOR 加密。"""
    with open(local_path, "rb") as f:
        data = f.read()
    # 对 mesh 文件 XOR 加密后上传（反爬保护 — 方案 C）
    if local_path.suffix.lower() in _MESH_ENCRYPT_EXTENSIONS:
        data = _xor_transform(data)
        logger.debug(f"[模型上传] XOR 加密: {local_path.name}")
    resp = requests.put(upload_url, data=data, timeout=120)
    resp.raise_for_status()
    logger.debug(f"[模型上传] 已上传: {local_path.name}")


def _download_file(url: str, local_path: Path) -> None:
    """下载单个文件到本地路径。对 mesh 文件自动 XOR 解密。"""
    local_path.parent.mkdir(parents=True, exist_ok=True)
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    data = resp.content
    # 从 OSS 下载的 mesh 文件是加密的，需要 XOR 解密后再存本地
    if local_path.suffix.lower() in _MESH_ENCRYPT_EXTENSIONS:
        data = _xor_transform(data)
        logger.debug(f"[模型下载] XOR 解密: {local_path.name}")
    local_path.write_bytes(data)
    logger.debug(f"[模型下载] 已下载: {local_path}")
