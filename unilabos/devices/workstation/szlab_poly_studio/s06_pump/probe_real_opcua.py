"""探测 SZLab 真机 OPC UA 的 S06 节点。

用法::

    PYTHONPATH=. python -m unilabos.devices.workstation.szlab_poly_studio.s06_pump.probe_real_opcua

如果 UaExpert 截图显示 ``ns=2;i=257`` 一类 NodeId，但程序读取失败，
这个脚本会快速扫描几个常见 namespace 下的 i=250..280，确认当前 endpoint
里实际存在的节点和显示名。
"""

from __future__ import annotations

from collections import deque

from opcua import Client

URL = "opc.tcp://192.168.1.10:4840/"
NAMESPACE_RANGE = range(0, 8)
NODE_ID_RANGE = range(250, 281)
KEYWORDS = ("S06", "注射泵", "允许加工", "参数写入", "加工完成", "溶液添加量", "2号溶液", "R10543")


def print_uplink_comm_children(client: Client) -> bool:
    """直接列出 GlobalVars/上位机通讯 下的相关变量，避免大范围递归导致真机 OPC 超时。"""
    try:
        comm_node = client.get_node("ns=4;s=上位机通讯")
        children = comm_node.get_children()
    except Exception as exc:
        print(f"\n无法直接打开 上位机通讯 节点: {exc}")
        return False

    print("\n上位机通讯 直接子节点（S06/溶液添加量相关）:")
    matched = 0
    for child in children:
        try:
            browse_name = child.get_browse_name().Name
            display_name = child.get_display_name().Text
        except Exception:
            continue
        text = f"{browse_name}/{display_name}"
        if not any(keyword in text for keyword in KEYWORDS):
            continue
        value = "<不可读>"
        try:
            value = child.get_value()
        except Exception as exc:
            value = f"<不可读: {exc}>"
        print(
            f"  nodeid={child.nodeid}, browse={browse_name!r}, "
            f"display={display_name!r}, value={value!r}"
        )
        matched += 1
    print(f"上位机通讯 直接扫描完成: matched={matched}")
    return matched > 0


def main() -> int:
    print(f"连接: {URL}")
    client = Client(URL, timeout=5)
    client.connect()
    try:
        namespace_array = client.get_node("i=2255").get_value()
        print("NamespaceArray:")
        for idx, namespace in enumerate(namespace_array):
            print(f"  ns={idx}: {namespace}")

        if print_uplink_comm_children(client):
            return 0

        print("\n扫描 ns=0..7, i=250..280:")
        found = 0
        for ns in NAMESPACE_RANGE:
            for identifier in NODE_ID_RANGE:
                node_id = f"ns={ns};i={identifier}"
                node = client.get_node(node_id)
                try:
                    display_name = node.get_display_name().Text
                    browse_name = node.get_browse_name().Name
                    value = None
                    try:
                        value = node.get_value()
                    except Exception as exc:
                        value = f"<不可读: {exc}>"
                    print(f"  {node_id}: browse={browse_name!r}, display={display_name!r}, value={value!r}")
                    found += 1
                except Exception:
                    continue
        if not found:
            print("  没有找到这些 numeric NodeId。继续按名称浏览 DeviceSet / DeviceTopology ...")

        objects = client.get_objects_node()
        top_children = objects.get_children()
        print("\n顶层对象:")
        for child in top_children:
            print(f"  {child.get_browse_name().Name}: {child.nodeid}")

        roots = [
            child
            for child in top_children
            if child.get_browse_name().Name in {"DeviceSet", "DeviceTopology", "NetworkSet"}
        ]
        print("\n按名称搜索 S06/注射泵（最多访问 1200 个节点）:")
        queue = deque((root, root.get_browse_name().Name, 0) for root in roots)
        visited = 0
        matched = 0
        max_nodes = 1200
        max_depth = 8
        while queue and visited < max_nodes:
            node, path, depth = queue.popleft()
            visited += 1
            try:
                browse_name = node.get_browse_name().Name
            except Exception:
                browse_name = ""
            try:
                display_name = node.get_display_name().Text
            except Exception:
                display_name = ""
            text = f"{path}/{browse_name}/{display_name}"
            if any(keyword in text for keyword in KEYWORDS):
                matched += 1
                value = "<未读>"
                try:
                    value = node.get_value()
                except Exception:
                    pass
                print(
                    f"  MATCH nodeid={node.nodeid}, browse={browse_name!r}, "
                    f"display={display_name!r}, value={value!r}, path={path}"
                )
            if depth >= max_depth:
                continue
            try:
                children = node.get_children()
            except Exception:
                continue
            for child in children:
                try:
                    child_name = child.get_browse_name().Name
                except Exception:
                    child_name = str(child.nodeid)
                queue.append((child, f"{path}/{child_name}", depth + 1))

        print(f"\n浏览完成: visited={visited}, matched={matched}")
        if not matched:
            print("没有按名称找到 S06。请在 UaExpert 确认 endpoint 和节点属性。")
            return 1
        return 0
    finally:
        client.disconnect()


if __name__ == "__main__":
    raise SystemExit(main())
