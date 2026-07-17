from __future__ import annotations

import ctypes
import os
import sys
from dataclasses import asdict, dataclass

import ctranslate2

from .windows import physical_core_count


@dataclass(frozen=True, slots=True)
class InferenceProfile:
    device: str
    compute_type: str
    device_index: int
    cuda_device_count: int
    cpu_compute_types: tuple[str, ...]
    cuda_compute_types: tuple[str, ...]
    cuda_error: str = ""
    physical_cores: int = 1

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ComputerAssessment:
    physical_cores: int
    logical_cores: int
    memory_gb: float
    windows_build: int
    accelerator: str
    preferred_recognition: str
    recommended_model: str
    expected_local_speed: str
    summary: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = (
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    )


def total_physical_memory() -> int:
    status = _MemoryStatusEx()
    status.dwLength = ctypes.sizeof(status)
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GlobalMemoryStatusEx.argtypes = (ctypes.POINTER(_MemoryStatusEx),)
        kernel32.GlobalMemoryStatusEx.restype = ctypes.c_int
        if kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.ullTotalPhys)
    except Exception:
        pass
    return 0


def windows_build_number() -> int:
    try:
        return int(sys.getwindowsversion().build)
    except (AttributeError, TypeError, ValueError):
        return 0


def assess_computer(
    profile: InferenceProfile | None = None,
    *,
    memory_bytes: int | None = None,
    build_number: int | None = None,
    logical_cores: int | None = None,
) -> ComputerAssessment:
    selected = profile or detect_inference_profile()
    physical = max(1, selected.physical_cores)
    logical = max(1, int(logical_cores or os.cpu_count() or physical))
    total_memory = (
        total_physical_memory() if memory_bytes is None else max(0, memory_bytes)
    )
    memory_gb = round(total_memory / (1024**3), 1) if total_memory else 0.0
    build = windows_build_number() if build_number is None else max(0, build_number)

    if selected.device == "cuda":
        accelerator = f"CUDA {selected.compute_type}"
        preferred_recognition = "local"
        recommended_model = "base"
        expected_speed = "высокая"
        summary = (
            "Есть совместимое GPU-ускорение; встроенный локальный резерв "
            "будет быстрым."
        )
    elif physical <= 2 or (memory_gb and memory_gb < 6):
        accelerator = f"CPU {selected.compute_type.upper()}"
        preferred_recognition = "cloud"
        recommended_model = "tiny"
        expected_speed = "низкая"
        summary = (
            "Слабый компьютер: основной выбор — системное или сетевое "
            "распознавание, локально используется Tiny."
        )
    elif physical <= 4 or (memory_gb and memory_gb < 10):
        accelerator = f"CPU {selected.compute_type.upper()}"
        preferred_recognition = "cloud"
        recommended_model = "base"
        expected_speed = "средняя"
        summary = (
            "Компьютер среднего уровня: облачный режим предпочтительнее, "
            "Base остаётся локальным резервом."
        )
    else:
        accelerator = f"CPU {selected.compute_type.upper()}"
        preferred_recognition = "local"
        recommended_model = "base"
        expected_speed = "средняя"
        summary = (
            "Ресурсов достаточно для локального резерва; сетевой режим "
            "снизит задержку и нагрузку."
        )

    return ComputerAssessment(
        physical_cores=physical,
        logical_cores=logical,
        memory_gb=memory_gb,
        windows_build=build,
        accelerator=accelerator,
        preferred_recognition=preferred_recognition,
        recommended_model=recommended_model,
        expected_local_speed=expected_speed,
        summary=summary,
    )


def _supported_compute_types(device: str) -> tuple[str, ...]:
    return tuple(sorted(ctranslate2.get_supported_compute_types(device)))


def detect_inference_profile(preference: str = "auto") -> InferenceProfile:
    if preference not in {"auto", "cpu", "cuda"}:
        raise ValueError(f"Неизвестный тип ускорителя: {preference}")

    physical_cores = max(1, physical_core_count())
    cpu_types = _supported_compute_types("cpu")
    cuda_count = 0
    if preference != "cpu":
        try:
            cuda_count = max(0, int(ctranslate2.get_cuda_device_count()))
        except Exception:
            cuda_count = 0

    cuda_types: tuple[str, ...] = ()
    cuda_error = ""
    if preference != "cpu" and cuda_count:
        try:
            cuda_types = _supported_compute_types("cuda")
        except Exception as exc:
            cuda_error = str(exc)
        else:
            for compute_type in ("float16", "int8_float16", "int8"):
                if compute_type in cuda_types:
                    return InferenceProfile(
                        device="cuda",
                        compute_type=compute_type,
                        device_index=0,
                        cuda_device_count=cuda_count,
                        cpu_compute_types=cpu_types,
                        cuda_compute_types=cuda_types,
                        physical_cores=physical_cores,
                    )

    if preference == "cuda" and not cuda_error:
        cuda_error = "Совместимая CUDA-видеокарта не найдена"
    cpu_compute_type = "int8" if "int8" in cpu_types else "float32"
    return InferenceProfile(
        device="cpu",
        compute_type=cpu_compute_type,
        device_index=0,
        cuda_device_count=cuda_count,
        cpu_compute_types=cpu_types,
        cuda_compute_types=cuda_types,
        cuda_error=cuda_error,
        physical_cores=physical_cores,
    )
