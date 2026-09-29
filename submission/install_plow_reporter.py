"""Configure the dedicated reporter using an already installed RecruiterAgent skill."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

UPSTREAM_SHA256 = "5be521644ade0f041e83370ac457edc8ad85410e14265f1b1243807772de9a5b"
MARKER = "# RecruiterAgent dedicated Plow reporter launcher"


def configure(skill, state=Path("/var/lib/plow/recruiteragent-install"),
              target=Path("/opt/plow/agent-index-client.py")):
    from resume_review.storage.no_clobber import atomic_no_clobber_move, MoveOutcome

    skill, state, target = skill.resolve(), state.resolve(), target.resolve()
    python = skill / ".venv/bin/python"
    if not python.is_file():
        raise ValueError("Install the skill's Python application first")
    state.mkdir(parents=True, exist_ok=True)
    state.chmod(0o700)
    backup = target.with_name("agent-index-client.upstream.py")
    installed = target.is_file() and MARKER.encode() in target.read_bytes()[:200]
    original = backup if installed else target
    if hashlib.sha256(original.read_bytes()).hexdigest() != UPSTREAM_SHA256:
        raise ValueError("Unexpected upstream reporter; inspect it before replacing its launcher")
    module = state / "plow_reporter.py"
    content = Path(__file__).with_name("plow_reporter.py").read_bytes()
    if module.exists() and module.read_bytes() != content:
        raise ValueError("A different reporter module already exists; inspect it before updating")
    if not module.exists():
        with module.open("xb") as stream:
            stream.write(content)
        module.chmod(0o600)
    config_path = state / "reporting.json"
    if not config_path.exists():
        metadata = json.loads(Path(__file__).with_name("listing.json").read_text())
        metadata["logo"] = ("https://raw.githubusercontent.com/synchronic1/RecruiterAgent/"
                            "main/submission/public/recruiteragent-logo.png")
        config = {"state_root": "/var/lib/plow", "report_home": "/var/lib/plow/recruiteragent-index",
                  "upstream_path": str(backup), "upstream_sha256": UPSTREAM_SHA256,
                  "started_at_ms": int(time.time() * 1000),
                  "excluded_session_keys": ["agent:main:recruiteragent-setup"], "metadata": metadata}
        with config_path.open("x", encoding="utf-8") as stream:
            json.dump(config, stream, indent=2)
        config_path.chmod(0o600)
    if not installed:
        staged = target.with_name("recruiteragent-index-launcher.staged.py")
        launcher = (MARKER + "\nimport os, sys\n" +
                    "os.execv(" + repr(str(python)) + ", [" + repr(str(python)) + ", " +
                    repr(str(module)) + ", *sys.argv[1:]])\n")
        with staged.open("x", encoding="utf-8") as stream:
            stream.write(launcher)
        staged.chmod(0o755)
        move = atomic_no_clobber_move(target, backup, expected_sha256=UPSTREAM_SHA256)
        if move.outcome != MoveOutcome.MOVED:
            raise ValueError("Could not preserve upstream reporter: " + move.outcome)
        move = atomic_no_clobber_move(staged, target)
        if move.outcome != MoveOutcome.MOVED:
            restored = atomic_no_clobber_move(backup, target, expected_sha256=UPSTREAM_SHA256)
            raise ValueError("Could not activate reporter; restore outcome: " + restored.outcome)
    return {"ok": True, "agent_id": "recruiteragent", "launcher": str(target),
            "config": str(config_path), "schedule": "inherited Plow five-minute reporter",
            "upstream_preserved": str(backup), "setup_usage_excluded": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill", type=Path,
                        default=Path("/var/lib/plow/workspace/skills/recruiteragent"))
    args = parser.parse_args()
    try:
        print(json.dumps(configure(args.skill)))
    except (ValueError, OSError) as error:
        print(json.dumps({"ok": False, "error": str(error)}), file=sys.stderr)
        sys.exit(1)
