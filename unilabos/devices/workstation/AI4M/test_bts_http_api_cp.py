#!/usr/bin/env python3
"""
BTS HTTP API 测试脚本 - CP计时电位法

使用方法：
1. 确保BTS软件已启动并运行
2. 确保HTTP服务器端口为8080（或修改脚本中的BASE_URL）
3. 运行脚本：python test_bts_http_api_cp.py

测试功能：
1. 设备校验
2. 获取设备信息
3. 启动CP测试
4. 停止测试
"""

from re import T
import requests
import json
import time

BASE_URL = "http://localhost:8089"
VALIDATE_CODE = "bts-validate-code-2024"

class BTSHttpApiCPTest:
    def __init__(self, base_url):
        self.base_url = base_url
        self.session = requests.Session()
        self.validated = False
        self.devices = []

    def test_validate(self):
        """测试设备校验"""
        print("\n=== 测试设备校验 ===")

        url = f"{self.base_url}/api/bts/validate"
        payload = {
            "cmd-type": 1,
            "request-id": f"validate-{int(time.time())}",
            "data": {
                "check-id": VALIDATE_CODE
            }
        }

        try:
            response = self.session.post(url, json=payload)
            print(f"状态码: {response.status_code}")
            print(f"响应: {response.text}")

            if response.status_code == 200:
                self.validated = True
                print("✅ 校验成功")
            else:
                print("❌ 校验失败")

        except Exception as e:
            print(f"❌ 请求失败: {str(e)}")

    def test_get_device_info(self):
        """测试获取设备信息"""
        print("\n=== 测试获取设备信息 ===")

        if not self.validated:
            print("⚠️  请先通过设备校验")
            return False

        url = f"{self.base_url}/api/bts/device/info"
        payload = {
            "cmd-type": 2,
            "request-id": f"device-info-{int(time.time())}"
        }

        try:
            response = self.session.get(url, json=payload)
            print(f"状态码: {response.status_code}")

            if response.status_code == 200:
                data = response.json()
                print(f"响应: {json.dumps(data, indent=2, ensure_ascii=False)}")

                if "data" in data and "dev-list" in data["data"]:
                    self.devices = data["data"]["dev-list"]
                    print(f"\n发现 {len(self.devices)} 个设备:")
                    for i, device in enumerate(self.devices):
                        print(f"  设备 {i+1}: {device['dev-uuid']}")
                        print(f"    通道: {device['chl']}")

                print("✅ 获取设备信息成功")
                return len(self.devices) > 0
            else:
                print(f"❌ 请求失败: {response.text}")
                return False

        except Exception as e:
            print(f"❌ 请求失败: {str(e)}")
            return False

    def test_start_cp_test(self, dev_uuid, chl_list):
        """测试启动CP测试"""
        print("\n=== 测试启动CP测试 ===")

        if not self.validated:
            print("⚠️  请先通过设备校验")
            return None

        url = f"{self.base_url}/api/bts/test/start"
        test_id = f"test-cp-{int(time.time())}"

        payload = {
            "cmd-type": 3,
            "request-id": f"start-test-{int(time.time())}",
            "data": {
                "test-id": test_id,
                "dev-ip": dev_uuid,
                "chl-list": chl_list,
                "globalProtect": {
                    "voltageProtect": {
                        "underVoltage": -5,
                        "overVoltage": 5,
                        "enableUnderVoltage": True,
                        "enableOverVoltage": True,
                        "enableRangeProtect": False,
                        "delayTime": 0,
                        "enableDelay": False
                    },
                    "currentProtect": {
                        "charge": 100,
                        "discharge": 100,
                        "enableCharge": True,
                        "enableDischarge": True,
                        "enableRangeProtect": False
                    }
                },
                "globalRecordCondi": {
                    "electricCurrent": 0,
                    "enable_electricCurrent": False,
                    "enable_time": True,
                    "enable_voltage": False,
                    "time": 1000,
                    "voltage": 0
                },
                "batteryInfo": {
                    "creator": "test-user",
                    "weight": 100,
                    "batteryBatchNum": "",
                    "currentUpperLimit": 100,
                    "voltageUpperLimit": 5,
                    "voltageLowerLimit": -5
                },

                "stepList": [
                    {
                        "type": 21,
                        "pType": 0,
                        "mode": 1,
                        "mPara": 50.0,  
                        "rateMode": False,
                        "rateValue": 0,
                        "recordCondi": 
                        {
                        "enable_time": False,
                        "time": 0
                        },
                        "recordCondi": {
                            "enable_time": True,
                            "time": 1000,
                            "enable_voltage": False,
                            "voltage": 0
                        },
                        "endCondi": [
                        {
                            "also": True,
                            "rateMode": False,
                            "rateModeType": 0,
                            "rateValue": 0,
                            "relation": 1,
                            "type": 3,
                            "userCustomVariable-arithmetic": 0,
                            "userCustomVariable-isVariable": False,
                            "userCustomVariable-value": 0,
                            "userCustomVariable-value2": 0,
                            "userCustomVariable-variableParam": 0,
                            "userCustomVariable-variableParam2": 0,
                            "value": 20000   # X000,X为时间s
                        }
                        ]
                        # "endCondi": [
                        #     {
                        #         "also": 1,
                        #         "type": 3, 
                        #         "relation": 1, 
                        #         "value": 60000
                        #     },
                        #     {
                        #         "also": 0,
                        #         "type": 2,
                        #         "relation": 2,
                        #         "value": 5
                        #     }
                        # ]
                    }
                ]
            }
        }

        try:
            response = self.session.post(url, json=payload)
            print(f"状态码: {response.status_code}")
            print(f"响应: {response.text}")

            if response.status_code == 200:
                print("✅ 启动CP测试成功")
                return test_id
            else:
                print("❌ 启动测试失败")
                return None

        except Exception as e:
            print(f"❌ 请求失败: {str(e)}")
            return None

    def test_stop_test(self, dev_uuid, chl_list):
        """测试停止测试"""
        print("\n=== 测试停止测试 ===")

        if not self.validated:
            print("⚠️  请先通过设备校验")
            return

        url = f"{self.base_url}/api/bts/test/stop"
        payload = {
            "cmd-type": 4,
            "request-id": f"stop-test-{int(time.time())}",
            "data": {
                "dev-ip": dev_uuid,
                "chl-list": chl_list
            }
        }

        try:
            response = self.session.post(url, json=payload)
            print(f"状态码: {response.status_code}")
            print(f"响应: {response.text}")

            if response.status_code == 200:
                print("✅ 停止测试成功")
            else:
                print("❌ 停止测试失败")

        except Exception as e:
            print(f"❌ 请求失败: {str(e)}")

    def test_get_channel_state(self, dev_uuid, chl_list):
        """测试获取通道状态"""
        print("\n=== 测试获取通道状态 ===")

        if not self.validated:
            print("⚠️  请先通过设备校验")
            return

        url = f"{self.base_url}/api/bts/test/state"
        payload = {
            "cmd-type": 5,
            "request-id": f"channel-state-{int(time.time())}",
            "data": [
                {
                    "dev-uuid": dev_uuid,
                    "chl-list": chl_list
                }
            ]
        }

        try:
            response = self.session.post(url, json=payload)
            print(f"状态码: {response.status_code}")
            print(f"响应: {response.text}")

            if response.status_code == 200:
                print("✅ 获取通道状态成功")
            else:
                print("❌ 获取通道状态失败")

        except Exception as e:
            print(f"❌ 请求失败: {str(e)}")

def main():
    print("BTS HTTP API CP测试工具")
    print("=" * 50)

    test = BTSHttpApiCPTest(BASE_URL)

    test.test_validate()

    has_device = test.test_get_device_info()

    if test.validated and has_device:
        first_device = test.devices[0]
        dev_uuid = first_device['dev-uuid']
        chl_list = first_device['chl']
        #chl_list = [2]

        print(f"\n将使用设备: {dev_uuid}")
        print(f"通道列表: {chl_list}")

        # input("\n按Enter键开始测试启动CP测试...")
        test_id = test.test_start_cp_test(dev_uuid, chl_list)

        if test_id:
            print(f"\n测试ID: {test_id}")
            print("等待60秒后测试停止...")
            time.sleep(60)

            test.test_get_channel_state(dev_uuid, chl_list)

            test.test_stop_test(dev_uuid, chl_list)
    elif test.validated:
        print("\n⚠️  没有发现可用设备，跳过启动测试")

    print("\n测试完成！")

if __name__ == "__main__":
    main()
