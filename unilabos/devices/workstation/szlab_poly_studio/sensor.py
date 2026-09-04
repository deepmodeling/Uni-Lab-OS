"""SZLab Poly Studio 统一传感器定义与通用方法。"""

from __future__ import annotations

import csv
import codecs
from concurrent.futures import ThreadPoolExecutor
import io
import json
import re
import time
from pathlib import Path
from typing import Any, ClassVar, Dict, Optional


DEFAULT_STACK_SENSOR_LAYOUT_NAME = "stack_sensor_layout.json"
SENSOR_ARRAY_COUNT = 10
SENSOR_BITS_PER_ARRAY = 16
SENSOR_BIT_NAME_PATTERN = re.compile(r"^传感器状态_上位机\[(\d+)\]\.NO\[(\d+)\]$")


def read_plc_csv_text(csv_path: str) -> str:
    """按 BOM 和内容特征安全解码 PLC CSV。"""
    raw = Path(csv_path).read_bytes()
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode("utf-16")
    if raw.startswith(codecs.BOM_UTF8):
        return raw.decode("utf-8-sig")

    sample = raw[:256]
    if sample:
        odd_nuls = sample[1::2].count(0)
        even_nuls = sample[0::2].count(0)
        if odd_nuls > len(sample) // 8:
            return raw.decode("utf-16-le")
        if even_nuls > len(sample) // 8:
            return raw.decode("utf-16-be")

    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("gb18030")


def _resolve_config_path(config_path: Optional[str]) -> Path:
    path = Path(config_path or DEFAULT_STACK_SENSOR_LAYOUT_NAME)
    if path.is_absolute():
        return path
    return Path(__file__).resolve().parent / path


def load_stack_sensor_groups_from_json(config_path: Optional[str] = None) -> Dict[str, Dict[str, str]]:
    """读取堆栈业务位置到 PLC 传感器变量的映射。"""
    with _resolve_config_path(config_path).open(encoding="utf-8") as config_file:
        config = json.load(config_file)
    sensor_groups = config.get("sensor_groups", {})
    return {
        str(group_name): {
            str(site_key): str(variable_name)
            for site_key, variable_name in group.items()
        }
        for group_name, group in sensor_groups.items()
    }


def load_sensor_bit_metadata_from_csv(csv_path: str) -> Dict[str, Dict[str, str]]:
    """读取传感器位的现场标签、软元件地址和 NodeId。"""
    text = read_plc_csv_text(csv_path)
    for delimiter in (",", "\t"):
        reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
        if "变量名" not in (reader.fieldnames or []):
            continue
        metadata: Dict[str, Dict[str, str]] = {}
        for row in reader:
            name = (row.get("变量名") or "").strip()
            if not SENSOR_BIT_NAME_PATTERN.fullmatch(name):
                continue
            metadata[name] = {
                "label": (row.get("注释") or "").strip(),
                "address": (row.get("软元件地址") or "").strip(),
                "node_id": (row.get("node_id") or row.get("nodeid") or "").strip(),
            }
        return metadata
    return {}


