"""Local-owner launch and authenticated connected dashboard (ADR 0003)."""

from __future__ import annotations

from typing import Any

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..auth import PairingManager, session_cookie_name
from ..auth.guards import check_pairing_rate
from ..models import Role
from ..reporting.connected import connected_page
from ..reporting.snapshot import load_assets
from .app import API_PREFIX
from .deps import InstanceContext, current_session, get_request_id, require_operation
from .envelope import ok_response


def install_desktop_routes(app: Any, worker: Any) -> None:
    runtime = app.state.runtime
    pairing = PairingManager(runtime.instance_id, runtime.sessions)
    app.state.desktop_pairing = pairing

    @app.get("/pair", include_in_schema=False)
    def pair(request: Request, token: str):
        check_pairing_rate(request.client.host if request.client else "local")
        session = pairing.exchange(token)
        response = RedirectResponse(f"/api/v1/instances/{runtime.instance_id}/review", status_code=303)
        response.set_cookie(
            session_cookie_name(runtime.instance_id), session.session_id,
            httponly=True, samesite="strict", path=f"/api/v1/instances/{runtime.instance_id}",
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get(API_PREFIX + "/review", include_in_schema=False)
    def review(
        ctx: InstanceContext = Depends(require_operation(Role.VIEWER)),
        session=Depends(current_session),
    ):
        html = connected_page(
            instance_id=ctx.instance_id, role=ctx.principal.role.value,
            csrf_token=runtime.csrf_store.issue(session.session_id),
        )
        return HTMLResponse(html, headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; img-src data:; connect-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'self'",
        })

    @app.get(API_PREFIX + "/assets/{name}", include_in_schema=False)
    def asset(name: str, ctx: InstanceContext = Depends(require_operation(Role.VIEWER))):
        from ..errors import NotFound

        if name not in ("report.css", "report.js"):
            raise NotFound("That asset does not exist.")
        key = "css" if name.endswith(".css") else "js"
        return Response(load_assets()[key], media_type="text/css" if key == "css" else "text/javascript")

    @app.get(API_PREFIX + "/connection", include_in_schema=False)
    def connection(
        ctx: InstanceContext = Depends(require_operation(Role.VIEWER)),
        request_id: str = Depends(get_request_id),
    ):
        return ok_response(worker.describe(), request_id=request_id, instance_id=ctx.instance_id, state_revision=ctx.state_revision)
