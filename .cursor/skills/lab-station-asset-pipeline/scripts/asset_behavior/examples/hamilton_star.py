"""Hamilton STAR 的机构动作（8 个独立通道，X 轴共用）；零导入。动作名和参数与 pylabrobot_unilab 移液站适配器一致：
``pick_up_tips`` / ``drop_tips`` / ``aspirate`` / ``dispense``。

资产不知道台面上摆了什么：孔、吸头位在台面坐标系里的点由 OS 沿资源树算好（``materials.point``），
这里只用资产自己的台面基准（库位 ``deck_origin``）和各通道的工具点把关节送过去。
液体量、吸头的归属变化由动作合同声明、OS 结算；这里的 carry 只是画面提示。
台面基准和工具点取自资产装配记录；行程是资产里的审阅包络，不是标定过的硬件限位。速度和停留时间是标称估计。
"""

TOOLS = ['Links/channel_01/tool', 'Links/channel_02/tool', 'Links/channel_03/tool', 'Links/channel_04/tool', 'Links/channel_05/tool', 'Links/channel_06/tool', 'Links/channel_07/tool', 'Links/channel_08/tool']
Z_JOINTS = ['pip_channel_0_stop_disk_z', 'pip_channel_1_stop_disk_z', 'pip_channel_2_stop_disk_z', 'pip_channel_3_stop_disk_z', 'pip_channel_4_stop_disk_z', 'pip_channel_5_stop_disk_z', 'pip_channel_6_stop_disk_z', 'pip_channel_7_stop_disk_z']

DECK = "deck_origin"
TRAVEL_SPEED_M_S = 0.5
Z_SECONDS = 0.6
DWELL_SECONDS = 0.5


def _channels(names, use_channels):
    channels = list(range(len(names))) if use_channels is None else [int(c) for c in use_channels]
    if len(channels) != len(names) or len(set(channels)) != len(channels):
        raise ValueError("use_channels 必须与资源一一对应且不重复")
    for channel in channels:
        if not 0 <= channel < len(TOOLS):
            raise ValueError("没有通道 %d" % channel)
    return channels


def _is_z(name, channel):
    return name == Z_JOINTS[channel] or name.endswith("/" + Z_JOINTS[channel])


