"""param_resolver 与 Go gjson/sjson + ``@@@`` 语义对齐测试。

对照 snapshot dag.go parsePreNodeParam（行 600-670）与 engine/model.go DataKeySplit。
"""

import pytest

from unilabos.app.scheduler.models import Handle, HandlePair
from unilabos.app.scheduler.param_resolver import (
    ParamResolveError,
    json_get,
    json_get_exists,
    json_set,
    resolve_parent_params,
    split_path,
)


class TestSplitPath:
    def test_dot_path(self):
        assert split_path("a.b.c") == ["a", "b", "c"]

    def test_escaped_dot(self):
        # gjson: "a\.b" 是字面 key "a.b"
        assert split_path(r"a\.b.c") == ["a.b", "c"]

    def test_single_segment(self):
        assert split_path("key") == ["key"]


class TestJsonGet:
    def test_nested_dict(self):
        assert json_get({"a": {"b": {"c": 1}}}, "a.b.c") == 1

    def test_array_index(self):
        assert json_get({"a": [10, 20, 30]}, "a.1") == 20

    def test_array_length_hash(self):
        # gjson: "a.#" 返回数组长度
        assert json_get({"a": [1, 2, 3]}, "a.#") == 3

    def test_missing_path(self):
        exists, _ = json_get_exists({"a": 1}, "b")
        assert exists is False

    def test_missing_deep(self):
        exists, _ = json_get_exists({"a": {"b": 1}}, "a.c.d")
        assert exists is False

    def test_escaped_dot_key(self):
        exists, value = json_get_exists({"a.b": 5}, r"a\.b")
        assert exists and value == 5


class TestJsonSet:
    def test_set_existing_key(self):
        assert json_set({"x": 1}, "x", 2) == {"x": 2}

    def test_create_nested(self):
        # sjson: 缺失中间层自动创建 object
        assert json_set({}, "a.b.c", 7) == {"a": {"b": {"c": 7}}}

    def test_no_mutation(self):
        original = {"a": {"b": 1}}
        result = json_set(original, "a.b", 2)
        assert original == {"a": {"b": 1}}
        assert result == {"a": {"b": 2}}

    def test_array_index_set(self):
        assert json_set({"a": [1, 2, 3]}, "a.1", 9) == {"a": [1, 9, 3]}

    def test_array_pad(self):
        # sjson: 数组越界赋值时以 null 填充
        assert json_set({"a": [1]}, "a.3", 9) == {"a": [1, None, None, 9]}

    def test_none_target_becomes_dict(self):
        assert json_set(None, "k", 1) == {"k": 1}

    def test_scalar_overwritten_by_object(self):
        # sjson.Set: 中间层是标量时直接覆写为 object
        assert json_set({"a": 5}, "a.b", 1) == {"a": {"b": 1}}


def _pair(source_node_id: str, source_data_key: str, target_data_key: str) -> HandlePair:
    return HandlePair(
        source_node_id=source_node_id,
        source_handle=Handle(
            uuid="sh", data_source="executor", handle_key="out", data_key=source_data_key
        ),
        target_handle=Handle(
            uuid="th", data_source="handle", handle_key="in", data_key=target_data_key
        ),
    )


class TestResolveParentParams:
    def test_simple_pass(self):
        # Go: res = gjson.Get(ret, "volume"); sjson.Set(param, "target_volume", res)
        param = resolve_parent_params(
            {"target_volume": 0},
            [_pair("n1", "volume", "target_volume")],
            {"n1": {"volume": 42}},
        )
        assert param == {"target_volume": 42}

    def test_nested_source_key(self):
        param = resolve_parent_params(
            {},
            [_pair("n1", "result.plate.id", "plate_id")],
            {"n1": {"result": {"plate": {"id": "P1"}}}},
        )
        assert param == {"plate_id": "P1"}

    def test_triple_at_split(self):
        # Go: dataKeys = split("inner@@@deep@@@target_key", "@@@")
        #     前两段继续 gjson.Get 下钻，最后一段是 sjson.Set 路径
        param = resolve_parent_params(
            {},
            [_pair("n1", "data", "inner@@@deep@@@target_key")],
            {"n1": {"data": {"inner": {"deep": {"x": 1}}}}},
        )
        assert param == {"target_key": {"x": 1}}

    def test_triple_at_with_dot_set_path(self):
        # 最后一段本身可以是 dot 路径（sjson.Set 语义）
        param = resolve_parent_params(
            {"cfg": {"keep": True}},
            [_pair("n1", "out", "sub@@@cfg.value")],
            {"n1": {"out": {"sub": 99}}},
        )
        assert param == {"cfg": {"keep": True, "value": 99}}

    def test_source_missing_raises(self):
        # Go: !res.Exists() → code.ValueNotExistErr
        with pytest.raises(ParamResolveError):
            resolve_parent_params(
                {},
                [_pair("n1", "not_there", "k")],
                {"n1": {"volume": 1}},
            )

    def test_nested_missing_raises(self):
        with pytest.raises(ParamResolveError):
            resolve_parent_params(
                {},
                [_pair("n1", "data", "missing@@@k")],
                {"n1": {"data": {"present": 1}}},
            )

    def test_parent_ret_missing_raises(self):
        # Go: 找不到父 job → code.CanNotGetParentJobErr
        with pytest.raises(ParamResolveError):
            resolve_parent_params({}, [_pair("n1", "v", "k")], {})

    def test_multiple_pairs_apply_in_order(self):
        param = resolve_parent_params(
            {},
            [
                _pair("n1", "a", "x"),
                _pair("n2", "b", "y"),
            ],
            {"n1": {"a": 1}, "n2": {"b": 2}},
        )
        assert param == {"x": 1, "y": 2}
