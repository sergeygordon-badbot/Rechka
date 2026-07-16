from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from faster_whisper import WhisperModel
from faster_whisper.audio import decode_audio


def transcribe_once(
    model: WhisperModel,
    samples: object,
    beam_size: int,
    chunk_length: int | None,
) -> tuple[float, str]:
    started = time.perf_counter()
    segments, _info = model.transcribe(
        samples,
        language="ru",
        task="transcribe",
        beam_size=beam_size,
        best_of=1,
        temperature=0.0,
        condition_on_previous_text=False,
        vad_filter=True,
        vad_parameters={
            "min_silence_duration_ms": 600,
            "speech_pad_ms": 250,
        },
        chunk_length=chunk_length,
        without_timestamps=True,
    )
    text = " ".join(segment.text.strip() for segment in segments if segment.text.strip())
    return time.perf_counter() - started, text


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("model", type=Path)
    parser.add_argument("audio", type=Path)
    parser.add_argument("--threads", default="6,8,12")
    parser.add_argument("--beams", default="1,3")
    parser.add_argument("--chunks", default="0")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    samples = decode_audio(str(args.audio), sampling_rate=16_000)
    results: list[dict[str, object]] = []
    for threads in (int(item) for item in args.threads.split(",")):
        model_started = time.perf_counter()
        model = WhisperModel(
            str(args.model),
            device="cpu",
            compute_type="int8",
            cpu_threads=threads,
            num_workers=1,
        )
        load_seconds = time.perf_counter() - model_started
        for chunk in (int(item) for item in args.chunks.split(",")):
            chunk_length = chunk or None
            for beam in (int(item) for item in args.beams.split(",")):
                seconds, text = transcribe_once(
                    model,
                    samples,
                    beam,
                    chunk_length,
                )
                result = {
                    "threads": threads,
                    "beam_size": beam,
                    "chunk_length": chunk_length,
                    "model_load_seconds": round(load_seconds, 3),
                    "transcription_seconds": round(seconds, 3),
                    "text": text,
                }
                results.append(result)
                print(json.dumps(result, ensure_ascii=False), flush=True)
        del model

    payload = {"results": results}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
