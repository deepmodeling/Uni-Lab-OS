# -*- coding: utf-8 -*-
import asyncio
import random
from datetime import datetime
import heapq
from abc import ABC, abstractmethod
from collections import defaultdict
from contextlib import suppress
from operator import itemgetter
from pydantic import BaseModel
from typing import Any, Dict, Optional, List

# 生成随机DAG（设备数量为字典格式，如{'A':3, 'B':8}）


def generate_random_dag(num_nodes, device_counts):
    """生成随机有向无环图(DAG)
    Args:
        num_nodes: 节点数量
        device_counts: 设备类型及数量字典
    Returns:
        包含nodes和edges结构的字典
    """
    nodes = []
    edges = []
    device_types = list(device_counts.keys())

    # 生成节点：随机分配设备类型和持续时间
    for i in range(num_nodes):
        device = random.choice(device_types)  # 随机选择设备类型
        duration = random.randint(1, 60)      # 随机生成1-60分钟的持续时间
        nodes.append({'id': i, 'device_type': device, 'duration': duration})

    # 生成无环边：确保不形成循环
    for i in range(num_nodes):
        # 每个节点随机连接到后面的节点（最多3条边）
        num_edges = random.randint(0, min(3, num_nodes - i - 1))
        targets = random.sample(range(i+1, num_nodes), num_edges)
        edges.extend([(i, j) for j in targets])

    return {'nodes': nodes, 'edges': edges}


class Task(BaseModel):
    id: str
    device: str
    duration: int
    submit_time: int
    pre_count: int = 0  # 前置任务计数
    start: float = 0  # 任务开始时间
    end: float = 0  # 任务结束时间