class SensorBase:
    """所有 SZLab 工位传感器定义的基类。"""

    ARRAY_COUNT: ClassVar[int] = SENSOR_ARRAY_COUNT
    BITS_PER_ARRAY: ClassVar[int] = SENSOR_BITS_PER_ARRAY
    BIT_NAME_PATTERN: ClassVar[re.Pattern[str]] = SENSOR_BIT_NAME_PATTERN

    @classmethod
    def bit(cls, group: int, index: int) -> str:
        group = int(group)
        index = int(index)
        if not 0 <= group < cls.ARRAY_COUNT:
            raise ValueError(f"传感器数组编号必须在 0-{cls.ARRAY_COUNT - 1} 范围内")
        if not 0 <= index < cls.BITS_PER_ARRAY:
            raise ValueError(f"传感器位编号必须在 0-{cls.BITS_PER_ARRAY - 1} 范围内")
        return f"传感器状态_上位机[{group}].NO[{index}]"

    @classmethod
    def array(cls, group: int) -> str:
        group = int(group)
        if not 0 <= group < cls.ARRAY_COUNT:
            raise ValueError(f"传感器数组编号必须在 0-{cls.ARRAY_COUNT - 1} 范围内")
        return f"传感器状态_上位机[{group}].NO"

    @classmethod
    def parse_bit_name(cls, variable_name: str) -> tuple[int, int] | None:
        match = cls.BIT_NAME_PATTERN.fullmatch(variable_name)
        if match is None:
            return None
        group, index = int(match.group(1)), int(match.group(2))
        if group >= cls.ARRAY_COUNT or index >= cls.BITS_PER_ARRAY:
            return None
        return group, index

    @staticmethod
    def wait_variable_equal(
        reader: Any,
        variable_name: str,
        expected: Any,
        *,
        interval: float = 1.0,
        timeout: float | None = None,
    ) -> bool:
        started_at = time.time()
        monotonic_started_at = time.monotonic()
        start_recorder = getattr(reader, "_record_opc_wait_start", None)
        if callable(start_recorder):
            start_recorder(variable_name, expected, interval=interval)

        success = False
        last_value = None
        error = None
        try:
            while (
                timeout is None
                or time.monotonic() - monotonic_started_at < timeout
            ):
                last_value = reader.read_variable(variable_name, use_cache=False)
                if last_value == expected:
                    success = True
                    return True
                abort_check = getattr(reader, "_mixing_wait_should_abort", None)
                if callable(abort_check) and abort_check():
                    return False
                time.sleep(interval)
            return False
        except Exception as exc:
            error = str(exc)
            raise
        finally:
            finish_recorder = getattr(reader, "_record_opc_wait_finish", None)
            if callable(finish_recorder):
                finish_recorder(
                    variable_name,
                    expected,
                    interval=interval,
                    success=success,
                    last_value=last_value,
                    elapsed=time.time() - started_at,
                    error=error,
                )

    @classmethod
    def wait_variable_true(
        cls,
        reader: Any,
        variable_name: str,
        *,
        interval: float = 1.0,
        timeout: float | None = None,
    ) -> bool:
        return cls.wait_variable_equal(
            reader,
            variable_name,
            True,
            interval=interval,
            timeout=timeout,
        )

    @staticmethod
    def wait_conditions(
        reader: Any,
        conditions: Dict[str, bool],
        *,
        interval: float = 0.2,
        context: str | None = None,
        timeout: float | None = None,
    ) -> tuple[bool, Dict[str, Any]]:
        if not conditions:
            return True, {}

        started_at = time.monotonic()
        last_values: Dict[str, Any] = {}
        previous_values: Dict[str, Any] | None = None
        start_recorded = False
        success = False
        error = None
        variable_names = tuple(conditions)
        try:
            # 同一轮的所有传感器同时发起读取，避免逐个读取导致检查延迟累加。
            # 线程池在整个等待周期内复用，防止每轮轮询反复创建线程。
            with ThreadPoolExecutor(
                max_workers=len(variable_names),
                thread_name_prefix="sensor-check",
            ) as executor:
                while timeout is None or time.monotonic() - started_at < timeout:
                    futures = {
                        variable_name: executor.submit(
                            reader.read_variable,
                            variable_name,
                            use_cache=False,
                        )
                        for variable_name in variable_names
                    }
                    last_values = {
                        variable_name: futures[variable_name].result()
                        for variable_name in variable_names
                    }
                    if not start_recorded:
                        start_recorder = getattr(reader, "_record_opc_sensor_wait_start", None)
                        if callable(start_recorder):
                            start_recorder(
                                conditions,
                                last_values,
                                interval=interval,
                                context=context,
                            )
                        start_recorded = True
                    elif previous_values is not None and last_values != previous_values:
                        change_recorder = getattr(reader, "_record_opc_sensor_wait_change", None)
                        if callable(change_recorder):
                            change_recorder(
                                conditions,
                                previous_values,
                                last_values,
                                context=context,
                            )
                    previous_values = dict(last_values)
                    if all(last_values[name] == expected for name, expected in conditions.items()):
                        success = True
                        return True, last_values
                    abort_check = getattr(reader, "_mixing_wait_should_abort", None)
                    if callable(abort_check) and abort_check():
                        return False, last_values
                    time.sleep(interval)
                return False, last_values
        except Exception as exc:
            error = str(exc)
            raise
        finally:
            finish_recorder = getattr(reader, "_record_opc_sensor_wait_finish", None)
            if callable(finish_recorder) and start_recorded:
                finish_recorder(
                    conditions,
                    last_values,
                    interval=interval,
                    success=success,
                    elapsed=time.monotonic() - started_at,
                    context=context,
                    error=error,
                )


