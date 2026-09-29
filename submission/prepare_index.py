"""Validate listing assets and print commands; never register or send usage."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
from pathlib import Path
from urllib.parse import urlsplit
from urllib.error import HTTPError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
CLIENT_REPO = "https://raw.githubusercontent.com/plow-pbc/agent-index-client"


def registration_command(data: dict, base: Path = ROOT) -> list[str]:
    missing = [key for key in ("agent", "name", "blurb", "repo", "video", "image", "install_url")
               if not isinstance(data.get(key), str) or not data[key].strip()]
    if missing:
        raise ValueError("Fill these listing fields first: " + ", ".join(missing))
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", data["agent"]):
        raise ValueError("agent must be a lowercase slug")
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", data["video"]):
        raise ValueError("video must be the 11-character YouTube ID, not a URL")
    for key in ("repo", "image", "install_url"):
        parsed = urlsplit(data[key])
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError(key + " must be a public HTTPS URL without credentials")
        if parsed.hostname in ("localhost", "example.com", "127.0.0.1"):
            raise ValueError(key + " is still a placeholder or local URL")
    logo = (base / data.get("logo", "public/recruiteragent-logo.png")).resolve()
    if not logo.is_file():
        raise ValueError("Logo file is missing")
    command = ["python3", "agent-index/agent_index_client.py", "--register"]
    for key, flag in (("agent", "--agent"), ("name", "--name"), ("blurb", "--blurb"),
                      ("runtime", "--runtime"), ("repo", "--repo"), ("video", "--video"),
                      ("image", "--image"), ("install_url", "--install-url")):
        command.extend([flag, data.get(key, "OpenClaw")])
    command.extend(["--logo", str(logo)])
    return command


def fetch_client() -> None:
    """Resolve upstream once, fetch by commit, and retain its original license."""
    directory = ROOT / "agent-index"
    names = {"agent_index_client.py": "standalone/agent_index_client.py",
             "LICENSE": "LICENSE", "NOTICE": "NOTICE"}
    if any((directory / name).exists() for name in names):
        raise ValueError("Client files already exist; inspect them before replacing")
    with urlopen("https://api.github.com/repos/plow-pbc/agent-index-client/commits/main",
                 timeout=30) as response:
        revision = json.load(response)["sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Upstream did not return a valid commit")
    # Fetch everything before writing so a failed download does not leave a partial install.
    contents = {}
    for name, remote in names.items():
        try:
            with urlopen(CLIENT_REPO + "/" + revision + "/" + remote, timeout=30) as response:
                contents[name] = response.read()
        except HTTPError as exc:
            if name != "NOTICE" or exc.code != 404:
                raise
    if b"OPENCLAW_STATE_DIR" not in contents["agent_index_client.py"]:
        raise ValueError("Fetched client lacks the required OpenClaw collector")
    directory.mkdir(parents=True, exist_ok=True)
    for name, content in contents.items():
        with (directory / name).open("xb") as stream:
            stream.write(content)
    (directory / "upstream-manifest.json").write_text(json.dumps({
        "repository": "https://github.com/plow-pbc/agent-index-client",
        "commit": revision,
        "sha256": {name: hashlib.sha256(content).hexdigest() for name, content in contents.items()},
    }, indent=2), encoding="utf-8")
    print("Fetched upstream client revision " + revision)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch-client", action="store_true")
    args = parser.parse_args()
    try:
        if args.fetch_client:
            fetch_client()
        data = json.loads((ROOT / "listing.json").read_text(encoding="utf-8"))
        print(shlex.join(registration_command(data)))
        print("Review the command before running it on the OpenClaw host.")
    except (ValueError, OSError) as exc:
        print(str(exc))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
