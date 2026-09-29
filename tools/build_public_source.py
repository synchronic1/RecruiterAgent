"""Prepare a clean GitHub source tree without local applicant or runtime data."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from build_openclaw_package import ROOT, package_files


def build(output: Path) -> Path:
    output = output.resolve()
    if output.exists():
        raise ValueError("Output already exists; choose a fresh directory")
    files = {}
    for relative, source in package_files().items():
        if relative.startswith("application/"):
            target = relative.removeprefix("application/")
        elif relative == "LICENSE":
            target = relative
        else:
            target = "skill/" + relative
        files[target] = source
    for name in ("build_openclaw_package.py", "build_public_source.py", "sync_web_assets.py"):
        files["tools/" + name] = ROOT / "tools" / name
    files[".gitignore"] = ROOT / ".gitignore"
    for folder, suffixes in (("tests", {".py", ".json", ".txt", ".md"}),
                             ("submission", {".py", ".json", ".md", ".html", ".png"})):
        for path in (ROOT / folder).rglob("*"):
            if (path.is_file() and path.suffix in suffixes
                    and "__pycache__" not in path.parts
                    and path.name not in ("agent_index_client.py", "upstream-manifest.json")):
                files[path.relative_to(ROOT).as_posix()] = path
    for source in files.values():
        if not source.is_file() or source.is_symlink():
            raise ValueError("Missing or symlinked public source")
    output.mkdir(parents=True)
    hashes = {}
    for relative, source in sorted(files.items()):
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        content = source.read_bytes()
        with destination.open("xb") as stream:
            stream.write(content)
        hashes[relative] = hashlib.sha256(content).hexdigest()
    (output / "public-source-manifest.json").write_text(json.dumps({
        "license": "MIT", "applicant_data_included": False, "sha256": hashes,
    }, indent=2), encoding="utf-8")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist/RecruiterAgent-GitHub")
    args = parser.parse_args()
    print(build(args.output))
