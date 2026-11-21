from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CanonicalStep:
    operation: str
    parameters: dict[str, Any]
    sequence: int
    source_path: str


@dataclass(frozen=True)
class CanonicalProcedure:
    components: tuple[dict[str, str], ...]
    reagents: tuple[dict[str, str], ...]
    steps: tuple[CanonicalStep, ...]
    source: Path
