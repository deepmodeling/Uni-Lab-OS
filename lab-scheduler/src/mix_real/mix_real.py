# -*- coding: utf-8 -*-
import random
import heapq
import os
from abc import ABC, abstractmethod
from collections import defaultdict
from typing import DefaultDict, Dict, List, Type, Optional
from pydantic import BaseModel, Field

import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle


class Node(BaseModel):
    id: str = Field(...)
    duration: int = Field(..., gt=0)
    device: str = Field(...)
    deadline: Optional[int] = None
    completed: bool = False
    ready: bool = False
    start: Optional[int] = None
    end: Optional[int] = None

    def reset(self):
        self.deadline = None
        self.completed = False
        self.ready = False
        self.start = None
        self.end = None


class Edge(BaseModel):
    front: Optional[str] = None
    back: Optional[str] = None


class Dag(BaseModel):
    nodes: List[Node] = []
    edges: List[Edge] = []


def generate_random_dag(num_nodes=5, device_types=None):
    if device_types is None:
        device_types = {'A': 3, 'B': 8, 'C': 10, 'D': 4, 'E': 6, 'F': 2}
    nodes = []
    edges = []
    device_list = list(device_types.keys())

    for i in range(num_nodes):
        node = Node(
            id=f'n{i}',
            duration=random.randint(50, 1000),
            device=random.choice(device_list))
        nodes.append(node)

    order = list(range(num_nodes))
    random.shuffle(order)
    for i in range(num_nodes):
        for j in range(i+1, num_nodes):
            if random.random() < 0.3:
                edges.append(Edge(front=nodes[order[i]].id, back=nodes[order[j]].id))

    return Dag(nodes=nodes, edges=edges)


def load_dag_from_json(dag) -> Dag:
    return dag


class SchedulerBase(ABC):
    class BatchPriority(BaseModel):
        priority: int = 0
        submit_time: int = 0

    class BatchInfo(BaseModel):
        nodes: Dict[str, Node] = {}
        parents: Dict[str, List[str]] = {}
        children: Dict[str, List[str]] = {}
        submit_time: int = 0

    def __init__(self, initial_devices):
        self.devices = initial_devices.copy()
        self.event_queue = []
        self.current_time = 0
        self.completed: List[Dict] = []
        # 新增优先级字典
        self.batch_priorities: Dict[int, 'SchedulerBase.BatchPriority'] = {}
        self.batches: List['SchedulerBase.BatchInfo'] = []

    def add_batch(self, dag: Dag, submit_time=0, priority=0):  # 添加优先级参数
        priority_info = SchedulerBase.BatchPriority(
            priority=priority,
            submit_time=self.current_time  # 记录批次提交时间
        )
        dag = load_dag_from_json(dag)
        edges = dag.edges

        self.batch_priorities[len(self.batches)] = priority_info

        parents = defaultdict(list)
        children = defaultdict(list)
        for edge in edges:
            parents[edge.back].append(edge.front)
            children[edge.front].append(edge.back)

        batch = SchedulerBase.BatchInfo(
            nodes={node.id: node for node in dag.nodes},
            parents=parents,
            children=children,
            submit_time=submit_time,
        )
        self.batches.append(batch)
        # 存储批次优先级
        self.batch_priorities[len(self.batches)-1].priority = priority  # 记录优先级
        self._init_ready_nodes(len(self.batches)-1)

    def _init_ready_nodes(self, batch_idx):
        batch: SchedulerBase.BatchInfo = self.batches[batch_idx]
        for node_id, node in batch.nodes.items():
            if not node.ready and not node.completed:
                if all(batch.nodes[p].completed for p in batch.parents.get(node_id, [])):
                    node.ready = True

    def _check_device(self, dev):
        return self.devices[dev] >= 1

    def _allocate(self, dev):
        self.devices[dev] -= 1

    def _release(self, dev):
        self.devices[dev] += 1

    @abstractmethod
    def _schedule_ready_tasks(self):
        pass

    def schedule(self):
        # 初始调度触发
        self._schedule_ready_tasks()

        while True:
            if self.event_queue:
                time, _, (batch_idx, node_id) = heapq.heappop(self.event_queue)
                self.current_time = time

                # 释放设备资源
                node: Node = self.batches[batch_idx].nodes[node_id]
                self._release(node.device)

                # 更新子节点状态
                for child_id in self.batches[batch_idx].children.get(node_id, []):
                    child: Node = self.batches[batch_idx].nodes[child_id]
                    if not child.completed and all(
                        self.batches[batch_idx].nodes[p].completed
                        for p in self.batches[batch_idx].parents.get(child_id, [])
                    ):
                        child.ready = True
                        self._schedule_ready_tasks()  # 新增状态更新后立即调度

                self._schedule_ready_tasks()
            else:
                # 查找下一个待提交批次
                future_batches = [b.submit_time for b in self.batches
                                  if b.submit_time > self.current_time]
                if not future_batches:
                    break  # 所有批次处理完成

                # 推进时间到最近批次提交时间
                next_submit = min(future_batches)
                self.current_time = next_submit
                self._schedule_ready_tasks()  # 触发新批次调度

    def print_statistics(self):
        print("\n=== Scheduling Statistics ===")
        for record in sorted(self.completed, key=lambda x: (x['batch'], x['start'])):
            print(f"Batch {record['batch']} Node {record['node']}: "
                  f"Start={record['start']}ms, End={record['end']}ms, "
                  f"Duration={record['end']-record['start']}ms, "
                  f"Device={record['device']}")


