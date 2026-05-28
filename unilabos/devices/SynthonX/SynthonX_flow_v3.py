"""
SynthonX_flow_v3.py
- 点到点移动：调用 SynthonX 的 move_to_work_safe（通过 SynthonX_gui.Station 封装）
- 下探/上升/小幅相对位移：调用 SynthonX_gui 的 move_relative_direct

功能：
1) 系统初始化
2) 取枪头 / 放枪头（丢到 C原位+4排；例如 C1 -> D49，找不到D49则尝试C49）
3) 转移液体（A > B，或 B > D；可多目标，后续目标用“相对位移”）
4) 过滤（D9-16 取推杆；D1-8 过滤；最后弹出）
5) 推动（D25：下探 → +Y推进 → 抬起 → 丢枪头）
6) 装核磁（D1-8 源 -> D17-24 NMR管，放下全部液体 → 丢枪头）

注意：
- 所有点位均从 points_gui.json 读取（工作坐标，单位 mm；需包含 x/y/z 字段）。
- 所有“下探/上升/相对XY位移”均使用 Station.move_relative_direct（SynthonX_gui 提供）。
- 所有“到某个点位（含上方高度）”均使用 Station.move_to_work_safe（SynthonX 提供）。

SynthonX团队
"""

from __future__ import annotations
import os
import json
import time
import re
from dataclasses import dataclass
from typing import Dict, List, Iterable, Optional, Tuple, Union
from contextlib import contextmanager
from .SynthonX_gui import Station
from .SynthonX_reactor import RelayController
import logging
logging.getLogger("unified_xyz_yyq").setLevel(logging.ERROR)  # 只看错误，不显示 CRC mismatch 的 warning

class _HideCRCFilter(logging.Filter):
    def filter(self, record):
        return "CRC mismatch" not in record.getMessage()

_crc_logger = logging.getLogger("unified_xyz_yyq")
_crc_logger.addFilter(_HideCRCFilter())
_crc_logger.propagate = False  # 防止向上层冒泡

# -------------------- 小工具 --------------------

def _require(cond: bool, msg: str):
    if not cond:
        raise ValueError(msg)

def _is_seq(x) -> bool:
    return isinstance(x, (list, tuple))

def _idx_from_name(name: str) -> int:
    """提取 'C1' / 'D49' 的编号 -> 1 / 49"""
    try:
        return int(''.join(c for c in name if c.isdigit()))
    except Exception:
        raise ValueError(f"无法解析点名编号: {name!r}")

def _zone_from_name(name: str) -> str:
    """返回点位所属分区的字母（A/B/C/D）"""
    for c in name:
        if c.isalpha():
            return c.upper()
    raise ValueError(f"无法解析点名分区: {name!r}")

def _split_names(names: Union[str, Iterable[str]]) -> List[str]:
    if isinstance(names, str):
        return [p.strip() for p in names.split(",") if p.strip()]
    elif isinstance(names, (list, tuple)):
        return [str(p).strip() for p in names]
    else:
        return [str(names).strip()]


def _zone_from_name(name: str) -> str:
    """返回点位所属分区的字母（A/B/C/D）"""
    for c in name:
        if c.isalpha():
            return c.upper()
    raise ValueError(f"无法解析点名分区: {name!r}")

# -------------------- 配置与主类 --------------------

@dataclass
class FlowConfig:
    port: str = "COM5"
    baudrate: int = 115200
    points_file: str = "points_gui.json"
    # 运动/沉降参数
    approach_lift: float = 0.0         # 到点位时在其上方 approach_lift(mm) 处驻停
    settle_s: int = 5            # 每次接触或位移后的沉降时间（秒）
    delay_after_aspirate: float = 0.35 # 吸液后的等待（秒）
    delay_after_dispense: float = 0.35 # 放液后的等待（秒）
    # —— 搅拌器（USB 继电器）串口配置 ——
    relay_port: str = "COM7"
    relay_baudrate: int = 9600
    relay_timeout: float = 1.0

