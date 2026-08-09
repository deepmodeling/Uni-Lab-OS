## 设备接入

当被要求添加设备驱动时，参考 `docs/ai_guides/add_device.md`。
该指南包含完整的模板和已有设备接口参考。

## 关键规则

- 动作方法的参数名是接口契约，不可重命名
- `status` 字符串必须与同类已有设备一致
- `self.data` 必须在 `__init__` 中预填充所有属性字段
- 异步方法中使用 `await self._ros_node.sleep()`，禁止 `time.sleep()`
