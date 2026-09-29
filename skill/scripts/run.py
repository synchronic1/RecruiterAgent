"""Run the installed helper without relying on shell activation or global PATH."""
import argparse
from pathlib import Path
import subprocess
import sys

from install import SKILL_ROOT, environment_python


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--venv", type=Path, default=SKILL_ROOT / ".venv")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    python = environment_python(args.venv.resolve())
    if not python.is_file():
        parser.error("Run scripts/install.py first; the skill's virtual environment is missing")
    return subprocess.run([str(python), "-I", "-m", "resume_review.cli", *command]).returncode


if __name__ == "__main__":
    raise SystemExit(main())
