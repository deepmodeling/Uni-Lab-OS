import argparse
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple, Union

import requests

from unilabos.app.web.client import http_client, HTTPClient
from unilabos.utils import logger


def _get_oss_token(
    filename: str,
    driver_name: str = "default",
    exp_type: str = "default",
    client: Optional[HTTPClient] = None,
) -> Tuple[bool, Dict]:
    """
    获取OSS上传Token

    Args:
        filename: 文件名
        driver_name: 驱动名称
        exp_type: 实验类型
        client: HTTPClient实例，如果不提供则使用默认的http_client

    Returns:
        (成功标志, Token数据字典包含token/path/host/expires)
    """
    # 使用提供的client或默认的http_client
    if client is None:
        client = http_client

    # 构造scene参数: driver_name-exp_type
    scene = f"{driver_name}-{exp_type}"

    # 构造请求URL，使用client的remote_addr（已包含/api/v1/）
    url = f"{client.remote_addr}/applications/token"
    params = {"scene": scene, "filename": filename}

    try:
        logger.info(f"[OSS] 请求预签名URL: scene={scene}, filename={filename}")
        response = requests.get(url, params=params, headers={"Authorization": f"Lab {client.auth}"}, timeout=10)

        if response.status_code == 200:
            result = response.json()
            if result.get("code") == 0:
                data = result.get("data", {})

                # 转换expires时间戳为可读格式
                expires_timestamp = data.get("expires", 0)
                expires_datetime = datetime.fromtimestamp(expires_timestamp)
                expires_str = expires_datetime.strftime("%Y-%m-%d %H:%M:%S")

                logger.info(f"[OSS] 获取预签名URL成功")
                logger.info(f"[OSS]   - URL: {data.get('url', 'N/A')}")
                logger.info(f"[OSS]   - Expires: {expires_str} (timestamp: {expires_timestamp})")

                return True, data

        logger.error(f"[OSS] 获取预签名URL失败: {response.status_code}, {response.text}")
        return False, {}
    except Exception as e:
        logger.error(f"[OSS] 获取预签名URL异常: {str(e)}")
        return False, {}


def _put_upload(file_path: str, upload_url: str) -> bool:
    """
    使用预签名URL上传文件到OSS

    Args:
        file_path: 本地文件路径
        upload_url: 完整的预签名上传URL

    Returns:
        是否成功
    """
    try:
        logger.info(f"[OSS] 开始上传文件: {file_path}")

        with open(file_path, "rb") as f:
            # 使用预签名URL上传，不需要额外的认证header
            response = requests.put(upload_url, data=f, timeout=300)

            if response.status_code == 200:
                logger.info(f"[OSS] 文件上传成功")
                return True

        logger.error(f"[OSS] 上传失败: {response.status_code}")
        logger.error(f"[OSS] 响应内容: {response.text[:500] if response.text else '无响应内容'}")
        return False
    except Exception as e:
        logger.error(f"[OSS] 上传异常: {str(e)}")
        return False


