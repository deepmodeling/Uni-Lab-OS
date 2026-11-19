"""资源领域对象共用的严格 Pydantic 配置。"""

from pydantic import BaseModel, ConfigDict


class ResourceObject(BaseModel):
    """可跨 Edge、微后端和 Adapter 复用的严格领域对象。"""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
        validate_default=True,
        allow_inf_nan=False,
    )


__all__ = ["ResourceObject"]
