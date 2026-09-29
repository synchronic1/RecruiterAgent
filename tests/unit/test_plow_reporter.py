"""The hosted reporter counts product engagement without setup or duplicate usage."""
import importlib.util
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace

import pytest


def reporter():
    path = Path(__file__).resolve().parents[2] / "submission/plow_reporter.py"
    spec = importlib.util.spec_from_file_location("plow_reporter_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture(tmp_path):
    database = tmp_path / "agents/main/agent/openclaw-agent.sqlite"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE session_windows (session_id TEXT PRIMARY KEY, session_key TEXT)")
        db.execute("CREATE TABLE transcript_events (session_id TEXT, event_json TEXT, "
                   "event_zstd BLOB, event_utf8_bytes INTEGER, created_at INTEGER)")
        db.executemany("INSERT INTO session_windows VALUES (?,?)",
                       [("user", "agent:main:main"), ("setup", "agent:main:recruiteragent-setup")])
    client = SimpleNamespace(KEYS=("input", "output", "cache_read", "cache_write"), FAILURES=[],
                             _event_json=lambda raw, compressed, size: raw,
                             _stamp_ms=lambda stamp, created: stamp if stamp is not None else created)
    return database, client, {"state_root": str(tmp_path), "started_at_ms": int(time.time() * 1000),
                              "excluded_session_keys": ["agent:main:recruiteragent-setup"]}


def add(database, session, response, created, stamp=None):
    event = {"message": {"responseId": response, "model": "test-model",
                         "usage": {"input": 7, "output": 3, "cacheRead": 2}}}
    if stamp is not None:
        event["timestamp"] = stamp
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO transcript_events VALUES (?, ?, NULL, NULL, ?)",
                   (session, json.dumps(event), created))


def test_counts_only_new_engagement_without_setup_or_duplicates(tmp_path):
    database, client, config = fixture(tmp_path)
    start = config["started_at_ms"]
    add(database, "user", "old", start - 100)
    add(database, "user", "late-copy-of-old-event", start + 300, start - 100)
    add(database, "setup", "installation", start + 400)
    add(database, "user", "new", start + 500)
    add(database, "user", "new", start + 600)
    module = reporter()
    result = module.collect_openclaw(client, config, 28)
    models = next(iter(result.values()))
    assert models["test-model"] == {"input": 7, "output": 3, "cache_read": 2, "cache_write": 0}
    assert client.FAILURES == []
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT count(*) FROM transcript_events").fetchone()[0] == 5


def test_unreadable_or_missing_store_fails_closed(tmp_path):
    module = reporter()
    database, client, config = fixture(tmp_path)
    database.write_bytes(b"corrupt database")
    assert module.collect_openclaw(client, config, 28) == {}
    assert client.FAILURES
    client.FAILURES.clear()
    config["state_root"] = str(tmp_path / "missing")
    assert module.collect_openclaw(client, config, 28) == {}
    assert client.FAILURES


def test_unmapped_usage_cannot_be_reported_as_a_partial_total(tmp_path):
    database, client, config = fixture(tmp_path)
    add(database, "unmapped", "unknown-session", config["started_at_ms"] + 100)
    assert reporter().collect_openclaw(client, config, 28) == {}
    assert client.FAILURES


def test_inherited_openclaw_argument_maps_to_product_and_preserves_metadata():
    module = reporter()
    assert module.report_arguments(["--agent", "openclaw", "--days", "7"], {}) == [
        "--agent", "recruiteragent", "--days", "7"]
    args = module.report_arguments(["--register", "--name", "Existing name"],
                                   {"name": "RecruiterAgent", "blurb": "Human curation"})
    assert args[args.index("--name") + 1] == "Existing name"
    assert args[args.index("--blurb") + 1] == "Human curation"
    with pytest.raises(ValueError):
        module.report_arguments(["--agent", "unrelated"], {})


def installer():
    path = Path(__file__).resolve().parents[2] / "submission/install_plow_reporter.py"
    spec = importlib.util.spec_from_file_location("plow_installer_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_configuration_preserves_upstream_and_reporting_cutoff(tmp_path, monkeypatch):
    module = installer()
    skill = tmp_path / "skill"
    python = skill / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("fixture")
    target = tmp_path / "opt/agent-index-client.py"
    target.parent.mkdir()
    original = b"# upstream fixture\n"
    target.write_bytes(original)
    monkeypatch.setattr(module, "UPSTREAM_SHA256", hashlib.sha256(original).hexdigest())
    state = tmp_path / "state"
    first = module.configure(skill, state, target)
    config = (state / "reporting.json").read_bytes()
    assert Path(first["upstream_preserved"]).read_bytes() == original
    assert module.MARKER in target.read_text()
    assert module.configure(skill, state, target) == first
    assert (state / "reporting.json").read_bytes() == config


def test_configuration_refuses_to_replace_an_unknown_reporter(tmp_path):
    module = installer()
    skill = tmp_path / "skill"
    python = skill / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("fixture")
    target = tmp_path / "foreign.py"
    target.write_text("unrelated script")
    with pytest.raises(ValueError, match="Unexpected upstream"):
        module.configure(skill, tmp_path / "state", target)
    assert target.read_text() == "unrelated script"