def oss_upload(
    file_path: Union[str, Path],
    filename: Optional[str] = None,
    driver_name: str = "default",
    exp_type: str = "default",
    max_retries: int = 3,
    client: Optional[HTTPClient] = None,
) -> Dict:
    """
    文件上传主函数，包含重试机制

    Args:
        file_path: 本地文件路径
        filename: 文件名，如果为None则使用file_path的文件名
        driver_name: 驱动名称，用于构造scene
        exp_type: 实验类型，用于构造scene
        max_retries: 最大重试次数
        client: HTTPClient实例，如果不提供则使用默认的http_client

    Returns:
        Dict: {
            "success": bool,  # 是否上传成功
            "original_path": str,  # 原始文件路径
            "oss_path": str  # OSS路径（成功时）或空字符串（失败时）
        }
    """
    file_path = Path(file_path)
    if filename is None:
        filename = os.path.basename(file_path)

    if not os.path.exists(file_path):
        logger.error(f"[OSS] 文件不存在: {file_path}")
        return {"success": False, "original_path": file_path, "oss_path": ""}

    retry_count = 0
    oss_path = ""

    while retry_count < max_retries:
        try:
            # 步骤1：获取预签名URL
            token_success, token_data = _get_oss_token(
                filename=filename, driver_name=driver_name, exp_type=exp_type, client=client
            )

            if not token_success:
                logger.warning(f"[OSS] 获取预签名URL失败，重试 {retry_count + 1}/{max_retries}")
                retry_count += 1
                time.sleep(1)
                continue

            # 获取预签名URL和OSS路径
            upload_url = token_data.get("url")
            oss_path = token_data.get("path", "")

            if not upload_url:
                logger.warning(f"[OSS] 无法获取上传URL，API未返回url字段")
                retry_count += 1
                time.sleep(1)
                continue

            # 步骤2：PUT上传文件
            put_success = _put_upload(file_path, upload_url)
            if not put_success:
                logger.warning(f"[OSS] PUT上传失败，重试 {retry_count + 1}/{max_retries}")
                retry_count += 1
                time.sleep(1)
                continue

            # 所有步骤都成功
            logger.info(f"[OSS] 文件 {file_path} 上传成功")
            return {"success": True, "original_path": file_path, "oss_path": oss_path}

        except Exception as e:
            logger.error(f"[OSS] 上传过程异常: {str(e)}，重试 {retry_count + 1}/{max_retries}")
            retry_count += 1
            time.sleep(1)

    logger.error(f"[OSS] 文件 {file_path} 上传失败，已达到最大重试次数 {max_retries}")
    return {"success": False, "original_path": file_path, "oss_path": oss_path}


if __name__ == "__main__":
    # python -m unilabos.app.oss_upload -f /path/to/your/file.txt --driver HPLC --type test
    # python -m unilabos.app.oss_upload -f /path/to/your/file.txt --driver HPLC --type test \
    #        --ak xxx --sk yyy --remote-addr http://xxx/api/v1
    # 命令行参数解析
    parser = argparse.ArgumentParser(description="文件上传测试工具")
    parser.add_argument("--file", "-f", type=str, required=True, help="要上传的本地文件路径")
    parser.add_argument("--driver", "-d", type=str, default="default", help="驱动名称")
    parser.add_argument("--type", "-t", type=str, default="default", help="实验类型")
    parser.add_argument("--ak", type=str, help="Access Key，如果提供则覆盖配置")
    parser.add_argument("--sk", type=str, help="Secret Key，如果提供则覆盖配置")
    parser.add_argument("--remote-addr", type=str, help="远程服务器地址（包含/api/v1），如果提供则覆盖配置")

    args = parser.parse_args()

    # 检查文件是否存在
    if not os.path.exists(args.file):
        logger.error(f"错误：文件 {args.file} 不存在")
        exit(1)

    # 如果提供了ak/sk/remote_addr，创建临时HTTPClient
    temp_client = None
    if args.ak and args.sk:
        import base64

        auth = base64.b64encode(f"{args.ak}:{args.sk}".encode("utf-8")).decode("utf-8")
        remote_addr = args.remote_addr if args.remote_addr else http_client.remote_addr
        temp_client = HTTPClient(remote_addr=remote_addr, auth=auth)
        logger.info(f"[配置] 使用自定义配置: remote_addr={remote_addr}")
    elif args.remote_addr:
        temp_client = HTTPClient(remote_addr=args.remote_addr, auth=http_client.auth)
        logger.info(f"[配置] 使用自定义remote_addr: {args.remote_addr}")
    else:
        logger.info(f"[配置] 使用默认配置: remote_addr={http_client.remote_addr}")

    logger.info("=" * 50)
    logger.info(f"开始上传文件: {args.file}")
    logger.info(f"驱动名称: {args.driver}")
    logger.info(f"实验类型: {args.type}")
    logger.info(f"Scene: {args.driver}-{args.type}")
    logger.info("=" * 50)

    # 执行上传
    result = oss_upload(
        file_path=args.file,
        filename=None,  # 使用默认文件名
        driver_name=args.driver,
        exp_type=args.type,
        client=temp_client,
    )

    # 输出结果
    if result["success"]:
        logger.info(f"\n√ 文件上传成功！")
        logger.info(f"原始路径: {result['original_path']}")
        logger.info(f"OSS路径: {result['oss_path']}")
        exit(0)
    else:
        logger.error(f"\n× 文件上传失败！")
        logger.error(f"原始路径: {result['original_path']}")
        exit(1)
