"""Render the installed dashboard shell with a non-Gateway bootstrap record."""

from importlib import resources

from ..models import canonical_json
from ..security.untrusted import escape_html, escape_json_for_html


def connected_page(*, instance_id: str, role: str, csrf_token: str) -> str:
    shell = resources.files("resume_review").joinpath("templates/report.html").read_text(encoding="utf-8")
    bootstrap = escape_json_for_html(canonical_json({
        "mode": "connected", "instance_id": instance_id,
        "api_base": "/api/v1/instances", "role": role, "desktop_companion": True,
    }))
    shell = shell.replace(
        '<script id="rr-bootstrap" type="application/json" data-report-bootstrap></script>',
        f'<script id="rr-bootstrap" type="application/json" data-report-bootstrap>{bootstrap}</script>',
    )
    shell = shell.replace('<meta charset="utf-8">', f'<meta charset="utf-8"><meta name="csrf-token" content="{escape_html(csrf_token)}">')
    shell = shell.replace('id="rr-connection-status" class="rr-status" role="status" hidden', 'id="rr-connection-status" class="rr-status" role="status"')
    shell = shell.replace('id="rr-criteria-editor" hidden', 'id="rr-criteria-editor"')
    base = f"/api/v1/instances/{escape_html(instance_id)}/assets"
    return shell.replace("../assets/report.css", f"{base}/report.css").replace("../assets/report.js", f"{base}/report.js")
