from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from unilabos.registry.ast_registry_scanner import scan_directory
from unilabos.registry.decorators import device, get_device_meta


def test_device_decorator_keeps_supported_backends() -> None:
    @device(
        id="backend_metadata_runtime_test",
        category=["test"],
        supported_backends=["hostlink", "ros2"],
    )
    class RuntimeDriver:
        pass

    metadata = get_device_meta(RuntimeDriver)
    assert metadata is not None
    assert metadata["supported_backends"] == ["hostlink", "ros2"]


def test_ast_scanner_keeps_supported_backends(tmp_path) -> None:
    source = tmp_path / "driver.py"
    source.write_text(
        "\n".join(
            [
                "from unilabos.registry.decorators import device",
                "",
                "@device(",
                "    id='backend_metadata_ast_test',",
                "    category=['test'],",
                "    supported_backends=['basic', 'hostlink', 'ros2'],",
                ")",
                "class Driver:",
                "    pass",
            ]
        ),
        encoding="utf-8",
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        result = scan_directory(
            tmp_path,
            python_path=tmp_path,
            executor=executor,
        )

    metadata = result["devices"]["backend_metadata_ast_test"]
    assert metadata["supported_backends"] == ["basic", "hostlink", "ros2"]
