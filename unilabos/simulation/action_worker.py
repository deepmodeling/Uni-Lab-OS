"""独立 OS 执行服务组合根；通过受信设备包工厂装配物理及过程端口。"""
import argparse
import importlib
import json
import os
import socket
import traceback
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--factory", required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--socket", type=Path, required=True)
    args = parser.parse_args()
    if args.socket.exists(): raise FileExistsError(args.socket)
    module, name = args.factory.split(":", 1)
    service = getattr(importlib.import_module(module), name)(json.loads(args.config.read_text()))
    bound = False
    control_worker = None
    try:
        control = getattr(service, "control", None)
        if control is not None:
            from .control_worker import ControlWorker
            control_worker = ControlWorker(str(args.socket)+".control", control)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(args.socket)); bound = True
            os.chmod(args.socket, 0o600); server.listen(4); server.settimeout(.2)
            print("UNILAB_ACTION_WORKER_READY", flush=True)
            while True:
                poll = getattr(service, "poll_controls", None)
                if poll is not None: poll()
                try: client, _ = server.accept()
                except socket.timeout: continue
                with client:
                    client.settimeout(120)
                    try:
                        with client.makefile("rb") as stream:
                            request = json.loads(stream.readline(65537))
                        result = service.execute(request)
                        response = {"ok": True, "result": result}
                    except Exception as error:
                        traceback.print_exc()
                        response = {"ok": False, "error": f"{type(error).__name__}: {error}"}
                    client.sendall(json.dumps(response, allow_nan=False).encode() + b"\n")
    finally:
        try:
            if control_worker is not None: control_worker.close()
        finally:
            try: service.close()
            finally:
                if bound: args.socket.unlink(missing_ok=True)

if __name__ == "__main__": main()
