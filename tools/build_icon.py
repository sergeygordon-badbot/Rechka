from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw


def create_icon(size: int = 1024) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    scale = size / 512

    def box(values: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        return tuple(round(value * scale) for value in values)

    draw.rounded_rectangle(
        box((24, 24, 488, 488)),
        radius=round(112 * scale),
        fill="#202123",
    )
    for values in (
        (128, 211, 160, 301),
        (184, 166, 216, 346),
        (240, 116, 272, 396),
        (296, 166, 328, 346),
        (352, 211, 384, 301),
    ):
        draw.rounded_rectangle(
            box(values),
            radius=round(16 * scale),
            fill="#FFFFFF",
        )

    draw.ellipse(box((362, 362, 470, 470)), fill="#FFFFFF")
    draw.ellipse(box((378, 378, 454, 454)), fill="#10A37F")
    return image


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "assets",
    )
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    source = create_icon()
    png_path = args.output_dir / "voiceinput.png"
    ico_path = args.output_dir / "voiceinput.ico"
    source.resize((512, 512), Image.Resampling.LANCZOS).save(png_path)
    source.save(
        ico_path,
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print(png_path.resolve())
    print(ico_path.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
