# raman_module.py
import os
import time as time_mod
import numpy as np
import pandas as pd

# clr / ATRWrapper 依赖：在真实环境中使用 Windows + .NET wrapper
# 本模块对缺少 clr 或 Wrapper 的情况提供“仿真”回退，方便离线/调试运行。
try:
    import clr
    has_clr = True
except Exception:
    clr = None
    has_clr = False

# 本函数返回 (success: bool, file_prefix: str|None, df: pandas.DataFrame|None)
def run_raman_test(integration_time=5000, laser_power=200,
                   save_csv=True, save_plot=True,
                   normalize=False, norm_max=None,
                   max_wavenum=1300):
    """
    拉曼测试流程（更稳健的实现）：
    - 若能加载 ATRWrapper 则使用之
    - 否则生成模拟光谱（方便调试）
    返回 (success, file_prefix, df)
    """
    timestamp = time_mod.strftime("%Y%m%d_%H%M%S")
    file_prefix = f"raman_{timestamp}"

    wrapper = None
    used_real_device = False

    try:
        if has_clr:
            try:
                # 请根据你的 DLL 路径调整
                dll_path = r"D:\Raman\Raman_RS\ATRWrapper\ATRWrapper.dll"
                if os.path.exists(dll_path):
                    clr.AddReference(dll_path)
                else:
                    # 试图直接 AddReference 名称（若已在 PATH）
                    try:
                        clr.AddReference("ATRWrapper")
                    except Exception:
                        pass

                from Optosky.Wrapper import ATRWrapper  # May raise
                wrapper = ATRWrapper()
                used_real_device = True
            except Exception as e:
                # 无法加载真实 wrapper -> fallback
                print("⚠️ 未能加载 ATRWrapper，使用模拟数据。详细:", e)
                wrapper = None

        if wrapper is None:
            # 生成模拟光谱（方便调试）
            # 模拟波数轴 50..1300
            WaveNum = np.linspace(50, max_wavenum, 1024)
            # 合成几条高斯峰 + 噪声
            def gauss(x, mu, sig, A):
                return A * np.exp(-0.5 * ((x - mu) / sig) ** 2)
            Spect_data = (gauss(WaveNum, 200, 8, 1000) +
                          gauss(WaveNum, 520, 12, 600) +
                          gauss(WaveNum, 810, 20, 400) +
                          50 * np.random.normal(scale=1.0, size=WaveNum.shape))
            Spect_bLC = Spect_data - np.min(Spect_data) * 0.05  # 简单 baseline
            Spect_smooth = np.convolve(Spect_bLC, np.ones(3) / 3, mode="same")
            df = pd.DataFrame({
                "WaveNum": WaveNum,
                "Raw_Spect": Spect_data,
                "BaseLineCorrected": Spect_bLC,
                "Smooth_Spect": Spect_smooth
            })
            success = True
            file_prefix = f"raman_sim_{timestamp}"
            # 保存 CSV / 绘图 等同真实设备
        else:
            # 使用真实设备 API（根据你提供的 wrapper 调用）
            On_flag = wrapper.OpenDevice()
            print("通讯连接状态:", On_flag)
            if not On_flag:
                wrapper.CloseDevice()
                return False, None, None

            wrapper.SetIntegrationTime(int(integration_time))
            wrapper.SetLdPower(int(laser_power), 1)
            # 可能的冷却设置（如果 wrapper 支持）
            try:
                wrapper.SetCool(-5)
            except Exception:
                pass

            Spect = wrapper.AcquireSpectrum()
            Spect_data = np.array(Spect.get_Data())
            if not Spect.get_Success():
                print("光谱采集失败")
                try:
                    wrapper.CloseDevice()
                except Exception:
                    pass
                return False, None, None
            WaveNum = np.array(wrapper.GetWaveNum())
            Spect_bLC = np.array(wrapper.BaseLineCorrect(Spect_data))
            Spect_smooth = np.array(wrapper.SmoothBoxcar(Spect_bLC, 3))
            df = pd.DataFrame({
                "WaveNum": WaveNum,
                "Raw_Spect": Spect_data,
                "BaseLineCorrected": Spect_bLC,
                "Smooth_Spect": Spect_smooth
            })
            wrapper.CloseDevice()
            success = True

        # 如果需要限定波数范围
        mask = df["WaveNum"] <= max_wavenum
        df = df[mask].reset_index(drop=True)

        # 可选归一化
        if normalize:
            arr = df["Smooth_Spect"].values
            mn, mx = arr.min(), arr.max()
            if mx == mn:
                df["Smooth_Spect"] = 0.0
            else:
                scale = 1.0 if norm_max is None else float(norm_max)
                df["Smooth_Spect"] = (arr - mn) / (mx - mn) * scale
            # 同时处理其它列（可选）
            arr_raw = df["Raw_Spect"].values
            mn_r, mx_r = arr_raw.min(), arr_raw.max()
            if mx_r == mn_r:
                df["Raw_Spect"] = 0.0
            else:
                scale = 1.0 if norm_max is None else float(norm_max)
                df["Raw_Spect"] = (arr_raw - mn_r) / (mx_r - mn_r) * scale

        # 保存 CSV
        if save_csv:
            csv_filename = f"{file_prefix}.csv"
            df.to_csv(csv_filename, index=False)
            print("✅ CSV 文件已生成:", csv_filename)

        # 绘图（使用 matplotlib），注意：不要启用 GUI 后台
        if save_plot:
            try:
                import matplotlib
                matplotlib.use("Agg")
                import matplotlib.pyplot as plt

                plt.figure(figsize=(8, 5))
                plt.plot(df["WaveNum"], df["Raw_Spect"], linestyle='-', alpha=0.6, label="原始")
                plt.plot(df["WaveNum"], df["BaseLineCorrected"], linestyle='--', alpha=0.8, label="基线校正")
                plt.plot(df["WaveNum"], df["Smooth_Spect"], linewidth=1.2, label="平滑")
                plt.xlabel("WaveNum (cm^-1)")
                plt.ylabel("Intensity (a.u.)")
                plt.title(f"Raman {file_prefix}")
                plt.grid(True)
                plt.legend()
                plt.tight_layout()
                plot_filename = f"{file_prefix}.png"
                plt.savefig(plot_filename, dpi=300, bbox_inches="tight")
                plt.close()
                # 小短暂等待以确保文件系统刷新
                time_mod.sleep(0.2)
                print("✅ 图像已生成:", plot_filename)
            except Exception as e:
                print("⚠️ 绘图失败:", e)

        return success, file_prefix, df

    except Exception as e:
        print("拉曼测试异常:", e)
        try:
            if wrapper is not None:
                try:
                    wrapper.CloseDevice()
                except Exception:
                    pass
        except Exception:
            pass
        return False, None, None
