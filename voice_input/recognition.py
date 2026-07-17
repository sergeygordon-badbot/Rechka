from __future__ import annotations

import io
import json
import queue
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import httpx
import numpy as np


StatusCallback = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class ProviderAvailability:
    provider_id: str
    label: str
    available: bool
    detail: str
    requires_network: bool


@dataclass(frozen=True, slots=True)
class RecognitionMetrics:
    provider_id: str
    provider_label: str
    audio_seconds: float
    elapsed_seconds: float

    @property
    def realtime_factor(self) -> float:
        if self.audio_seconds <= 0:
            return 0.0
        return self.elapsed_seconds / self.audio_seconds

    @property
    def speed_label(self) -> str:
        factor = self.realtime_factor
        if factor <= 0:
            return "ещё не измерена"
        if factor < 1:
            return f"{1 / factor:.1f}× быстрее реального времени"
        return f"{factor:.1f}× длительности записи"


class RecognitionProviderError(RuntimeError):
    pass


def encode_mono_wav(samples: np.ndarray, sample_rate: int = 16_000) -> bytes:
    normalized = np.asarray(samples, dtype=np.float32).reshape(-1)
    normalized = np.nan_to_num(normalized, nan=0.0, posinf=1.0, neginf=-1.0)
    pcm = np.round(np.clip(normalized, -1.0, 1.0) * 32767.0).astype("<i2")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(pcm.tobytes())
    return buffer.getvalue()


def parse_gradio_sse(payload: str) -> Any:
    event_name = ""
    complete_value: Any = None
    for raw_line in payload.splitlines():
        line = raw_line.strip()
        if line.startswith("event:"):
            event_name = line[6:].strip()
            continue
        if not line.startswith("data:"):
            continue
        value = line[5:].strip()
        if event_name == "error":
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError:
                decoded = value
            raise RecognitionProviderError(f"Сервис вернул ошибку: {decoded}")
        if event_name == "complete":
            try:
                complete_value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise RecognitionProviderError(
                    "Сервис вернул ответ неизвестного формата"
                ) from exc
    if complete_value is None:
        raise RecognitionProviderError("Сервис не завершил распознавание")
    return complete_value


@dataclass(frozen=True, slots=True)
class HuggingFaceSpaceProvider:
    provider_id: str
    label: str
    base_url: str
    endpoint: str

    @property
    def endpoint_name(self) -> str:
        return self.endpoint.lstrip("/")

    def probe(self, timeout_seconds: float = 5.0) -> ProviderAvailability:
        try:
            response = httpx.get(
                f"{self.base_url}/gradio_api/info",
                timeout=timeout_seconds,
                follow_redirects=True,
            )
            response.raise_for_status()
            named_endpoints = response.json().get("named_endpoints", {})
            path = f"/{self.endpoint_name}"
            if path not in named_endpoints:
                raise RecognitionProviderError(
                    f"нет ожидаемого метода {path}"
                )
        except Exception as exc:
            return ProviderAvailability(
                provider_id=self.provider_id,
                label=self.label,
                available=False,
                detail=_short_error(exc),
                requires_network=True,
            )
        return ProviderAvailability(
            provider_id=self.provider_id,
            label=self.label,
            available=True,
            detail="публичный сервер отвечает",
            requires_network=True,
        )

    def transcribe(
        self,
        samples: np.ndarray,
        *,
        status: StatusCallback | None = None,
        client: httpx.Client | None = None,
    ) -> str:
        callback = status or (lambda _text: None)
        wav_bytes = encode_mono_wav(samples)
        own_client = client is None
        active_client = client or httpx.Client(
            timeout=httpx.Timeout(120.0, connect=7.0, write=30.0, pool=7.0),
            follow_redirects=True,
        )
        try:
            callback(f"{self.label}: отправляю аудио…")
            upload = active_client.post(
                f"{self.base_url}/gradio_api/upload",
                files={"files": ("rechka-recording.wav", wav_bytes, "audio/wav")},
            )
            upload.raise_for_status()
            uploaded_path = _uploaded_path(upload.json())
            file_data = {
                "path": uploaded_path,
                "url": (
                    f"{self.base_url}/gradio_api/file="
                    f"{quote(uploaded_path, safe='')}"
                ),
                "orig_name": Path(uploaded_path).name or "rechka-recording.wav",
                "size": len(wav_bytes),
                "mime_type": "audio/wav",
                "is_stream": False,
                "meta": {"_type": "gradio.FileData"},
            }
            callback(f"{self.label}: распознаю речь…")
            submitted = active_client.post(
                (
                    f"{self.base_url}/gradio_api/call/"
                    f"{self.endpoint_name}"
                ),
                json={"data": [file_data, "transcribe"]},
            )
            submitted.raise_for_status()
            event_id = str(submitted.json().get("event_id", "")).strip()
            if not event_id:
                raise RecognitionProviderError(
                    "Сервис не выдал номер задачи"
                )
            result = active_client.get(
                (
                    f"{self.base_url}/gradio_api/call/"
                    f"{self.endpoint_name}/{quote(event_id, safe='')}"
                )
            )
            result.raise_for_status()
            decoded = parse_gradio_sse(result.text)
            text = _extract_text(decoded)
            if not text:
                return ""
            return text.strip()
        except RecognitionProviderError:
            raise
        except (httpx.HTTPError, OSError, ValueError, TypeError) as exc:
            raise RecognitionProviderError(_short_error(exc)) from exc
        finally:
            if own_client:
                active_client.close()