class SynthonXFlowV2:
    """
    提供高层流程 API；内部统一用 Station：
      - 点到点：station.move_to_work_safe(x, y, z)
      - 相对位移/下探/上升：station.move_relative_direct(dx, dy, dz)
      - 移液：station.pip_init / pip_asp / pip_dsp / pip_eject
    """
    def __init__(self,
                 port: str = "COM5",
                 baudrate: int = 115200,
                 points_file: str = "points_gui.json",
                 approach_lift: float = 0.0,
                 settle_s: int = 5,
                 delay_after_aspirate: float = 0.35,
                 delay_after_dispense: float = 0.35,
                 relay_port: str = "COM7",
                 relay_baudrate: int = 9600,
                 relay_timeout: float = 1.0):
        
        # 在内部重新组合成 cfg 对象，供其他方法使用
        self.cfg = FlowConfig(
            port=port,
            baudrate=baudrate,
            points_file=points_file,
            approach_lift=approach_lift,
            settle_s=settle_s,
            delay_after_aspirate=delay_after_aspirate,
            delay_after_dispense=delay_after_dispense,
            relay_port = relay_port,
            relay_baudrate = relay_baudrate,
            relay_timeout = relay_timeout
        )
        
        # 后续代码不变，继续使用 self.cfg
        self.station = Station(self.cfg.port, self.cfg.baudrate)
        if not self.station.connect():
            raise RuntimeError("串口连接失败，请检查端口/波特率/接线。")
        self.points: Dict[str, Dict[str, float]] = self._load_points(self.cfg.points_file)

        # —— 初始化搅拌器控制器（可选）——
        self.reactor: Optional[RelayController] = None
        try:
            self.reactor = RelayController(port=self.cfg.relay_port,
                                           baudrate=self.cfg.relay_baudrate,
                                           timeout=self.cfg.relay_timeout)
        except Exception as e:
            print(f"[警告] 创建 RelayController 失败：{e}（继续运行，仅禁用搅拌功能）")

    def stir_connect(self) -> bool:
        """连接搅拌器串口。"""
        _require(self.reactor is not None, "未创建 Reactor 控制器")
        try:
            self.reactor.connect()
            return True
        except Exception as e:
            print(f"[搅拌] 连接失败：{e}")
            return False
            
    def stir_on(self, wait_response: bool = True) -> bool:
        _require(self.reactor is not None, "未创建 Reactor 控制器")
        if not self.reactor.ser or not self.reactor.ser.is_open:
            self.stir_connect()
        try:
            self.reactor.on(wait_response=wait_response)
            print("[搅拌] ON")
            return True
        except Exception as e:
            print(f"[搅拌] 开启失败：{e}")
            return False

    def stir_off(self, wait_response: bool = True) -> bool:
        _require(self.reactor is not None, "未创建 Reactor 控制器")
        if not self.reactor.ser or not self.reactor.ser.is_open:
            self.stir_connect()
        try:
            self.reactor.off(wait_response=wait_response)
            print("[搅拌] OFF")
            return True
        except Exception as e:
            print(f"[搅拌] 关闭失败：{e}")
            return False 

    def stir_toggle(self, wait_response: bool = True) -> bool:
        _require(self.reactor is not None, "未创建 Reactor 控制器")
        if not self.reactor.ser or not self.reactor.ser.is_open:
            self.stir_connect()
        try:
            self.reactor.toggle(wait_response=wait_response)
            print("[搅拌] TOGGLE")
            return True
        except Exception as e:
            print(f"[搅拌] 切换失败：{e}")
            return False

    def stir_for(self, seconds: float, wait_response: bool = True) -> bool:
        """阻塞式搅拌指定秒数，超简单实用。"""
        _require(seconds > 0, "seconds 必须>0")
        if self.stir_on(wait_response=wait_response):
            try:
                print(f"持续搅拌 {seconds} 秒...")
                time.sleep(float(seconds))
            finally:
                self.stir_off(wait_response=wait_response)
                print("搅拌结束")
            return True
        return False

    @contextmanager
    def stirring(self, wait_response: bool = True):
        """上下文模式：with flows.stirring(): ..."""
        self.stir_on(wait_response=wait_response)
        try:
            yield
        finally:
            self.stir_off(wait_response=wait_response)

    def stir_safe_shutdown(self):
        """脚本结束时可调用，确保关闭继电器并释放串口。"""
        if self.reactor is not None:
            try:
                self.reactor.ensure_off_on_exit()
            except Exception:
                pass

    def pipette_init(self) -> bool:
        return self.station.pip_init()

    # ---------- 点位 ----------
    def _load_points(self, path: str) -> Dict[str, Dict[str, float]]:
        abspath = os.path.join(os.path.dirname(__file__), path)
        with open(abspath, "r", encoding="utf-8") as f:
            data = json.load(f)
        # 基础校验
        for k, v in data.items():
            for key in ("x", "y", "z"):
                if key not in v:
                    raise KeyError(f"点位 {k} 缺少字段 {key}")
        return data

    def _pt(self, name: str) -> Dict[str, float]:
        if name not in self.points:
            raise KeyError(f"点位不存在: {name}")
        return self.points[name]

    # ---------- 运动辅助（严格按你的规则调用底层 API） ----------
    def _go_to_point_above(self, name: str, lift: Optional[float] = None) -> float:
        """使用 move_to_work_safe 到达“点位上方 lift 高度”，返回上方的绝对 z 高度。"""
        p = self._pt(name)
        lift = self.cfg.approach_lift if lift is None else float(lift)
        z_above = float(p["z"]) + float(lift)
        self.station.move_to_work_safe(x=p["x"], y=p["y"], z=z_above)
        return z_above

    def _down_rel(self, dz: float):
        _require(dz >= 0, "下探距离必须>=0")
        self.station.move_relative_direct(0.0, 0.0, -float(dz))
        time.sleep(self.cfg.settle_s)

    def _up_rel(self, dz: float):
        _require(dz >= 0, "上移距离必须>=0")
        self.station.move_relative_direct(0.0, 0.0, +float(dz))
        time.sleep(self.cfg.settle_s)

    def _xy_rel(self, dx: float, dy: float):
        self.station.move_relative_direct(float(dx), float(dy), 0.0)
        time.sleep(self.cfg.settle_s)

    def _map_waste_slot(self, c_tip_name: str) -> str:
        zone = _zone_from_name(c_tip_name)
        if zone == "D":
            if c_tip_name in self.points:
                return c_tip_name
            raise KeyError(f"废弃位 {c_tip_name} 不存在于 points")
        _require(zone == "C", "枪头原始点名应来自 C 区（或直接给一个 D 区废弃位）")
        idx = _idx_from_name(c_tip_name)
        target_idx = idx + 48
        d_name = f"D{target_idx}"
        if d_name in self.points:
            return d_name
        c_name2 = f"C{target_idx}"
        if c_name2 in self.points:
            return c_name2
        raise KeyError(f"未找到废弃位：既没有 {d_name} 也没有 {c_name2}")

    # =======================================================
    # 1) 系统初始化
    # =======================================================
    def system_init(self) -> bool:
        print("系统初始化：全轴回零...")
        ok = self.station.home_safe()
        print("系统已回零")
        ok2 = self.station.set_work_origin_here()
        print("设置当前位置设为工作原点")
        return bool(ok and ok2)

    # =======================================================
    # 2) 取枪头 / 放枪头
    # =======================================================
    def pick_tip(self, tip_point: str, down_mm: float = 120) -> bool:
        p = self._pt(tip_point)
        self.station.move_to_work_safe(x=p["x"], y=p["y"], z=p["z"])
        time.sleep(self.cfg.settle_s)
        self.station.move_relative_direct(0.0, 0.0, float(down_mm))
        time.sleep(self.cfg.settle_s)
        self.station.move_relative_direct(0.0, 0.0, -float(down_mm))
        time.sleep(self.cfg.settle_s)
        print(f'{tip_point}枪头已经装载')
        return True
    
    def drop_tip(self, tip_point: str, down_mm: float = 60) -> bool:
        p = self._pt(tip_point)
        self.station.move_to_work_safe(x=p["x"], y=p["y"], z=p["z"])
        time.sleep(self.cfg.settle_s)
        self.station.move_relative_direct(0.0, 0.0, float(down_mm))
        time.sleep(self.cfg.settle_s)
        self.station.pip_eject()
        time.sleep(self.cfg.settle_s)
        self.station.move_relative_direct(0.0, 0.0, -float(down_mm))
        time.sleep(self.cfg.settle_s)
        print(f'枪头已经弃置在{tip_point}')
        return True
    
    # =======================================================
    # 3) 转移液体（A > B 或 B > D）
    # =======================================================
    def _do_transfer(self,
                    src_name: str,
                    dst_names: Union[str, Iterable[str]],
                    tip_c_name: str,
                    total_ul: float,
                    down_src_mm: float,
                    down_dst_mm: float,
                    split_volumes: Optional[List[float]] = None,
                    stir_post_s: Optional[float] = None) -> bool:

        # 修改：统一解析目标名
        dst_list = _split_names(dst_names)
        _require(len(dst_list) >= 1, "目标点名至少1个")

        # (1) 取枪头
        self.pick_tip(tip_c_name, down_mm=120)
        time.sleep(self.cfg.settle_s)

        # (2) 到源位
        src_p = self._pt(src_name)
        self.station.move_to_work_safe(x=src_p["x"], y=src_p["y"], z=src_p["z"])
        time.sleep(self.cfg.settle_s)
        print('到达源点位')

        # (3) 下探源位
        self.station.move_relative_direct(0.0, 0.0, float(down_src_mm))
        time.sleep(self.cfg.settle_s)
        print('下探完成')

        # (4) 吸液
        self.station.pip_asp(float(total_ul))
        time.sleep(self.cfg.settle_s)
        print('吸取液体完成')

        # (5) 回升源位
        self.station.move_relative_direct(0.0, 0.0, -float(down_src_mm))
        time.sleep(self.cfg.settle_s)
        print('回升完成')

        # === 目标处理 ===
        if len(dst_list) == 1:
            # 单目标
            dst_p = self._pt(dst_list[0])
            self.station.move_to_work_safe(x=dst_p["x"], y=dst_p["y"], z=dst_p["z"])
            time.sleep(self.cfg.settle_s)
            print('移动到目标点位')

            self.station.move_relative_direct(0.0, 0.0, float(down_dst_mm))
            time.sleep(self.cfg.settle_s)
            print('下探')

            self.station.pip_dsp(float(total_ul))
            time.sleep(self.cfg.settle_s)
            time.sleep(self.cfg.delay_after_dispense)

            self.station.move_relative_direct(0.0, 0.0, -float(down_dst_mm))
            time.sleep(self.cfg.settle_s)

        else:
            # 多目标
            if split_volumes is not None:
                _require(len(split_volumes) == len(dst_list), "split_volumes 长度需与目标数量一致")
                vols = [float(v) for v in split_volumes]
            else:
                each = float(total_ul) / float(len(dst_list))
                vols = [each] * len(dst_list)

            first_name = dst_list[0]
            first_p = self._pt(first_name)
            self.station.move_to_work_safe(x=first_p["x"], y=first_p["y"], z=first_p["z"])
            time.sleep(self.cfg.settle_s)

            self.station.move_relative_direct(0.0, 0.0, float(down_dst_mm))
            time.sleep(self.cfg.settle_s)

            self.station.pip_dsp(vols[0])
            time.sleep(self.cfg.settle_s)

            base_x, base_y = first_p["x"], first_p["y"]
            for nm, v in zip(dst_list[1:], vols[1:]):
                self.station.move_relative_direct(0.0, 0.0, -float(down_dst_mm))
                time.sleep(self.cfg.settle_s)
                p = self._pt(nm)
                dx, dy = p["x"] - base_x, p["y"] - base_y
                self.station.move_relative_direct(float(dx), float(dy), 0.0)
                time.sleep(self.cfg.settle_s)
                self.station.move_relative_direct(0.0, 0.0, float(down_dst_mm))
                time.sleep(self.cfg.settle_s)
                self.station.pip_dsp(v)
                time.sleep(self.cfg.settle_s)
                base_x, base_y = p["x"], p["y"]

        # —— 如果设定了加液后搅拌时间，则触发搅拌 ——
        if stir_post_s is not None and float(stir_post_s) > 0:
            try:
                print(f"[搅拌] 加液完成，搅拌 {float(stir_post_s)} s ...")
                self.stir_for(float(stir_post_s))
            except Exception as e:
                print(f"[搅拌] 触发失败：{e}（忽略，不影响主流程）")

        # 映射 C 槽到 +48 的弃置位
        def upgrade_c_name(name: str) -> str:
            m = re.fullmatch(r"C(\d+)", name.strip().upper())
            if not m:
                return name
            idx = int(m.group(1))
            return f"C{idx + 48}"
        tip_c_name_new = upgrade_c_name(tip_c_name)

        # (10) 放枪头
        self.drop_tip(tip_c_name_new, down_mm=60.0)
        return True

    def transfer_A_to_B(self,
                        a_name: str,
                        b_names: Union[str, Iterable[str]],
                        tip_c_name: str,
                        total_ul: float,
                        split_volumes: Optional[List[float]] = None,
                        stir_post_s: Optional[float] = None) -> bool:
        _require(_zone_from_name(a_name) == "A", "源位必须在 A 区")
        down_a_mm = 121.0
        down_b_mm = 26.0
        if _is_seq(b_names):
            for nm in b_names:
                _require(_zone_from_name(nm) == "B", "目标必须都在 B 区")
        else:
            _require(_zone_from_name(b_names) == "B", "目标必须在 B 区")
        _require(_zone_from_name(tip_c_name) in ("C"), "枪头点名应在 C 区（或给定 D 区废弃位）")
        return self._do_transfer(a_name, b_names, tip_c_name, total_ul, down_a_mm, down_b_mm,
                                 split_volumes, stir_post_s)

    def transfer_B_to_D(self,
                        b_name: str,
                        d_names: Union[str, Iterable[str]],
                        tip_c_name: str,
                        total_ul: float,
                        split_volumes: Optional[List[float]] = None,
                        stir_post_s: Optional[float] = None) -> bool:
        _require(_zone_from_name(b_name) == "B", "源位必须在 B 区")
        down_b_mm = 46.0
        down_d_mm = 6.0
        if _is_seq(d_names):
            for nm in d_names:
                _require(_zone_from_name(nm) == "D", "目标必须都在 D 区")
        else:
            _require(_zone_from_name(d_names) == "D", "目标必须在 D 区")
        _require(_zone_from_name(tip_c_name) in ("C"), "枪头点名应在 C 区")
        return self._do_transfer(b_name, d_names, tip_c_name, total_ul, down_b_mm, down_d_mm,
                                 split_volumes, stir_post_s)

    # =======================================================
    # 4) 过滤
    # =======================================================
    def filtering(self,
                  pusher_name: str,      # D9-D16
                  filter_name: str,      # D1-D8
                  down_pick_mm: float,
                  down_filter_mm: float) -> bool:
        pusher_p = self._pt(pusher_name)
        self.station.move_to_work_safe(x=pusher_p["x"], y=pusher_p["y"], z=pusher_p["z"])
        time.sleep(self.cfg.settle_s)
        print("到达推杆点位")

        self.station.move_relative_direct(0.0, 0.0, float(down_pick_mm))
        time.sleep(self.cfg.settle_s)

        self.station.move_relative_direct(0.0, 0.0, -float(down_pick_mm))
        time.sleep(self.cfg.settle_s)
        print("取推杆完成")

        filter_p = self._pt(filter_name)
        self.station.move_to_work_safe(x=filter_p["x"], y=filter_p["y"], z=filter_p["z"])
        time.sleep(self.cfg.settle_s)
        print("到达过滤点位")

        self.station.move_relative_direct(0.0, 0.0, float(down_filter_mm))
        time.sleep(self.cfg.settle_s)
        print("下压过滤完成")

        self.station.move_relative_direct(0.0, 0.0, -20.0)
        time.sleep(self.cfg.settle_s)
        print("吸取空气完成")

        self.station.move_relative_direct(0.0, 0.0, 20.0)
        time.sleep(self.cfg.settle_s)
        print("排除剩余液体完成")

        self.station.move_relative_direct(0.0, 0.0, -10.0)
        time.sleep(self.cfg.settle_s)

        self.station.pip_eject()
        time.sleep(self.cfg.settle_s)
        print("推杆已弹出")

        self.station.move_relative_direct(0.0, 0.0, -50.0)
        time.sleep(self.cfg.settle_s)

        print("过滤流程完成")
        return True

    # =======================================================
    # 5) 推动（D25）
    # =======================================================
    def pushing(self,
                tip_c_name: str) -> bool:
        z_down_mm = 136.0
        y_forward_mm = 60.0
        _require(z_down_mm > 0, "z_down_mm 必须>0")
        _require(y_forward_mm > 0, "y_forward_mm 必须>0")
        _require(_zone_from_name(tip_c_name) in ("C"), "枪头点名应在C区")

        self.pick_tip(tip_c_name)

        d25_p = self._pt("D25")
        self.station.move_to_work_safe(x=d25_p["x"], y=d25_p["y"], z=d25_p["z"])
        time.sleep(self.cfg.settle_s)
        print("到达 D25 点位")

        self.station.move_relative_direct(0.0, 0.0, float(z_down_mm))
        time.sleep(self.cfg.settle_s)
        print("下压完成")

        self.station.move_relative_direct(0.0, float(y_forward_mm), 0.0)
        time.sleep(self.cfg.settle_s)
        print("推动完成")

        self.station.move_relative_direct(0.0, 0.0, -float(z_down_mm))
        time.sleep(self.cfg.settle_s)
        print("抬升完成")

        def upgrade_c_name(name: str) -> str:
            m = re.fullmatch(r"C(\d+)", name.strip().upper())
            if not m:
                return name
            idx = int(m.group(1))
            return f"C{idx + 48}"
        tip_c_name_new = upgrade_c_name(tip_c_name)
        self.drop_tip(tip_c_name_new)
        print("推动流程完成")

        return True

    # =======================================================
    # 6) 装核磁（单独函数）
    # =======================================================
    def load_for_nmr(self,
                    src_d_name: str,     # 源：D区 (如 D1-D8)
                    dst_d_name: str,     # 目标：D区 (如 D17-D24)
                    tip_c_name: str,     # 枪头：C区
                    total_ul: float,
                    stir_post_s: Optional[float] = None) -> bool:
        down_src_mm = 138
        down_dst_mm = 9
        _require(_zone_from_name(src_d_name) == "D", "源位必须在 D 区")
        _require(_zone_from_name(dst_d_name) == "D", "目标位必须在 D 区")
        _require(src_d_name != dst_d_name, "源与目标不能相同")
        _require(_zone_from_name(tip_c_name) == "C", "枪头点名必须在 C 区")

        return self._do_transfer(
            src_name=src_d_name,
            dst_names=dst_d_name,
            tip_c_name=tip_c_name,
            total_ul=float(total_ul),
            down_src_mm=float(down_src_mm),
            down_dst_mm=float(down_dst_mm),
            split_volumes=None,
            stir_post_s=stir_post_s
        )


