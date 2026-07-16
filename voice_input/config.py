from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any


APP_DIR_NAME = "VoiceInput"

MODEL_OPTIONS = {
    "small": "Small — быстро, включена в установщик",
    "turbo": "Turbo — максимум точности, очень медленно на CPU (~1,6 ГБ)",
    "base": "Base — максимально быстро, точность ниже",
    "medium": "Medium — точнее Small, медленно на CPU (~1,5 ГБ)",
}

MODEL_REPOSITORIES = {
    "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small",
    "turbo": "dropbox-dash/faster-whisper-large-v3-turbo",
    "medium": "Systran/faster-whisper-medium",
}

MODEL_FILES = {
    "base": (
        "config.json",
        "model.bin",
        "tokenizer.json",
        "vocabulary.txt",
    ),
    "small": (
        "config.json",
        "model.bin",
        "tokenizer.json",
        "vocabulary.txt",
    ),
    "turbo": (
        "config.json",
        "preprocessor_config.json",
        "model.bin",
        "tokenizer.json",
        "vocabulary.json",
    ),
    "medium": (
        "config.json",
        "model.bin",
        "tokenizer.json",
        "vocabulary.txt",
    ),
}

MODEL_DOWNLOAD_DESCRIPTIONS = {
    "base": "~150 МБ",
    "small": "~470 МБ",
    "turbo": "~1,6 ГБ",
    "medium": "~1,5 ГБ",
}

DECODING_OPTIONS = {
    "fast": "Быстро — минимальная задержка",
    "balanced": "Баланс — немного точнее",
    "accurate": "Точно — заметно медленнее",
}

DECODING_BEAM_SIZES = {
    "fast": 1,
    "balanced": 2,
    "accurate": 3,
}

OUTPUT_MODE_OPTIONS = {
    "communication": "Общение — близко к оригиналу",
    "ai_prompt": "Промпт для AI — структурированная задача",
}

AI_TARGET_OPTIONS = {
    "universal": "Универсальный промпт",
    "chatgpt": "ChatGPT",
    "claude": "Claude",
    "gemini": "Gemini",
}

LANGUAGE_OPTIONS = {
    "ru": "Русский",
    "auto": "Автоопределение",
    "en": "Английский",
}

INSERTION_OPTIONS = {
    "paste": "Буфер обмена + Ctrl+V",
    "type": "Прямая печать (не меняет буфер)",
    "clipboard": "Только скопировать в буфер",
}

HOTKEY_OPTIONS = {
    "ctrl_alt_space": "Ctrl + Alt + Пробел",
    "ctrl_shift_space": "Ctrl + Shift + Пробел",
    "ctrl_alt_f8": "Ctrl + Alt + F8",
    "f8": "F8",
}


@dataclass(slots=True)
class AppConfig:
    model: str = "small"
    decoding_mode: str = "fast"
    output_mode: str = "communication"
    ai_target: str = "universal"
    language: str = "ru"
    device_index: int | None = None
    insertion_mode: str = "paste"
    hotkey: str = "ctrl_alt_space"
    append_space: bool = True
    punctuation_commands: bool = True
    start_minimized: bool = False
    autostart: bool = False
    sound_feedback: bool = False
    custom_terms: str = ""
    project_context: str = ""
    use_local_ai: bool = True
    ollama_model: str = "qwen3:4b"
    beam_size: int = 1


def data_dir() -> Path:
    override = os.environ.get("VOICE_INPUT_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()

    root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if root:
        return Path(root) / APP_DIR_NAME
    return Path.home() / f".{APP_DIR_NAME.lower()}"


def settings_path() -> Path:
    return data_dir() / "settings.json"


def bundled_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def bundled_model_path(model_name: str) -> Path:
    return bundled_root() / "models" / f"faster-whisper-{model_name}"


def downloaded_model_path(model_name: str) -> Path:
    return data_dir() / "models" / f"faster-whisper-{model_name}"


def load_config() -> AppConfig:
    path = settings_path()
    if not path.exists():
        return AppConfig()

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return AppConfig()

    allowed = {item.name for item in fields(AppConfig)}
    clean: dict[str, Any] = {key: value for key, value in payload.items() if key in allowed}
    is_legacy_config = "decoding_mode" not in payload
    config = AppConfig(**clean)

    if config.model not in MODEL_OPTIONS:
        config.model = "small"
    elif is_legacy_config and config.model in {"medium", "turbo"}:
        config.model = "small"
    if config.decoding_mode not in DECODING_OPTIONS:
        config.decoding_mode = "fast"
    if config.output_mode not in OUTPUT_MODE_OPTIONS:
        config.output_mode = "communication"
    if config.ai_target not in AI_TARGET_OPTIONS:
        config.ai_target = "universal"
    if config.language not in LANGUAGE_OPTIONS:
        config.language = "ru"
    if config.insertion_mode not in INSERTION_OPTIONS:
        config.insertion_mode = "paste"
    if config.hotkey not in HOTKEY_OPTIONS:
        config.hotkey = "ctrl_alt_space"
    config.beam_size = DECODING_BEAM_SIZES[config.decoding_mode]
    return config


def save_config(config: AppConfig) -> None:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(asdict(config), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)
