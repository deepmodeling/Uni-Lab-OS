"""公共资产接收边界必须拒绝歧义输入，且不执行资产源码。"""

import json

import pytest

from unilabos.assets.contracts import (
    ContractError,
    number,
    read_json,
    safe_file,
    sha256_file,
    validate_frame_tree,
    validate_sources,
    write_acceptance,
)


@pytest.mark.parametrize("body", ['{"x":1,"x":2}', '[NaN]', '[1e999]'])
def test_json_rejects_ambiguous_or_nonfinite_values(tmp_path, body):
    path = tmp_path / "source.json"
    path.write_text(body)
    with pytest.raises(ContractError):
        read_json(path)


@pytest.mark.parametrize("value", [True, float("inf"), 10 ** 400])
def test_numbers_must_be_finite_non_boolean_values(value):
    with pytest.raises(ContractError, match="invalid_number"):
        number(value)


@pytest.mark.parametrize("path", ["../secret", "/secret", "a/../secret", "a//secret", "C:secret", "a\\secret"])
def test_sources_cannot_escape_the_explicit_root(tmp_path, path):
    with pytest.raises(ContractError, match="path_escape"):
        safe_file(tmp_path, path)


def test_source_validation_does_not_execute_and_detects_change(tmp_path):
    source = tmp_path / "model.py"
    source.write_text('raise RuntimeError("never import an untrusted source")')
    entry = {"source_id": "model", "path": source.name, "sha256": sha256_file(source), "bytes": source.stat().st_size}
    assert validate_sources([entry], tmp_path, tmp_path / "contract.json") == {"model": source}
    source.write_text(source.read_text().replace("never", "NEVER"))
    with pytest.raises(ContractError, match="source_digest_mismatch"):
        validate_sources([entry], tmp_path, tmp_path / "contract.json")
    alias = tmp_path / "alias.py"
    alias.symlink_to(source)
    with pytest.raises(ContractError, match="symlink_dependency"):
        safe_file(tmp_path, alias.name)


def test_disconnected_cycle_cannot_hide_behind_a_valid_root():
    def frame(name, parent):
        return {"frame_id": name, "parent": parent, "translation_m": [0, 0, 0],
                "quaternion_xyzw": [0, 0, 0, 1], "provenance": "fixture"}
    with pytest.raises(ContractError, match="frame_cycle_or_missing_parent"):
        validate_frame_tree([frame("root", None), frame("a", "b"), frame("b", "a")])


def test_acceptance_is_exclusive_and_seals_exact_written_bytes(tmp_path):
    target = tmp_path / "accepted"
    result = {"activation": {"status": "unknown"}, "receipt": {"hardware": False}}
    write_acceptance(target, result)
    seal = json.loads((target / "seal.json").read_text())
    for name, digest in seal["files"].items():
        assert sha256_file(target / name) == digest
    with pytest.raises(FileExistsError):
        write_acceptance(target, result)
