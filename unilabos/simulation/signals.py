"""复用 PLC-Sim 信号合同，为直接调用和隔离协议提供统一传输及驱动网关。"""
from threading import RLock


class LocalSignals:
    """以确认时钟向本地信号库发布样本，不创建网络端点。"""
    def __init__(self, store, now):
        """绑定信号库、确认时钟及每信号递增序号。"""
        self.store, self.now = store, now
        self.sequences = {}

    def publish(self, token, name, value, **sample):
        """在信号库锁内分配序号并按原始值发布，保留调用方来源和质量。"""
        with self.store._lock:
            sequence = self.sequences.get(name, 0) + 1
            self.store.publish(token, name, value, sequence=sequence, acquired_ns=self.now(), **sample)
            self.sequences[name] = sequence

    def close(self):
        """本地发布端不拥有外部资源，关闭无需副作用。"""


class SignalGateway:
    """为原设备驱动提供串行读写、质量检查、等待及协作取消。"""
    def __init__(self, read, write):
        """注入带质量的读取函数与写入函数，不依赖设备模型对象。"""
        self.read, self.write = read, write
        self.lock = RLock()
        self.cancelled = False

    def cancel(self):
        """取消后续读写，使轮询中的原驱动能够退出。"""
        self.cancelled = True

    def _check(self):
        """已请求取消时拒绝继续协议访问。"""
        if self.cancelled:
            raise RuntimeError('protocol_action_cancelled')

    def read_variable(self, name, use_cache=False):
        """执行真实读取并拒绝坏质量；保留原驱动缓存参数兼容性。"""
        self._check()
        with self.lock:
            value = self.read(name)
            if not value.StatusCode.is_good():
                raise RuntimeError('protocol_sample_invalid: ' + name)
            return value.Value.Value

    def get_variables(self, names, use_cache=False):
        """逐项读取指定变量，不使用可能过期的网关缓存。"""
        return {name: self.read_variable(name) for name in names}

    def write_variable(self, name, value):
        """在串行锁内调用受控写入函数。"""
        self._check()
        with self.lock:
            self.write(name, value)

    def wait_equal(self, name, expected, timeout, interval=.01):
        """在墙钟超时内轮询相等条件；坏质量或取消立即传播。"""
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.read_variable(name) == expected:
                return True
            time.sleep(min(interval, .01))
        return False


class ResponseSignals:
    """为响应模型路由授权写入，阻止其改写主机命令缓存。"""
    def __init__(self, session, writer, sensor_writer, publish):
        """绑定信号会话、角色写入者及设备完成门控制的发布函数。"""
        self.session, self.writer = session, writer
        self.sensor_writer, self.publish = sensor_writer, publish

    def read(self, name):
        """返回有效工程值；无有效样本时返回 None。"""
        return self.session.local_value(name)

    def write(self, name, value):
        """按角色路由状态及传感输入，保持主机命令的唯一写入权。"""
        spec = self.session.store.signals.get(name)
        if spec is None or spec.role == 'host_command':
            return
        self.publish(self.sensor_writer if spec.role == 'sensor_input' else self.writer, name, value)


class SignalSession:
    """拥有同一信号库及可选 OPC-UA 端点；电气实现仍由 PLC-Sim 提供。"""
    def __init__(self, definitions, semantics, *, attachment, endpoint, now, session_id, epoch=0, command_writer='pc'):
        """编译信号声明并授予声明中的写入者；协议端点延迟至 start 启动。"""
        from plc_sim.signal_runtime import SignalStore, compile_signals
        if attachment not in ('direct', 'protocol', 'internal_protocol'):
            raise ValueError('attachment_unsupported')
        self.attachment, self.now, self.command_writer = attachment, now, command_writer
        self.store = SignalStore(compile_signals(definitions, semantics), session_id=session_id, epoch=epoch)
        self.tokens = {writer:self.store.grant(writer) for writer in sorted({s['writer'] for s in semantics.values()})}
        self.client = None
        self.url = endpoint
        if attachment == 'direct':
            self.endpoint = LocalSignals(self.store, now)
        else:
            from plc_sim.owned_opcua import OwnedOpcuaEndpoint
            self.endpoint = OwnedOpcuaEndpoint(self.store, endpoint=endpoint, now_ns=now)
            self.credentials = self.endpoint.credential(command_writer)

    def start(self):
        """在初始状态写入后启动隔离端点并连接主机客户端；失败时关闭所拥有资源。"""
        if self.attachment == 'direct':
            return
        from opcua import Client
        self.endpoint.start()
        self.client = Client(self.url)
        self.client.set_user(self.credentials[0])
        self.client.set_password(self.credentials[1])
        try:
            self.client.connect()
        except BaseException:
            self.close()
            raise

    def publish(self, writer, name, value, **sample):
        """凭写入者令牌发布原始值及来源元数据。"""
        self.endpoint.publish(self.tokens[writer], name, value, **sample)

    def local_value(self, name):
        """读取当前确认时刻的有效工程值，不触发协议访问。"""
        row = self.store.snapshot(self.now())['signals'][name]
        return row['value'] if row['quality'] == 'good' else None

    def write(self, name, value):
        """直接入口发布本地命令；协议入口执行原类型的实际 OPC-UA 写入。"""
        if self.attachment == 'direct':
            self.publish(self.command_writer, name, value)
            return
        from opcua import ua
        from plc_sim.common import VTYPE_MAP
        spec = self.store.signals[name]
        self.client.get_node(spec.node_id).set_value(ua.Variant(value, VTYPE_MAP[spec.data_type]))

    def read(self, name):
        """返回原始值及质量 DataValue，兼容现有设备驱动的 OPC-UA 合同。"""
        from opcua import ua
        from plc_sim.common import VTYPE_MAP
        if self.attachment == 'direct':
            row = self.store.snapshot(self.now())['signals'][name]
            value = ua.DataValue(ua.Variant(row['raw'], VTYPE_MAP[self.store.signals[name].data_type]))
            value.StatusCode = ua.StatusCode(ua.StatusCodes.Good if row['quality'] == 'good' else ua.StatusCodes.BadNoData)
            return value
        item = ua.ReadValueId()
        item.NodeId = ua.NodeId.from_string(self.store.signals[name].node_id)
        item.AttributeId = ua.AttributeIds.Value
        params = ua.ReadParameters()
        params.NodesToRead = [item]
        return self.client.uaclient.read(params)[0]

    def close(self):
        """关闭本会话客户端和端点，即使客户端断开失败也尝试关闭端点。"""
        try:
            if self.client is not None:
                self.client.disconnect()
        finally:
            self.endpoint.close()
