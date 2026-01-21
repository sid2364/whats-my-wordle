#!/usr/bin/env python3
"""wordle_last.py

Print the most recent result saved by nyt_wordle_bot.py --result-path.

Default path:
  ~/.cache/wordle-bot/last.json

Usage:
  python3 src/bot/wordle_last.py
  python3 src/bot/wordle_last.py --path ~/.cache/wordle-bot/last.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_CACHE_PATH = "~/.cache/wordle-bot/last.json"


def _expand(p: str) -> Path:
    return Path(p).expanduser().resolve()


def _notify(message: str) -> None:
    if shutil.which("notify-send") is None:
        return
    subprocess.run(
        ["notify-send", "Wordle bot", message],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Print the latest Wordle bot result.")
    ap.add_argument("--path", type=str, default=DEFAULT_CACHE_PATH, help="Path to result JSON.")
    ap.add_argument("--notify", action="store_true", help="Send a desktop notification with the result.")
    args = ap.parse_args(argv)

    path = _expand(args.path)
    if not path.exists():
        print(f"No saved result found at: {path}", file=sys.stderr)
        return 1

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Failed to read result JSON: {e}", file=sys.stderr)
        return 2

    date = data.get("date", "?")
    solved = bool(data.get("solved", False))
    answer = data.get("answer")
    turns = data.get("turns")

    if solved and isinstance(answer, str):
        msg = f"Wordle {date}: {answer} in {turns}"
    else:
        msg = f"Wordle {date}: not solved (turns={turns})"

    print(msg)
    if args.notify:
        _notify(msg)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
