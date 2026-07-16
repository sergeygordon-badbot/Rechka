from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice_input import __version__
from voice_input.updater import parse_version


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("tag")
    args = parser.parse_args()

    tag_version = ".".join(str(part) for part in parse_version(args.tag))
    if tag_version != __version__:
        raise SystemExit(
            f"Тег {args.tag} не совпадает с версией приложения {__version__}."
        )
    print(__version__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
