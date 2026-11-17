"""Run development checks without a bot token or a running application."""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full", action="store_true", help="require media, Deno and browser checks")
    arguments = parser.parse_args()
    os.chdir(ROOT)
    required = ["ffmpeg", "ffprobe", "yt-dlp", "gallery-dl", "deno", "node"] if arguments.full else []
    missing = [name for name in required if not shutil.which(name)]
    if missing:
        print("Missing integration tools: " + ", ".join(missing), file=sys.stderr)
        return 1
    commands = [
        [sys.executable, "-m", "ruff", "check", "."],
        [sys.executable, "-m", "ruff", "format", "--check", "."],
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
    ]
    if arguments.full:
        commands.extend(
            [
                ["deno", "fmt", "--check", "--no-config", "archive/web", "tests/web_test.js"],
                ["deno", "test", "--no-config", "--allow-read=archive/web", "tests/web_test.js"],
                ["node", "tests/ui_smoke.cjs"],
            ]
        )
    for command in commands:
        print("Running: " + " ".join(command), flush=True)
        result = subprocess.run(command, check=False)
        if result.returncode:
            return result.returncode
    if not arguments.full:
        print("Python checks passed. Use --full for required media, Deno and browser checks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