class GreedyScheduler(SchedulerBase):
    def _schedule_ready_tasks(self):
        all_ready = []
        for batch_idx, batch in enumerate(self.batches):
            batch_priority = self.batch_priorities[batch_idx].priority  # 获取批次优先级
            if self.current_time >= batch.submit_time:
                self._init_ready_nodes(batch_idx)
                for node_id, node in batch.nodes.items():
                    if node.ready and not node.completed:
                        # 添加优先级排序
                        all_ready.append((
                            -batch_priority,  # 负号使高优先级在前
                            node.duration,
                            batch_idx,
                            node_id,
                            node
                        ))

        all_ready.sort()  # 元组排序：优先级>持续时间

        for _, duration, batch_idx, node_id, node in all_ready:
            dev = node.device
            if self._check_device(dev):
                self._allocate(dev)
                node.completed = True
                node.ready = False
                node.device = dev
                start = self.current_time
                end = start + node.duration
                node.start = start
                node.end = end
                heapq.heappush(self.event_queue, (end, 'end', (batch_idx, node_id)))
                self.completed.append({
                    'batch': batch_idx,
                    'node': node_id,
                    'start': start,
                    'end': end,
                    'device': dev
                })


class CriticalPathScheduler(SchedulerBase):
    def __init__(self, initial_devices):
        super().__init__(initial_devices)
        self.critical_paths = []  # 存储每个批次的关键路径数据

    def add_batch(self, dag, submit_time=0, priority=0):
        # 调用基类方法添加批次
        super().add_batch(dag, submit_time, priority)

        # 计算关键路径并存储
        nodes = self.batches[-1].nodes
        edges = [(e.front, e.back) for e in dag.edges]
        self.critical_paths.append(self._compute_critical_paths(nodes, edges))

    def _compute_critical_paths(self, nodes: Dict[str, Node], edges):
        """计算每个节点的最长路径(关键路径长度)"""
        graph = defaultdict(list)
        in_degree = defaultdict(int)
        dist = {}

        # 构建图结构
        for f, t in edges:
            graph[f].append(t)
            in_degree[t] += 1

        # 初始化距离
        for node_id in nodes:
            dist[node_id] = nodes[node_id].duration

        # 拓扑排序计算最长路径
        queue = [node_id for node_id in nodes if in_degree[node_id] == 0]

        while queue:
            u = queue.pop(0)
            for v in graph[u]:
                if dist[v] < dist[u] + nodes[v].duration:
                    dist[v] = dist[u] + nodes[v].duration
                in_degree[v] -= 1
                if in_degree[v] == 0:
                    queue.append(v)

        return dist

    def _schedule_ready_tasks(self):
        # 遍历所有批次
        for batch_idx, batch in enumerate(self.batches):
            ready_nodes = []
            batch_priority = self.batch_priorities[batch_idx].priority
            if self.current_time >= batch.submit_time:
                self._init_ready_nodes(batch_idx)
                # 收集就绪节点
                for node_id, node in batch.nodes.items():
                    if node.ready and not node.completed:
                        # 使用优先级和关键路径长度排序
                        ready_nodes.append((
                            -batch_priority,  # 负号使高优先级在前
                            -self.critical_paths[batch_idx][node_id],  # 关键路径越长越优先
                            node_id,
                            node
                        ))

            # 排序：优先级 > 关键路径长度
            ready_nodes.sort()

            # 调度节点
            for _, _, node_id, node in ready_nodes:
                dev = node.device
                if self._check_device(dev):
                    # 分配资源
                    self._allocate(dev)

                    # 更新节点状态
                    node.completed = True
                    node.ready = False
                    node.device = dev

                    # 记录时间
                    start_time = self.current_time
                    end_time = start_time + node.duration
                    node.start = start_time
                    node.end = end_time

                    # 添加完成事件
                    heapq.heappush(self.event_queue,
                                   (end_time, 'complete', (batch_idx, node_id)))

                    # 记录完成信息
                    self.completed.append({
                        'batch': batch_idx,
                        'node': node_id,
                        'start': start_time,
                        'end': end_time,
                        'device': dev
                    })


