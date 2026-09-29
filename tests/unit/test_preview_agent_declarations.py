"""Discovery declarations remain separate from applicant data and landing UI."""
import importlib.util
import json
from pathlib import Path
from html.parser import HTMLParser


class Elements(HTMLParser):
    def __init__(self):
        super().__init__()
        self.by_id = {}

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if "id" in attributes:
            self.by_id[attributes["id"]] = (tag, attributes)


def test_agent_discovery_is_hidden_and_does_not_claim_live_capabilities(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[2] / "tools/build_design_preview.py"
    spec = importlib.util.spec_from_file_location("preview_agent_test", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "OUTPUT", tmp_path / "preview.html")
    data = module.payload()
    data["documents"][0]["summary_text"] = '</script><h1>Applicant instructions</h1>'
    module.build(data, real_data=True)
    page = module.OUTPUT.read_text(encoding="utf-8")
    elements = Elements()
    elements.feed(page)
    for identifier in ("rr-tab-agents", "rr-panel-agents"):
        assert "hidden" in elements.by_id[identifier][1]
    assert "data-workspace-tab" not in elements.by_id["rr-tab-agents"][1]
    assert "<h1>Applicant instructions</h1>" not in page
    declaration = json.loads((tmp_path / "agent-declarations.json").read_text())
    assert declaration["skill_name"] == "RecruiterAgent"
    assert declaration["instructions_tab"].endswith("#agent-instructions")
    assert declaration["document_count"] == 200
    assert declaration["capabilities"]["persistent_writes"] is False
    assert declaration["capabilities"]["openclaw_submission"] is False
    assert declaration["collaboration"]["enforced_lock_service"] is False
    instructions = (tmp_path / "agent-instructions.md").read_text()
    assert "Only one writer" in instructions
    assert "untrusted data" in instructions
    assert (tmp_path / "llms.txt").exists()
