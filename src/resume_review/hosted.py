"""Owner-authenticated Plow companion, behind the fixed local plugin bridge."""

from __future__ import annotations

import argparse
import asyncio
import hmac
import re
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from .api.deps import ApiConfig
from .auth import session_cookie_name
from .bootstrap.registry import HostRegistry
from .cli import _resolve_instance
from .helper import build_desktop_app
from .models import Role
from .security.untrusted import escape_html

INSTANCE_PATH = re.compile(r"^/api/v1/instances/(inst_[a-f0-9]+)(?:/|$)")
OWNER = re.compile(r"^[A-Za-z0-9_-]{1,100}$")


def create_hosted_app(*, bridge_secret: str, public_origin: str, preview_path: Path | None = None):
    """Issue per-instance human sessions only after the Gateway authenticates its owner."""
    if not re.fullmatch(r"[a-f0-9]{64}", bridge_secret):
        raise ValueError("A private bridge secret is required")
    origin = httpx.URL(public_origin)
    if origin.scheme != "https" and not (origin.scheme == "http" and origin.host in ("127.0.0.1", "localhost")):
        raise ValueError("Hosted public origin must use HTTPS")
    if origin.path != "/" or origin.query or origin.userinfo:
        raise ValueError("Expected an origin, not a URL path")
    apps = {}
    mutex = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            for child, context, db in apps.values():
                await context.__aexit__(None, None, None)
                db.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def authenticate_bridge(request, call_next):
        secret = request.headers.get("x-recruiteragent-bridge", "")
        owner = request.headers.get("x-recruiteragent-owner", "")
        if not hmac.compare_digest(secret.encode(), bridge_secret.encode()) or not OWNER.fullmatch(owner):
            return JSONResponse({"error": "Owner authentication required"}, status_code=401)
        if request.method not in ("GET", "HEAD") and request.headers.get("origin") != public_origin:
            return JSONResponse({"error": "Origin rejected"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.get("/recruiteragent/")
    @app.get("/recruiteragent")
    def landing():
        entries = HostRegistry().load().get("instances", {})
        links = []
        for instance_id, entry in entries.items():
            if not re.fullmatch(r"inst_[a-f0-9]+", instance_id):
                continue
            label = escape_html(Path(entry["canonical_root"]).name)
            links.append(f'<li><a href="/api/v1/instances/{instance_id}/review">{label}</a></li>')
        jobs = "<ul>" + "".join(links) + "</ul>" if links else "<p>No job workspaces yet. Ask RecruiterAgent to set up your requisition and resume folder.</p>"
        return HTMLResponse('<!doctype html><html><head><meta charset="utf-8"><title>RecruiterAgent</title><style>body{font:16px system-ui;max-width:860px;margin:64px auto;padding:24px;color:#173c49;background:#fafbf9}a{color:#176b61}li{margin:16px 0}</style></head><body><h1>RecruiterAgent</h1><p>Cut through application volumes with evidence-backed review and human curation.</p><h2>Your job workspaces</h2>' + jobs + '<p><a href="/recruiteragent/demo">Explore the 200-candidate synthetic design demo</a></p><p>Workspaces retain decisions and evidence. Analysis requires an approved restricted route. File organization requires your approval of an exact plan.</p></body></html>', headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"})

    @app.get("/recruiteragent/demo")
    def demo():
        if preview_path is None or not preview_path.is_file():
            return JSONResponse({"error": "Synthetic preview unavailable"}, status_code=503)
        return HTMLResponse(preview_path.read_text(encoding="utf-8"), headers={"Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'none'; frame-ancestors 'none'; base-uri 'none'"})

    async def child_for(instance_id):
        async with mutex:
            if instance_id not in apps:
                root, db, repo = _resolve_instance(instance_id)
                try:
                    child = build_desktop_app(repo, root, api_config=ApiConfig(
                        allowed_hosts=("127.0.0.1", "localhost"), allowed_origins=(public_origin,),
                        require_origin_for_mutations=True,
                    ))
                    context = child.router.lifespan_context(child)
                    await context.__aenter__()
                    apps[instance_id] = (child, context, db)
                except BaseException:
                    db.close()
                    raise
            return apps[instance_id][0]

    @app.api_route("/api/v1/instances/{instance_id}/{suffix:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])
    async def dispatch(instance_id: str, suffix: str, request: Request):
        from .errors import ResumeReviewError

        if not INSTANCE_PATH.match(request.url.path):
            return JSONResponse({"error": "Unknown instance"}, status_code=404)
        try:
            child = await child_for(instance_id)
        except ResumeReviewError as exc:
            return JSONResponse({"error": exc.code}, status_code=exc.http_status)
        owner = "human:plow:" + request.headers["x-recruiteragent-owner"]
        runtime = child.state.runtime
        cookie_name = session_cookie_name(instance_id)
        presented = request.cookies.get(cookie_name)
        session = None
        if presented:
            try:
                session = runtime.sessions.resolve(presented, instance_id=instance_id)
                if session.actor_ref != owner:
                    return JSONResponse({"error": "Session owner mismatch"}, status_code=403)
            except ResumeReviewError:
                session = None
        minted = False
        if request.method == "GET" and suffix == "review" and session is None:
            session = runtime.sessions.issue(instance_id, owner, Role.ADMINISTRATOR, is_local_owner=True)
            minted = True
        headers = {k: v for k, v in request.headers.items() if k.lower() not in (
            "authorization", "host", "x-recruiteragent-bridge", "x-recruiteragent-owner", "content-length",
        )}
        if minted:
            cookies = dict(request.cookies)
            cookies[cookie_name] = session.session_id
            headers["cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        body = await request.body()
        if len(body) > 2 * 1024 * 1024:
            return JSONResponse({"error": "Request too large"}, status_code=413)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=child), base_url="http://127.0.0.1") as client:
            result = await client.request(request.method, str(request.url.path) + ("?" + request.url.query if request.url.query else ""), headers=headers, content=body)
        response = Response(result.content, status_code=result.status_code, headers={k: v for k, v in result.headers.items() if k.lower() not in ("content-length", "transfer-encoding", "set-cookie")})
        if minted:
            response.set_cookie(cookie_name, session.session_id, httponly=True, secure=origin.scheme == "https", samesite="strict", path=f"/api/v1/instances/{instance_id}")
        return response

    return app


def main():
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--bridge-secret-file", type=Path, required=True)
    parser.add_argument("--public-origin", required=True)
    parser.add_argument("--port", type=int, default=8775)
    parser.add_argument("--preview", type=Path)
    args = parser.parse_args()
    if args.bridge_secret_file.is_symlink() or args.bridge_secret_file.stat().st_mode & 0o077:
        raise ValueError("Bridge secret must be a private regular file")
    secret = args.bridge_secret_file.read_text().strip()
    uvicorn.run(create_hosted_app(bridge_secret=secret, public_origin=args.public_origin, preview_path=args.preview), host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
