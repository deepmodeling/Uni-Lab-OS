#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Iterable, Sequence


Vector = tuple[float, float, float]
Triangle = tuple[Vector, Vector, Vector]


def _normal(a: Vector, b: Vector, c: Vector) -> Vector:
    ux, uy, uz = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
    vx, vy, vz = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
    nx = uy * vz - uz * vy
    ny = uz * vx - ux * vz
    nz = ux * vy - uy * vx
    length = (nx * nx + ny * ny + nz * nz) ** 0.5
    if length == 0:
        return (0.0, 0.0, 0.0)
    return (nx / length, ny / length, nz / length)


def _format_vertex(vertex: Vector) -> str:
    return f"{vertex[0]:.6f} {vertex[1]:.6f} {vertex[2]:.6f}"


def build_box(width: float, depth: float, height: float) -> Sequence[Triangle]:
    half_width = width / 2.0
    half_depth = depth / 2.0
    vertices = {
        "lbf": (-half_width, -half_depth, 0.0),
        "rbf": (half_width, -half_depth, 0.0),
        "rtf": (half_width, half_depth, 0.0),
        "ltf": (-half_width, half_depth, 0.0),
        "lbb": (-half_width, -half_depth, height),
        "rbb": (half_width, -half_depth, height),
        "rtb": (half_width, half_depth, height),
        "ltb": (-half_width, half_depth, height),
    }
    return (
        (vertices["lbf"], vertices["rtf"], vertices["rbf"]),
        (vertices["lbf"], vertices["ltf"], vertices["rtf"]),
        (vertices["lbb"], vertices["rbb"], vertices["rtb"]),
        (vertices["lbb"], vertices["rtb"], vertices["ltb"]),
        (vertices["lbf"], vertices["rbf"], vertices["rbb"]),
        (vertices["lbf"], vertices["rbb"], vertices["lbb"]),
        (vertices["ltf"], vertices["ltb"], vertices["rtb"]),
        (vertices["ltf"], vertices["rtb"], vertices["rtf"]),
        (vertices["lbf"], vertices["lbb"], vertices["ltb"]),
        (vertices["lbf"], vertices["ltb"], vertices["ltf"]),
        (vertices["rbf"], vertices["rtf"], vertices["rtb"]),
        (vertices["rbf"], vertices["rtb"], vertices["rbb"]),
    )


def write_ascii_stl(path: Path, solid_name: str, triangles: Iterable[Triangle]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="ascii") as handle:
        handle.write(f"solid {solid_name}\n")
        for triangle in triangles:
            normal = _normal(*triangle)
            handle.write(f"  facet normal {_format_vertex(normal)}\n")
            handle.write("    outer loop\n")
            for vertex in triangle:
                handle.write(f"      vertex {_format_vertex(vertex)}\n")
            handle.write("    endloop\n")
            handle.write("  endfacet\n")
        handle.write(f"endsolid {solid_name}\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a simple meter-based box STL.")
    parser.add_argument("--width", type=float, required=True, help="Box width in meters.")
    parser.add_argument("--depth", type=float, required=True, help="Box depth in meters.")
    parser.add_argument("--height", type=float, required=True, help="Box height in meters.")
    parser.add_argument("--output", type=Path, required=True, help="Output STL path.")
    parser.add_argument("--name", default="device_box", help="ASCII STL solid name.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    triangles = build_box(width=args.width, depth=args.depth, height=args.height)
    write_ascii_stl(path=args.output, solid_name=args.name, triangles=triangles)


if __name__ == "__main__":
    main()
