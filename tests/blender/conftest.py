"""Blender Bullet 相关测试的公共夹具。

独立 Bullet 库需要 conda 工具链与 bullet-cpp；缺失时跳过而不是伪造通过。
设置 ``UNILABOS_BULLET_STANDALONE_REQUIRE=1`` 可把缺失视为失败（本机验收用）。
"""

import pytest

from unilabos.simulation.bullet_standalone import loader


@pytest.fixture(scope="session")
def abi():
    available, reason = loader.availability()
    if not available:
        if loader.require_environment():
            pytest.fail(reason)
        pytest.skip(reason)
    try:
        return loader.load()
    except loader.BulletBuildError as error:
        pytest.fail(str(error))
