import json
import logging

import requests


class BaseRequest:
    def __init__(self):
        self._logger = logging.getLogger(__name__)

    def get_logger(self):
        return self._logger

    def get(self, url, params, headers={"Content-Type": "application/json"}):
        try:
            response = requests.get(url, params=params, headers=headers, timeout=30)
            self.get_logger().debug(
                f"Request >>> : {params} {response.status_code} {response.text}"
            )
            if response.status_code == 200:
                return response.json()

        except Exception as e:
            self.get_logger().error(f"Request ERROR: {e}")
            return

    def post(self, url, params={}, files=None, headers={"Content-Type": "application/json"}):
        try:
            response = requests.post(
                url, data=json.dumps(params) if params else None, headers=headers, timeout=120, files=files
            )
            self.get_logger().debug(
                f"Request >>> : {response.request.body} {response.status_code} {response.text}"
            )
            if response.status_code == 200:
                return response.json()
            else:
                raise Exception("Request ERROR:", response.text)

        except Exception as e:
            self.get_logger().error(f"Request ERROR: {e}")
            return

    def form_post(self, url, params):
        try:
            response = requests.post(
                url=url,
                data=params,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=3,
            )
            self.get_logger().debug(
                f"Request >>> : {response.request.body} {response.status_code} {response.text}"
            )
            if response.status_code == 200:
                return response.json()
            else:
                raise Exception("Request ERROR:", response.text)

        except Exception as e:
            self.get_logger().error(f"Request ERROR: {e}")
            return


# 使用示例
if __name__ == "__main__":
    pass
