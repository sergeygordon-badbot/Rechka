from __future__ import annotations

import os
import re
import threading
import math
from collections.abc import Callable
from pathlib import Path

import numpy as np

os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

from faster_whisper import WhisperModel

from .config import (
    MODEL_DOWNLOAD_DESCRIPTIONS,
    MODEL_FILES,
    MODEL_REPOSITORIES,
    bundled_model_path,
    downloaded_model_path,
)
from .audio import prepare_audio_for_whisper
from .model_download import download_model_files, model_is_complete
from .windows import physical_core_count


StatusCallback = Callable[[str], None]


def apply_voice_commands(text: str) -> str:
    replacements = (
        (r"\bновый абзац\b", "\n\n"),
        (r"\bновая строка\b", "\n"),
        (r"\bпоставь точку\b", "."),
        (r"\bпоставь запятую\b", ","),
        (r"\bвопросительный знак\b", "?"),
        (r"\bвосклицательный знак\b", "!"),
    )
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text


def normalize_transcript(text: str, punctuation_commands: bool = True) -> str:
    text = text.strip()
    if punctuation_commands:
        text = apply_voice_commands(text)

    lines = []
    for line in text.splitlines():
        clean = re.sub(r"[ \t]+", " ", line).strip()
        clean = re.sub(r"\s+([,.;:!?])", r"\1", clean)
        clean = re.sub(r"([(\[«])\s+", r"\1", clean)
        for index, character in enumerate(clean):
            if character.isalpha():
                clean = clean[:index] + character.upper() + clean[index + 1 :]
                break
        lines.append(clean)
    text = "\n".join(lines).strip()
    return text


def choose_chunk_length(sample_count: int, sample_rate: int = 16_000) -> int:
    duration = max(0.0, sample_count / sample_rate)
    return max(10, min(30, math.ceil(duration) + 1))


class WhisperEngine:
    def __init__(self) -> None:
        self._model: WhisperModel | None = None
        self._model_name: str | None = None
        self._cpu_threads = max(1, physical_core_count())
        self._lock = threading.Lock()
        self._transcribe_lock = threading.Lock()

    @property
    def model_name(self) -> str | None:
        return self._model_name

    @property
    def cpu_threads(self) -> int:
        return self._cpu_threads

    def _resolve_model(self, model_name: str, status: StatusCallback) -> Path:
        required_files = MODEL_FILES[model_name]
        bundled = bundled_model_path(model_name)
        if model_is_complete(bundled, required_files):
            status("Найдена локальная модель")
            return bundled

        destination = downloaded_model_path(model_name)
        if model_is_complete(destination, required_files):
            status("Найдена загруженная модель")
            return destination

        repository = MODEL_REPOSITORIES[model_name]
        destination.parent.mkdir(parents=True, exist_ok=True)
        size = MODEL_DOWNLOAD_DESCRIPTIONS[model_name]
        status(f"Скачивание модели ({size}) — не закрывайте программу…")
        return download_model_files(
            repo_id=repository,
            destination=destination,
            files=required_files,
            model_label=model_name.capitalize(),
            status=status,
        )

    def load(self, model_name: str, status: StatusCallback | None = None) -> None:
        callback = status or (lambda _message: None)
        with self._lock:
            if self._model is not None and self._model_name == model_name:
                return

            model_path = self._resolve_model(model_name, callback)
            callback(
                f"Загрузка модели в память: INT8, {self._cpu_threads} потоков…"
            )
            self._model = WhisperModel(
                str(model_path),
                device="cpu",
                compute_type="int8",
                cpu_threads=self._cpu_threads,
                num_workers=1,
            )
            self._model_name = model_name

    def transcribe(
        self,
        samples: np.ndarray,
        language: str = "ru",
        beam_size: int = 1,
        custom_terms: str = "",
        punctuation_commands: bool = True,
    ) -> str:
        model = self._model
        if model is None:
            raise RuntimeError("Модель ещё не загружена")

        selected_language = None if language == "auto" else language
        prompt = custom_terms.strip() or None
        prepared = prepare_audio_for_whisper(samples)
        chunk_length = choose_chunk_length(prepared.size)
        with self._transcribe_lock:
            segments, _info = model.transcribe(
                prepared,
                language=selected_language,
                task="transcribe",
                beam_size=max(1, min(5, beam_size)),
                best_of=1,
                temperature=0.0,
                condition_on_previous_text=prepared.size > 28 * 16_000,
                vad_filter=True,
                vad_parameters={
                    "min_silence_duration_ms": 600,
                    "speech_pad_ms": 250,
                },
                chunk_length=chunk_length,
                initial_prompt=prompt,
                no_speech_threshold=0.6,
                log_prob_threshold=-1.0,
                compression_ratio_threshold=2.4,
                without_timestamps=True,
            )
            text = " ".join(
                segment.text.strip()
                for segment in segments
                if segment.text.strip()
            )
        return normalize_transcript(text, punctuation_commands=punctuation_commands)
