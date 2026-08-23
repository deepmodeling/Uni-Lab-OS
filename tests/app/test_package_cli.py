"""社区包 GitHub 安装归一化与安装后设备发现。"""

from __future__ import annotations

import pytest

from unilabos.app.cli import package as package_cli
from unilabos.app.cli.package import (
    InstalledDistributionRecord,
    PackageCLIError,
    install_package,
    normalize_install_spec,
)
from unilabos.app.cli.parser import build_parser


@pytest.mark.parametrize(
    ("value", "ref", "expected"),
    [
        (
            "https://github.com/example/LabDeviceLanDemo",
            None,
            "git+https://github.com/example/LabDeviceLanDemo.git",
        ),
        (
            "https://github.com/example/LabDeviceLanDemo.git",
            "0123456789abcdef",
            "git+https://github.com/example/LabDeviceLanDemo.git@0123456789abcdef",
        ),
        (
            "https://github.com/example/LabDeviceLanDemo.git@v1.2.3",
            None,
            "git+https://github.com/example/LabDeviceLanDemo.git@v1.2.3",
        ),
        (
            "git+https://github.com/example/LabDeviceLanDemo.git@main",
            None,
            "git+https://github.com/example/LabDeviceLanDemo.git@main",
        ),
        (
            "https://github.com/example/LabDeviceLanDemo.git@feature/hostlink",
            None,
            "git+https://github.com/example/LabDeviceLanDemo.git@feature/hostlink",
        ),
    ],
)
def test_normalize_install_spec_accepts_plain_and_pinned_github_urls(
    value: str,
    ref: str | None,
    expected: str,
) -> None:
    assert normalize_install_spec(value, ref=ref) == expected


@pytest.mark.parametrize(
    "value",
    [
        "lan-demo==0.2.0",
        "lan-demo @ git+https://github.com/example/LabDeviceLanDemo.git@abc123",
        "https://downloads.example.org/lan-demo-0.2.0.zip",
        "https://github.com/example/repo/archive/refs/tags/v0.2.0.zip",
        ".\\local-device-package",
    ],
)
def test_normalize_install_spec_preserves_non_repository_pip_specs(value: str) -> None:
    assert normalize_install_spec(value) == value


@pytest.mark.parametrize(
    ("value", "ref"),
    [
        ("https://github.com/example", None),
        ("https://github.com/example/repo.git@main", "other"),
        ("https://downloads.example.org/pkg.zip", "main"),
        ("https://github.com/example/repo", "bad ref"),
    ],
)
def test_normalize_install_spec_rejects_ambiguous_github_or_ref(
    value: str,
    ref: str | None,
) -> None:
    with pytest.raises(PackageCLIError):
        normalize_install_spec(value, ref=ref)


def test_package_parser_exposes_explicit_ref() -> None:
    args = build_parser().parse_args(
        [
            "package",
            "install",
            "https://github.com/example/repo",
            "--ref",
            "abc123",
        ]
    )
    assert args.install_ref == "abc123"


def _record(name: str, direct_url: str, fingerprint: str) -> InstalledDistributionRecord:
    return InstalledDistributionRecord(
        name=name,
        version="0.1.0",
        direct_url=direct_url,
        fingerprint=fingerprint,
    )


def test_url_install_discovers_real_distribution_and_scans_devices(monkeypatch) -> None:
    before = {
        "unrelated": _record("unrelated", "", "old"),
    }
    after = {
        **before,
        # 仓库名是 LabDeviceLanDemo，但 project.name 故意是 lan_demo。
        "lan_demo": _record(
            "lan_demo",
            "https://github.com/Xuwznln/LabDeviceLanDemo.git",
            "new",
        ),
    }
    snapshots = iter((before, after))
    monkeypatch.setattr(
        package_cli,
        "_installed_distribution_records",
        lambda: next(snapshots),
    )
    installed: list[str] = []
    monkeypatch.setattr(
        package_cli,
        "_run_pip_install",
        lambda spec: installed.append(spec) or "pip install",
    )
    scanned: list[str] = []
    monkeypatch.setattr(
        package_cli,
        "_installed_device_ids",
        lambda name: scanned.append(name) or ["lan_hub", "lan_sub"],
    )

    result = install_package(
        "https://github.com/Xuwznln/LabDeviceLanDemo",
        ref="0123456789abcdef",
    )

    assert installed == [
        "git+https://github.com/Xuwznln/LabDeviceLanDemo.git@0123456789abcdef"
    ]
    assert scanned == ["lan_demo"]
    assert result["dist_name"] == "lan_demo"
    assert result["device_ids"] == ["lan_hub", "lan_sub"]


def test_reinstall_uses_direct_url_even_without_distribution_diff(monkeypatch) -> None:
    record = _record(
        "workstation_demo",
        "https://github.com/Xuwznln/LabDeviceWorkstationDemo",
        "same",
    )
    snapshot = {"workstation_demo": record}
    monkeypatch.setattr(
        package_cli,
        "_installed_distribution_records",
        lambda: snapshot,
    )
    monkeypatch.setattr(package_cli, "_run_pip_install", lambda _spec: "uv pip install")
    scanned: list[str] = []
    monkeypatch.setattr(
        package_cli,
        "_installed_device_ids",
        lambda name: scanned.append(name) or ["virtual_workstation"],
    )

    result = install_package(
        "https://github.com/Xuwznln/LabDeviceWorkstationDemo.git@abc123"
    )

    assert result["dist_name"] == "workstation_demo"
    assert scanned == ["workstation_demo"]
