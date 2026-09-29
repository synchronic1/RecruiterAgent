"""Install RecruiterAgent into a skill-local virtual environment."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import venv

SKILL_ROOT = Path(__file__).resolve().parents[1]


def application_root() -> Path:
    for path in (SKILL_ROOT / "application", SKILL_ROOT.parent):
        if (path / "pyproject.toml").is_file() and (path / "requirements.txt").is_file():
            return path
    raise RuntimeError("Application source and requirements.txt are missing from the skill package")


def environment_python(directory: Path) -> Path:
    return directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


VERIFY = """
import importlib, importlib.metadata, importlib.resources, json
for name in ('fastapi', 'uvicorn', 'h11', 'pypdf', 'docx', 'httpx', 'resume_review.cli'):
    importlib.import_module(name)
root = importlib.resources.files('resume_review')
for path in ('templates/report.html', 'templates/report.css', 'templates/report.js',
             'bundle/assets/report.js', 'bundle/templates/report.html'):
    assert root.joinpath(path).is_file(), 'Missing packaged asset: ' + path
assert any(p.name.endswith('.sql') for p in root.joinpath('migrations').iterdir())
assert any(p.name.endswith('.json') for p in root.joinpath('schemas').iterdir())
print(json.dumps({'ok': True, 'application': 'RecruiterAgent',
                  'version': importlib.metadata.version('resume-review'),
                  'packaged_assets': 'verified', 'openclaw_live_tested': False}))
"""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--venv", type=Path, default=SKILL_ROOT / ".venv")
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--demo", action="store_true", help="Install optional synthetic PDF demo dependencies and build it")
    args = parser.parse_args(argv)
    if sys.version_info < (3, 12):
        parser.error("Python 3.12 or later is required; Windows/Python 3.14 is the verified host")
    try:
        app = application_root()
        directory = args.venv.resolve()
        python = environment_python(directory)
        if not args.verify_only:
            if not python.is_file():
                venv.EnvBuilder(with_pip=True).create(directory)
            subprocess.run([str(python), "-m", "pip", "install", "-r", str(app / "requirements.txt")], cwd=app, check=True)
            if args.demo:
                subprocess.run([str(python), "-m", "pip", "install", "-r", str(app / "tools/requirements-demo.txt")], cwd=app, check=True)
                subprocess.run([str(python), str(app / "tools/build_design_preview.py")], cwd=app, check=True)
        if not python.is_file():
            raise RuntimeError("Virtual environment is missing; run installation first")
        subprocess.run([str(python), "-m", "pip", "check"], cwd=directory, check=True)
        subprocess.run([str(python), "-I", "-c", VERIFY], cwd=directory, check=True)
        return 0
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(json.dumps({"ok": False, "error": str(error)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
