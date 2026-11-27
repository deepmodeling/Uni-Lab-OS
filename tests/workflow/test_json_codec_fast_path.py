"""浅层大图加速必须保留深度、数值和语法的完整拒绝边界。"""
import sys

import pytest

from unilabos.workflow.json_codec import decode_json_bytes


@pytest.mark.parametrize("raw", [b'NaN', b'Infinity', b'-Infinity', b'1e400', b'{"x":1e400}', b'[1,]', b'{"x":1,}', b'01', b'{}{}'])
def test_fast_path_rejects_invalid_values(raw):
    with pytest.raises(ValueError):
        decode_json_bytes(raw)


@pytest.mark.parametrize("depth", [2, 10000])
def test_depth_budget_is_checked_even_for_overwritten_duplicate_key(depth):
    raw = b'{"x":' + b'[' * depth + b'0' + b']' * depth + b',"x":0}'
    with pytest.raises(ValueError):
        decode_json_bytes(raw, max_depth=depth)


def test_deep_value_falls_back_without_changing_process_recursion_limit():
    limit = sys.getrecursionlimit()
    value = decode_json_bytes(b'[' * 10000 + b'0' + b']' * 10000)
    for _ in range(10000):
        value = value[0]
    assert value == 0
    assert sys.getrecursionlimit() == limit
