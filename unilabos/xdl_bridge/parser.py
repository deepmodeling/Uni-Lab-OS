from __future__ import annotations

from pathlib import Path
import xml.etree.ElementTree as ET

from .models import CanonicalProcedure, CanonicalStep


_SECTIONS = {"Procedure", "Prep", "Reaction", "Workup", "Purification"}


def _synthesis(root: ET.Element) -> ET.Element:
    if root.tag == "Synthesis":
        return root
    syntheses = root.findall("Synthesis")
    if len(syntheses) != 1:
        raise ValueError("XDL document must contain exactly one Synthesis element")
    return syntheses[0]


def _ensure_unique(
    records: tuple[dict[str, str], ...], *, key: str, label: str
) -> None:
    seen: set[str] = set()
    for record in records:
        value = record.get(key, "")
        if not value:
            raise ValueError(f"{label.capitalize()} must declare {key}")
        if value in seen:
            raise ValueError(f"Duplicate {label} {key}: {value}")
        seen.add(value)


def parse_xdl(path: str | Path) -> CanonicalProcedure:
    source = Path(path)
    root = ET.parse(source).getroot()
    synthesis = _synthesis(root)
    procedure = synthesis.find("Procedure")
    if procedure is None:
        raise ValueError("XDL Synthesis must contain a Procedure element")

    components = tuple(
        dict(component.attrib)
        for component in synthesis.findall("./Hardware/Component")
    )
    reagents = tuple(
        dict(reagent.attrib) for reagent in synthesis.findall("./Reagents/Reagent")
    )
    _ensure_unique(components, key="id", label="hardware")
    _ensure_unique(reagents, key="name", label="reagent")
    steps: list[CanonicalStep] = []

    def append(element: ET.Element, source_path: str) -> None:
        if element.tag in _SECTIONS:
            for index, child in enumerate(element):
                append(child, f"{source_path}/{child.tag}[{index}]")
            return
        if element.tag == "Repeat":
            raw_repeats = element.attrib.get(
                "repeats", element.attrib.get("times", "1")
            )
            try:
                repeats = int(raw_repeats)
            except ValueError as exc:
                raise ValueError(
                    f"Repeat count must be an integer: {raw_repeats}"
                ) from exc
            if repeats < 1:
                raise ValueError("Repeat count must be at least one")
            for repeat_index in range(repeats):
                for child_index, child in enumerate(element):
                    append(
                        child,
                        f"{source_path}/repeat[{repeat_index}]/{child.tag}[{child_index}]",
                    )
            return
        steps.append(
            CanonicalStep(
                operation=element.tag,
                parameters=dict(element.attrib),
                sequence=len(steps) + 1,
                source_path=source_path,
            )
        )

    append(procedure, "/XDL/Synthesis/Procedure")
    return CanonicalProcedure(
        components=components,
        reagents=reagents,
        steps=tuple(steps),
        source=source,
    )
