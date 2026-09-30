"""Install a pinned isolated runtime while building the Plow variant image."""
from pathlib import Path
import hashlib
import io
import os
import subprocess
import urllib.request
import zipfile

WHEEL = "uv-0.11.8-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"
SHA256 = "d97bb2920d6cddc07faa475013461294cc09b77ec8139278416c6e54b938d037"
ROOT = Path("/opt/recruiteragent")
SKILL = Path("/opt/plow/skills/recruiteragent")


def main():
    metadata_url = "https://pypi.org/pypi/uv/0.11.8/json"
    import json
    with urllib.request.urlopen(metadata_url, timeout=60) as response:
        metadata = json.load(response)
    match = next(item for item in metadata["urls"] if item["filename"] == WHEEL)
    if match["digests"]["sha256"] != SHA256:
        raise ValueError("Unexpected uv distribution hash")
    with urllib.request.urlopen(match["url"], timeout=120) as response:
        archive = response.read()
    if hashlib.sha256(archive).hexdigest() != SHA256:
        raise ValueError("Downloaded uv checksum mismatch")
    with zipfile.ZipFile(io.BytesIO(archive)) as wheel:
        binary = wheel.read("uv-0.11.8.data/scripts/uv")
    uv = ROOT / "uv"
    with uv.open("xb") as stream:
        stream.write(binary)
    uv.chmod(0o755)
    env = dict(os.environ, UV_PYTHON_INSTALL_DIR=str(ROOT / "python"),
               UV_NO_CACHE="1", PIP_NO_CACHE_DIR="1", PIP_DISABLE_PIP_VERSION_CHECK="1",
               PYTHONDONTWRITEBYTECODE="1")
    subprocess.run([str(uv), "python", "install", "3.12.13"], env=env, check=True)
    python = subprocess.check_output([str(uv), "python", "find", "3.12.13"], env=env, text=True).strip()
    subprocess.run([python, str(SKILL / "scripts/install.py"), "--demo"], env=env, check=True)
    subprocess.run([python, str(SKILL / "scripts/install.py"), "--verify-only"], env=env, check=True)
    prompt = Path("/opt/plow/prompt/AGENTS.md")
    with prompt.open("a", encoding="utf-8") as stream:
        stream.write("\n\n" + (ROOT / "prompt.md").read_text(encoding="utf-8") + "\n")
    marker = "  registerFull(api) {"
    for name, expected, relative in (
        ("index.ts", "f4141a4a810f5ad2643b80664e36b050eff12eb01a3658f5421740f13a7b803d", "./recruiteragent.mjs"),
        ("dist/index.js", "146b8f3fdc71acf8f9b5375139a59dac0adf80ed2b042cbdd5709a6376d0daa4", "../recruiteragent.mjs"),
    ):
        entry = Path("/opt/plow/plugin") / name
        if hashlib.sha256(entry.read_bytes()).hexdigest() != expected:
            raise ValueError("Pinned Plow plugin checksum mismatch")
        text = entry.read_text(encoding="utf-8")
        if text.count(marker) != 1:
            raise ValueError("Unexpected pinned Plow plugin registration contract")
        entry.write_text(f'import {{ registerCompanion }} from "{relative}";\n' + text.replace(
            marker, marker + '\n    if (api.registrationMode === "full") registerCompanion(api);', 1,
        ), encoding="utf-8")
    Path("/opt/plow/plugin/recruiteragent.mjs").write_bytes((ROOT / "companion.mjs").read_bytes())
    print("RecruiterAgent runtime, dependencies, and synthetic preview installed")


if __name__ == "__main__":
    main()
