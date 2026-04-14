
from abc import abstractmethod
from functools import wraps
import inspect
import json
import logging
import queue
from socket import socket
import threading
import time
import traceback
from typing import Optional

import serial

class SingleRunningExecutor(object):
    """
    异步执行单个任务，不允许重复执行，通过class的函数获得唯一任务名的实例
    需要对
    """
    __instance = {}

    @classmethod
    def get_instance(cls, func, post_func=None, name=None, *var, **kwargs):
        print(f"!!!get_instance: {name} {kwargs}")
        if name is None:
            name = func.__name__
        if name not in cls.__instance:
            cls.__instance[name] = cls(func, post_func, *var, **kwargs)
        return cls.__instance[name]

    start_time: float = None
    end_time: float = None
    is_running: bool = None
    is_error: bool = None
    is_success: bool = None

    @property
    def is_ended(self):
        return not self.is_running and (self.is_error or self.is_success)
    
    @property
    def is_started(self):
        return self.is_running or self.is_error or self.is_success
    
    def reset(self):
        self.start_time = None
        self.end_time = None
        self.is_running = False
        self.is_error = False
        self.is_success = False
        self._final_var = {}
        self._thread = threading.Thread(target=self._execute)

    def __init__(self, func, post_func=None, *var, **kwargs):
        self._func = func
        self._post_func = post_func
        self._sig = inspect.signature(self._func)
        self._var = var
        self._kwargs = kwargs
        self.reset()

    def _execute(self):
        res = None
        try:
            for ind, i in enumerate(self._var):
                self._final_var[list(self._sig.parameters.keys())[ind]] = i  
            for k, v in self._kwargs.items():
                if k in self._sig.parameters.keys():
                    self._final_var[k] = v
            self.is_running = True
            print(f"!!!_final_var: {self._final_var}")
            res = self._func(**self._final_var)
        except Exception as e:
            self.is_running = False
            self.is_error = True
            traceback.print_exc()
        if callable(self._post_func):
            self._post_func(res, self._final_var)

    def start(self, **kwargs):
        if len(kwargs) > 0:
            self._kwargs = kwargs
        self.start_time = time.time()
        self._thread.start()

    def join(self):
        if self.is_running: 
            self._thread.join()
            self.end_time = time.time()


def command(func):
    """
    Decorator for command_set execution. Checks if the method is called in the same thread as the class instance,
    if so enqueues the command_set and waits for a reply in the reply queue. Else it concludes it must be the command
    handler thread and actually executes the method. This way methods in the child classes need to be written
    just once and decorated accordingly.
    :return: decorated method
    """

    @wraps(func)
    def wrapper(*args, **kwargs):
        device_instance = args[0]
        if threading.get_ident() == device_instance.current_thread:
            command_set = [func, args, kwargs]
            device_instance.command_queue.put(command_set)
            while True:
                if not device_instance.reply_queue.empty():
                    return device_instance.reply_queue.get()
        else:
            return func(*args, **kwargs)

    return wrapper



class UniversalDriver(object):
    def _init_logger(self):
        self.logger = logging.getLogger(f"{self.__class__.__name__}_logger")

    def __init__(self):
        self._init_logger()
    
    def execute_command_from_outer(self, command: str):
        try:
            command = json.loads(command.replace("'", '"').replace("False", "false").replace("True", "true"))  # 要求不能出现'作为字符串
        except Exception as e:
            print(f"Json解析失败: {e}")
            return False
        for k, v in command.items():
            print(f"执行{k}方法，参数为{v}")
            try:
                getattr(self, k)(**v)
            except Exception as e:
                print(f"执行{k}方法失败: {e}")
                traceback.print_exc()
                return False
        return True
    

class TransportDriver(UniversalDriver):
    COMMAND_QUEUE_ENABLE = True
    command_handler_thread: Optional[threading.Thread] = None
    __connection: Optional[serial.Serial | socket] = None


    def _init_command_queue(self):
        self.command_queue = queue.Queue()
        self.reply_queue = queue.Queue()

    def __command_handler_daemon(self):
        while True:
            try:
                if not self.command_queue.empty():
                    command_item = self.command_queue.get()
                    method = command_item[0]
                    arguments = command_item[1]
                    keywordarguments = command_item[2]
                    reply = method(*arguments, **keywordarguments)
                    self.reply_queue.put(reply)
                else:
                    self.keepalive()
            except ValueError as e:
                # workaround if something goes wrong with the serial connection
                # future me will certainly not hate past me for this...
                self.logger.critical(e)
                self.__connection.flush()
                # thread-safe purging of both queues
                while not self.command_queue.empty():
                    self.command_queue.get()
                while not self.reply_queue.empty():
                    self.reply_queue.get()

    def launch_command_handler(self):
        if self.COMMAND_QUEUE_ENABLE:
            self.command_handler_thread = threading.Thread(target=self.__command_handler_daemon, name="{0}_command_handler".format(self.device_name), daemon=True)
            self.command_handler_thread.start()

    @abstractmethod
    def open_connection(self):
        pass

    @abstractmethod
    def close_connection(self):
        pass

    @abstractmethod
    def keepalive(self):
        pass

    def __init__(self):
        super().__init__()
        if self.COMMAND_QUEUE_ENABLE:
            self.launch_command_handler()


class DriverChecker(object):
    def __init__(self, driver, interval: int | float):
        self.driver = driver
        self.interval = interval
        self._thread = threading.Thread(target=self._monitor)
        self._thread.daemon = True
        self._stop_event = threading.Event()

    def _monitor(self):
        while not self._stop_event.is_set():
            try:
                # print(self.__class__.__name__, "Started!")
                self.check()
            except Exception as e:
                print(f"Error in {self.__class__.__name__}: {str(e)}")
                traceback.print_exc()
            finally:
                time.sleep(self.interval)

    @abstractmethod
    def check(self):
        """子类必须实现此方法"""
        raise NotImplementedError

    def start_monitoring(self):
        self._thread.start()

    def stop_monitoring(self):
        self._stop_event.set()
        self._thread.join()

