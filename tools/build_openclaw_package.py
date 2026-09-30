"""Build a source-only OpenClaw skill directory; never include applicant data."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def package_files() -> dict[str, Path]:
    files = {"SKILL.md": ROOT / "skill/SKILL.md", "LICENSE": ROOT / "LICENSE"}
    for folder in ("scripts", "references"):
        for path in (ROOT / "skill" / folder).rglob("*"):
            if path.is_file() and path.suffix in (".py", ".md") and "__pycache__" not in path.parts:
                files[str(path.relative_to(ROOT / "skill")).replace("\\", "/")] = path
    for name in ("pyproject.toml", "requirements.txt", "requirements.lock", "README.md", "AGENTS.md", "LICENSE"):
        files["application/" + name] = ROOT / name
    allowed = {"src": {".py", ".html", ".css", ".js", ".json", ".sql"},
               "web": {".html", ".css", ".js"},
               "branding": {".svg", ".png", ".md"},
               "schemas": {".json"}, "migrations": {".sql"}, "examples": {".json"}}
    for folder, extensions in allowed.items():
        for path in (ROOT / folder).rglob("*"):
            if path.is_file() and path.suffix in extensions and not any(
                part == "__pycache__" or part.endswith(".egg-info") for part in path.parts
            ):
                files["application/" + path.relative_to(ROOT).as_posix()] = path
    for name in ("build_design_preview.py", "load_tech_demo.py", "requirements-demo.txt", "agent-instructions.md"):
        files["application/tools/" + name] = ROOT / "tools" / name
    for path in (ROOT / "docs").rglob("*.md"):
        if "review-artifacts" not in path.parts:
            files["application/" + path.relative_to(ROOT).as_posix()] = path
    return files


def build(output: Path) -> Path:
    output = output.resolve()
    # A fresh output avoids retaining data or stale files from previous builds.
    if output.exists():
        raise ValueError("Output already exists; choose a fresh directory instead of overwriting it")
    sources = package_files()
    for path in sources.values():
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"Missing or symlinked source: {path.relative_to(ROOT)}")
    output.mkdir(parents=True)
    hashes = {}
    for relative, source in sorted(sources.items()):
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        content = source.read_bytes()
        with destination.open("xb") as stream:
            stream.write(content)
        hashes[relative] = hashlib.sha256(content).hexdigest()
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    requirements = requirements.replace("-c requirements.lock", "-c application/requirements.lock")
    requirements = "\n".join("./application" if line == "." else line for line in requirements.splitlines()) + "\n"
    (output / "requirements.txt").write_text(requirements, encoding="utf-8")
    hashes["requirements.txt"] = hashlib.sha256((output / "requirements.txt").read_bytes()).hexdigest()
    (output / "package-manifest.json").write_text(json.dumps({
        "name": "RecruiterAgent", "format": "source-only-openclaw-skill", "files": hashes,
        "bundled_dependencies": False, "bundled_applicant_data": False,
    }, indent=2), encoding="utf-8")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist/RecruiterAgent")
    args = parser.parse_args()
    print(build(args.output))
