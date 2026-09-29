"""Use Plow's inherited schedule for a dedicated RecruiterAgent usage report."""
from __future__ import annotations

import datetime
import glob
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

CONFIG = Path("/var/lib/plow/recruiteragent-install/reporting.json")


def collect_openclaw(client, config, days):
    """Read token counters after installation, excluding administrative setup."""
    oldest = datetime.date.today() - datetime.timedelta(days=days)
    since = max(config["started_at_ms"], int(datetime.datetime.combine(
        oldest, datetime.time.min).timestamp() * 1000))
    stores = sorted(glob.glob(str(Path(config["state_root"]) /
                                  "agents/*/agent/openclaw-agent.sqlite")))
    if not stores:
        client.FAILURES.append("openclaw: configured usage store is missing")
        return {}
    out, seen = {}, set()
    excluded = config["excluded_session_keys"]
    for store in stores:
        try:
            with sqlite3.connect(Path(store).resolve().as_uri() + "?mode=ro", uri=True) as db:
                orphaned = db.execute("""SELECT count(*) FROM transcript_events e
                                        LEFT JOIN session_windows w ON w.session_id = e.session_id
                                        WHERE e.created_at >= ? AND w.session_id IS NULL""",
                                      (since,)).fetchone()[0]
                if orphaned:
                    raise ValueError("Usage events have no session identity")
                placeholders = ",".join("?" for _ in excluded)
                query = """SELECT e.event_json, e.event_zstd, e.event_utf8_bytes, e.created_at
                           FROM transcript_events e
                           JOIN session_windows w ON w.session_id = e.session_id
                           WHERE e.created_at >= ?"""
                if excluded:
                    query += " AND w.session_key NOT IN (" + placeholders + ")"
                rows = db.execute(query, (since, *excluded)).fetchall()
            for raw, compressed, size, created in rows:
                event = json.loads(client._event_json(raw, compressed, size)) or {}
                message = event.get("message") or {}
                usage = message.get("usage")
                if not isinstance(usage, dict):
                    continue
                stamp = client._stamp_ms(event.get("timestamp"), created)
                if stamp < config["started_at_ms"]:
                    continue
                response = message.get("responseId")
                if response is not None:
                    if response in seen:
                        continue
                    seen.add(response)
                day = datetime.datetime.fromtimestamp(stamp / 1000).date().isoformat()
                model = message.get("model") or "unknown"
                counters = out.setdefault(day, {}).setdefault(model, dict.fromkeys(client.KEYS, 0))
                for key, field in (("input", "input"), ("output", "output"),
                                   ("cache_read", "cacheRead"), ("cache_write", "cacheWrite")):
                    value = usage.get(field)
                    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                        counters[key] += value
        except (sqlite3.Error, ValueError, TypeError, RuntimeError, OSError):
            client.FAILURES.append("openclaw: configured usage store could not be read completely")
            return {}
    return out


def report_arguments(argv, metadata):
    argv = list(argv)
    if "--agent" in argv:
        position = argv.index("--agent") + 1
        if position == len(argv) or argv[position] not in ("openclaw", "recruiteragent"):
            raise ValueError("This reporter is dedicated to recruiteragent")
        argv[position] = "recruiteragent"
    if "--register" in argv:
        for key, flag in (("name", "--name"), ("blurb", "--blurb"), ("repo", "--repo"),
                          ("runtime", "--runtime"), ("image", "--image"),
                          ("logo", "--logo"), ("install_url", "--install-url")):
            if flag not in argv and metadata.get(key):
                argv.extend((flag, metadata[key]))
    return argv


def main(argv):
    config = json.loads(CONFIG.read_text())
    upstream = Path(config["upstream_path"])
    if hashlib.sha256(upstream.read_bytes()).hexdigest() != config["upstream_sha256"]:
        raise ValueError("Upstream client changed; inspect it before reporting")
    home = Path(config["report_home"])
    home.mkdir(parents=True, exist_ok=True)
    home.chmod(0o700)
    os.environ.update(HOME=str(home), OPENCLAW_STATE_DIR=config["state_root"],
                      AGENT_ID="recruiteragent")
    spec = importlib.util.spec_from_file_location("recruiteragent_upstream_index", upstream)
    client = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(client)
    client.from_agentsview = lambda days: {}
    client.from_hermes = lambda days: {}
    client.from_openclaw = lambda days: collect_openclaw(client, config, days)
    calls = []
    post = client._post

    def observe(url, *args, **kwargs):
        status, payload = post(url, *args, **kwargs)
        if "/v1/usage?" in url:
            calls.append({"endpoint": "usage", "status": status})
        return status, payload

    client._post = observe
    result = 1
    try:
        try:
            result = client.main(report_arguments(argv, config["metadata"])) or 0
        except SystemExit as exc:
            result = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
            if isinstance(exc.code, str):
                print(exc.code, file=sys.stderr)
        return result
    finally:
        record = {"finished_at_ms": int(time.time() * 1000), "exit_code": result,
                  "mode": "status" if argv[:1] == ["status"] else (
                      "register" if "--register" in argv else (
                          "dry-run" if "--dry-run" in argv else "report")),
                  "usage_requests": calls}
        fd = os.open(home / "run-events.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as stream:
            stream.write(json.dumps(record) + "\n")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
