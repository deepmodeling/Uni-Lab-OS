"""资产自带的轨道：一排等距的定位格，载架挂在某一格上、占若干格。

轨道不是库位。库位是“一个位置放一件东西”，占用记在库位上；轨道上的载架盖住若干格，
被盖住的格并没有被“占用”，所以不能把每一格建成库位。做法与 PyLabRobot 的 ``TecanDeck`` /
``HamiltonDeck`` 一致：载架是设备的子物料（组成关系），挂在第几格决定它的位姿，格号由位姿反推、
不另存；冲突按占格区间是否重叠判断；问被盖住的任意一格都能找到同一个载架。

两段资产数据（``unilab.json`` 的 ``parameters``）：

* 设备资产 ``rails``（``unilab.rails/1``）：每条轨道的格数、起始格号、节距，第一格的挂点坐标系
  在设备资产坐标系里的位姿 ``asset_from_first_mount``，以及每格的步进 ``step_mm``。
* 载架资产 ``rail_mount``（``unilab.rail-mount/1``）：能挂哪条轨道、节距、占几格 ``span``，
  以及载架资产坐标系在挂点坐标系里的位姿 ``asset_from_mount``（载架位姿 = 挂点位姿 · 它）。

长度单位毫米。只用标准库。
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

RAILS_SCHEMA = "unilab.rails/1"
MOUNT_SCHEMA = "unilab.rail-mount/1"
Matrix = list[list[float]]


def _times(a: Matrix, b: Matrix) -> Matrix:
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def _inverse(matrix: Matrix) -> Matrix:
    turn = [[matrix[j][i] for j in range(3)] for i in range(3)]
    move = [-sum(turn[i][j] * matrix[j][3] for j in range(3)) for i in range(3)]
    return [turn[i] + [move[i]] for i in range(3)] + [[0.0, 0.0, 0.0, 1.0]]


def rails_from_asset(adapter: Mapping[str, Any]) -> dict[str, dict]:
    """设备资产声明的轨道，按轨道名；没有声明返回空表。结构不对时报错，不静默当作没有。"""
    block = (adapter.get("parameters") or {}).get("rails")
    if block is None:
        return {}
    if block.get("schema") != RAILS_SCHEMA:
        raise ValueError(f"rails: schema {block.get('schema')!r}, expected {RAILS_SCHEMA!r}")
    rails = {}
    for rail in block.get("rails") or []:
        missing = [key for key in ("id", "count", "first", "pitch_mm", "asset_from_first_mount", "step_mm") if key not in rail]
        if missing:
            raise ValueError(f"rails: {rail.get('id')!r} lacks {missing}")
        length = sum(float(value) ** 2 for value in rail["step_mm"]) ** 0.5
        if abs(length - float(rail["pitch_mm"])) > 0.01:
            raise ValueError(f"rails: {rail['id']!r} steps {length:.4f} mm per grid, its pitch says {rail['pitch_mm']}")
        if rail["id"] in rails:
            raise ValueError(f"rails: {rail['id']!r} declared twice")
        rails[rail["id"]] = dict(rail)
    return rails


def rail_mount_from_asset(adapter: Mapping[str, Any]) -> dict | None:
    """载架资产声明的挂点；没有声明返回 ``None``。"""
    block = (adapter.get("parameters") or {}).get("rail_mount")
    if block is None:
        return None
    if block.get("schema") != MOUNT_SCHEMA:
        raise ValueError(f"rail_mount: schema {block.get('schema')!r}, expected {MOUNT_SCHEMA!r}")
    missing = [key for key in ("rail", "pitch_mm", "span", "asset_from_mount") if key not in block]
    if missing:
        raise ValueError(f"rail_mount: lacks {missing}")
    if int(block["span"]) < 1:
        raise ValueError("rail_mount: span must be at least 1")
    return dict(block)


def _fits(rail: Mapping[str, Any], mount: Mapping[str, Any]) -> None:
    if mount["rail"] != rail["id"] or abs(float(mount["pitch_mm"]) - float(rail["pitch_mm"])) > 1e-6:
        raise ValueError(f"a mount for {mount['rail']!r} at {mount['pitch_mm']} mm does not fit rail "
                         f"{rail['id']!r} at {rail['pitch_mm']} mm")


def covered(grid: int, span: int) -> range:
    """挂在 ``grid``、占 ``span`` 格的载架盖住的格。"""
    return range(int(grid), int(grid) + int(span))


def mount_pose(rail: Mapping[str, Any], grid: int) -> Matrix:
    """第 ``grid`` 格的挂点坐标系在设备资产坐标系里的位姿。"""
    last = int(rail["first"]) + int(rail["count"]) - 1
    if not int(rail["first"]) <= int(grid) <= last:
        raise ValueError(f"rail {rail['id']!r} has grids {rail['first']}..{last}, not {grid}")
    pose = [[float(value) for value in row] for row in rail["asset_from_first_mount"]]
    for i in range(3):
        pose[i][3] += (int(grid) - int(rail["first"])) * float(rail["step_mm"][i])
    return pose


def carrier_pose(rail: Mapping[str, Any], grid: int, mount: Mapping[str, Any]) -> Matrix:
    """挂在第 ``grid`` 格的载架在设备资产坐标系里的位姿：挂点位姿 · ``asset_from_mount``。占的格超出轨道时报错。"""
    _fits(rail, mount)
    mount_pose(rail, covered(grid, mount["span"])[-1])
    return _times(mount_pose(rail, grid), [[float(value) for value in row] for row in mount["asset_from_mount"]])


def grid_at(rail: Mapping[str, Any], pose: Matrix, mount: Mapping[str, Any]) -> tuple[float, float]:
    """载架位姿反推出的格号（小数，整数才是挂在格上），以及它偏离这排格的距离（毫米）。"""
    _fits(rail, mount)
    at = _times(pose, _inverse([[float(value) for value in row] for row in mount["asset_from_mount"]]))
    step = [float(value) for value in rail["step_mm"]]
    delta = [at[i][3] - float(rail["asset_from_first_mount"][i][3]) for i in range(3)]
    along = sum(delta[i] * step[i] for i in range(3)) / sum(value * value for value in step)
    off = sum((delta[i] - along * step[i]) ** 2 for i in range(3)) ** 0.5
    return along + int(rail["first"]), off


def carrier_at(layout: Iterable[Mapping[str, Any]], grid: int) -> Mapping[str, Any] | None:
    """盖住第 ``grid`` 格的载架（``layout`` 的每项带 ``grid`` 和 ``span``）；没有返回 ``None``。"""
    return next((entry for entry in layout if int(grid) in covered(entry["grid"], entry["span"])), None)


def check_layout(rail: Mapping[str, Any], layout: Iterable[Mapping[str, Any]]) -> list[str]:
    """一条轨道上的摆放有没有问题：超出轨道、互相重叠。``layout`` 的每项带 ``name``、``grid``、``span``。"""
    problems: list[str] = []
    first, last = int(rail["first"]), int(rail["first"]) + int(rail["count"]) - 1
    owner: dict[int, str] = {}
    for entry in layout:
        grids = covered(entry["grid"], entry["span"])
        if grids[0] < first or grids[-1] > last:
            problems.append(f"{entry['name']} covers grids {grids[0]}..{grids[-1]}, the rail has {first}..{last}")
        clash = sorted({owner[grid] for grid in grids if grid in owner})
        if clash:
            problems.append(f"{entry['name']} at grid {entry['grid']} overlaps {', '.join(clash)}")
        for grid in grids:
            owner.setdefault(grid, entry["name"])
    return problems
