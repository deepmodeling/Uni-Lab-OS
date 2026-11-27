"""控制接收线程只投递请求；世界推进仍在执行器所有者线程。"""
import json
import os
import socket
import threading
from pathlib import Path


class ControlWorker:
    def __init__(self,path,control):
        self.path=Path(path)
        if self.path.exists():raise FileExistsError(path)
        self.server=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        self.server.bind(str(path));os.chmod(path,0o600);self.server.listen(4);self.server.settimeout(.2)
        self.stop=threading.Event();self.control=control
        self.thread=threading.Thread(target=self.run,name='simulation-control',daemon=True)
        self.thread.start()

    def run(self):
        while not self.stop.is_set():
            try:client,_=self.server.accept()
            except socket.timeout:continue
            except OSError:break
            with client:
                client.settimeout(12)
                try:
                    with client.makefile('rb') as stream:request=json.loads(stream.readline(4097))
                    result={'ok':True,'result':self.control(request)}
                except Exception as error:result={'ok':False,'error':str(error)}
                try:client.sendall(json.dumps(result,allow_nan=False).encode()+b'\n')
                except OSError:pass

    def close(self):
        self.stop.set();self.server.close();self.thread.join(timeout=13)
        if self.thread.is_alive():raise RuntimeError('control_worker_stop_unconfirmed')
        self.path.unlink(missing_ok=True)
