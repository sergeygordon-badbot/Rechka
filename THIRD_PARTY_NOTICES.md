# Сторонние компоненты

Программа использует бесплатные компоненты с открытым исходным кодом:

- OpenAI Whisper — MIT License;
- faster-whisper — MIT License;
- CTranslate2 — MIT License;
- Hugging Face Hub client — Apache License 2.0;
- ONNX Runtime — MIT License;
- PyAV — BSD License;
- sounddevice — MIT License;
- PySide6 / Qt for Python — LGPL-3.0-only / GPL-3.0-only / commercial Qt license.

Модель `Systran/faster-whisper-small` является конвертацией весов OpenAI Whisper
и опубликована с указанием MIT License.

Полные тексты доступных лицензий включаются установщиком в каталог `licenses`.
Для Qt for Python 6.11.1 там также указана официальная ссылка на исходный код
точно этой версии. Библиотеки Qt поставляются отдельными динамически загружаемыми
DLL; программа не ограничивает разрешённый LGPL анализ и замену этих компонентов.
