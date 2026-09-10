"""本地资源解析：duck-typed 树上的 uuid/id 查找与子树导出。"""

import pytest

from unilabos.hostlink.resolver import LocalResourceResolver, ResourceNotFound


class _Content:
    def __init__(self, uuid, res_id, name):
        self.uuid = uuid
        self.id = res_id
        self._name = name

    def model_dump(self, by_alias=True):
        return {"uuid": self.uuid, "id": self.id, "name": self._name}


class _Node:
    def __init__(self, uuid, res_id, name, children=None):
        self.res_content = _Content(uuid, res_id, name)
        self.children = children or []

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()


class _Tree:
    def __init__(self, root):
        self.root = root

    def get_all_nodes(self):
        return list(self.root.walk())


class _TreeSet:
    def __init__(self, *roots):
        self.trees = [_Tree(r) for r in roots]


@pytest.fixture()
def tree_set():
    #   station(u-1)
    #     ├─ deck(u-2)
    #     │    └─ plate(u-3)
    #     └─ arm(u-4)
    plate = _Node("u-3", "plate_1", "plate")
    deck = _Node("u-2", "deck_1", "deck", [plate])
    arm = _Node("u-4", "arm_1", "arm")
    station = _Node("u-1", "station_1", "station", [deck, arm])
    return _TreeSet(station)


class TestResolve:
    def test_by_uuid_with_children(self, tree_set):
        resolver = LocalResourceResolver(lambda: tree_set)
        nodes = resolver.resolve(uuid="u-2")
        assert [n["uuid"] for n in nodes] == ["u-2", "u-3"]

    def test_by_uuid_without_children(self, tree_set):
        resolver = LocalResourceResolver(lambda: tree_set)
        nodes = resolver.resolve(uuid="u-1", with_children=False)
        assert [n["uuid"] for n in nodes] == ["u-1"]

    def test_by_id_fallback(self, tree_set):
        resolver = LocalResourceResolver(lambda: tree_set)
        nodes = resolver.resolve(res_id="arm_1")
        assert [n["id"] for n in nodes] == ["arm_1"]

    def test_missing_raises(self, tree_set):
        resolver = LocalResourceResolver(lambda: tree_set)
        with pytest.raises(ResourceNotFound):
            resolver.resolve(uuid="u-404")

    def test_tree_not_ready(self):
        resolver = LocalResourceResolver(lambda: None)
        with pytest.raises(ResourceNotFound, match="not ready"):
            resolver.resolve(uuid="u-1")

    def test_requires_key(self, tree_set):
        resolver = LocalResourceResolver(lambda: tree_set)
        with pytest.raises(ValueError):
            resolver.resolve()

    def test_dump_all(self, tree_set):
        resolver = LocalResourceResolver(lambda: tree_set)
        assert len(resolver.dump_all()) == 4