PUBLIC_HUGGING_FACE_PROVIDERS = (
    HuggingFaceSpaceProvider(
        provider_id="hf_whisper_large_v3",
        label="Hugging Face Whisper Large v3",
        base_url="https://hf-audio-whisper-large-v3.hf.space",
        endpoint="/transcribe",
    ),
    HuggingFaceSpaceProvider(
        provider_id="hf_openai_whisper",
        label="Hugging Face OpenAI Whisper",
        base_url="https://openai-whisper.hf.space",
        endpoint="/predict",
    ),
)


def find_public_hugging_face_provider(
    status: StatusCallback | None = None,
) -> tuple[HuggingFaceSpaceProvider | None, tuple[ProviderAvailability, ...]]:
    callback = status or (lambda _text: None)
    results: list[ProviderAvailability] = []
    for provider in PUBLIC_HUGGING_FACE_PROVIDERS:
        callback(f"Проверяю {provider.label}…")
        availability = provider.probe()
        results.append(availability)
        if availability.available:
            return provider, tuple(results)
    return None, tuple(results)


def timed_provider_transcription(
    provider: HuggingFaceSpaceProvider,
    samples: np.ndarray,
    *,
    status: StatusCallback | None = None,
    deadline_seconds: float = 60.0,
) -> tuple[str, RecognitionMetrics]:
    started = time.monotonic()
    outcome: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

    def worker() -> None:
        try:
            outcome.put(("result", provider.transcribe(samples, status=status)))
        except Exception as exc:
            outcome.put(("error", exc))

    request_thread = threading.Thread(
        target=worker,
        name=f"{provider.provider_id}-request",
        daemon=True,
    )
    request_thread.start()
    effective_deadline = max(0.01, deadline_seconds)
    request_thread.join(effective_deadline)
    if request_thread.is_alive():
        raise RecognitionProviderError(
            "публичный сервер не завершил запрос за "
            f"{effective_deadline:g} секунд"
        )
    state, value = outcome.get_nowait()
    if state == "error":
        raise value
    text = str(value)
    elapsed = max(0.001, time.monotonic() - started)
    audio_seconds = np.asarray(samples).size / 16_000
    return text, RecognitionMetrics(
        provider_id=provider.provider_id,
        provider_label=provider.label,
        audio_seconds=audio_seconds,
        elapsed_seconds=elapsed,
    )


def _uploaded_path(payload: Any) -> str:
    if isinstance(payload, list) and payload:
        first = payload[0]
        if isinstance(first, str) and first:
            return first
        if isinstance(first, dict):
            path = first.get("path")
            if isinstance(path, str) and path:
                return path
    if isinstance(payload, dict):
        path = payload.get("path")
        if isinstance(path, str) and path:
            return path
    raise RecognitionProviderError("Сервис не принял аудиофайл")


def _extract_text(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, list):
        for value in payload:
            text = _extract_text(value)
            if text:
                return text
    if isinstance(payload, dict):
        for key in ("text", "value", "data"):
            if key in payload:
                text = _extract_text(payload[key])
                if text:
                    return text
    return ""


def _short_error(error: Exception) -> str:
    if isinstance(error, httpx.TimeoutException):
        return "сервер не ответил вовремя"
    if isinstance(error, httpx.ConnectError):
        return "нет соединения с сервером"
    text = str(error).replace("\r", " ").replace("\n", " ").strip()
    return text[:180] or error.__class__.__name__
