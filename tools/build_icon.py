from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def create_icon(size: int = 1024) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    scale = size / 512

    def box(values: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        return tuple(round(value * scale) for value in values)

    # A compact voice-to-text mark: the waveform becomes two lines of text.
    # The offset acid shape mirrors the landing page cards and remains readable
    # even in the 16 px Windows taskbar/shortcut variant.
    draw.rounded_rectangle(
        box((48, 48, 480, 480)),
        radius=round(108 * scale),
        fill="#C7FF36",
    )
    draw.rounded_rectangle(
        box((24, 24, 456, 456)),
        radius=round(108 * scale),
        fill="#F3F1EB",
        outline="#171816",
        width=round(12 * scale),
    )
    draw.rounded_rectangle(
        box((82, 82, 398, 398)),
        radius=round(88 * scale),
        fill="#171816",
    )

    for index, values in enumerate(
        (
            (126, 191, 148, 277),
            (164, 160, 186, 308),
            (202, 124, 224, 344),
            (240, 160, 262, 308),
            (278, 191, 300, 277),
        )
    ):
        draw.rounded_rectangle(
            box(values),
            radius=round(11 * scale),
            fill="#C7FF36" if index % 2 == 0 else "#71E5BD",
        )

    for values in ((126, 337, 354, 355), (186, 371, 354, 389)):
        draw.rounded_rectangle(
            box(values),
            radius=round(9 * scale),
            fill="#F3F1EB",
        )
    return image


def svg_icon() -> str:
    bars = "\n".join(
        f'  <rect x="{x}" y="{y}" width="22" height="{height}" rx="11" fill="{fill}"/>'
        for x, y, height, fill in (
            (126, 191, 86, "#C7FF36"),
            (164, 160, 148, "#71E5BD"),
            (202, 124, 220, "#C7FF36"),
            (240, 160, 148, "#71E5BD"),
            (278, 191, 86, "#C7FF36"),
        )
    )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <rect x="48" y="48" width="432" height="432" rx="108" fill="#C7FF36"/>
  <rect x="30" y="30" width="420" height="420" rx="102" fill="#F3F1EB" stroke="#171816" stroke-width="12"/>
  <rect x="82" y="82" width="316" height="316" rx="88" fill="#171816"/>
{bars}
  <rect x="126" y="337" width="228" height="18" rx="9" fill="#F3F1EB"/>
  <rect x="186" y="371" width="168" height="18" rx="9" fill="#F3F1EB"/>
</svg>
"""


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    names = (
        "C:/Windows/Fonts/seguisb.ttf" if bold
        else "C:/Windows/Fonts/segoeui.ttf",
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
    )
    for name in names:
        try:
            return ImageFont.truetype(name, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def create_installer_wizard() -> Image.Image:
    width, height = 492, 942
    image = Image.new("RGB", (width, height), "#F3F1EB")
    draw = ImageDraw.Draw(image)

    draw.rounded_rectangle(
        (78, 108, 450, 635),
        radius=92,
        fill="#C7FF36",
    )
    draw.rounded_rectangle(
        (42, 76, 414, 603),
        radius=92,
        fill="#171816",
    )
    draw.ellipse(
        (82, 166, 374, 458),
        outline="#454641",
        width=2,
    )

    center_y = 310
    heights = (46, 92, 144, 84, 132, 72, 118, 56, 96)
    colors = ("#71E5BD", "#C7FF36")
    start_x = 111
    for index, bar_height in enumerate(heights):
        x = start_x + index * 27
        draw.rounded_rectangle(
            (
                x,
                center_y - bar_height // 2,
                x + 10,
                center_y + bar_height // 2,
            ),
            radius=5,
            fill=colors[index % 2],
        )

    draw.text(
        (46, 682),
        "РЕЧКА",
        font=_font(58, bold=True),
        fill="#171816",
    )
    draw.text(
        (48, 758),
        "Голос становится",
        font=_font(26, bold=True),
        fill="#171816",
    )
    draw.text(
        (48, 794),
        "готовым текстом.",
        font=_font(26, bold=True),
        fill="#171816",
    )
    draw.rounded_rectangle(
        (48, 859, 165, 894),
        radius=17,
        fill="#171816",
    )
    draw.text(
        (67, 866),
        "ЛОКАЛЬНО",
        font=_font(14, bold=True),
        fill="#C7FF36",
    )
    return image


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "assets",
    )
    parser.add_argument("--site-icon", type=Path)
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    source = create_icon()
    png_path = args.output_dir / "voiceinput.png"
    ico_path = args.output_dir / "voiceinput.ico"
    svg_path = args.output_dir / "voiceinput.svg"
    wizard_path = args.output_dir / "installer-wizard.png"
    wizard_small_path = args.output_dir / "installer-small.png"
    source.resize((512, 512), Image.Resampling.LANCZOS).save(png_path)
    source.save(
        ico_path,
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    svg_path.write_text(svg_icon(), encoding="utf-8")
    create_installer_wizard().save(wizard_path)
    source.resize((256, 256), Image.Resampling.LANCZOS).save(
        wizard_small_path
    )
    if args.site_icon:
        args.site_icon.parent.mkdir(parents=True, exist_ok=True)
        source.resize((512, 512), Image.Resampling.LANCZOS).save(args.site_icon)
    print(png_path.resolve())
    print(ico_path.resolve())
    print(svg_path.resolve())
    print(wizard_path.resolve())
    print(wizard_small_path.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
