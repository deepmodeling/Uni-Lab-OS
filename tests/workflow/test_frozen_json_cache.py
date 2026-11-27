"""大字段解码缓存必须按内容精确命中、不共享顶层容器、小字段不进缓存。"""
from __future__ import annotations

import json

import pytest

from unilabos.workflow import frozen_json_cache as cache_module
from unilabos.workflow.frozen_json_cache import (
    CACHE_MIN_TEXT_LENGTH,
    cache_stats,
    clear_cache,
    decode_large_json_text,
)


@pytest.fixture(autouse=True)
def _reset_cache():
    clear_cache()
    yield
    clear_cache()


def _large_plan(tag: str, padding: int = CACHE_MIN_TEXT_LENGTH) -> str:
    nodes = [{"uuid": f"{tag}-{i}", "payload": "x" * 64} for i in range(padding // 80)]
    return json.dumps({"tag": tag, "nodes": nodes})


def _counting_decoder():
    calls = {"n": 0}

    def decode(text: str):
        calls["n"] += 1
        return json.loads(text)

    return decode, calls


def test_same_content_decodes_once_and_returns_equal_value():
    decode, calls = _counting_decoder()
    text = _large_plan("a")
    first = decode_large_json_text(text, decode)
    second = decode_large_json_text(str(text), decode)  # 新字符串对象, 内容相同
    assert first == second == json.loads(text)
    assert calls["n"] == 1
    assert cache_stats()["hits"] == 1


def test_top_level_mutation_does_not_leak_into_next_call():
    decode, _ = _counting_decoder()
    text = _large_plan("b")
    first = decode_large_json_text(text, decode)
    first["nodes"] = []
    first["injected"] = True
    second = decode_large_json_text(text, decode)
    assert "injected" not in second
    assert len(second["nodes"]) == len(json.loads(text)["nodes"])


def test_same_length_prefix_suffix_but_different_middle_is_not_a_hit():
    decode, calls = _counting_decoder()
    base = _large_plan("c")
    middle = len(base) // 2
    altered = base[:middle] + ("Y" if base[middle] != "Y" else "Z") + base[middle + 1 :]
    assert len(altered) == len(base) and altered != base
    first = decode_large_json_text(base, decode)
    second = decode_large_json_text(altered, decode)  # 未命中, 真的走了解码器
    assert calls["n"] == 2
    assert first != second
    assert cache_stats()["hits"] == 0


def test_rewritten_plan_replaces_stale_entry_by_content():
    decode, _ = _counting_decoder()
    before = _large_plan("d")
    after = json.dumps({**json.loads(before), "inventory_resource_binding": "bound"})
    assert decode_large_json_text(before, decode)["tag"] == "d"
    assert decode_large_json_text(after, decode)["inventory_resource_binding"] == "bound"
    assert "inventory_resource_binding" not in decode_large_json_text(before, decode)


def test_small_text_bypasses_cache():
    decode, calls = _counting_decoder()
    small = json.dumps({"k": 1})
    for _ in range(3):
        assert decode_large_json_text(small, decode) == {"k": 1}
    assert calls["n"] == 3
    assert cache_stats() == {"hits": 0, "misses": 0, "entries": 0}


def test_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(cache_module, "CACHE_MAX_ENTRIES", 2)
    decode, _ = _counting_decoder()
    for tag in ("e1", "e2", "e3"):
        decode_large_json_text(_large_plan(tag), decode)
    assert cache_stats()["entries"] == 2


def test_store_get_task_hits_cache_for_plan_and_snapshot(tmp_path):
    from unilabos.workflow.store import _load

    plan_text = _large_plan("store")
    assert _load(plan_text, {}) == json.loads(plan_text)
    assert _load(plan_text, {}) == json.loads(plan_text)
    stats = cache_stats()
    assert stats["misses"] == 1 and stats["hits"] == 1
