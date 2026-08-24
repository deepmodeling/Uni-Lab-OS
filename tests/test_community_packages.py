import json
from pathlib import Path

from unilabos.app.community_packages import (
    extract_community_classes,
    prepare_community_packages,
)


def test_extract_community_classes_dedupes_and_ignores_regular_nodes():
    graph = {
        "nodes": [
            {"id": "a", "class": "community.counter.counting_device"},
            {"id": "b", "class": "regular_device"},
            {"id": "c", "class": "community.counter.counting_device"},
            {"id": "d"},
        ]
    }

    assert extract_community_classes(graph) == ["community.counter.counting_device"]


def test_prepare_community_packages_uses_local_manifest_cache(tmp_path: Path):
    package_dir = tmp_path / "community_devices" / "counter" / "0.1.0" / "package"
    package_dir.mkdir(parents=True)
    manifest = {
        "packages": {
            "community.counter": {
                "version": "0.1.0",
                "sha256": "sha256:test",
                "package_dir": str(package_dir),
                "aliases": {
                    "community.counter.counting_device": "counting_device",
                },
            }
        }
    }
    manifest_path = tmp_path / "community_devices" / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = prepare_community_packages(
        {"nodes": [{"id": "counter_1", "class": "community.counter.counting_device"}]},
        working_dir=tmp_path,
    )

    assert result.devices_dirs == [str(package_dir.resolve())]
    # 社区包目录 -> 命名空间映射，供注册表扫描期把 device/resource id 命名空间化
    assert result.namespaces == {str(package_dir.resolve()): "community.counter"}
