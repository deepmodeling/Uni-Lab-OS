"""
SynthonX GUI (External Front-End)
---------------------------------
为 `SynthonX.py` 提供一个外接可视化界面，不修改后端脚本。
"""
from __future__ import annotations
import os, sys, time, json, math, threading
from dataclasses import dataclass
from typing import Optional, Dict

try:
    from .SynthonX import (
        SharedRS485Bus,
        SharedXYZController,
        SOPAPipetteYYQ,
        MachineConfig,
        MotorAxis,
    )
except Exception as e:
    raise RuntimeError("未找到 SynthonX.py，请将本GUI与 SynthonX.py 放在同一目录") from e

# =============================
#  后端封装（薄层）：Station
# =============================
class Station:
    def __init__(self, port: str = "COM3", baudrate: int = 115200):
        self.port = port
        self.baudrate = baudrate
        self.bus: Optional[SharedRS485Bus] = None
        self.xyz: Optional[SharedXYZController] = None
        self.pip: Optional[SOPAPipetteYYQ] = None
        self.cfg = MachineConfig()
        self.connected = False

    # ---- 连接/断开 ----
    def connect(self) -> bool:
        try:
            self.bus = SharedRS485Bus(self.port, self.baudrate)
            self.bus.open()
            self.xyz = SharedXYZController(self.bus, self.cfg)
            self.pip = SOPAPipetteYYQ(self.bus)
            self.connected = True
            return True
        except Exception:
            self.connected = False
            return False

    def disconnect(self) -> None:
        if self.bus:
            try:
                self.bus.close()
            except Exception:
                pass
        self.connected = False

    # ---- XYZ 基础 ----
    def set_work_origin_here(self) -> bool:
        assert self.xyz is not None
        return self.xyz.set_work_origin_here()

    def home_safe(self) -> bool:
        """全轴回零（Z→X→Y）。"""
        assert self.xyz is not None
        try:
            # 优先使用后端的 home_all（若支持传入顺序则按 Z→X→Y）
            if hasattr(self.xyz, 'home_all'):
                try:
                    return bool(self.xyz.home_all((MotorAxis.Z, MotorAxis.X, MotorAxis.Y)))
                except TypeError:
                    return bool(self.xyz.home_all())
            # 兼容后端不提供 home_all 的情况：逐轴回零（Z→X→Y）
            ok = True
            for ax in (MotorAxis.Z, MotorAxis.X, MotorAxis.Y):
                try:
                    self.xyz.home_axis(ax, -1)
                    self.xyz.wait_for_completion(ax, 60.0)
                except Exception:
                    ok = False
            return ok
        except Exception:
            return False

    def emergency_stop(self) -> bool:
        assert self.xyz is not None
        ok = True
        for ax in (MotorAxis.X, MotorAxis.Y, MotorAxis.Z):
            try:
                self.xyz.emergency_stop(ax)
            except Exception:
                ok = False
        return ok

    # ---- 读取“工作坐标系”下的当前位置(mm) ----
    def get_status_mm(self) -> Dict[str, float]:
        """返回【工作坐标系】下的当前位置 (mm)。"""
        assert self.xyz is not None
        # 1) 优先：后端直接提供工作坐标
        if hasattr(self.xyz, "get_work_position_mm"):
            try:
                pos = self.xyz.get_work_position_mm()
                return {
                    "x": float(pos.get("x", 0.0)),
                    "y": float(pos.get("y", 0.0)),
                    "z": float(pos.get("z", 0.0)),
                }
            except Exception:
                pass
        # 2) 其次：基于步数做“机->工”换算
        sx = self.xyz.get_motor_status(MotorAxis.X).steps
        sy = self.xyz.get_motor_status(MotorAxis.Y).steps
        sz = self.xyz.get_motor_status(MotorAxis.Z).steps
        # 2.1 若提供 machine_steps_to_work_mm
        if hasattr(self.xyz, "machine_steps_to_work_mm"):
            try:
                w = self.xyz.machine_steps_to_work_mm(x=sx, y=sy, z=sz)
                return {"x": float(w["x"]), "y": float(w["y"]), "z": float(w["z"])}
            except Exception:
                pass
        # 2.2 若提供 machine_to_work_mm（先转 mm 再“机->工”）
        if hasattr(self.xyz, "machine_to_work_mm"):
            try:
                mx = self.xyz.steps_to_mm(MotorAxis.X, sx)
                my = self.xyz.steps_to_mm(MotorAxis.Y, sy)
                mz = self.xyz.steps_to_mm(MotorAxis.Z, sz)
                w = self.xyz.machine_to_work_mm(x=mx, y=my, z=mz)
                return {"x": float(w["x"]), "y": float(w["y"]), "z": float(w["z"])}
            except Exception:
                pass
        # 3) 兜底：假定 steps_to_mm 已包含零点偏置（部分固件/后端会这么做）
        return {
            "x": self.xyz.steps_to_mm(MotorAxis.X, sx),
            "y": self.xyz.steps_to_mm(MotorAxis.Y, sy),
            "z": self.xyz.steps_to_mm(MotorAxis.Z, sz),
        }

    # ---- 绝对安全移动（调用后端已有策略：抬Z→XY→落Z）----
    def move_to_work_safe(self, x=None, y=None, z=None, speed: Optional[int]=None, acc: Optional[int]=None) -> bool:
        assert self.xyz is not None
        return self.xyz.move_to_work_safe(x, y, z, speed, acc)

    def move_to_work_direct(self, x=None, y=None, z=None,
                            speed: Optional[int] = None,
                            acc: Optional[int] = None,
                            z_order: str = "auto") -> bool:
        """
        绝对直达：不抬Z。
        优先调用后端 SynthonX.SharedXYZController.move_to_work_direct(..., accel=..., z_order=...)
        若后端没有该API，则回退到现有的相对直达策略。
        """
        assert self.xyz is not None
        speed = speed or self.cfg.default_speed
        acc = acc or self.cfg.default_acceleration

        # 优先走后端的原生实现（支持 z_order）
        try:
            if hasattr(self.xyz, "move_to_work_direct"):
                # 注意后端参数名是 accel，这里把 acc 传给 accel
                return bool(self.xyz.move_to_work_direct(
                    x=x, y=y, z=z, speed=speed, accel=acc, z_order=z_order
                ))
        except Exception:
            # 不中断，下面走回退路径
            pass

        # ---- 回退实现：用当前位置算 Δ，再走相对直达（不抬Z）----
        try:
            cur = self.get_status_mm()
            dx = (x - cur['x']) if x is not None else 0.0
            dy = (y - cur['y']) if y is not None else 0.0
            dz = (z - cur['z']) if z is not None else 0.0
            return self.move_relative_direct(dx, dy, dz, speed=speed, acc=acc)
        except Exception:
            return False

    # ---- 相对直接移动（快速移动：不抬Z）----
    def move_relative_direct(self, dx: float, dy: float, dz: float, speed: Optional[int]=None, acc: Optional[int]=None) -> bool:
        """基于当前位置直接到新目标（工作坐标 Δmm），不抬Z；
        策略：若目标Z>当前Z，先XY后Z；若目标Z<=当前Z，先Z后XY。
        """
        assert self.xyz is not None
        speed = speed or self.cfg.default_speed
        acc = acc or self.cfg.default_acceleration
        # 当前绝对步（机坐标步数）
        sx = self.xyz.get_motor_status(MotorAxis.X).steps
        sy = self.xyz.get_motor_status(MotorAxis.Y).steps
        sz = self.xyz.get_motor_status(MotorAxis.Z).steps
        # Δmm→Δsteps（Δ与零点无关，可直接换算）
        tx = sx + self.xyz.mm_to_steps(MotorAxis.X, dx)
        ty = sy + self.xyz.mm_to_steps(MotorAxis.Y, dy)
        tz = sz + self.xyz.mm_to_steps(MotorAxis.Z, dz)
        # 顺序：仅按相对大小决定
        order = ("xy","z") if tz > sz else ("z","xy")
        ok = True
        try:
            if "z" in order[0]:
                ok &= self.xyz.move_to_steps(MotorAxis.Z, tz, speed, acc)
                ok &= self.xyz.wait_for_completion(MotorAxis.Z, 20.0)
                ok &= self.xyz.move_to_steps(MotorAxis.X, tx, speed, acc)
                ok &= self.xyz.move_to_steps(MotorAxis.Y, ty, speed, acc)
                ok &= self.xyz.wait_for_completion(MotorAxis.X, 20.0)
                ok &= self.xyz.wait_for_completion(MotorAxis.Y, 20.0)
            else:
                ok &= self.xyz.move_to_steps(MotorAxis.X, tx, speed, acc)
                ok &= self.xyz.move_to_steps(MotorAxis.Y, ty, speed, acc)
                ok &= self.xyz.wait_for_completion(MotorAxis.X, 20.0)
                ok &= self.xyz.wait_for_completion(MotorAxis.Y, 20.0)
                ok &= self.xyz.move_to_steps(MotorAxis.Z, tz, speed, acc)
                ok &= self.xyz.wait_for_completion(MotorAxis.Z, 20.0)
        except Exception:
            ok = False
        return bool(ok)

    # ---- 点位存取 ----
    def load_points(self, path: str) -> Dict[str, Dict[str, float]]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def save_points(self, path: str, data: Dict[str, Dict[str, float]]) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # ---- Pipette ----
    def pip_init(self) -> bool:
        assert self.pip is not None
        return self.pip.initialize()

    def pip_eject(self) -> bool:
        assert self.pip is not None
        self.pip.eject_tip()
        return True

    def pip_asp(self, ul: float) -> bool:
        assert self.pip is not None
        return self.pip.aspirate(ul)

    def pip_dsp(self, ul: float) -> bool:
        assert self.pip is not None
        return self.pip.dispense(ul)


