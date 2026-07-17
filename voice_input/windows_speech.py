from __future__ import annotations

import ctypes
from ctypes import wintypes

from .recognition import ProviderAvailability


APPMODEL_ERROR_NO_PACKAGE = 15700
ERROR_INSUFFICIENT_BUFFER = 122


def has_package_identity() -> bool:
    try:
        get_name = ctypes.windll.kernel32.GetCurrentPackageFullName
    except (AttributeError, OSError):
        return False
    length = wintypes.UINT(0)
    result = int(get_name(ctypes.byref(length), None))
    if result == APPMODEL_ERROR_NO_PACKAGE:
        return False
    if result != ERROR_INSUFFICIENT_BUFFER or length.value <= 1:
        return False
    buffer = ctypes.create_unicode_buffer(length.value)
    return int(get_name(ctypes.byref(length), buffer)) == 0 and bool(buffer.value)


def probe_windows_online_speech(
    language_tag: str = "ru-RU",
) -> ProviderAvailability:
    label = "Распознавание Windows"
    if not has_package_identity():
        return ProviderAvailability(
            provider_id="windows_online",
            label=label,
            available=False,
            detail=(
                "текущий установщик не даёт приложению пакетную "
                "идентификацию Windows"
            ),
            requires_network=True,
        )
    try:
        from winrt.windows.globalization import Language
        from winrt.windows.media.speechrecognition import SpeechRecognizer

        supported = {
            item.language_tag.casefold()
            for item in SpeechRecognizer.supported_topic_languages
        }
        if language_tag.casefold() not in supported:
            return ProviderAvailability(
                provider_id="windows_online",
                label=label,
                available=False,
                detail=f"язык {language_tag} не установлен в Windows",
                requires_network=True,
            )
        SpeechRecognizer(Language(language_tag))
    except Exception as exc:
        text = str(exc).replace("\r", " ").replace("\n", " ").strip()
        return ProviderAvailability(
            provider_id="windows_online",
            label=label,
            available=False,
            detail=(text[:180] or exc.__class__.__name__),
            requires_network=True,
        )
    return ProviderAvailability(
        provider_id="windows_online",
        label=label,
        available=True,
        detail="доступно через системную службу Windows",
        requires_network=True,
    )
