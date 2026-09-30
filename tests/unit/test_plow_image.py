"""Image context isolation is checked without Docker or applicant data."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_image_context_has_only_allowlisted_source(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "tools"))
    spec = importlib.util.spec_from_file_location("plow_image_builder_test", ROOT / "tools/build_plow_image.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = module.build(tmp_path / "context")
    manifest = json.loads((output / "image-context-manifest.json").read_text())
    assert manifest["applicant_data_included"] is False
    assert manifest["host_dependencies_included"] is False
    for name in manifest["sha256"]:
        assert not set(Path(name).parts).intersection({"resume", "datasets", ".venv", ".git", "review-artifacts"})
        assert Path(name).suffix not in {".pdf", ".db", ".sqlite", ".secret", ".exe", ".dll", ".pyd", ".pyc"}
    assert (output / "skills/recruiteragent/application/requirements.txt").is_file()
    assert (output / "skills/recruiteragent/application/branding/recruiteragent-fold-logo-light.svg").is_file()
    with pytest.raises(ValueError, match="already exists"):
        module.build(output)