if __name__ == "__main__":
    flows = SynthonXFlowV2(FlowConfig(
        port="COM5",
        baudrate=115200,
        points_file="points_gui.json",
        approach_lift=6.0,
        settle_s=6,
        relay_port="COM7",
        relay_baudrate=9600,
        relay_timeout=1.0
    ))
    flows.stir_for(30)
'''
    # 1) 初始化坐标系统
    flows.system_init()
    # 2) 初始化移液枪
    flows.pipette_init()
    # # flows.pipette_aspirate(300)
    # # flows.pipette_dispense(300)

    # # 2) 取/放枪头（示例）
    # flows.pick_tip("C1", down_mm=120)
    # flows.drop_tip("C96", down_mm=60)

    # # # 3) A > B（单目标或多目标；多目标示例按均分）
    # flows.transfer_A_to_B("A1", ["B1"], "C1", total_ul=300.0)

    # # 4) B > D
    # flows.transfer_B_to_D("B1", ["D1"], "C2", total_ul=300.0)

    # 5) 过滤（D9-16 → D1-8）
    # flows.filtering("D9", "D1", down_pick_mm=117, down_filter_mm=73)

    # # 6) 推动（D25）
    # flows.pushing("C3", z_down_mm=135, y_forward_mm=60)

    # 7) 装核磁
    flows.load_for_nmr("D1", "D17", "C4", total_ul=300)
'''
    