"""资产接收的公共数据校验；领域能力、工位绑定和资格声明由调用方判断。"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn

CONVENTION = "T_parent_child;right_handed;xyzw"


class ContractError(ValueError):
    """资产未满足显式合同，消息为可稳定处理的原因码。"""


def reject(code: str) -> NoReturn:
    raise ContractError(code)


def require(condition: Any, code: str) -> None:
    if not condition:
        reject(code)


def fields(value: Any, expected: Any) -> None:
    require(isinstance(value, dict) and set(value) == set(expected), "unknown_or_missing_fields")


def identity(value: Any) -> str:
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", value), "invalid_identifier")
    return value


def trusted_digest(value: Any) -> str:
    require(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value), "invalid_trusted_digest")
    return value


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    except (TypeError, ValueError) as error:
        raise ContractError("invalid_json_value") from error


def canonical_digest(value: Any) -> str:
    return digest(canonical(value))


def sha256_file(path: str | Path) -> str:
    return digest(Path(path).read_bytes())


def read_json(path: str | Path) -> Any:
    """拒绝重复键和非有限数，避免校验与执行端对同一文件作不同解释。"""
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate_json_key")
            result[key] = value
        return result

    try:
        return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=pairs,
                          parse_float=lambda text: number(float(text)),
                          parse_constant=lambda _: reject("nonfinite_json_number"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ContractError("invalid_json") from error


def relative_path(value: Any) -> PurePosixPath:
    """合同路径必须是原样规范的相对 POSIX 路径，跨平台拒绝驱动器和 URI。"""
    require(isinstance(value, str) and value and "\\" not in value and ":" not in value, "path_escape")
    path = PurePosixPath(value)
    require(not path.is_absolute() and all(part not in ("", ".", "..") for part in value.split("/")), "path_escape")
    return path


def safe_file(root: str | Path, relative: Any, *, missing_code: str = "missing_dependency") -> Path:
    path = relative_path(relative)
    base = Path(root).absolute()
    candidate = base
    for part in path.parts:
        candidate = candidate / part
        require(not candidate.is_symlink(), "symlink_dependency")
    require(candidate.is_file() and candidate.resolve().is_relative_to(base.resolve()), missing_code)
    return candidate


def pinned_file(root: str | Path, reference: Any) -> Path:
    fields(reference, {"path", "sha256"})
    expected = trusted_digest(reference["sha256"])
    path = safe_file(root, reference["path"])
    require(sha256_file(path) == expected, "digest_mismatch")
    return path


def number(value: Any) -> float:
    require(type(value) in (int, float), "invalid_number")
    try:
        result = float(value)
    except OverflowError as error:
        raise ContractError("invalid_number") from error
    require(math.isfinite(result), "invalid_number")
    return result


def vector(value: Any, length: int = 3) -> tuple[float, ...]:
    require(isinstance(value, list) and len(value) == length, "invalid_vector")
    return tuple(number(component) for component in value)


def pose(value: Any) -> None:
    fields(value, {"translation_m", "quaternion_xyzw"})
    vector(value["translation_m"])
    rotation = vector(value["quaternion_xyzw"], 4)
    require(math.isclose(sum(v * v for v in rotation), 1.0, abs_tol=1e-6), "invalid_rotation")


def unique(values: list[Any], code: str) -> None:
    require(len(values) == len(set(values)), code)


def validate_frame_tree(frames: Any) -> dict[str, dict[str, Any]]:
    """验证坐标系、刚性位姿和单根无环拓扑；不推断缺失变换。"""
    require(isinstance(frames, list) and frames, "missing_frames")
    result = {}
    for frame in frames:
        fields(frame, {"frame_id", "parent", "translation_m", "quaternion_xyzw", "provenance"})
        name = identity(frame["frame_id"])
        require(name not in result, "duplicate_frame")
        pose({key: frame[key] for key in ("translation_m", "quaternion_xyzw")})
        require(isinstance(frame["provenance"], str) and frame["provenance"], "invalid_provenance")
        if frame["parent"] is not None:
            identity(frame["parent"])
        result[name] = frame
    require(sum(frame["parent"] is None for frame in frames) == 1, "invalid_frame_root")
    for name in result:
        visited = set()
        while name is not None:
            require(name in result, "frame_cycle_or_missing_parent")
            require(name not in visited, "frame_cycle_or_missing_parent")
            visited.add(name)
            name = result[name]["parent"]
    return result


def validate_sources(entries: Any, root: str | Path, entrypoint: str | Path) -> dict[str, Path]:
    """读取封闭源清单并同时核对尺寸和摘要；不导入其中的源码。"""
    require(isinstance(entries, list) and entries, "missing_sources")
    result = {}
    paths = set()
    for entry in entries:
        fields(entry, {"source_id", "path", "sha256", "bytes"})
        name = identity(entry["source_id"])
        require(name not in result, "duplicate_source")
        path = safe_file(root, entry["path"], missing_code="missing_or_escaped_source")
        require(path.resolve() != Path(entrypoint).resolve() and path.resolve() not in paths, "duplicate_or_self_source")
        require(type(entry["bytes"]) is int and entry["bytes"] >= 0, "invalid_source_size")
        require(path.stat().st_size == entry["bytes"], "source_size_mismatch")
        require(sha256_file(path) == trusted_digest(entry["sha256"]), "source_digest_mismatch")
        result[name] = path
        paths.add(path.resolve())
    return result


@dataclass(frozen=True)
class AcceptedContract:
    """保存不可变的规范字节，每次读取返回独立的合同和评估对象。"""
    sha256: str
    contract_bytes: bytes
    assessment_bytes: bytes

    @property
    def contract(self) -> dict[str, Any]:
        return json.loads(self.contract_bytes)

    @property
    def assessment(self) -> dict[str, Any]:
        return json.loads(self.assessment_bytes)


def write_acceptance(directory: str | Path, result: dict[str, Any]) -> None:
    """只写新目录；seal 最后写入，未完成的输出不会具有完整验收标记。"""
    fields(result, {"activation", "receipt"})
    bodies = {name + ".json": canonical(value) + b"\n" for name, value in result.items()}
    seal = canonical({"files": {name: digest(body) for name, body in bodies.items()}}) + b"\n"
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=False)
    for name, body in bodies.items():
        (target / name).write_bytes(body)
    (target / "seal.json").write_bytes(seal)
