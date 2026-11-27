"""管理阻塞设备驱动的工作线程、结果和可确认的退出。"""
from threading import Thread


class DriverTask:
    """隔离驱动调用的生命周期；设备方法和参数由调用方提供。"""
    def __init__(self, cancel, *, name='original-driver', error_prefix='original_driver'):
        """绑定协作取消函数及诊断名称，不创建线程。"""
        self.cancel = cancel
        self.name, self.error_prefix = name, error_prefix
        self.thread = None
        self.result = None

    def start(self, call):
        """启动唯一在途调用；保存成功结果或异常，不吞掉失败状态。"""
        if self.thread is not None and self.thread.is_alive():
            raise RuntimeError(self.error_prefix + '_busy')
        self.result = None
        def run():
            """在线程内捕获驱动结果，供扫描线程检查。"""
            try:
                self.result = call()
            except BaseException as error:
                self.result = {'success': False, 'error': repr(error)}
        self.thread = Thread(target=run, name=self.name, daemon=True)
        self.thread.start()

    def check(self):
        """返回当前结果；已完成但非成功的调用向扫描线程传播错误。"""
        if self.result is not None and self.result.get('success') is not True:
            raise RuntimeError(self.error_prefix + '_failed: ' + str(self.result))
        return self.result

    def close(self):
        """请求取消并等待退出；线程未退出时报告清理未确认。"""
        self.cancel()
        if self.thread is not None:
            self.thread.join(timeout=2)
            if self.thread.is_alive():
                raise RuntimeError(self.error_prefix + '_stop_unconfirmed')
