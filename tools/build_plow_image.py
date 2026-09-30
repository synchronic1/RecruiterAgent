"""Prepare an allowlisted Docker context with no host state or applicant data."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from build_openclaw_package import ROOT, build as build_skill


def build(output: Path) -> Path:
    output = output.resolve()
    if output.exists():
        raise ValueError("Output already exists; choose a fresh directory")
    output.mkdir(parents=True)
    build_skill(output / "skills/recruiteragent")
    source = ROOT / "submission/plow-image"
    for name in ("Dockerfile", ".dockerignore", "install_runtime.py", "smoke.py", "prompt.md"):
        path = source / name
        if not path.is_file() or path.is_symlink():
            raise ValueError("Missing or symlinked image source: " + name)
        with (output / name).open("xb") as stream:
            stream.write(path.read_bytes())
    hashes = {path.relative_to(output).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in sorted(output.rglob("*")) if path.is_file()}
    (output / "image-context-manifest.json").write_text(json.dumps({
        "format": "recruiteragent-plow-image-context", "applicant_data_included": False,
        "host_dependencies_included": False, "sha256": hashes,
    }, indent=2), encoding="utf-8")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist/plow-image")
    print(build(parser.parse_args().output))
