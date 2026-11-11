"""驱动包管理请求；HTTP、CLI 与 SDK 共用，源码登记由 Host 服务负责。"""

from pydantic import BaseModel, Field
from unilabos.protocol.runtime.resource_intervals import ResourceIntervalReleaseRequest


class DriverPackageInstallRequest(BaseModel):
    """源码来源为 GitHub、归档 URL 或 Host 本机目录；只安装第三方依赖。"""

    spec: str = Field(min_length=1)
    enable: bool = True
    upgrade: bool = Field(default=False, description="重新下载源码树，并以 --upgrade 重装其依赖")
    name: str = Field(default="", description="源码树没有 pyproject 时用于登记的包名")


class DriverPackageEnableRequest(BaseModel):
    enabled: bool


class DriverPackageOperationConfirmRequest(ResourceIntervalReleaseRequest):
    """核实中断安装后的源码、台账与共享依赖；此操作不触发重启。"""