class DynamicPriorityScheduler(SchedulerBase):
    def _calculate_priority(self, node_id: str, batch: SchedulerBase.BatchInfo):
        return len(batch.children.get(node_id, []))

    def _schedule_ready_tasks(self):
        all_ready = []
        for batch_idx, batch in enumerate(self.batches):
            batch_priority = self.batch_priorities[batch_idx].priority  # 获取优先级
            if self.current_time >= batch.submit_time:
                self._init_ready_nodes(batch_idx)
                for node_id, node in batch.nodes.items():
                    if node.ready and not node.completed:
                        node_priority = self._calculate_priority(node_id, batch)
                        # 综合批次和节点优先级
                        total_priority = batch_priority * 1000 + node_priority  # 批次优先
                        all_ready.append((-total_priority, batch_idx, node_id, node))

        heapq.heapify(all_ready)

        while all_ready:
            _, batch_idx, node_id, node = heapq.heappop(all_ready)
            dev = node.device
            if self._check_device(dev):
                self._allocate(dev)
                node.completed = True
                node.ready = False
                node.device = dev
                start = self.current_time
                end = start + node.duration
                node.start = start
                node.end = end
                heapq.heappush(self.event_queue, (end, 'end', (batch_idx, node_id)))
                self.completed.append({
                    'batch': batch_idx,
                    'node': node_id,
                    'start': start,
                    'end': end,
                    'device': dev,
                })


class MultiObjectiveScheduler(SchedulerBase):
    def _calculate_score(self, node: Node, current_devices):
        total_used = 1
        total_available = sum(current_devices.values())
        utilization = total_used / total_available if total_available > 0 else 0
        duration_score = 1 / node.duration
        return 0.6 * utilization + 0.4 * duration_score

    def _schedule_ready_tasks(self):
        candidates = []
        for batch_idx, batch in enumerate(self.batches):
            batch_priority = self.batch_priorities[batch_idx].priority  # 获取优先级
            if self.current_time >= batch.submit_time:
                self._init_ready_nodes(batch_idx)
                for node_id, node in batch.nodes.items():
                    if node.ready and not node.completed:
                        candidates.append((batch_idx, node_id, node, batch_priority))

        scored = []
        for batch_idx, node_id, node, priority in candidates:
            dev = node.device
            if self._check_device(dev):
                score = self._calculate_score(node, self.devices)
                # 将优先级融入评分
                adjusted_score = score * (2 ** priority)  # 优先级指数放大
                scored.append((-adjusted_score, batch_idx, node_id, node))

        heapq.heapify(scored)

        while scored:
            _, batch_idx, node_id, node = heapq.heappop(scored)
            dev = node.device
            if self._check_device(dev):
                self._allocate(dev)
                node.completed = True
                node.ready = False
                node.device = dev
                start = self.current_time
                end = start + node.duration
                node.start = start
                node.end = end
                heapq.heappush(self.event_queue, (end, 'end', (batch_idx, node_id)))
                self.completed.append({
                    'batch': batch_idx,
                    'node': node_id,
                    'start': start,
                    'end': end,
                    'device': dev
                })