# 异步调度器基类
class AsyncSchedulerBase(ABC):
    """异步调度器基类，定义通用调度逻辑"""

    def __init__(self, device_counts):
        # 初始化设备配置
        self.device_counts = device_counts.copy()                   # 设备类型及数量字典
        self.batche: int = 0                                        # 存储批次提交信息
        self.tasks: Dict[str, Task] = {}                            # 所有任务字典{task_id: task_info}
        self.dependencies: Dict[str, List] = defaultdict(list)      # 任务依赖关系{task_id: [前置任务]}
        self.reverse_deps: Dict[str, List] = defaultdict(list)      # 反向依赖关系{task_id: [后续任务]}
        self.ready_tasks: List = []                                 # 就绪任务队列（前置条件已满足）
        self.completed = {}                                         # 已完成任务字典
        self.devices: Dict[str, List] = {dev: [0]*cnt for dev, cnt in device_counts.items()}  # 设备可用时间
        self.events: List = []                                      # 事件堆（时间，事件类型，参数）
        self.current_time: int = 0                                  # 当前模拟时间
        self.running: bool = False                                  # 调度器运行状态
        self.lock = asyncio.Lock()                                  # 异步操作锁
        self.start_time: int = datetime.now().second                # 当前启动时间
        self.finished_event = asyncio.Event()                       # 完成事件信号

    async def add_batch(self, dag):
        """添加新批次任务到调度器
        Args:
            dag: DAG结构字典
            submit_time: 批次提交时间（必须>=当前时间）
        """
        async with self.lock:  # 保证并发安全
            # 创建批次记录
            self.batche += 1

            # 添加任务节点
            for node in dag['nodes']:
                task_id = f"B{self.batche}-N{node['id']}"  # 生成唯一任务ID
                self.tasks[task_id] = Task(
                    id=task_id,
                    device=node['device_type'],
                    duration=node['duration'],
                    submit_time=self.batche,
                )

            for src, dst in dag['edges']:
                from_id = f"B{self.batche}-N{src}"
                to_id = f"B{self.batche}-N{dst}"
                self.dependencies[to_id].append(from_id)  # 记录任务依赖
                self.reverse_deps[from_id].append(to_id)  # 记录反向依赖

            # 初始化前置任务计数器
            for task_id in self.tasks:
                if task_id.startswith(f"B{self.batche}-"):
                    self.tasks[task_id].pre_count = len(self.dependencies[task_id])

            # 将批次提交加入事件堆
            heapq.heappush(self.events, (self.batche, 'submit_batch', self.batche))
            self.finished_event.clear()  # 重置完成标记

    async def _process_events(self):
        """主事件处理循环"""
        while self.running:
            async with self.lock:
                # 检查完成条件：所有任务已完成且无待处理事件
                delta_second = datetime.now().second - self.start_time
                if delta_second > 500:
                    # if not self.events and all(t.get('end') is not None for t in self.tasks.values()):
                    self.finished_event.set()
                    break

                # 处理所有到期的当前时间事件
                while self.events and self.events[0][0] <= self.current_time:
                    time, event_type, args = heapq.heappop(self.events)
                    self.current_time = time  # 推进当前时间

                    if event_type == 'submit_batch':
                        batch_id = args
                        self._activate_batch(batch_id)  # 激活批次任务
                    elif event_type == 'task_finish':
                        pass  # 设备释放已通过更新可用时间处理

                # 尝试调度就绪任务
                scheduled = True
                while scheduled and self.ready_tasks:
                    task_id = self._pick_task()  # 调用具体策略选择任务
                    if task_id is None:
                        break
                    scheduled = self._schedule_task(task_id)

                # 计算到下一个事件的时间间隔
                if self.events:
                    next_time = self.events[0][0]
                    delay = max(0, next_time - self.current_time)
                else:
                    delay = 0  # 立即检查新事件

            # 异步等待时间推进（非阻塞）
            await asyncio.sleep(delay)
            async with self.lock:
                self.current_time += delay

    def _activate_batch(self, batch_id):
        """激活批次的就绪任务（无前置任务的任务）"""
        for task_id in self.tasks:
            if self.tasks[task_id].pre_count == 0:
                self.ready_tasks.append(task_id)

    def _schedule_task(self, task_id):
        """尝试调度指定任务到设备"""
        task = self.tasks[task_id]
        dev_type = task.device

        # 查找最早可用的设备
        earliest_avail = float('inf')
        dev_idx = -1
        for idx, avail_time in enumerate(self.devices[dev_type]):
            if avail_time < earliest_avail:  # 正确条件：仅比较可用时间
                earliest_avail = avail_time
                dev_idx = idx

        if dev_idx == -1:  # 无可用设备
            print('')
            return False

        # 计算任务时间并更新设备状态
        start_time = max(earliest_avail, self.current_time)
        end_time = start_time + task.duration
        self.devices[dev_type][dev_idx] = end_time
        task.start = start_time
        task.end = end_time
        # 模拟任务完成
        print(f'==============completed task: {task_id}')
        self.completed[task_id] = task

        # 添加任务完成事件
        heapq.heappush(self.events, (end_time, 'task_finish', task_id))

        # 更新后续任务的前置计数
        for succ in self.reverse_deps[task_id]:
            self.tasks[succ].pre_count -= 1
            if self.tasks[succ].pre_count == 0:
                self.ready_tasks.append(succ)

        return True

    @abstractmethod
    def _pick_task(self) -> Optional[str]:
        """抽象方法：具体调度策略选择任务"""
        pass

    async def run(self):
        """启动调度器"""
        self.running = True
        try:
            await self._process_events()
        finally:
            self.running = False

    async def wait_until_finished(self):
        """等待所有任务完成"""
        await self.finished_event.wait()

    async def print_statistics(self):
        """打印统计信息"""
        async with self.lock:
            if not self.completed:
                print("No tasks completed.")
                return

            # 计算总体耗时
            total_time = max(task.end for task in self.completed.values())
            print(f"Total Time: {total_time} minutes")

            # 按批次统计
            batches: Dict[str, List[Task]] = defaultdict(list)
            for task in self.completed.values():
                batch_id = task.id.split('-')[0][1:]
                batches[batch_id].append(task)

            # 打印各批次信息
            for bid, tasks in batches.items():
                start = min(t.start for t in tasks)
                end = max(t.end for t in tasks)
                print(f"Batch {bid}: "
                      f"Start={start}, "
                      f"End={end}, "
                      f"Duration={end - start} minutes")

# ========== 具体调度策略实现 ==========


class AsyncGreedyScheduler(AsyncSchedulerBase):
    """贪心调度：优先选择耗时最短的任务"""

    def _pick_task(self):
        if not self.ready_tasks:
            return None
        # 按任务持续时间排序，选择最短的
        shortest = min(self.ready_tasks, key=lambda x: self.tasks[x].duration)
        self.ready_tasks.remove(shortest)
        return shortest