class HamiltonSTAR:
    def __init__(self):
        self._tips = {}   # 通道 → 已装吸头露在通道端面以下的长度（米）

    def _visit(self, joints, sites, channels, tool_points, carries=None, state=None):
        """各通道的工具点到各自的目标点：先抬到最高，平移，下探，停留，再抬起。

        共用的平移轴要求同一批通道的目标一致；不一致就分批依次去。"""
        batches = []
        for channel, point in zip(channels, tool_points):
            targets = sites.reach(TOOLS[channel], sites.from_frame(DECK, point))
            planar = {k: v for k, v in targets.items() if not _is_z(k, channel)}
            down = {k: v for k, v in targets.items() if _is_z(k, channel)}
            last = batches[-1] if batches else None
            if last is not None and all(abs(last["planar"].get(k, v) - v) < 1e-4 for k, v in planar.items()):
                last["planar"].update(planar); last["down"].update(down); last["channels"].append(channel)
            else:
                batches.append({"planar": planar, "down": down, "channels": [channel]})
        steps, previous = [], None
        for index, batch in enumerate(batches):
            up = {Z_JOINTS[c]: joints.upper(Z_JOINTS[c]) for c in batch["channels"]}
            distance = max([abs(v - joints.position(k)) if previous is None else abs(v - previous.get(k, v))
                            for k, v in batch["planar"].items()] or [0.0])
            previous = dict(previous or {}, **batch["planar"])
            dwell = {"duration": DWELL_SECONDS}
            if index == len(batches) - 1 and state is not None:
                dwell["state"] = state
            steps += [{"move": up, "duration": Z_SECONDS},
                      {"move": batch["planar"], "duration": max(0.4, distance / TRAVEL_SPEED_M_S)},
                      {"move": batch["down"], "duration": Z_SECONDS}]
            for channel in batch["channels"]:
                if carries and channel in carries:
                    steps.append({"duration": 0.0, "carry": carries[channel]})
            steps += [dwell, {"move": up, "duration": Z_SECONDS}]
        return steps

    def pick_up_tips(self, joints, sites, materials, tip_spot_names, use_channels=None,
                     offset_x_mm=0.0, offset_y_mm=0.0, offset_z_mm=0.0):
        """取吸头：各通道到吸头位上方，下探到通道端面插入吸头的配合深度"""
        channels = _channels(tip_spot_names, use_channels)
        points, carries, mounted = [], {}, {}
        for name, channel in zip(tip_spot_names, channels):
            if channel in self._tips:
                raise ValueError("通道 %d 上已经有吸头" % channel)
            top = materials.point(name, "top_center")
            fitting = materials.get(name, "fitting_depth")
            mounted[channel] = materials.get(name, "tip_length") - fitting
            points.append((top[0] + offset_x_mm / 1000.0, top[1] + offset_y_mm / 1000.0,
                           top[2] - fitting + offset_z_mm / 1000.0))
            carries[channel] = {"from": "resource:" + name, "to": TOOLS[channel]}
        steps = self._visit(joints, sites, channels, points, carries, {"tips": sorted({**self._tips, **mounted})})
        self._tips.update(mounted)
        return steps

    def drop_tips(self, joints, sites, materials, tip_spot_names, use_channels=None,
                  offset_x_mm=0.0, offset_y_mm=0.0, offset_z_mm=0.0, allow_nonzero_volume=False):
        """退吸头：各通道带着吸头回到吸头位，把吸头留下"""
        channels = _channels(tip_spot_names, use_channels)
        points, carries = [], {}
        for name, channel in zip(tip_spot_names, channels):
            if channel not in self._tips:
                raise ValueError("通道 %d 上没有吸头" % channel)
            top = materials.point(name, "top_center")
            points.append((top[0] + offset_x_mm / 1000.0, top[1] + offset_y_mm / 1000.0,
                           top[2] - materials.get(name, "fitting_depth") + offset_z_mm / 1000.0))
            carries[channel] = {"from": TOOLS[channel], "to": "resource:" + name}
        remaining = {c: v for c, v in self._tips.items() if c not in channels}
        steps = self._visit(joints, sites, channels, points, carries, {"tips": sorted(remaining)})
        self._tips = remaining
        return steps

    def _liquid(self, joints, sites, materials, resource_names, use_channels, liquid_heights_mm):
        channels = _channels(resource_names, use_channels)
        heights = list(liquid_heights_mm) if liquid_heights_mm else [0.0] * len(channels)
        if len(heights) != len(channels):
            raise ValueError("liquid_heights_mm 必须与资源一一对应")
        points = []
        for name, channel, height in zip(resource_names, channels, heights):
            if channel not in self._tips:
                raise ValueError("通道 %d 上没有吸头，不能吸液或分液" % channel)
            bottom = materials.point(name, "bottom_center")
            # 吸头尖到孔底以上 liquid_height；工具点（通道端面）在吸头尖上方一个露出长度处。
            points.append((bottom[0], bottom[1], bottom[2] + float(height) / 1000.0 + self._tips[channel]))
        return self._visit(joints, sites, channels, points)

    def aspirate(self, joints, sites, materials, resource_names, volumes_ul=None, use_channels=None,
                 flow_rates_ul_s=None, liquid_heights_mm=None, blow_out_air_volumes_ul=None):
        """吸液：吸头尖下探到孔底以上指定高度并停留；体积变化由 OS 按动作合同结算"""
        return self._liquid(joints, sites, materials, resource_names, use_channels, liquid_heights_mm)

    def dispense(self, joints, sites, materials, resource_names, volumes_ul=None, use_channels=None,
                 flow_rates_ul_s=None, liquid_heights_mm=None, blow_out_air_volumes_ul=None):
        """分液：吸头尖下探到孔底以上指定高度并停留；体积变化由 OS 按动作合同结算"""
        return self._liquid(joints, sites, materials, resource_names, use_channels, liquid_heights_mm)