class RealtimeScheduler(SchedulerBase):
    def __init__(self, initial_devices):
        super().__init__(initial_devices)
        self.submission_times = []

    def add_batch(self, dag: Dag, submit_time=0, priority=0):  # 重写以记录提交时间
        super().add_batch(dag, submit_time, priority)
        self.submission_times.append(self.current_time)

    def _schedule_ready_tasks(self):
        candidates = []
        for batch_idx, batch in enumerate(self.batches):
            batch_priority = self.batch_priorities[batch_idx].priority
            if self.current_time >= batch.submit_time:
                self._init_ready_nodes(batch_idx)
                for node_id, node in batch.nodes.items():
                    if node.ready and not node.completed:
                        # 优先级影响调度顺序
                        candidates.append((
                            -batch_priority,  # 优先级第一
                            self.submission_times[batch_idx],  # 提交时间第二
                            batch_idx,
                            node_id,
                            node
                        ))

        candidates.sort()

        for priority, submit_time, batch_idx, node_id, node in candidates:
            dev = node.device
            if self._check_device(dev):
                self._allocate(dev)
                node.completed = True
                node.ready = False
                node.device = dev
                start = self.current_time
                end = start + node.duration
                node.start = start
                node.end = end
                heapq.heappush(self.event_queue, (end, 'end', (batch_idx, node_id)))
                self.completed.append({
                    'batch': batch_idx,
                    'node': node_id,
                    'start': start,
                    'end': end,
                    'device': dev
                })


class HybridCriticalityScheduler(SchedulerBase):
    def __init__(self, initial_devices):
        super().__init__(initial_devices)
        # 新增关键度阈值参数
        self.critical_threshold_ratio = 2.0  # 剩余时间/任务时长阈值比

    def _calculate_remaining_time(self, node: Node, batch_idx):
        """计算剩余可用时间（考虑批次提交时间）"""
        # 获取批次提交时间（假设存储于batch_priorities字典）
        submit_time = self.batch_priorities[batch_idx].submit_time

        # 获取当前节点截止时间（如果未设置则动态计算）
        if not node.deadline:
            default_deadline = submit_time + node.duration * 5  # 默认5倍时长
            return default_deadline - self.current_time
        return node.deadline - self.current_time

    def _classify_criticality(self, batch_idx, node_id, node):
        """任务关键度分类逻辑"""
        # 计算剩余时间
        remaining_time = self._calculate_remaining_time(node, batch_idx)

        # 计算紧急程度
        time_ratio = remaining_time / node.duration

        # 分类规则
        if time_ratio < self.critical_threshold_ratio:
            return 'critical'
        return 'non-critical'

    def _schedule_ready_tasks(self):
        """混合关键度调度核心逻辑"""
        # 第一阶段：收集所有就绪任务
        all_ready = []
        for batch_idx, batch in enumerate(self.batches):
            batch_info = {
                'priority': self.batch_priorities[batch_idx].priority,
                'submit_time': self.batch_priorities[batch_idx].submit_time
            }
            if self.current_time >= batch.submit_time:
                self._init_ready_nodes(batch_idx)
                for node_id, node in batch.nodes.items():
                    if node.ready and not node.completed:
                        # 分类关键度
                        criticality = self._classify_criticality(batch_idx, node_id, node)
                        all_ready.append({
                            'batch_idx': batch_idx,
                            'node_id': node_id,
                            'node': node,
                            'criticality': criticality,
                            'duration': node.duration,
                            'deadline': node.deadline if node.deadline else batch_info['submit_time'] + node.duration * 5,
                            'priority': batch_info['priority']
                        })

        # 第二阶段：任务分类排序
        critical_tasks = []
        non_critical_tasks = []

        for task in all_ready:
            if task['criticality'] == 'critical':
                critical_tasks.append(task)
            else:
                non_critical_tasks.append(task)

        # 关键任务排序策略：最早截止时间优先
        critical_tasks.sort(key=lambda x: (x['deadline'], -x['priority']))

        # 非关键任务排序策略：高优先级+短任务优先
        non_critical_tasks.sort(key=lambda x: (
            -x['priority'],
            x['duration']
        ))

        # 第三阶段：分阶段调度
        def process_tasks(task_list):
            """通用任务处理流程"""
            for task in task_list:
                dev = task['node'].device
                if self._check_device(dev):
                    self._allocate(dev)
                    task['node'].completed = True
                    task['node'].ready = False
                    task['node'].device = dev
                    start = self.current_time
                    end = start + task['node'].duration
                    task['node'].start = start
                    task['node'].end = end
                    heapq.heappush(self.event_queue,
                                   (end, 'end', (task['batch_idx'], task['node_id'])))
                    self.completed.append({
                        'batch': task['batch_idx'],
                        'node': task['node_id'],
                        'start': start,
                        'end': end,
                        'device': dev
                    })

        # 优先调度关键任务
        process_tasks(critical_tasks)

        # 剩余资源调度非关键任务
        process_tasks(non_critical_tasks)


