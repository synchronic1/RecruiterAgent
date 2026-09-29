"""Portable skill packaging excludes local data and preserves installable source."""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


def load_builder():
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("source_package_test", root / "tools/build_openclaw_package.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_source_package_is_complete_without_local_data_or_binaries(tmp_path):
    module = load_builder()
    output = module.build(tmp_path / "RecruiterAgent")
    manifest = json.loads((output / "package-manifest.json").read_text())
    assert manifest["bundled_dependencies"] is False
    assert manifest["bundled_applicant_data"] is False
    for relative, digest in manifest["files"].items():
        assert hashlib.sha256((output / relative).read_bytes()).hexdigest() == digest
        assert not any(part in ("resume", ".venv", "__pycache__", "review-artifacts") for part in Path(relative).parts)
        assert Path(relative).suffix not in (".pdf", ".db", ".sqlite", ".exe", ".dll", ".pyd", ".whl", ".pyc")
    for relative in ("SKILL.md", "requirements.txt", "scripts/install.py", "scripts/run.py",
                     "references/agent-instructions.md", "references/install.md",
                     "application/pyproject.toml", "application/requirements.lock",
                     "application/src/resume_review/cli.py", "application/tools/agent-instructions.md"):
        assert (output / relative).is_file()
    requirements = (output / "requirements.txt").read_text()
    assert "-c application/requirements.lock" in requirements
    assert "./application" in requirements
    assert "pytest" not in requirements
    assert "name: recruiteragent" in (output / "SKILL.md").read_text()
    assert (output / "LICENSE").read_bytes() == (output / "application/LICENSE").read_bytes()
    assert "MIT License" in (output / "LICENSE").read_text()
    assert 'license = { text = "MIT" }' in (output / "application/pyproject.toml").read_text()
    with pytest.raises(ValueError, match="already exists"):
        module.build(output)


def test_builder_does_not_package_generated_or_symlinked_source(tmp_path, monkeypatch):
    module = load_builder()
    missing = tmp_path / "missing.py"
    monkeypatch.setattr(module, "package_files", lambda: {"application/missing.py": missing})
    monkeypatch.setattr(module, "ROOT", tmp_path)
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="Missing or symlinked"):
        module.build(output)
    assert not output.exists()
