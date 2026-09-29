"""``POST /backup`` -- create a consistent workspace backup (PRD section 12.1).

Authority: PRD section 12.1 (allowed principal: administrator), section 12.2
(retryable POST requires ``Idempotency-Key``; 202 for long work), and section 6
("Keep SQLite on supported local storage").

How the backup is taken
-----------------------

The backup uses SQLite's online backup API, the one supported way to copy a live
database without a torn read: the API takes a consistent snapshot while other
connections continue. A plain file copy of a database that is mid-write is not a
backup and is never used here.

The copy is destination-local, next to the database (``<db-dir>/backups/``, which
is ``.review/backups`` in a provisioned workspace), then reopened and checked with
``PRAGMA integrity_check``. A copy that fails the check is unlinked and reported
as a conflict; the endpoint never returns a backup it has not verified.

This is an administrator-only, synchronous operation. For the workspace sizes the
PRD names (hundreds of submissions) the copy is short; the response is the verified
result, not a job id.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from ..auth import require_role
from ..errors import Code, Conflict
from ..models import Role
from ..util import new_id, now_iso
from .deps import InstanceContext, get_request_id, require_mutation
from .envelope import ok_response
from .idempotency import IdempotencyGuard, idempotency_guard

__all__ = ["BackupRequest", "register", "take_backup"]

_ROUTE = "backup"


class BackupRequest(BaseModel):
    """An empty, strict body: a backup can carry no caller-chosen destination."""

    model_config = ConfigDict(extra="forbid")


def take_backup(database: Any) -> dict[str, Any]:
    """Copy ``database`` to a verified backup file and return its metadata.

    Raises :class:`Conflict` (and removes the partial copy) when the copy does not
    pass an integrity check. The destination directory is derived from the live
    database file's own directory, so the backup always lands on the same local
    volume as the database it copies.
    """
    db_file = Path(database.path)
    backups = db_file.parent / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    stamp = now_iso().replace(":", "").replace("-", "").split(".")[0]
    suffix = new_id("tmp").split("_")[-1][:8]
    target = backups / f"review-{stamp}-{suffix}.db"

    source = database.connect()
    destination = sqlite3.connect(str(target))
    try:
        source.backup(destination)
    finally:
        destination.close()

    check = sqlite3.connect(str(target))
    try:
        row = check.execute("PRAGMA integrity_check").fetchone()
        verified = bool(row) and str(row[0]).lower() == "ok"
    finally:
        check.close()

    if not verified:
        try:
            target.unlink()
        except OSError:  # pragma: no cover - cleanup only
            pass
        raise Conflict(
            "The backup did not pass an integrity check and was discarded.",
            code=Code.SNAPSHOT_PUBLISH_FAILED,
            detail={"reason": "backup_integrity_failed"},
        )

    return {
        "backup_file": target.name,
        "byte_size": int(target.stat().st_size),
        "verified": True,
        "integrity": "ok",
    }


def register(router: APIRouter) -> None:
    @router.post("/backup", name="backup", response_class=JSONResponse)
    def create_backup(
        payload: BackupRequest | None = None,
        guard: IdempotencyGuard = Depends(idempotency_guard(_ROUTE)),
        ctx: InstanceContext = Depends(require_mutation),
        request_id: str = Depends(get_request_id),
    ) -> JSONResponse:
        require_role(ctx.principal, Role.ADMINISTRATOR)
        body = payload.model_dump() if payload is not None else {}

        hit = guard.replay(body)
        if hit is not None:
            return ok_response(
                hit.response,
                request_id=request_id,
                instance_id=ctx.instance_id,
                state_revision=hit.state_revision,
            )

        data = take_backup(ctx.db)
        guard.commit(body, response=data)
        return ok_response(
            data,
            request_id=request_id,
            instance_id=ctx.instance_id,
            state_revision=ctx.state_revision,
        )