class SchedulerVisualizer:
    @staticmethod
    def plot_gantt(scheduler, filename='gantt.png'):
        # 配置中文字体
        plt.rcParams['font.sans-serif'] = ['Microsoft YaHei', 'SimHei', 'PingFang HK']
        plt.rcParams['axes.unicode_minus'] = False

        if not scheduler.completed:
            print("无任务数据，跳过生成图片")
            return

        max_end = max(r['end'] for r in scheduler.completed)
        devices = ['A', 'B', 'C', 'D', 'E', 'F']
        device_ybase = {dev: i*2 for i, dev in enumerate(devices)}

        fig, ax = plt.subplots(figsize=(15, 8), dpi=150)

        # 绘制泳道背景
        for dev, y in device_ybase.items():
            ax.add_patch(Rectangle((0, y-0.9), max_end+100, 1.8,
                                   facecolor='#F0F0F0', alpha=0.5))

        # 绘制任务块
        color_map = plt.get_cmap('tab20')
        legend_handles = []
        for idx, record in enumerate(scheduler.completed):
            main_dev = record['device']
            y = device_ybase[main_dev] - 0.7
            rect = Rectangle((record['start'], y),
                             record['end'] - record['start'], 1.4,
                             facecolor=color_map(idx % 20),
                             edgecolor='#333333', linewidth=0.8,
                             label=f"批次{record['batch']}-节点{record['node']}")
            ax.add_patch(rect)
            legend_handles.append(rect)

        # 标注最终结束时间
        ax.axvline(max_end, color='#2E7D32', linestyle='--', linewidth=2)
        ax.text(max_end, len(devices)*2-0.5, f'最终结束时间: {max_end}ms',
                color='#2E7D32', fontsize=12, ha='right', va='top', rotation=45,
                backgroundcolor='#E8F5E9', fontweight='bold')

        # 配置坐标轴
        ax.set_yticks([v for v in device_ybase.values()])
        ax.set_yticklabels(devices, fontsize=10)
        ax.set_xlabel('时间 (毫秒)', fontsize=12)
        ax.set_ylabel('设备类型', fontsize=12)
        ax.set_title(f'{os.path.splitext(filename)[0]} 调度甘特图', fontsize=14, pad=20)
        ax.set_xlim(0, max_end*1.15)
        ax.set_ylim(-1, len(devices)*2)

        # 添加图例
        # ax.legend(handles=legend_handles[:10],
        ax.legend(handles=legend_handles,
                  bbox_to_anchor=(1.05, 1),
                  loc='upper left',
                  title="任务说明",
                  prop={'size': 8})

        plt.savefig(filename, bbox_inches='tight')
        print(f"泳道图已保存至: {filename}")


if __name__ == "__main__":
    # devices = {'A':3, 'B':8, 'C':10, 'D':4, 'E':6, 'F':2}
    dag_count = 2
    num_nodes = random.randint(6, 9)
    devices = {'A': 1, 'B': 1, 'C': 1, 'D': 1, 'E': 1, 'F': 1}
    dags: List[Dag] = []
    for _ in range(dag_count):
        dags.append(generate_random_dag(num_nodes=num_nodes, device_types=devices))

    schedulers: List[Type[SchedulerBase]] = [GreedyScheduler, CriticalPathScheduler,
                                             DynamicPriorityScheduler, MultiObjectiveScheduler, RealtimeScheduler, HybridCriticalityScheduler]

    for scheduler in schedulers:
        s = scheduler(devices.copy())
        delta_time = 0
        for dag in dags:
            for node in dag.nodes:
                node.reset()

            print(f'模拟时间差==============delta_time: {delta_time}')
            s.add_batch(dag, submit_time=delta_time)
            delta_time += random.randint(1000, 9000)
        s.schedule()
        s.print_statistics()
        SchedulerVisualizer.plot_gantt(s, f'{s.__class__.__name__}_1.png')