STACK_SENSOR_GROUPS = load_stack_sensor_groups_from_json()


class S02Sensors(SensorBase):
    TIP_BOX: ClassVar[Dict[str, str]] = dict(STACK_SENSOR_GROUPS["s2_tip"])


class S03Sensors(SensorBase):
    UNUSED_BEAKER: ClassVar[Dict[str, str]] = dict(STACK_SENSOR_GROUPS["s3_unused_beaker"])
    UNUSED_SAMPLE_VIAL: ClassVar[Dict[str, str]] = dict(STACK_SENSOR_GROUPS["s3_unused_sample_vial"])


class S04Sensors(SensorBase):
    MATERIAL_BY_POSITION: ClassVar[Dict[int, str]] = {
        position: SensorBase.bit(2, position + 9)
        for position in range(1, 5)
    }

    @classmethod
    def material(cls, position: int) -> str:
        position = int(position)
        if position not in cls.MATERIAL_BY_POSITION:
            raise ValueError("S04磁搅位置必须在 1-4 范围内")
        return cls.MATERIAL_BY_POSITION[position]


class S05Sensors(SensorBase):
    MATERIAL: ClassVar[str] = SensorBase.bit(3, 0)


class S06Sensors(SensorBase):
    MATERIAL: ClassVar[str] = SensorBase.bit(3, 1)


class S07Sensors(SensorBase):
    POWDER_CONTAINER_BY_POSITION: ClassVar[Dict[str, str]] = dict(STACK_SENSOR_GROUPS["powder_container"])
    POWDER_CONTAINER_BY_INDEX: ClassVar[Dict[int, str]] = {
        index: variable_name
        for index, variable_name in enumerate(STACK_SENSOR_GROUPS["powder_container"].values(), start=1)
    }


class S08Sensors(SensorBase):
    CAP_STATION: ClassVar[Dict[int, str]] = {
        1: SensorBase.bit(3, 14),
        2: SensorBase.bit(3, 15),
    }
    CAP_STORAGE_SLOT: ClassVar[Dict[int, str]] = {
        slot: SensorBase.bit(4, slot - 1)
        for slot in range(1, 6)
    }
    POUR_SAMPLE_VIAL: ClassVar[str] = CAP_STATION[1]


class S09Sensors(SensorBase):
    TIP_BOX: ClassVar[Dict[int, str]] = {
        1: SensorBase.bit(4, 5),
        2: SensorBase.bit(4, 6),
    }
    STATION: ClassVar[Dict[int, str]] = {
        position: SensorBase.bit(4, position + 6)
        for position in range(1, 6)
    }


class S10Sensors(SensorBase):
    LIQUID_REAGENT: ClassVar[Dict[str, str]] = dict(STACK_SENSOR_GROUPS["s10_liquid_reagent"])


class S11Sensors(SensorBase):
    USED_BEAKER: ClassVar[Dict[str, str]] = dict(STACK_SENSOR_GROUPS["s11_used_beaker"])
    USED_SAMPLE_VIAL: ClassVar[Dict[str, str]] = dict(STACK_SENSOR_GROUPS["s11_used_sample_vial"])


def wait_variable_equal(
    reader: Any,
    variable_name: str,
    expected: Any,
    *,
    interval: float = 1.0,
    timeout: float | None = None,
) -> bool:
    return SensorBase.wait_variable_equal(
        reader,
        variable_name,
        expected,
        interval=interval,
        timeout=timeout,
    )


def wait_variable_true(
    reader: Any,
    variable_name: str,
    *,
    interval: float = 1.0,
    timeout: float | None = None,
) -> bool:
    return SensorBase.wait_variable_true(
        reader,
        variable_name,
        interval=interval,
        timeout=timeout,
    )


def wait_sensor_conditions(
    reader: Any,
    conditions: Dict[str, bool],
    *,
    interval: float = 0.2,
    context: str | None = None,
    timeout: float | None = None,
) -> tuple[bool, Dict[str, Any]]:
    return SensorBase.wait_conditions(
        reader,
        conditions,
        interval=interval,
        context=context,
        timeout=timeout,
    )
