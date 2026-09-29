"""Submission preparation validates real metadata without publishing it."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def complete_listing(tmp_path):
    (tmp_path / "logo.png").write_bytes(b"fixture")
    return dict(agent="recruiteragent", name="RecruiterAgent", blurb="Human-curated review",
                runtime="OpenClaw", repo="https://github.com/owner/recruiteragent",
                video="abcdefghijk", image="https://assets.test/demo.png",
                install_url="https://assets.test/install", logo="logo.png")


def test_registration_preserves_blurb_as_one_argument(tmp_path):
    module = load("submission/prepare_index.py", "submission_prepare_test")
    data = complete_listing(tmp_path)
    command = module.registration_command(data, tmp_path)
    assert command[command.index("--blurb") + 1] == data["blurb"]
    assert command[command.index("--video") + 1] == "abcdefghijk"
    assert "--register" in command


def test_registration_allows_video_to_be_added_later(tmp_path):
    module = load("submission/prepare_index.py", "submission_prepare_test")
    data = complete_listing(tmp_path)
    data["video"] = ""
    command = module.registration_command(data, tmp_path)
    assert "--video" not in command
    assert command[command.index("--image") + 1] == data["image"]
    assert "--register" in command


@pytest.mark.parametrize("key,value", [("repo", ""), ("video", "https://youtube.com/watch?v=abcdefghijk"),
                                      ("install_url", "http://localhost/install"),
                                      ("image", "https://example.com/demo.png")])
def test_registration_refuses_incomplete_or_placeholder_metadata(tmp_path, key, value):
    module = load("submission/prepare_index.py", "submission_prepare_test")
    data = complete_listing(tmp_path)
    data[key] = value
    with pytest.raises(ValueError):
        module.registration_command(data, tmp_path)


def test_github_tree_excludes_local_data_and_can_build_skill(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "tools"))
    module = load("tools/build_public_source.py", "public_source_test")
    output = module.build(tmp_path / "github")
    manifest = json.loads((output / "public-source-manifest.json").read_text())
    assert manifest["applicant_data_included"] is False
    assert (output / "LICENSE").read_text().startswith("MIT License")
    assert (output / "skill/SKILL.md").is_file()
    assert (output / "tools/build_openclaw_package.py").is_file()
    for relative in manifest["sha256"]:
        assert not set(Path(relative).parts).intersection({"resume", "datasets", ".venv", "review-artifacts"})
        assert Path(relative).suffix not in {".pdf", ".db", ".sqlite", ".exe", ".dll", ".pyd", ".pyc"}
    with pytest.raises(ValueError, match="already exists"):
        module.build(output)