# =============================
#  GUI
# =============================
import tkinter as tk
from tkinter import ttk, messagebox

class LogConsole(ttk.Frame):
    def __init__(self, master):
        super().__init__(master)
        self.text = tk.Text(self, height=10)
        self.text.pack(fill=tk.BOTH, expand=True)
    def log(self, s: str):
        ts = time.strftime('%H:%M:%S')
        self.text.insert(tk.END, f"[{ts}] {s}\n")
        self.text.see(tk.END)

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SynthonX GUI (External) — 工作坐标")
        self.geometry("980x780")
        self.resizable(True, True)

        # 顶部：连接区
        top = ttk.Frame(self)
        top.pack(fill=tk.X, padx=8, pady=6)
        ttk.Label(top, text="串口:").pack(side=tk.LEFT)
        self.port_var = tk.StringVar(value=("COM3" if sys.platform == "win32" else "/dev/ttyUSB0"))
        ttk.Entry(top, textvariable=self.port_var, width=14).pack(side=tk.LEFT, padx=6)
        ttk.Label(top, text="波特率:").pack(side=tk.LEFT)
        self.baud_var = tk.IntVar(value=115200)
        ttk.Entry(top, textvariable=self.baud_var, width=8).pack(side=tk.LEFT, padx=6)
        self.btn_conn = ttk.Button(top, text="连接", command=self.on_connect)
        self.btn_conn.pack(side=tk.LEFT, padx=6)
        ttk.Button(top, text="断开", command=self.on_disconnect).pack(side=tk.LEFT, padx=6)
        self.lbl_conn = ttk.Label(top, text="未连接", foreground="#B00")
        self.lbl_conn.pack(side=tk.LEFT, padx=10)

        # Notebook
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill=tk.BOTH, expand=True)
        self.tab_xyz = ttk.Frame(self.nb)
        self.tab_pip = ttk.Frame(self.nb)
        self.tab_flow = ttk.Frame(self.nb)
        self.tab_map = ttk.Frame(self.nb)
        self.nb.add(self.tab_xyz, text="XYZ 控制（工作坐标）")
        self.nb.add(self.tab_pip, text="移液枪")
        self.nb.add(self.tab_flow, text="操作向导")
        self.nb.add(self.tab_map, text="示意图（工作坐标）")

        # 日志
        self.console = LogConsole(self)
        self.console.pack(fill=tk.BOTH, expand=False, padx=8, pady=6)

        # Station & 点位
        self.station: Optional[Station] = None
        self.points_path = os.path.join(os.path.dirname(__file__), "points_gui.json")
        self.points: Dict[str, Dict[str, float]] = {}
        self._load_points()

        # 仅在点击后显示的“所选点名”
        self._selected_point_name: str = ""   # <<< 新增：记录被选中的点名

        # 构建各页
        self._build_xyz_tab()
        self._build_pip_tab()
        self._build_flow_tab()
        self._build_map_tab()

    # ---------- 连接 ----------
    def on_connect(self):
        try:
            self.station = Station(self.port_var.get(), int(self.baud_var.get()))
            if self.station.connect():
                self.lbl_conn.config(text="已连接", foreground="#0A0")
                self.console.log("连接成功（真实串口）")
            else:
                self.lbl_conn.config(text="连接失败", foreground="#B00")
                self.console.log("连接失败：请检查端口/波特率/接线")
        except Exception as e:
            self.lbl_conn.config(text="异常", foreground="#B00")
            self.console.log(f"连接异常：{e}")

    def on_disconnect(self):
        if self.station:
            self.station.disconnect()
        self.lbl_conn.config(text="未连接", foreground="#B00")
        self.console.log("已断开")

    # ---------- XYZ Tab ----------
    def _build_xyz_tab(self):
        f = self.tab_xyz
        # 基本
        base = ttk.LabelFrame(f, text="基本")
        base.pack(fill=tk.X, padx=8, pady=8)
        ttk.Button(base, text="设置当前位置为工作原点", command=self.xyz_set_origin).pack(side=tk.LEFT, padx=6, pady=6)
        ttk.Button(base, text="全轴回零 (Z→X→Y)", command=self.xyz_home).pack(side=tk.LEFT, padx=6, pady=6)
        ttk.Button(base, text="紧急停止", command=self.xyz_emg).pack(side=tk.LEFT, padx=6, pady=6)

        # 速度/加速度
        sp = ttk.LabelFrame(f, text="速度/加速度 (rpm / 无量纲)")
        sp.pack(fill=tk.X, padx=8, pady=8)
        self.speed_var = tk.IntVar(value=100)
        self.acc_var = tk.IntVar(value=1000)
        row = ttk.Frame(sp); row.pack(fill=tk.X, padx=4, pady=4)
        ttk.Label(row, text="速度:").pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=self.speed_var, width=8).pack(side=tk.LEFT, padx=6)
        ttk.Label(row, text="加速度:").pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=self.acc_var, width=8).pack(side=tk.LEFT, padx=6)
        # 运动区：左=绝对移动，右=相对位移
        mv = ttk.LabelFrame(f, text="移动（工作坐标，mm）：左=绝对 | 右=相对(Δ)")
        mv.pack(fill=tk.X, padx=8, pady=8)

        mv_left = ttk.Frame(mv);  mv_left.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 3), pady=4)
        mv_right = ttk.Frame(mv); mv_right.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(3, 6), pady=4)

        # --- 左侧：绝对移动 ---
        ttk.Label(mv_left, text="绝对目标 (X/Y/Z，mm，工作坐标)").pack(anchor="w")
        self.x_var = tk.DoubleVar(value=0.0)
        self.y_var = tk.DoubleVar(value=0.0)
        self.z_var = tk.DoubleVar(value=0.0)
        for label, var in (("X", self.x_var),("Y", self.y_var),("Z", self.z_var)):
            row = ttk.Frame(mv_left); row.pack(fill=tk.X, padx=0, pady=3)
            ttk.Label(row, text=f"{label}=").pack(side=tk.LEFT)
            ttk.Entry(row, textvariable=var, width=10).pack(side=tk.LEFT)

        # 勾选后不抬Z：绝对“直达”
        self.direct_abs_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            mv_left,
            text="不抬Z轴（绝对直达，按Z高低决定先后）",
            variable=self.direct_abs_var
        ).pack(anchor="w", pady=(4,2))

        ttk.Button(mv_left, text="执行绝对移动", command=self.xyz_move_absolute).pack(anchor="w", pady=(2,0))

        # --- 右侧：相对位移 ---
        ttk.Label(mv_right, text="相对位移 Δ (mm，工作坐标)").pack(anchor="w")
        self.rx_var = tk.DoubleVar(value=0.0)
        self.ry_var = tk.DoubleVar(value=0.0)
        self.rz_var = tk.DoubleVar(value=0.0)
        for label, var in (("ΔX", self.rx_var),("ΔY", self.ry_var),("ΔZ", self.rz_var)):
            row = ttk.Frame(mv_right); row.pack(fill=tk.X, padx=0, pady=3)
            ttk.Label(row, text=f"{label}=").pack(side=tk.LEFT)
            ttk.Entry(row, textvariable=var, width=10).pack(side=tk.LEFT)

        ttk.Button(mv_right, text="执行相对位移", command=self.xyz_move_relative_inputs).pack(anchor="w", pady=(2,0))


        # 点位管理
        pm = ttk.LabelFrame(f, text="位置点管理（JSON，工作坐标）")
        pm.pack(fill=tk.X, padx=8, pady=8)
        self.point_name_var = tk.StringVar(value="")
        row1 = ttk.Frame(pm); row1.pack(fill=tk.X, padx=4, pady=3)
        ttk.Label(row1, text="点名:").pack(side=tk.LEFT)
        ttk.Entry(row1, textvariable=self.point_name_var, width=18).pack(side=tk.LEFT, padx=6)
        ttk.Button(row1, text="保存当前 X/Y/Z 为该点", command=self.save_point).pack(side=tk.LEFT, padx=6)
        row2 = ttk.Frame(pm); row2.pack(fill=tk.X, padx=4, pady=3)
        ttk.Label(row2, text="点列表:").pack(side=tk.LEFT)
        self.point_combo = ttk.Combobox(row2, values=sorted(self.points.keys()), width=20)
        self.point_combo.pack(side=tk.LEFT, padx=6)
        ttk.Button(row2, text="移动到点(安全)", command=self.move_to_point).pack(side=tk.LEFT, padx=6)
        ttk.Button(row2, text="删除点", command=self.delete_point).pack(side=tk.LEFT, padx=6)
        ttk.Button(row2, text="刷新列表", command=self.refresh_points_combo).pack(side=tk.LEFT, padx=6)

        # 状态读取
        st = ttk.LabelFrame(f, text="当前位置（工作坐标 mm / 步）")
        st.pack(fill=tk.X, padx=8, pady=8)
        self.lbl_pos = ttk.Label(st, text="X: -, Y: -, Z: -")
        self.lbl_pos.pack(side=tk.LEFT, padx=6)
        ttk.Button(st, text="刷新", command=self.xyz_refresh).pack(side=tk.LEFT, padx=6)

    # XYZ 事件
    def xyz_move_absolute(self):
        s = self._need_station();  
        if not s: return
        try:
            sp = int(self.speed_var.get())
            ac = int(self.acc_var.get())
            x, y, z = self.x_var.get(), self.y_var.get(), self.z_var.get()
            if getattr(self, "direct_abs_var", None) and self.direct_abs_var.get():
                ok = s.move_to_work_direct(x, y, z, speed=sp, acc=ac)
                self.console.log(f"绝对直达（工作坐标，不抬Z）：{'OK' if ok else 'Fail'} (x={x}, y={y}, z={z}, speed={sp}, acc={ac})")
            else:
                ok = s.move_to_work_safe(x, y, z, speed=sp, acc=ac)
                self.console.log(f"安全移动（工作坐标，抬Z）：{'OK' if ok else 'Fail'} (x={x}, y={y}, z={z}, speed={sp}, acc={ac})")
        except Exception as e:
            messagebox.showerror("移动失败", str(e))
            self.console.log(f"移动失败：{e}")

    def xyz_move_relative_inputs(self):
        s = self._need_station();  
        if not s: return
        try:
            sp = int(self.speed_var.get())
            ac = int(self.acc_var.get())
            dx, dy, dz = self.rx_var.get(), self.ry_var.get(), self.rz_var.get()
            ok = s.move_relative_direct(dx, dy, dz, speed=sp, acc=ac)
            if ok:
                # 获取并输出当前绝对坐标（工作坐标）
                pos = s.get_status_mm()
                self.console.log(
                    f"相对位移（工作坐标）：OK (Δx={dx}, Δy={dy}, Δz={dz}, speed={sp}, acc={ac}) → 绝对(x={pos['x']:.3f}, y={pos['y']:.3f}, z={pos['z']:.3f})"
                )
                # 刷新状态标签
                try: self.xyz_refresh()
                except Exception: pass
            else:
                self.console.log(f"相对位移（工作坐标）：Fail (Δx={dx}, Δy={dy}, Δz={dz}, speed={sp}, acc={ac})")
        except Exception as e:
            messagebox.showerror("相对位移失败", str(e))
            self.console.log(f"相对位移失败：{e}")

    def xyz_move_dispatch(self):
        if self.relative_var.get():
            self.xyz_move_relative()
        else:
            self.xyz_move_safe()

    def xyz_set_origin(self):
        s = self._need_station();  
        if not s: return
        s.set_work_origin_here()
        self.console.log("工作原点已更新为当前位置")

    def xyz_home(self):
        s = self._need_station();  
        if not s: return
        s.home_safe()
        self.console.log("全轴回零 (Z→X→Y) 完成")

    def xyz_emg(self):
        s = self._need_station();  
        if not s: return
        ok = s.emergency_stop()
        self.console.log(f"紧急停止：{'OK' if ok else 'Fail'}")

    def xyz_move_safe(self):
        s = self._need_station();  
        if not s: return
        try:
            sp = int(self.speed_var.get())
            ac = int(self.acc_var.get())
            ok = s.move_to_work_safe(self.x_var.get(), self.y_var.get(), self.z_var.get(), speed=sp, acc=ac)
            self.console.log(f"安全移动（工作坐标）：{'OK' if ok else 'Fail'} (speed={sp}, acc={ac})")
        except Exception as e:
            messagebox.showerror("移动失败", str(e))
            self.console.log(f"移动失败：{e}")

    def xyz_move_relative(self):
        s = self._need_station();  
        if not s: return
        try:
            sp = int(self.speed_var.get())
            ac = int(self.acc_var.get())
            dx, dy, dz = self.x_var.get(), self.y_var.get(), self.z_var.get()
            ok = s.move_relative_direct(dx, dy, dz, speed=sp, acc=ac)
            if ok:
                pos = s.get_status_mm()
                self.console.log(
                    f"相对直接移动（工作坐标）：OK (Δx={dx}, Δy={dy}, Δz={dz}, speed={sp}, acc={ac}) → 绝对(x={pos['x']:.3f}, y={pos['y']:.3f}, z={pos['z']:.3f})"
                )
                try: self.xyz_refresh()
                except Exception: pass
            else:
                self.console.log(f"相对直接移动（工作坐标）：Fail (Δx={dx}, Δy={dy}, Δz={dz}, speed={sp}, acc={ac})")
        except Exception as e:
            messagebox.showerror("相对移动失败", str(e))
            self.console.log(f"相对移动失败：{e}")

    def xyz_refresh(self):
        s = self._need_station();  
        if not s: return
        try:
            pos = s.get_status_mm()  # 工作坐标
            # 同时显示步数（机坐标步）供调试
            px = s.xyz.get_motor_status(MotorAxis.X).steps
            py = s.xyz.get_motor_status(MotorAxis.Y).steps
            pz = s.xyz.get_motor_status(MotorAxis.Z).steps
            self.lbl_pos.config(text=(
                f"工作坐标  X:{pos['x']:.2f} mm ({px}步)  "
                f"Y:{pos['y']:.2f} mm ({py}步)  "
                f"Z:{pos['z']:.2f} mm ({pz}步)"
            ))
        except Exception as e:
            messagebox.showerror("读取失败", str(e))

    # ---------- Pipette Tab ----------
    def _build_pip_tab(self):
        f = self.tab_pip
        base = ttk.LabelFrame(f, text="基础")
        base.pack(fill=tk.X, padx=8, pady=8)
        ttk.Button(base, text="初始化", command=self.pip_init).pack(side=tk.LEFT, padx=6, pady=6)
        ttk.Button(base, text="弹出枪头", command=self.pip_eject).pack(side=tk.LEFT, padx=6, pady=6)
        ops = ttk.LabelFrame(f, text="吸/排液 (uL)")
        ops.pack(fill=tk.X, padx=8, pady=8)
        self.asp_var = tk.DoubleVar(value=100)
        self.dsp_var = tk.DoubleVar(value=100)
        ttk.Label(ops, text="吸液:").pack(side=tk.LEFT)
        ttk.Entry(ops, textvariable=self.asp_var, width=8).pack(side=tk.LEFT)
        ttk.Button(ops, text="执行吸液", command=self.pip_asp).pack(side=tk.LEFT, padx=6)
        ttk.Label(ops, text="排液:").pack(side=tk.LEFT)
        ttk.Entry(ops, textvariable=self.dsp_var, width=8).pack(side=tk.LEFT)
        ttk.Button(ops, text="执行排液", command=self.pip_dsp).pack(side=tk.LEFT, padx=6)
        st = ttk.LabelFrame(f, text="状态")
        st.pack(fill=tk.X, padx=8, pady=8)
        self.lbl_pip = ttk.Label(st, text="-")
        self.lbl_pip.pack(side=tk.LEFT, padx=6, pady=6)

    def pip_init(self):
        s = self._need_station();  
        if not s: return
        if s.pip_init():
            self.console.log("移液枪初始化完成")
            self.lbl_pip.config(text="已初始化")

    def pip_eject(self):
        s = self._need_station();  
        if not s: return
        s.pip_eject(); self.console.log("枪头已弹出")

    def pip_asp(self):
        s = self._need_station();  
        if not s: return
        v = self.asp_var.get()
        if s.pip_asp(v):
            self.console.log(f"吸液 {v} uL 完成")

    def pip_dsp(self):
        s = self._need_station();  
        if not s: return
        v = self.dsp_var.get()
        if s.pip_dsp(v):
            self.console.log(f"排液 {v} uL 完成")

    # ---------- Flow Tab ----------
    def _build_flow_tab(self):
        f = self.tab_flow
        box = ttk.LabelFrame(f, text="Transfer Demo")
        box.pack(fill=tk.X, padx=8, pady=8)
        ttk.Label(box, text="演示：吸 100 → 排 100（需已装枪头）").pack(side=tk.LEFT, padx=6)
        ttk.Button(box, text="Run", command=self.flow_demo).pack(side=tk.LEFT, padx=6)

    def flow_demo(self):
        s = self._need_station();  
        if not s: return
        s.pip_asp(100)
        s.pip_dsp(100)
        self.console.log("Transfer demo 完成")

    # ---------- Map Tab ----------
    def _build_map_tab(self):
        f = self.tab_map
        container = ttk.Frame(f); container.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        left = ttk.Frame(container); right = ttk.Frame(container)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        right.pack(side=tk.LEFT, fill=tk.Y, padx=10)
        self.map_canvas = tk.Canvas(left, width=640, height=480, background="white", highlightthickness=1, highlightbackground="#999")
        self.map_canvas.pack(fill=tk.BOTH, expand=True)
        self.map_canvas.bind("<Button-1>", self.map_on_click)
        self.map_canvas.bind("<Configure>", lambda e: self.draw_map())
        ttk.Label(right, text="所选点：").pack(anchor="w", pady=(4,0))
        self.map_selected = tk.StringVar(value="")
        ttk.Label(right, textvariable=self.map_selected).pack(anchor="w")
        ttk.Button(right, text="刷新示意图", command=self.draw_map).pack(fill=tk.X, pady=6)
        ttk.Separator(right, orient="horizontal").pack(fill=tk.X, pady=8)
        ttk.Button(right, text="安全运动到所选点（绝对）", command=self.map_move_safe).pack(fill=tk.X, pady=6)
        ttk.Button(right, text="相对运动到所选点（Δ=目标-当前）", command=self.map_move_relative).pack(fill=tk.X, pady=6)
        tip = "示意图仅展示 XY 平面；点击点后会在画布上显示该点名称。\n安全移动=抬Z-XY-落Z；相对移动=直接Δmm。\n所有坐标均为【工作坐标】。"
        ttk.Label(right, text=tip, wraplength=240, foreground="#555").pack(fill=tk.X, pady=8)
        self.draw_map()

    def _workspace_xy(self):
        try:
            if self.station and self.station.xyz:
                mc = self.station.cfg
            else:
                mc = MachineConfig()
            return float(mc.max_travel_x), float(mc.max_travel_y)
        except Exception:
            return 340.0, 250.0

    def _xy_to_canvas(self, x_mm, y_mm, cw, ch, scale, margin):
        cx = margin + x_mm * scale
        cy = ch - margin - y_mm * scale  # 画布 y 向下
        return cx, cy

    def draw_map(self):
        cnv = getattr(self, 'map_canvas', None)
        if not cnv: return
        cnv.delete("all")
        cw = cnv.winfo_width() or 640
        ch = cnv.winfo_height() or 480
        margin = 40
        max_x, max_y = self._workspace_xy()
        scale = min((cw-2*margin)/max_x, (ch-2*margin)/max_y)
        # 边框
        cnv.create_rectangle(margin, margin, cw-margin, ch-margin, outline="#333", width=2)
        # 外侧标注为“工作坐标系”
        cnv.create_text(cw - margin, ch - 6, anchor="se", text="工作坐标系 (mm)", fill="#333")
        # 点（默认只画点，不显示文字；仅被选中的点显示名称）
        self._map_item_to_name = {}
        r = 5
        for name, p in sorted(self.points.items()):
            try:
                x, y = float(p.get("x", 0.0)), float(p.get("y", 0.0))
            except Exception:
                continue
            cx, cy = self._xy_to_canvas(x, y, cw, ch, scale, margin)
            # 选中点：红色加粗并显示名称；未选中：蓝色
            sel = (name == self._selected_point_name)
            color = "#ff375f" if sel else "#0a84ff"
            width = 3 if sel else 2
            item = cnv.create_oval(cx-r, cy-r, cx+r, cy+r, outline=color, width=width, tags=("point",))
            self._map_item_to_name[item] = name
            if sel:
                cnv.create_text(cx+10, cy-12, anchor="w", text=name, fill="#333")  # 仅选中时显示名称

        # 当前坐标十字（工作坐标）
        try:
            if self.station and self.station.connected:
                cur = self.station.get_status_mm()  # 工作坐标
                cx, cy = self._xy_to_canvas(cur['x'], cur['y'], cw, ch, scale, margin)
                cnv.create_line(cx-8, cy, cx+8, cy, width=2)
                cnv.create_line(cx, cy-8, cx, cy+8, width=2)
                cnv.create_text(cx+12, cy-10, anchor="w", text=f"当前({cur['x']:.1f},{cur['y']:.1f})")
        except Exception:
            pass

    def map_on_click(self, event):
        cnv = getattr(self, 'map_canvas', None)
        if not cnv: return
        item = cnv.find_closest(event.x, event.y)
        if not item: return
        iid = item[0]
        if "point" not in cnv.gettags(iid):
            return
        # 通过映射拿到点名
        name = self._map_item_to_name.get(iid, "")
        # 记录并刷新（仅选中时显示名称）
        self._selected_point_name = name
        self.map_selected.set(name)
        self.draw_map()

    def map_move_safe(self):
        s = self._need_station();  
        if not s: return
        name = self.map_selected.get().strip()
        if name not in self.points:
            messagebox.showwarning("未选择点", "请先在示意图上点击一个点"); return
        p = self.points[name]
        sp, ac = int(self.speed_var.get()), int(self.acc_var.get())
        try:
            ok = s.move_to_work_safe(p['x'], p['y'], p['z'], speed=sp, acc=ac)
            self.console.log(f"[示意图] 安全移动到 '{name}'（工作坐标）: {'OK' if ok else 'Fail'} → (x={p['x']}, y={p['y']}, z={p['z']})")
        except Exception as e:
            messagebox.showerror("移动失败", str(e))
            self.console.log(f"[示意图] 移动失败：{e}")

    def map_move_relative(self):
        s = self._need_station();  
        if not s: return
        name = self.map_selected.get().strip()
        if name not in self.points:
            messagebox.showwarning("未选择点", "请先在示意图上点击一个点"); return
        p = self.points[name]
        try:
            cur = s.get_status_mm()  # 工作坐标
            dx, dy, dz = p['x']-cur['x'], p['y']-cur['y'], p['z']-cur['z']
        except Exception:
            dx, dy, dz = p['x'], p['y'], p['z']
        sp, ac = int(self.speed_var.get()), int(self.acc_var.get())
        try:
            ok = s.move_relative_direct(dx, dy, dz, speed=sp, acc=ac)
            if ok:
                pos = s.get_status_mm()
                self.console.log(
                    f"[示意图] 相对移动至 '{name}'（工作坐标）: OK (Δx={dx:.3f}, Δy={dy:.3f}, Δz={dz:.3f}) → 绝对(x={pos['x']:.3f}, y={pos['y']:.3f}, z={pos['z']:.3f})"
                )
                try: self.draw_map()
                except Exception: pass
            else:
                self.console.log(f"[示意图] 相对移动至 '{name}'（工作坐标）: Fail (Δx={dx:.3f}, Δy={dy:.3f}, Δz={dz:.3f})")
        except Exception as e:
            messagebox.showerror("相对移动失败", str(e))
            self.console.log(f"[示意图] 相对移动失败：{e}")

    # ---------- 点位存取 ----------
    def _load_points(self):
        try:
            if os.path.exists(self.points_path):
                with open(self.points_path, 'r', encoding='utf-8') as f:
                    self.points = json.load(f)
            else:
                self.points = {}
        except Exception:
            self.points = {}
        # 若组合框已创建，刷新
        if hasattr(self, 'point_combo'):
            self.refresh_points_combo()

    def _save_points(self):
        try:
            with open(self.points_path, 'w', encoding='utf-8') as f:
                json.dump(self.points, f, ensure_ascii=False, indent=2)
            self.console.log("点位已保存（工作坐标）")
        except Exception as e:
            messagebox.showerror("保存失败", str(e))
            self.console.log(f"点位保存失败：{e}")

    def save_point(self):
        s = self._need_station();  
        if not s: return
        name = (self.point_name_var.get() or '').strip()
        if not name:
            messagebox.showwarning("无名称", "请先输入点名称"); return
        pos = s.get_status_mm()  # 工作坐标
        self.points[name] = {"x": pos['x'], "y": pos['y'], "z": pos['z']}
        self._save_points(); self.refresh_points_combo()
        self.console.log(f"保存点 '{name}'（工作坐标）: x={pos['x']:.3f}, y={pos['y']:.3f}, z={pos['z']:.3f}")

    def move_to_point(self):
        s = self._need_station();  
        if not s: return
        name = (self.point_combo.get() or self.point_name_var.get()).strip()
        if name not in self.points:
            messagebox.showwarning("未找到点", f"未找到点: {name}"); return
        p = self.points[name]
        sp, ac = int(self.speed_var.get()), int(self.acc_var.get())
        try:
            ok = s.move_to_work_safe(p['x'], p['y'], p['z'], speed=sp, acc=ac)
            self.console.log(f"移动到点 '{name}'（工作坐标）: {'OK' if ok else 'Fail'} → (x={p['x']}, y={p['y']}, z={p['z']})")
        except Exception as e:
            messagebox.showerror("移动失败", str(e))
            self.console.log(f"移动失败：{e}")

    def delete_point(self):
        name = (self.point_combo.get() or self.point_name_var.get()).strip()
        if name and name in self.points:
            del self.points[name]
            self._save_points(); self.refresh_points_combo()
            self.console.log(f"已删除点 '{name}'")
            # 若删除的是已选点，清空选择
            if name == self._selected_point_name:
                self._selected_point_name = ""
                self.map_selected.set("")
                self.draw_map()
        else:
            messagebox.showwarning("未找到点", f"未找到点: {name}")

    def refresh_points_combo(self):
        if hasattr(self, 'point_combo'):
            names = sorted(self.points.keys())
            self.point_combo['values'] = names
            if names and (self.point_combo.get() not in names):
                self.point_combo.set(names[0])
        self.draw_map()

    # ---------- helpers ----------
    def _need_station(self) -> Optional[Station]:
        if not self.station or not self.station.connected:
            messagebox.showwarning("未连接", "请先连接设备")
            return None
        return self.station


def main():
    app = App()
    app.mainloop()

if __name__ == "__main__":
    main()
