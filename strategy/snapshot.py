from __future__ import annotations

import argparse
import logging
import re
import shutil
from pathlib import Path

log = logging.getLogger("snapshot")

BOTS_DIR = Path(__file__).parent / "bots"
SNAPSHOTS_DIR = BOTS_DIR / "snapshots"
CURRENT_FILE = BOTS_DIR / "current.py"


def get_next_version() -> int:
    existing = []
    for f in SNAPSHOTS_DIR.glob("v*.py"):
        match = re.match(r"v(\d+)\.py", f.name)
        if match:
            existing.append(int(match.group(1)))
    return max(existing, default=0) + 1


def create_snapshot(tag: str | None = None) -> str:
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)

    version = get_next_version()
    version_str = f"v{version:03d}"
    target = SNAPSHOTS_DIR / f"{version_str}.py"

    source = CURRENT_FILE.read_text(encoding="utf-8")

    source = re.sub(
        r'class\s+CurrentBot\s*\(',
        f'class Bot{version_str.upper()}(',
        source,
    )
    source = re.sub(
        r'name\s*=\s*"current"',
        f'name = "{version_str}"',
        source,
    )

    target.write_text(source, encoding="utf-8")
    log.info("Снапшот создан: %s", target)
    return version_str


def list_snapshots() -> list[str]:
    names = []
    for f in sorted(SNAPSHOTS_DIR.glob("v*.py")):
        if f.name == "__init__.py":
            continue
        names.append(f.stem)
    return names


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

    parser = argparse.ArgumentParser(description="Управление снапшотами бота")
    sub = parser.add_subparsers(dest="action")

    sub.add_parser("create", help="Создать снапшот текущего бота")
    sub.add_parser("list", help="Показать все снапшоты")

    args = parser.parse_args()

    if args.action == "create":
        v = create_snapshot()
        print(f"Создан снапшот: {v}")
    elif args.action == "list":
        snaps = list_snapshots()
        if snaps:
            for s in snaps:
                print(s)
        else:
            print("Нет снапшотов")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
