"""
功能概览：
生成、下发参数文件 params.json
启动配方并轮询状态、拉取结果
启动烧结、球磨与查询炉子状态
上下料任务控制

实现要点：
全部 TCP 命令通过长连接发送，使用 CRLF 作为消息结束符
返回存在 return_value 时优先解析；否则解析顶层 request_id/result/data
统一返回结构：除少数查询类接口外，仅返回 request_id 与 result

任务ID
1: load_params
2: start_recipt
3: start_sintering
4: get_furnace_status
6: get_recipt_status
7: get_recipt_result
8: start_milling
9: loading_material
10: unloading_material
11: start_material
12: get_ball_bead
13: set_ball_bead
14: unload_sintering
"""

import json
import os
import socket
import time
import datetime

class SSEAutoSynthesisStation:
    """
    SSE auto synthesis 设备命令封装类（长连接）
    属性：
    ip: 目标设备 IP
    port: 目标设备端口
    _sock: 复用的 TCP 套接字（出现异常时自动重连）
    status: 设备侧状态（简单标记）
    status: 设备侧状态（简单标记）
    """
    def __init__(self, **kwargs):
        self.status = "idle"
        self.ip = kwargs.get("ip", "127.0.0.1")
        self.port = kwargs.get("port", 8091)
        self._sock = None

    def _parse_kv_pair(self, s):
        """
        将 'Key:val1,val2' / 'Key=val1,val2' / 'Key val1 val2' 解析为 (key, [v1, v2])
        兼容多种输入格式，便于从前端/文本批量导入
        """
        if ":" in s:
            key, rest = s.split(":", 1)
        elif "=" in s:
            key, rest = s.split("=", 1)
        else:
            parts = s.split()
            if len(parts) != 3:
                raise ValueError("Bad item format. Use 'Key:val1,val2' or 'Key val1 val2'")
            key, v1, v2 = parts
            return key, [float(v1), float(v2)]
        if "," not in rest:
            raise ValueError("Value part must be 'val1,val2'")
        v1, v2 = rest.split(",", 1)
        return key.strip(), [float(v1), float(v2)]

    def _entries_to_dict(self, entries):
        """
        将材料条目列表转换为字典 {MaterialName: [Quality, Precision]}
        支持 dict 条目或字符串条目
        dict 支持多种键：key/name/Material 与 values/[val1,val2]/Quality (g)/Precision (g)
        示例：
        字符串："A1:1.23,0.01" 或 "A1 1.23 0.01" 或 "A1=1.23,0.01"
        字典：{"Material":"A1","Quality (g)":1.23,"Precision (g)":0.01}
        字典：{"key":"A1","values":[1.23,0.01]}
        """
        d = {}
        if isinstance(entries, list):
            for item in entries:
                try:
                    key = None
                    if isinstance(item, dict):
                        key = item.get("key") or item.get("name") or item.get("Material")
                        values = item.get("values")
                        if isinstance(values, list) and len(values) >= 2:
                            v1, v2 = float(values[0]), float(values[1])
                        else:
                            v1 = item.get("val1")
                            v2 = item.get("val2")
                            if v1 is None or v2 is None:
                                v1 = item.get("Quality (g)")
                                v2 = item.get("Precision (g)")
                            if v1 is None or v2 is None:
                                continue
                            v1, v2 = float(v1), float(v2)
                    elif isinstance(item, str):
                        key, pair = self._parse_kv_pair(item)
                        v1, v2 = pair
                    else:
                        continue
                    if not key:
                        continue
                    d[str(key)] = [v1, v2]
                except Exception:
                    continue
        return d

    def check_connection(self, ip: str | None = None, port: int | None = None, timeout: float = 20.0) -> dict:
        """
        简单联通性检查（创建一次连接并设置超时）
        返回：
        reachable: 是否可达
        error: 异常信息（可为空）
        duration: 尝试时长（秒）
        示例：
        check_connection() -> {"reachable": true, ...}
        check_connection(ip="192.168.1.10", port=8091, timeout=5.0)
        """
        target_ip = ip or self.ip
        target_port = int(port or self.port)
        ok = False
        err = ""
        start = time.time()
        try:
            if self._sock is None:
                self._sock = socket.create_connection((target_ip, target_port), timeout=timeout)
                try:
                    self._sock.settimeout(3.0)
                except Exception:
                    pass
            ok = True
        except Exception as e:
            err = str(e)
            try:
                if self._sock is not None:
                    self._sock.close()
            except Exception:
                pass
            self._sock = None
        duration = time.time() - start
        return {
            "ip": target_ip,
            "port": target_port,
            "reachable": ok,
            "error": err,
            "duration": duration,
        }

    def _ensure_socket(self, ip: str | None = None, port: int | None = None, timeout: float = 5.0) -> bool:
        """
        确保长连接套接字存在：
        不存在则创建
        设置接收超时（用于按行接收）
        幂等：连接有效时重复调用不会重新创建连接
        """
        target_ip = ip or self.ip
        target_port = int(port or self.port)
        try:
            if self._sock is None:
                self._sock = socket.create_connection((target_ip, target_port), timeout=timeout)
                try:
                    self._sock.settimeout(3.0)
                except Exception:
                    pass
            return True
        except Exception:
            try:
                if self._sock is not None:
                    self._sock.close()
            except Exception:
                pass
            self._sock = None
            return False

    def _send_only(self, obj, ip: str | None = None, port: int | None = None) -> bool:
        """
        仅发送一条 JSON 命令（追加 CRLF），不等待返回
        若发送失败会尝试一次自动重连并重发
        """
        if not self._ensure_socket(ip, port):
            return False
        try:
            send_text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
            data = (send_text + "\r\n").encode("utf-8")
            self._sock.sendall(data)
            return True
        except Exception:
            try:
                if self._sock is not None:
                    self._sock.close()
            except Exception:
                pass
            self._sock = None
            if not self._ensure_socket(ip, port):
                return False
            try:
                send_text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
                data = (send_text + "\r\n").encode("utf-8")
                self._sock.sendall(data)
                return True
            except Exception:
                return False

    def _recv_text(self) -> str:
        """
        按行读取服务端返回：
        聚合字节块直到遇到 '\\n' 或 '\\r\\n'
        忽略编码异常并去除首尾空白
        行协议：服务端一条消息以换行结束（与发送端 CRLF 对齐）
        """
        txt = ""
        try:
            chunks = []
            while True:
                try:
                    b = self._sock.recv(4096)
                except socket.timeout:
                    break
                if not b:
                    break
                chunks.append(b)
                # 以换行符作为消息结束标记（与发送端 CRLF 协议匹配）
                if b.endswith(b"\n") or b.endswith(b"\r\n"):
                    break
            txt = (b"".join(chunks)).decode("utf-8", "ignore").strip()
        except Exception:
            txt = ""
        return txt

    def _send_recv(self, obj, ip: str | None = None, port: int | None = None) -> str:
        """
        发送一条 JSON 命令并读取单条返回文本：
        失败时会尝试关闭重连后重发一次
        示例：
        obj={"request_id":3,"action":"start_sintering"} → 返回形如 '{"request_id":3,"result":0}'
        """
        if not self._ensure_socket(ip, port):
            return ""
        try:
            send_text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
            data = (send_text + "\r\n").encode("utf-8")
            self._sock.sendall(data)
            return self._recv_text()
        except Exception:
            try:
                if self._sock is not None:
                    self._sock.close()
            except Exception:
                pass
            self._sock = None
            if not self._ensure_socket(ip, port):
                return ""
            try:
                send_text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
                data = (send_text + "\r\n").encode("utf-8")
                self._sock.sendall(data)
                return self._recv_text()
            except Exception:
                return ""


    def _build_params_json(self, r1_items, r2_items, name_r1: str = "Recipe_R1", name_r2: str = "Recipe_R2", nbead_r1: int | None = None, nbead_r2: int | None = None):
        """
        构造 params.json 主体结构
        参数：
        r1_items/r2_items: 形如 {'A1':[1.23,0.01], ...} 的材料字典
        name_r1/name_r2: 配方名（默认 Recipe_R1/Recipe_R2）
        nbead_r1/nbead_r2: 球珠数量（空则使用默认 100）
        返回：
        params 字典（包含 Solid_loading_module、Ball_milling_module、Sintering_module）
        输出示例片段：
        {
          "Solid_loading_module": {
            "status": true,
            "n_Recipes": 1,
            "Recipe_R1": {"name":"YYYYMMDD-R1","n_ball_bead":100,"A1":[1.23,0.01]}
          },
          "Sintering_module": {"Status": true, "furnace_01": {...}}
        }
        """
        def recipe_obj(name, items_dict, nbead=None):
            nb = 100
            if nbead is not None:
                try:
                    nb = int(nbead)
                except Exception:
                    pass
            obj = {"name": name, "n_ball_bead": nb}
            for k, v in (items_dict or {}).items():
                obj[k] = v
            return obj
        recipes_dict = {}
        if isinstance(r1_items, dict) and len(r1_items) > 0:
            recipes_dict["Recipe_R1"] = recipe_obj(name_r1, r1_items, nbead_r1)
        if isinstance(r2_items, dict) and len(r2_items) > 0:
            recipes_dict["Recipe_R2"] = recipe_obj(name_r2, r2_items, nbead_r2)
        n_recipes = len(recipes_dict)

        params = {
            "Solid_loading_module": {
                "status": True,
                "n_Recipes": n_recipes,
            },
            "Ball_milling_module": {
                "status": False,
                "n_ball_milling": 2,
                "n_step": 20,
                "Parameter": {
                    "Step1": {"run_direct": True, "speed": 600, "run_time": 2, "stop_time": 5},
                    "Step2": {"run_direct": False, "speed": 600, "run_time": 2, "stop_time": 5},
                },
            },
            "Sintering_module": {
                "Status": True,
                "sintering_recipe": ([name_r1] if "Recipe_R1" in recipes_dict else []) + ([name_r2] if "Recipe_R2" in recipes_dict else []),
                "n_day": 2,
                "furnace_01": {
                    "status": True,
                    "begin_temp1": 25,
                    "time_temp1": 180,
                    "end_temp1": 480,
                    "time_temp2": 360,
                    "end_temp2": 480,
                    "open_door_temp": 50,
                },
                "furnace_02": {
                    "status": True,
                    "begin_temp1": 25,
                    "time_temp1": 180,
                    "end_temp1": 490,
                    "time_temp2": 360,
                    "end_temp2": 490,
                    "open_door_temp": 50,
                },
                "furnace_03": {
                    "status": True,
                    "begin_temp1": 25,
                    "time_temp1": 180,
                    "end_temp1": 500,
                    "time_temp2": 360,
                    "end_temp2": 500,
                    "open_door_temp": 50,
                },
                "furnace_04": {
                    "status": True,
                    "begin_temp1": 25,
                    "time_temp1": 180,
                    "end_temp1": 510,
                    "time_temp2": 360,
                    "end_temp2": 510,
                    "open_door_temp": 50,
                },
            },
        }
        params["Solid_loading_module"].update(recipes_dict)
        return params

    def generate_params(self, recipe1_entries: list | None = None, recipe2_entries: list | None = None) -> dict:
        """
        将输入条目转换并生成基本的 params 结构（不写文件/不下发）
        """
        r1 = self._entries_to_dict(recipe1_entries)
        r2 = self._entries_to_dict(recipe2_entries)
        return self._build_params_json(r1, r2)

    def update_params(
        self,
        out: str | None = None,
        send: bool = True,
        recipe1_entries: list | None = None,
        recipe2_entries: list | None = None,
        furnace_01: dict | None = None,
        furnace_02: dict | None = None,
        furnace_03: dict | None = None,
        furnace_04: dict | None = None,
        **kwargs,
        
    ) -> dict:
        """
        生成 params.json、可写入指定位置并通过 TCP 下发
        关键行为：
        A/Recipe_* 可同时提供材料列表与 n_ball_bead；配方名按当天日期生成
        B/Status 与各 furnace_* 字段将覆盖默认烧结参数
        “添加温度段”支持动态扩展 time_tempN/end_tempN
        out 为空时写到脚本目录 params.json；send 控制是否发送 TCP
        返回：
        {"request_id":1,"result":<code>}
        输入示例：
        A = {
          "Recipe_1": {"Materials":[{"Material":"A1","Quality (g)":1.23,"Precision (g)":0.01}], "n_ball_bead":100},
          "Recipe_2": {"Materials":[{"Material":"B1","Quality (g)":3.21,"Precision (g)":0.01}], "n_ball_bead":100}
        }
        B = {
          "Status": true,
          "furnace_01": {"status":true,"begin_temp1":25,"time_temp1":180,"end_temp1":480},
          "furnace_02": {"status":true,"begin_temp1":25,"time_temp1":180,"end_temp1":490}
        }
        """
        A = kwargs.get("A") if isinstance(kwargs.get("A"), dict) else {}
        r1_from_a = None
        r2_from_a = None
        if isinstance(A.get("Recipe_1"), dict):
            r1_from_a = A.get("Recipe_1").get("Materials")
        if isinstance(A.get("Recipe_2"), dict):
            r2_from_a = A.get("Recipe_2").get("Materials")
        if recipe1_entries is None and isinstance(r1_from_a, list):
            recipe1_entries = r1_from_a
        if recipe2_entries is None and isinstance(r2_from_a, list):
            recipe2_entries = r2_from_a
        r1 = self._entries_to_dict(recipe1_entries)
        r2 = self._entries_to_dict(recipe2_entries)
        today_str = datetime.date.today().strftime("%Y%m%d")
        name_r1 = f"{today_str}-R1"
        name_r2 = f"{today_str}-R2"
        n1 = None
        n2 = None
        if isinstance(A.get("Recipe_1"), dict):
            n1 = A.get("Recipe_1").get("n_ball_bead")
        if isinstance(A.get("Recipe_2"), dict):
            n2 = A.get("Recipe_2").get("n_ball_bead")
        params = self._build_params_json(r1, r2, name_r1, name_r2, n1, n2)
        B = kwargs.get("B") if isinstance(kwargs.get("B"), dict) else {}
        if "Status" in B and B["Status"] is not None:
            try:
                params["Sintering_module"]["Status"] = bool(B["Status"])
            except Exception:
                pass
        furnaces = {
            "furnace_01": furnace_01 if isinstance(furnace_01, dict) else B.get("furnace_01"),
            "furnace_02": furnace_02 if isinstance(furnace_02, dict) else B.get("furnace_02"),
            "furnace_03": furnace_03 if isinstance(furnace_03, dict) else B.get("furnace_03"),
            "furnace_04": furnace_04 if isinstance(furnace_04, dict) else B.get("furnace_04"),
        }
        for k, v in furnaces.items():
            if isinstance(v, dict):
                target = params.get("Sintering_module", {}).get(k, {})
                for key in [
                    "status",
                    "begin_temp1",
                    "time_temp1",
                    "end_temp1",
                    "time_temp2",
                    "end_temp2",
                ]:
                    if key in v and v[key] is not None:
                        target[key] = v[key]
                # 安全考虑：开门温度固定 50（覆盖输入）
                target["open_door_temp"] = 50
                if "添加温度段" in v and isinstance(v["添加温度段"], list):
                    idx = 3
                    for step in v["添加温度段"]:
                        try:
                            tt = step.get("time_temp")
                            et = step.get("end_temp")
                        except AttributeError:
                            tt = None
                            et = None
                        if tt is None or et is None:
                            continue
                        target[f"time_temp{idx}"] = tt
                        target[f"end_temp{idx}"] = et
                        idx += 1
        text = json.dumps(params, ensure_ascii=False, indent=2)
        envelope = {
            "request_id": 1,
            "action": "load_params",
            "data": params,
        }
        send_text = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
        if out:
            target = out
            try:
                if os.path.isdir(target):
                    target = os.path.join(target, "params.json")
            except Exception:
                pass
        else:
            target = os.path.join(os.path.dirname(os.path.abspath(__file__)), "params.json")

        dirpath = os.path.dirname(target)
        if dirpath and not os.path.exists(dirpath):
            os.makedirs(dirpath, exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="\n") as f:
            f.write(text + "\n")

        target_ip = self.ip
        target_port = int(self.port)
        UpdateParams = {"request_id": 1, "result": -1}
        if send:
            resp_text = self._send_recv(envelope, target_ip, target_port)
            if resp_text:
                try:
                    obj = json.loads(resp_text)
                except Exception:
                    obj = None
                if isinstance(obj, dict):
                    d = obj.get("return_value") if isinstance(obj.get("return_value"), dict) else obj
                    if isinstance(d, dict):
                        try:
                            UpdateParams["request_id"] = int(d.get("request_id", 1))
                        except Exception:
                            UpdateParams["request_id"] = 1
                        if "result" in d:
                            UpdateParams["result"] = d.get("result")
        return UpdateParams

    def start_recipt(self, ip: str | None = None, port: int | None = None, out: str | None = None) -> dict:
        """
        启动配方 → 轮询状态 → 拉取结果（可选导出 JSON）
        返回：
        成功：{"request_id":2,"result":0,"data":{ "<recipt>":{...}, ... }}
        失败：{"request_id":2,"result":<非0>}
        备注：
        状态轮询最多持续 1 小时；服务端状态值需为 0/1/2（2 表示完成）
        轮询间隔 5 秒；完成后逐个调用 get_recipt_result 拉取明细
        服务端状态返回示例：{"data":{"#1-20251205-R1":2,"#2-20251205-R2":1}}
        """
        t0 = self._send_recv({"request_id": 2, "action": "start_recipt"}, ip, port)
        if t0:
            try:
                o0 = json.loads(t0)
            except Exception:
                o0 = None
            if isinstance(o0, dict):
                d0 = o0.get("return_value") if isinstance(o0.get("return_value"), dict) else o0
                if isinstance(d0, dict) and ("result" in d0):
                    try:
                        r0 = int(d0.get("result"))
                    except Exception:
                        r0 = d0.get("result")
                    if r0 != 0:
                        return {"request_id": 2, "result": r0}
        done = False
        statuses = {}
        deadline = time.time() + 3600.0
        while time.time() < deadline:
            t = self._send_recv({"request_id": 6, "action": "get_recipt_status"}, ip, port)
            obj = None
            if t:
                try:
                    obj = json.loads(t)
                except Exception:
                    obj = None
            if isinstance(obj, dict):
                d = obj.get("return_value") if isinstance(obj.get("return_value"), dict) else obj
                data_map = d.get("data") if isinstance(d.get("data"), dict) else None
                if isinstance(data_map, dict) and len(data_map) > 0:
                    parsed = {}
                    for k, v in data_map.items():
                        code = None
                        if isinstance(v, (int, float)):
                            try:
                                code = int(v)
                            except Exception:
                                code = None
                        elif isinstance(v, str):
                            s = v.strip()
                            try:
                                code = int(s)
                            except Exception:
                                try:
                                    code = int(float(s))
                                except Exception:
                                    code = None
                        if code is not None and code in (0, 1, 2):
                            parsed[k] = code
                    if parsed:
                        statuses = parsed
                        all_done = True
                        for _, c in parsed.items():
                            if c != 2:
                                all_done = False
                                break
                        if all_done:
                            done = True
                            break
            time.sleep(5.0)
        results = {}
        if done and isinstance(statuses, dict):
            for k, v in statuses.items():
                try:
                    if int(v) != 2:
                        continue
                except Exception:
                    continue
                t2 = self._send_recv({"request_id": 7, "action": "get_recipt_result", "param": {"recipt": k}}, ip, port)
                o = None
                if t2:
                    try:
                        o = json.loads(t2)
                    except Exception:
                        o = None
                if isinstance(o, dict):
                    d2 = o.get("return_value") if isinstance(o.get("return_value"), dict) else o
                    dat = d2.get("data") if isinstance(d2.get("data"), dict) else None
                    if isinstance(dat, dict):
                        results[k] = dat
        if done:
            result_obj = {"request_id": 2, "result": 0, "data": results}
            if out:
                target = out
                try:
                    if os.path.isdir(target):
                        target = os.path.join(target, "实际加样重量.json")
                except Exception:
                    pass
                dirpath = os.path.dirname(target)
                if dirpath and not os.path.exists(dirpath):
                    os.makedirs(dirpath, exist_ok=True)
                try:
                    with open(target, "w", encoding="utf-8", newline="\n") as f:
                        f.write(json.dumps(result_obj, ensure_ascii=False, indent=2) + "\n")
                except Exception:
                    pass
            return result_obj
        else:
            return {"request_id": 2, "result": -1}

    def start_sintering(self, ip: str | None = None, port: int | None = None) -> dict:
        """
        启动烧结流程
        返回：{"request_id":3,"result":<code>}
        """
        envelope = {
            "request_id": 3,
            "action": "start_sintering",
        }
        target_ip = ip or self.ip
        target_port = int(port or self.port)
        resp_text = self._send_recv(envelope, target_ip, target_port)
        Sintering = {"request_id": 3, "result": -1}
        if resp_text:
            try:
                obj = json.loads(resp_text)
            except Exception:
                obj = None
            if isinstance(obj, dict):
                d = obj.get("return_value") if isinstance(obj.get("return_value"), dict) else obj
                if isinstance(d, dict):
                    try:
                        Sintering["request_id"] = int(d.get("request_id", 3))
                    except Exception:
                        Sintering["request_id"] = 3
                    if "result" in d:
                        Sintering["result"] = d.get("result")
        return Sintering

    def start_milling(self, ip: str | None = None, port: int | None = None) -> dict:
        """
        启动球磨流程
        返回：{"request_id":8,"result":<code>}
        """
        envelope = {
            "request_id": 8,
            "action": "start_milling",
        }
        target_ip = ip or self.ip
        target_port = int(port or self.port)
        resp_text = self._send_recv(envelope, target_ip, target_port)
        Milling = {"request_id": 8, "result": -1}
        if resp_text:
            try:
                obj = json.loads(resp_text)
            except Exception:
                obj = None
            if isinstance(obj, dict):
                d = obj.get("return_value") if isinstance(obj.get("return_value"), dict) else obj
                if isinstance(d, dict):
                    try:
                        Milling["request_id"] = int(d.get("request_id", 8))
                    except Exception:
                        Milling["request_id"] = 8
                    if "result" in d:
                        Milling["result"] = d.get("result")
        return Milling

    def get_ball_bead(self, ip: str | None = None, port: int | None = None) -> dict:
        """
        查询球磨珠数量
        返回：{"request_id":12,"result":<code>,"data":{"n_ball_bead":<int>}}
        示例：发送 {"request_id":12,"action":"get_ball_bead"} → 返回包含 data 的对象
        """
        envelope = {
            "request_id": 12,
            "action": "get_ball_bead",
        }
        target_ip = ip or self.ip
        target_port = int(port or self.port)
        resp_text = self._send_recv(envelope, target_ip, target_port)
        result_obj = {"request_id": 12, "result": -1, "data": {}}
        if resp_text:
            try:
                obj = json.loads(resp_text)
            except Exception:
                obj = None
            if isinstance(obj, dict):
                d = obj.get("return_value") if isinstance(obj.get("return_value"), dict) else obj
                if isinstance(d, dict):
                    try:
                        result_obj["request_id"] = int(d.get("request_id", 12))
                    except Exception:
                        result_obj["request_id"] = 12
                    if "result" in d:
                        result_obj["result"] = d.get("result")
                    if "data" in d and isinstance(d.get("data"), dict):
                        result_obj["data"] = d.get("data")
        return result_obj

    def set_ball_bead(self, n_ball_bead: int, ip: str | None = None, port: int | None = None) -> dict:
        """
        设置球磨珠数量（前端输入）
        参数：
        n_ball_bead: 球珠数（将转为 int）
        返回：{"request_id":13,"result":<code>}
        示例：发送 {"request_id":13,"action":"set_ball_bead","param":{"n_ball_bead":200}}
        """
        envelope = {
            "request_id": 13,
            "action": "set_ball_bead",
            "param": {
                "n_ball_bead": int(n_ball_bead),
            },
        }
        target_ip = ip or self.ip
        target_port = int(port or self.port)
        resp_text = self._send_recv(envelope, target_ip, target_port)
        result_obj = {"request_id": 13, "result": -1}
        if resp_text:
            try:
                obj = json.loads(resp_text)
            except Exception:
                obj = None
            if isinstance(obj, dict):
                d = obj.get("return_value") if isinstance(obj.get("return_value"), dict) else obj
                if isinstance(d, dict):
                    try:
                        result_obj["request_id"] = int(d.get("request_id", 13))
                    except Exception:
                        result_obj["request_id"] = 13
                    if "result" in d:
                        result_obj["result"] = d.get("result")
        return result_obj

    def get_furnace_status(self, ip: str | None = None, port: int | None = None) -> dict:
        """
        查询四台马弗炉状态
        返回：{"request_id":4,"result":<code>,"data":{...}}
        示例：{"request_id":4,"action":"get_furnace_status"} → data 含 furnace_01..04
        """
        envelope = {
            "request_id": 4,
            "action": "get_furnace_status",
        }
        target_ip = ip or self.ip
        target_port = int(port or self.port)
        resp_text = self._send_recv(envelope, target_ip, target_port)
        furnace_status = {"request_id": 4, "result": -1, "data": {}}
        if resp_text:
            try:
                obj = json.loads(resp_text)
            except Exception:
                obj = None
            if isinstance(obj, dict):
                d = obj.get("return_value") if isinstance(obj.get("return_value"), dict) else obj
                if isinstance(d, dict):
                    try:
                        furnace_status["request_id"] = int(d.get("request_id", 4))
                    except Exception:
                        furnace_status["request_id"] = 4
                    if "result" in d:
                        furnace_status["result"] = d.get("result")
                    if "data" in d and isinstance(d.get("data"), dict):
                        furnace_status["data"] = d.get("data")
        return furnace_status

    def unload_sintering(self, furnace: list | None = None, ip: str | None = None, port: int | None = None) -> dict:
        """
        下坩埚
        参数：
        furnace: 炉号列表（元素可为字符串/数字，将转为 int；可传单个值）
        返回：{"request_id":14,"result":<code>}
        示例：{"request_id":14,"action":"unload_sintering","param":{"furnace":[1,2]}}
        """
        vals = []
        if isinstance(furnace, list):
            for x in furnace:
                try:
                    vals.append(int(x))
                except Exception:
                    pass
        elif furnace is not None:
            try:
                vals.append(int(furnace))
            except Exception:
                pass
        envelope = {
            "request_id": 14,
            "action": "unload_sintering",
            "param": {
                "furnace": vals,
            },
        }
        target_ip = ip or self.ip
        target_port = int(port or self.port)
        resp_text = self._send_recv(envelope, target_ip, target_port)
        result_obj = {"request_id": 14, "result": -1}
        if resp_text:
            try:
                obj = json.loads(resp_text)
            except Exception:
                obj = None
            if isinstance(obj, dict):
                d = obj.get("return_value") if isinstance(obj.get("return_value"), dict) else obj
                if isinstance(d, dict):
                    try:
                        result_obj["request_id"] = int(d.get("request_id", 14))
                    except Exception:
                        result_obj["request_id"] = 14
                    if "result" in d:
                        result_obj["result"] = d.get("result")
        return result_obj

    def loading_material(self, materials: list | dict | None = None, ip: str | None = None, port: int | None = None) -> dict:
        """
        上料（材料列表可变，键名固定）
        参数（materials 列表/单个 dict）：
        material: 物料名（str）
        weight: 目标重量（float）
        manual_rack: 手动货位（int）
        auto_rack: 自动货位（int）
        返回：{"request_id":9,"result":<code>}
        示例：
        {"request_id":9,"action":"loading_material","param":[
          {"material":"Li2S","weight":10.0,"manual_rack":1,"auto_rack":1},
          {"material":"P2S5","weight":10.0,"manual_rack":2,"auto_rack":2}
        ]}
        """
        items = []
        if isinstance(materials, list):
            for it in materials:
                if isinstance(it, dict):
                    m = it.get("material")
                    w = it.get("weight")
                    mr = it.get("manual_rack")
                    ar = it.get("auto_rack")
                    try:
                        items.append({
                            "material": str(m),
                            "weight": float(w),
                            "manual_rack": int(mr),
                            "auto_rack": int(ar),
                        })
                    except Exception:
                        pass
        elif isinstance(materials, dict):
            m = materials.get("material")
            w = materials.get("weight")
            mr = materials.get("manual_rack")
            ar = materials.get("auto_rack")
            try:
                items.append({
                    "material": str(m),
                    "weight": float(w),
                    "manual_rack": int(mr),
                    "auto_rack": int(ar),
                })
            except Exception:
                pass
        envelope = {
            "request_id": 9,
            "action": "loading_material",
            "param": items,
        }
        target_ip = ip or self.ip
        target_port = int(port or self.port)
        resp_text = self._send_recv(envelope, target_ip, target_port)
        result_obj = {"request_id": 9, "result": -1}
        if resp_text:
            try:
                obj = json.loads(resp_text)
            except Exception:
                obj = None
            if isinstance(obj, dict):
                d = obj.get("return_value") if isinstance(obj.get("return_value"), dict) else obj
                if isinstance(d, dict):
                    try:
                        result_obj["request_id"] = int(d.get("request_id", 9))
                    except Exception:
                        result_obj["request_id"] = 9
                    if "result" in d:
                        result_obj["result"] = d.get("result")
        return result_obj

    def unloading_material(self, materials: list | dict | None = None, ip: str | None = None, port: int | None = None) -> dict:
        """
        下料（列表可变，键名固定）
        参数（materials 列表/单个 dict）：
        manual_rack: 手动货位（int）
        auto_rack: 自动货位（int）
        返回：{"request_id":10,"result":<code>}
        示例：
        {"request_id":10,"action":"unloading_material","param":[
          {"auto_rack":1,"manual_rack":1},
          {"auto_rack":2,"manual_rack":2}
        ]}
        """
        items = []
        if isinstance(materials, list):
            for it in materials:
                if isinstance(it, dict):
                    mr = it.get("manual_rack")
                    ar = it.get("auto_rack")
                    try:
                        items.append({
                            "manual_rack": int(mr),
                            "auto_rack": int(ar),
                        })
                    except Exception:
                        pass
        elif isinstance(materials, dict):
            mr = materials.get("manual_rack")
            ar = materials.get("auto_rack")
            try:
                items.append({
                    "manual_rack": int(mr),
                    "auto_rack": int(ar),
                })
            except Exception:
                pass
        envelope = {
            "request_id": 10,
            "action": "unloading_material",
            "param": items,
        }
        target_ip = ip or self.ip
        target_port = int(port or self.port)
        resp_text = self._send_recv(envelope, target_ip, target_port)
        result_obj = {"request_id": 10, "result": -1}
        if resp_text:
            try:
                obj = json.loads(resp_text)
            except Exception:
                obj = None
            if isinstance(obj, dict):
                d = obj.get("return_value") if isinstance(obj.get("return_value"), dict) else obj
                if isinstance(d, dict):
                    try:
                        result_obj["request_id"] = int(d.get("request_id", 10))
                    except Exception:
                        result_obj["request_id"] = 10
                    if "result" in d:
                        result_obj["result"] = d.get("result")
        return result_obj

    def start_material(self, ip: str | None = None, port: int | None = None) -> dict:
        """
        启动上下料任务
        返回：{"request_id":11,"result":<code>}
        示例：{"request_id":11,"action":"start_material"} → {"request_id":11,"result":0}
        """
        envelope = {
            "request_id": 11,
            "action": "start_material",
        }
        target_ip = ip or self.ip
        target_port = int(port or self.port)
        resp_text = self._send_recv(envelope, target_ip, target_port)
        result_obj = {"request_id": 11, "result": -1}
        if resp_text:
            try:
                obj = json.loads(resp_text)
            except Exception:
                obj = None
            if isinstance(obj, dict):
                d = obj.get("return_value") if isinstance(obj.get("return_value"), dict) else obj
                if isinstance(d, dict):
                    try:
                        result_obj["request_id"] = int(d.get("request_id", 11))
                    except Exception:
                        result_obj["request_id"] = 11
                    if "result" in d:
                        result_obj["result"] = d.get("result")
        return result_obj


