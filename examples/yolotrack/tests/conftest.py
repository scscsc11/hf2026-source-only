"""conftest: 注册 mock_redis / mock_frame 等 fixture。"""
import pytest

from tests.fixtures.mock_redis import MockRedis
from tests.fixtures.mock_frame import make_synthetic_frame


@pytest.fixture
def mock_redis() -> MockRedis:
    """每个测试一个全新的 MockRedis 实例。"""
    return MockRedis()


@pytest.fixture
def mock_frame_factory():
    """提供 make_synthetic_frame 函数（fixture 形式更明确）。"""
    return make_synthetic_frame
