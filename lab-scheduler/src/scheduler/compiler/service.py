"""Compile material-aware authoring steps into fixed execution DAGs."""

from __future__ import annotations

import math
from dataclasses import dataclass

from scheduler.api.schemas import ScheduleRequest, Step, Task
from scheduler.compiler.schemas import (
    CompiledContainer,
    CompiledStepMetadata,
    CompilerRequest,
    CompilerResponse,
    CompilerStep,
    DispatchStrategy,
    DurationModel,
    MaterialOutput,
    MaterialRef,
    MaterialSpec,
)

DEFAULT_CONTAINER_CAPACITIES: dict[str, int] = {
    "plate384": 384,
    "plate96": 96,
    "plate48": 48,
    "plate24": 24,
    "plate12": 12,
    "plate6": 6,
    "tube": 1,
    "vial": 1,
}


@dataclass
class _ContainerInstance:
    container_id: str
    material_id: str
    container_type: str
    amount: int
    producer_node_id: str | None = None


class CompilerService:
    """Material-aware compiler that emits scheduler-compatible fixed DAGs."""

    def compile(self, req: CompilerRequest) -> CompilerResponse:
        container_caps = dict(DEFAULT_CONTAINER_CAPACITIES)
        for spec in req.container_types:
            container_caps[spec.type] = spec.capacity

        inventory: dict[str, list[_ContainerInstance]] = {}
        containers: list[_ContainerInstance] = []
        for material in req.initial_materials:
            for inst in self._expand_initial_material(material, container_caps):
                inventory.setdefault(inst.material_id, []).append(inst)
                containers.append(inst)

        steps: list[Step] = []
        dependencies: list[tuple[str, str]] = []
        metadata: list[CompiledStepMetadata] = []
        source_nodes: dict[str, list[str]] = {}
        warnings: list[str] = []

        for source_step in req.steps:
            input_groups = [
                self._select_input_containers(ref, inventory)
                for ref in source_step.inputs
            ]
            input_containers = [inst for group in input_groups for inst in group]
            output_containers = self._expand_outputs(source_step.outputs, container_caps)

            strategy = self._resolve_strategy(
                source_step=source_step,
                input_groups=input_groups,
                output_containers=output_containers,
            )
            dispatch_count = self._dispatch_count(
                strategy=strategy,
                input_groups=input_groups,
                output_containers=output_containers,
            )

            node_ids = self._node_ids(source_step.step_id, dispatch_count)
            source_nodes[source_step.step_id] = node_ids

            inputs_by_node = self._assign_inputs(
                strategy=strategy,
                dispatch_count=dispatch_count,
                input_containers=input_containers,
            )
            outputs_by_node = self._assign_outputs(
                dispatch_count=dispatch_count,
                output_containers=output_containers,
            )

            for index, node_id in enumerate(node_ids):
                node_inputs = inputs_by_node[index]
                node_outputs = outputs_by_node[index]
                duration = self._estimate_duration(
                    model=source_step.duration_model,
                    input_containers=node_inputs,
                    output_containers=node_outputs,
                )
                steps.append(
                    Step(
                        step_id=node_id,
                        type=source_step.type,
                        machine_type=source_step.machine_type,
                        duration=duration,
                        input_samples=[c.container_id for c in node_inputs],
                        output_samples=[c.container_id for c in node_outputs],
                    )
                )

                for input_container in node_inputs:
                    if input_container.producer_node_id:
                        dependencies.append((input_container.producer_node_id, node_id))

                for output_container in node_outputs:
                    output_container.producer_node_id = node_id
                    inventory.setdefault(output_container.material_id, []).append(
                        output_container
                    )
                    containers.append(output_container)

                metadata.append(
                    CompiledStepMetadata(
                        node_id=node_id,
                        source_step_id=source_step.step_id,
                        dispatch_index=index + 1,
                        dispatch_count=dispatch_count,
                        dispatch_strategy=strategy,
                        machine_type=source_step.machine_type,
                        duration=duration,
                        input_containers=[c.container_id for c in node_inputs],
                        output_containers=[c.container_id for c in node_outputs],
                    )
                )

            for dep in source_step.depends_on:
                if dep not in source_nodes:
                    raise ValueError(
                        f"Unknown dependency '{dep}' for step '{source_step.step_id}'"
                    )
                for parent_id in source_nodes[dep]:
                    for node_id in node_ids:
                        dependencies.append((parent_id, node_id))

            if dispatch_count > 1:
                warnings.append(
                    f"Step '{source_step.step_id}' compiled into "
                    f"{dispatch_count} execution nodes using {strategy}"
                )

        task = Task(
            task_id=req.task_id,
            priority=req.priority,
            steps=steps,
            dependencies=self._dedupe_edges(dependencies),
            time_constraints=req.time_constraints,
        )
        schedule_request = ScheduleRequest(
            lab_id=req.lab_id,
            tasks=[task],
            resources=req.resources,
            algorithm=req.algorithm,
            current_time=req.current_time,
        )
        return CompilerResponse(
            schedule_request=schedule_request,
            metadata=metadata,
            containers=[
                CompiledContainer(
                    container_id=c.container_id,
                    material_id=c.material_id,
                    container_type=c.container_type,
                    amount=c.amount,
                    producer_node_id=c.producer_node_id,
                )
                for c in containers
            ],
            warnings=warnings,
        )

    def _expand_initial_material(
        self,
        material: MaterialSpec,
        container_caps: dict[str, int],
    ) -> list[_ContainerInstance]:
        count = self._container_count(
            amount=material.amount,
            container_type=material.container_type,
            explicit_count=material.count,
            container_caps=container_caps,
        )
        amounts = self._split_amount(material.amount, count)
        return [
            _ContainerInstance(
                container_id=self._container_id(
                    base=material.container_id or material.material_id,
                    index=i,
                    count=count,
                ),
                material_id=material.material_id,
                container_type=material.container_type,
                amount=amounts[i],
            )
            for i in range(count)
        ]

    def _expand_outputs(
        self,
        outputs: list[MaterialOutput],
        container_caps: dict[str, int],
    ) -> list[_ContainerInstance]:
        instances: list[_ContainerInstance] = []
        for output in outputs:
            count = self._container_count(
                amount=output.amount,
                container_type=output.container_type,
                explicit_count=output.count,
                container_caps=container_caps,
            )
            amounts = self._split_amount(output.amount, count)
            for index in range(count):
                instances.append(
                    _ContainerInstance(
                        container_id=self._container_id(
                            base=output.material_id,
                            index=index,
                            count=count,
                        ),
                        material_id=output.material_id,
                        container_type=output.container_type,
                        amount=amounts[index],
                    )
                )
        return instances

    def _select_input_containers(
        self,
        ref: MaterialRef,
        inventory: dict[str, list[_ContainerInstance]],
    ) -> list[_ContainerInstance]:
        candidates = inventory.get(ref.material_id, [])
        if ref.container_type is not None:
            candidates = [
                c for c in candidates if c.container_type == ref.container_type
            ]
        if not candidates:
            raise ValueError(f"Input material '{ref.material_id}' is not available")
        if ref.amount is None:
            return list(candidates)

        selected: list[_ContainerInstance] = []
        total = 0
        for container in candidates:
            selected.append(container)
            total += container.amount
            if total >= ref.amount:
                break
        if total < ref.amount:
            raise ValueError(
                f"Input material '{ref.material_id}' has amount {total}, "
                f"requires {ref.amount}"
            )
        return selected

    def _resolve_strategy(
        self,
        source_step: CompilerStep,
        input_groups: list[list[_ContainerInstance]],
        output_containers: list[_ContainerInstance],
    ) -> DispatchStrategy:
        if source_step.dispatch_strategy != "auto":
            return source_step.dispatch_strategy
        if len(output_containers) > 1:
            return "per_output_container"
        max_input_count = max((len(group) for group in input_groups), default=0)
        if max_input_count > 1:
            return "per_input_container"
        return "one"

    def _dispatch_count(
        self,
        strategy: DispatchStrategy,
        input_groups: list[list[_ContainerInstance]],
        output_containers: list[_ContainerInstance],
    ) -> int:
        if strategy == "one":
            return 1
        if strategy == "per_output_container":
            return max(1, len(output_containers))
        if strategy == "per_input_container":
            return max(1, max((len(group) for group in input_groups), default=0))
        return 1

    def _assign_inputs(
        self,
        strategy: DispatchStrategy,
        dispatch_count: int,
        input_containers: list[_ContainerInstance],
    ) -> list[list[_ContainerInstance]]:
        if dispatch_count == 1:
            return [list(input_containers)]
        if strategy == "per_input_container":
            return self._chunk_items(input_containers, dispatch_count)
        if len(input_containers) == dispatch_count:
            return [[input_containers[i]] for i in range(dispatch_count)]
        return [list(input_containers) for _ in range(dispatch_count)]

    def _assign_outputs(
        self,
        dispatch_count: int,
        output_containers: list[_ContainerInstance],
    ) -> list[list[_ContainerInstance]]:
        if dispatch_count == 1:
            return [list(output_containers)]
        return self._chunk_items(output_containers, dispatch_count)

    def _estimate_duration(
        self,
        model: DurationModel,
        input_containers: list[_ContainerInstance],
        output_containers: list[_ContainerInstance],
    ) -> float:
        if model.fixed is not None:
            return model.fixed
        work_amount = sum(c.amount for c in output_containers)
        if work_amount <= 0:
            work_amount = sum(c.amount for c in input_containers)
        cycles = math.ceil(max(work_amount, 1) / model.items_per_cycle)
        duration = model.setup + cycles * model.per_item + model.teardown
        return max(duration, 1)

    def _container_count(
        self,
        amount: int,
        container_type: str,
        explicit_count: int | None,
        container_caps: dict[str, int],
    ) -> int:
        if explicit_count is not None:
            return explicit_count
        if container_type not in container_caps:
            raise ValueError(
                f"Unknown container_type '{container_type}'. "
                "Add it to container_types with a capacity."
            )
        return max(1, math.ceil(amount / container_caps[container_type]))

    def _split_amount(self, amount: int, count: int) -> list[int]:
        base = amount // count
        remainder = amount % count
        return [base + (1 if i < remainder else 0) for i in range(count)]

    def _container_id(self, base: str, index: int, count: int) -> str:
        if count == 1:
            return base
        return f"{base}_{index + 1:02d}"

    def _node_ids(self, step_id: str, dispatch_count: int) -> list[str]:
        if dispatch_count == 1:
            return [step_id]
        return [f"{step_id}_{i + 1:02d}" for i in range(dispatch_count)]

    def _chunk_items(
        self,
        items: list[_ContainerInstance],
        chunk_count: int,
    ) -> list[list[_ContainerInstance]]:
        chunks: list[list[_ContainerInstance]] = [[] for _ in range(chunk_count)]
        for index, item in enumerate(items):
            chunks[index % chunk_count].append(item)
        return chunks

    def _dedupe_edges(self, edges: list[tuple[str, str]]) -> list[tuple[str, str]]:
        seen: set[tuple[str, str]] = set()
        result: list[tuple[str, str]] = []
        for edge in edges:
            if edge[0] == edge[1] or edge in seen:
                continue
            seen.add(edge)
            result.append(edge)
        return result
