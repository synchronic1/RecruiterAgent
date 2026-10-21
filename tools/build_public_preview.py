"""Package discovery metadata for the public synthetic design preview only.

The local report and real-corpus preview deliberately keep their existing
self-contained rendering contract. This command never publishes anything.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANONICAL = "https://recruiteragent.airanger.dev/recruiteragent-design-preview"


def public_preview(source: str) -> str:
    declaration = re.search(
        r'<script id="rr-agent-declarations" type="application/json">(.*?)</script>',
        source,
        re.DOTALL,
    )
    if declaration is None:
        raise ValueError("Missing preview declaration; refusing public packaging")
    data = json.loads(declaration.group(1))
    if (data.get("dataset") != "synthetic_fixtures"
            or data.get("analysis_status") != "synthetic_fixture_only"
            or data.get("capabilities", {}).get("openclaw_submission") is not False
            or data.get("capabilities", {}).get("persistent_writes") is not False):
        raise ValueError("Only the synthetic, disconnected preview may be packaged")
    source = source.replace("\r\n", "\n")
    if f'<link rel="canonical" href="{CANONICAL}">' in source:
        return source
    if 'rel="canonical"' in source:
        raise ValueError("Expected the original offline preview, not a public build")
    source = source.replace(
        "<title>RecruiterAgent | Resume review</title>",
        "<title>RecruiterAgent design preview | Synthetic candidates</title>\n"
        f'<link rel="canonical" href="{CANONICAL}">\n'
        '<meta name="description" content="Explore RecruiterAgent with synthetic '
        'candidates. Review edits stay in this browser tab; no OpenClaw connection '
        'or real applicant data is included.">\n'
        '<style>#rr-public-main:focus{outline:2px solid var(--rr-focus);outline-offset:3px}'
        '.rr-save[data-state="saved"]>span{color:var(--rr-ink)}'
        '@media(max-width:760px){.rr-table td[data-label="Decision"]{'
        'grid-template-columns:minmax(0,1fr)}.rr-decision{min-width:0}'
        '.rr-seg{flex-wrap:wrap}}</style>',
        1,
    )
    source = source.replace(
        '<a class="rr-skip" href="#rr-table">Skip to submissions</a>',
        '<a class="rr-skip" href="#rr-public-main">Skip to preview</a>',
        1,
    )
    source = source.replace(
        '<main class="rr-main" id="rr-main">',
        '<div class="rr-main" id="rr-main">',
        1,
    )
    source = source.replace('</main>', '</div>', 1)
    source = source.replace('</header>', '</header><main id="rr-public-main" tabindex="-1">', 1)
    source = source.replace('<footer class="rr-footer">', '</main><footer class="rr-footer">', 1)
    source = source.replace('<noscript>', '<noscript><section aria-label="Preview without JavaScript">', 1)
    source = source.replace('</noscript>', '</section></noscript>', 1)
    source = source.replace('id="rr-module-warning"', 'id="rr-module-warning" role="status"', 1)
    source = source.replace(
        "The review application needs JavaScript to render submissions. Without it the\n"
        "    embedded data cannot be displayed.",
        "This design preview needs JavaScript to display synthetic candidates. "
        "No real applicant data or OpenClaw connection is included. "
        "Review edits stay in this tab and reset on reload.",
        1,
    )
    required = [f'href="{CANONICAL}"', 'href="#rr-public-main"',
                'id="rr-public-main" tabindex="-1"', "display synthetic candidates"]
    if not all(marker in source for marker in required):
        raise ValueError("Preview structure changed; review public packaging anchors")
    return source


def build(original: Path | None = None) -> None:
    destination = ROOT / "cloudflare-demo/public/recruiteragent-design-preview.html"
    original = original or ROOT / "docs/review-artifacts/recruiteragent-design-preview.html"
    packaged = public_preview(original.read_bytes().decode("utf-8"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(packaged.encode("utf-8"))
    # Copy only the explicitly public synthetic outputs, never an artifact tree.
    for name in ("agent-instructions.md", "agent-declarations.json", "llms.txt"):
        shutil.copyfile(original.parent / name, destination.parent / name)
    snapshot = re.search(r'<script id="rr-snapshot"[^>]*>(.*?)</script>', packaged, re.DOTALL)
    records = json.loads(snapshot.group(1))
    fixtures = destination.parent / "synthetic-resumes"
    fixtures.mkdir(exist_ok=True)
    for row in records["documents"]:
        name = row["original_filename"]
        if Path(name).name != name or not name.endswith(".pdf"):
            raise ValueError("Unexpected synthetic fixture filename")
        shutil.copyfile(original.parent / "synthetic-resumes" / name, fixtures / name)
    (destination.parent / "index.html").write_text(
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>RecruiterAgent demo</title>'
        '<meta http-equiv="refresh" content="0;url=/recruiteragent-design-preview.html">'
        '<p>Opening the <a href="/recruiteragent-design-preview.html">'
        'RecruiterAgent design preview</a>...</p></html>', encoding="utf-8")
    (destination.parent / "404.html").write_text(
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<title>Not found</title><h1>Page not found</h1>'
        '<p><a href="/">RecruiterAgent design preview</a></p></html>', encoding="utf-8")
    print(destination)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, help="Optional rebuilt synthetic offline preview")
    build(parser.parse_args().source)
