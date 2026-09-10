# Equipment Arrange 参考

真值：`unilabos/resources/resource_tracker.py`（`ResourceDict`、`ResourceDictPosition`）。

---

## pose 写入规则

- **只写 `pose`**；不写顶层 `position`、不写 `config`。
- **物料**（`type` ≠ `device`）：仅 `position` / `position_3d`；禁止 size/scale/rotation/layout。
- **设备**：pose 全字段可写。
- **pos 层级**：depth 0~3 可改；depth≥4 物料不动。
- 云端 key：`position_3d`（下划线），非 `position3d`。

---

## 层级与编辑权限

```python
def node_depth(node, nodes_by_uuid):
    d, cur = 0, node
    while cur.get("parent_uuid") and cur["parent_uuid"] in nodes_by_uuid:
        d += 1
        cur = nodes_by_uuid[cur["parent_uuid"]]
    return d

def is_device(node):
    return node.get("type") == "device"

def can_edit_pos(node, nodes_by_uuid):
    return is_device(node) or node_depth(node, nodes_by_uuid) <= 3

def allowed_pose_patch(node, nodes_by_uuid, *, new_position=None, new_size=None):
    if not can_edit_pos(node, nodes_by_uuid):
        raise ValueError(f"depth≥4: {node['name']}")
    patch = {}
    if new_position is not None:
        patch["position"] = new_position
        patch["position_3d"] = new_position
    if is_device(node) and new_size is not None:
        patch["size"] = new_size
    elif new_size is not None:
        raise ValueError(f"物料禁止改 size: {node['name']}")
    return patch
```

典型树：

```
host_node (device, 0)
├── 子设备 (device, 1)     ← pos + size
│   └── deck (2)           ← 物料，仅 pos
│       └── plate (3)      ← 最深可改 pos
│           └── well (4)   ← ❌
```

---

## bbox 冲突检测

| 条件 | footprint | 说明 |
|------|-----------|------|
| 有 `model.bbox` | model.bbox + pose.position | 必做 |
| 有 model 无 bbox | pose.size | 标注 bbox_fallback |
| 无 model | pose.size | 设备须先消除 size0 |

```python
def has_model(node):
    m = node.get("model")
    return isinstance(m, dict) and bool(m)

def is_size_zero_device(node):
    if node.get("type") != "device":
        return False
    s = (node.get("pose") or {}).get("size") or {}
    return any((s.get(k, 0) or 0) <= 0 for k in ("width", "height", "depth"))

def footprint_from_bbox(model, pose):
    bb = model.get("bbox") or {}
    mn, mx = bb.get("min", {}), bb.get("max", {})
    p = pose.get("position") or {}
    ox, oy, oz = p.get("x", 0), p.get("y", 0), p.get("z", 0)
    return (ox + mn.get("x", 0), oy + mn.get("y", 0), oz + mn.get("z", 0),
            ox + mx.get("x", 0), oy + mx.get("y", 0), oz + mx.get("z", 0))

def footprint_from_pose_size(pose):
    p, s = pose.get("position") or {}, pose.get("size") or {}
    x, y, z = p.get("x", 0), p.get("y", 0), p.get("z", 0)
    w, h, d = s.get("width", 0), s.get("height", 0), s.get("depth", 0)
    return (x, y, z, x + w, y + h, z + d)

def node_footprint(node):
    pose = node.get("pose") or {}
    if has_model(node) and node["model"].get("bbox"):
        return footprint_from_bbox(node["model"], pose), "model.bbox"
    src = "bbox_fallback" if has_model(node) else "pose.size"
    return footprint_from_pose_size(pose), src

def aabb_overlap(a, b):
    return (a[0] < b[3] and a[3] > b[0] and a[1] < b[4] and a[4] > b[1]
            and a[2] < b[5] and a[5] > b[2])
```

---

## 坐标系

- `pose.position`：相对**父节点**局部坐标（mm）。
- world 坐标：沿 `parent_uuid` 链累加各级 `position`（无旋转近似）。
- 兄弟冲突：同一 `parent_uuid` 下局部 AABB。

---

## curl 示例

```bash
# 下载
curl -s -X GET "$BASE/api/v1/lab/material/download/$lab_uuid" -H "$AUTH" -o tree.json

# 设备：补立方体 + 摆位（只改 pose）
# 物料：只改 position / position_3d
curl -s -X PUT "$BASE/api/v1/lab/material" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"nodes":[{ "...完整节点...", "pose": { "position": {"x":0,"y":0,"z":0}, "position_3d": {"x":0,"y":0,"z":0}, "size": {"width":200,"height":200,"depth":200} } }]}'

# 台面废弃（无 lab_uuid）
curl -s -X POST "$BASE/api/v1/edge/material/bench/discard" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"uuids":["<uuid1>"]}'
```

错误码 #6：`100002` 节点不存在，`100003` 状态不允许。

---

## 源码路径

| 内容 | 路径 |
|------|------|
| ResourceDict / pose | `unilabos/resources/resource_tracker.py` |
| HTTP 客户端 | `unilabos/app/web/client.py` |
| 注册表 model | `unilabos/registry/` |