class AsyncCriticalPathScheduler(AsyncSchedulerBase):
    """关键路径调度：优先调度关键路径上的任务"""

    def __init__(self, device_counts):
        super().__init__(device_counts)
        self.criticality = {}  # 存储任务的关键路径长度

    def _compute_criticality(self):
        """计算所有任务的关键路径长度（最长路径）"""
        # 初始化关键路径长度为任务自身持续时间
        self.criticality = {tid: task.duration for tid, task in self.tasks.items()}

        # 逆拓扑排序处理任务
        sorted_tasks = sorted(self.tasks.keys(),
                              key=lambda x: -len(self.reverse_deps[x]))

        for task_id in sorted_tasks:
            for succ in self.reverse_deps[task_id]:
                # 如果后继任务的关键路径+当前任务持续时间更长，则更新
                if self.criticality[task_id] < self.criticality[succ] + self.tasks[task_id].duration:
                    self.criticality[task_id] = self.criticality[succ] + self.tasks[task_id].duration

    def _pick_task(self):
        if not self.ready_tasks:
            return None
        # 计算最新关键路径
        self._compute_criticality()
        # 选择关键路径最长的任务
        most_critical = max(self.ready_tasks, key=lambda x: self.criticality[x])
        self.ready_tasks.remove(most_critical)
        return most_critical


class AsyncDynamicPriorityScheduler(AsyncSchedulerBase):
    """动态优先级调度：后续任务越多优先级越高"""

    def _pick_task(self):
        if not self.ready_tasks:
            return None
        # 计算每个任务的后续任务数
        priorities = {tid: len(self.reverse_deps[tid]) for tid in self.ready_tasks}
        # 选择后续任务最多的
        chosen = max(priorities, key=itemgetter(1))
        self.ready_tasks.remove(chosen)
        return chosen


class AsyncMultiObjectiveScheduler(AsyncSchedulerBase):
    """多目标调度：优先选择设备利用率低的类型"""

    def _pick_task(self):
        if not self.ready_tasks:
            return None

        # 统计各设备类型当前需求数
        dev_demand = defaultdict(int)
        for tid in self.ready_tasks:
            dev_type = self.tasks[tid].device
            dev_demand[dev_type] += 1

        # 选择需求最少的设备类型的任务
        min_demand = float('inf')
        selected_task = ""
        for tid in self.ready_tasks:
            dev = self.tasks[tid].device
            if dev_demand[dev] < min_demand:
                min_demand = dev_demand[dev]
                selected_task = tid
            elif dev_demand[dev] == min_demand:
                # 相同需求时选择耗时短的
                if self.tasks[tid].duration < self.tasks[selected_task].duration:
                    selected_task = tid

        if selected_task:
            self.ready_tasks.remove(selected_task)
        return selected_task


class AsyncRealTimeScheduler(AsyncSchedulerBase):
    """实时调度：先到先服务（FIFO）"""

    def _pick_task(self):
        if not self.ready_tasks:
            return None
        # 直接取队列中第一个任务
        return self.ready_tasks.pop(0)

# ========== 测试用例 ==========


async def test_scheduler(SchedulerClass, name):
    """测试指定调度器"""
    print(f"\n=== Testing {name} ===")
    device_counts = {'A': 3, 'B': 8, 'C': 10, 'D': 4, 'E': 6, 'F': 2}

    # 初始化调度器
    scheduler = SchedulerClass(device_counts)

    # 创建测试任务
    dag1 = generate_random_dag(10, device_counts)
    dag2 = generate_random_dag(5, device_counts)
    print(f'dag1=======\n{dag1}')
    print(f'dag2=======\n{dag2}')

    # 启动调度器
    scheduler_task = asyncio.create_task(scheduler.run())

    # 提交批次
    await scheduler.add_batch(dag1)          # 立即提交第一批次
    await asyncio.sleep(5)
    await scheduler.add_batch(dag2)        # 在100分钟时提交第二批次

    # 等待完成
    await scheduler.wait_until_finished()

    # 打印统计
    await scheduler.print_statistics()

    # 清理
    with suppress(asyncio.CancelledError):
        scheduler_task.cancel()
        await scheduler_task


async def main():
    await test_scheduler(AsyncGreedyScheduler, "Greedy Scheduler")
    print('=========1')
    await test_scheduler(AsyncCriticalPathScheduler, "Critical Path Scheduler")
    print('=========2')
    await test_scheduler(AsyncDynamicPriorityScheduler, "Dynamic Priority Scheduler")
    print('=========3')
    await test_scheduler(AsyncMultiObjectiveScheduler, "Multi-Objective Scheduler")
    print('=========4')
    await test_scheduler(AsyncRealTimeScheduler, "Real-Time Scheduler")

if __name__ == "__main__":
    asyncio.run(main())
