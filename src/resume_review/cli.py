"""Product command line interface (PRD section 5.3).

The ``resume-review`` console script declared in ``pyproject.toml`` points here;
before this module existed that entry point died with an ImportError.

Every command is a thin, honest wrapper over the frozen domain modules:

* ``setup``          -> :func:`resume_review.bootstrap.setup.setup_instance`
* ``start``          -> the HTTP API package, when one is installed
* ``status``         -> :class:`resume_review.db.Repository.status_counts`
* ``scan``           -> :class:`resume_review.analysis.pipeline.Pipeline.scan`
* ``summarize``      -> :class:`resume_review.analysis.queue.AnalysisQueue`
* ``render``         -> :mod:`resume_review.reporting`
* ``plan-actions``   -> :func:`resume_review.actions.planner.plan_actions`
* ``apply-actions``  -> :func:`resume_review.actions.executor.apply_batch`
* ``backup``         -> the SQLite online backup API plus an integrity check
* ``repair``         -> :func:`resume_review.actions.recovery.plan_recovery`
* ``stop``           -> the OS-backed instance lock

Two behavioral rules are deliberate and load-bearing:

1. ``apply-actions`` moves files only for an approval already recorded through the
   review interface. The CLI exposes no approve command: constructing a plan is a
   request, and a request is not consent. ``apply_batch`` refuses a batch with no
   stored, unexpired, plan-hash-matched approval (PRD 13.1).
2. Where a capability genuinely does not exist yet -- no HTTP API package, no
   approved inference route -- the command fails with a stable non-zero exit code
   and a message that says so. It never reports success it did not achieve.

Machine-readable output (``--json``; on by default for ``status``) is an envelope
with the keys ``ok``, ``code``, ``instance_id``, ``data``, ``warnings`` and
``request_id``, matching ``schemas/api_envelope.schema.json`` in spirit.

Process exit codes (PRD 5.3):

    ======  ======================================================
    code    meaning
    ======  ======================================================
    0       success
    1       unexpected internal failure
    2       invalid input / not found / refused downgrade
    3       permission failure
    4       conflict (revision, stale plan, missing approval, ...)
    5       dependency failure (no API, no analysis route, ...)
    6       unsupported storage topology
    ======  ======================================================
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from enum import IntEnum
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import SCHEMA_VERSION, __version__
from .errors import (
    Code,
    Conflict,
    DependencyUnavailable,
    InvalidInput,
    NotFound,
    ResumeReviewError,
    exit_code_for,
)
from .util import new_id, now_iso

__all__ = ["ExitCode", "main", "build_parser"]

#: Actor reference recorded for CLI-initiated writes. It never starts with
#: ``agent:`` or ``worker:``, so :meth:`Repository.approve_batch` would refuse it
#: as an approver; the CLI never approves anyway.
LOCAL_ACTOR = "local-owner"

#: Env vars read to configure a live analysis route for ``summarize``. The names
#: match the live integration suite so an operator configures a route once.
LIVE_ENV = (
    "RESUME_REVIEW_LIVE_BASE_URL",
    "RESUME_REVIEW_LIVE_AGENT_ID",
    "RESUME_REVIEW_LIVE_SECRET_FILE",
    "RESUME_REVIEW_LIVE_ROUTE",
    "RESUME_REVIEW_LIVE_ATTESTATION",
    "RESUME_REVIEW_LIVE_PROVIDER_RECORD",
)


class ExitCode(IntEnum):
    """Stable process exit codes, grouped by the failure class PRD 5.3 requires.

    The numeric values match :data:`resume_review.errors.ExitCode`; this IntEnum
    is the type the CLI returns from :func:`main`.
    """

    OK = 0
    UNEXPECTED = 1
    INVALID_INPUT = 2
    PERMISSION = 3
    CONFLICT = 4
    DEPENDENCY = 5
    UNSUPPORTED_STORAGE = 6


# ---------------------------------------------------------------------------
# Envelope helpers
# ---------------------------------------------------------------------------
def _warning(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {
            "code": str(value.get("code") or "WARNING"),
            "message": str(value.get("message") or ""),
            "detail": dict(value.get("detail") or {}),
        }
    return {"code": "WARNING", "message": str(value), "detail": {}}


def _envelope(
    *,
    ok: bool,
    code: str,
    instance_id: str | None = None,
    data: Any = None,
    warnings: Sequence[Any] = (),
    request_id: str = "",
    state_revision: int | None = None,
    error: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    env: dict[str, Any] = {
        "ok": bool(ok),
        "code": str(code),
        "instance_id": instance_id,
        "data": data,
        "warnings": [_warning(w) for w in warnings],
        "request_id": request_id,
    }
    if state_revision is not None:
        env["state_revision"] = int(state_revision)
    if error is not None:
        env["error"] = dict(error)
    return env


def _error_envelope(exc: ResumeReviewError, *, request_id: str, instance_id: str | None = None) -> dict[str, Any]:
    return _envelope(
        ok=False,
        code=exc.code,
        instance_id=instance_id,
        data=None,
        warnings=[],
        request_id=request_id,
        error={
            "code": exc.code,
            "message": exc.message,
            "retryable": bool(exc.retryable),
            "detail": dict(exc.detail),
        },
    )


def _exit_for(code: str) -> ExitCode:
    try:
        return ExitCode(int(exit_code_for(code)))
    except ValueError:  # pragma: no cover - exit_code_for always returns a mapped int
        return ExitCode.UNEXPECTED


# ---------------------------------------------------------------------------
# Instance resolution
# ---------------------------------------------------------------------------
def _resolve_instance(instance_id: str):
    """Return ``(root, database, repository)`` for a registered instance.

    The root comes from the host registry, never from the caller: a caller cannot
    point a command at an arbitrary folder (PRD 12.2). The caller owns closing the
    returned database.
    """
    from .bootstrap import registry as registry_module
    from .db import Database, Repository
    from .db.connection import DbConfig

    if not instance_id or not str(instance_id).strip():
        raise InvalidInput("An instance id is required.", code=Code.INVALID_INPUT)

    entry = registry_module.HostRegistry().get(instance_id)
    if entry is None:
        raise NotFound(
            "No instance with that id is registered on this host.",
            code=Code.INSTANCE_NOT_FOUND,
            detail={"reason": "instance_not_registered"},
        )

    root = Path(entry.canonical_root)
    if not root.is_dir():
        raise NotFound(
            "The registered workspace folder for this instance is not present.",
            code=Code.INSTANCE_NOT_FOUND,
            detail={"reason": "root_missing"},
        )

    from .bootstrap import workspace

    db = Database(DbConfig(path=workspace.db_path(root)))
    try:
        if str(db.instance_id) != str(instance_id):
            raise Conflict(
                "The workspace database belongs to a different instance id than the "
                "registry names.",
                code=Code.INSTANCE_MISMATCH,
                detail={"reason": "database_instance_mismatch"},
            )
    except ResumeReviewError:
        db.close()
        raise
    except Exception:
        db.close()
        raise
    return root, db, Repository(db)


def _local_principal(instance_id: str):
    from .models import Principal, Role

    return Principal(
        actor_ref=LOCAL_ACTOR,
        role=Role.REVIEWER,
        session_id="cli",
        instance_id=str(instance_id),
        is_local_owner=True,
    )


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------
def _cmd_setup(args: argparse.Namespace, request_id: str) -> tuple[dict[str, Any], ExitCode]:
    from .bootstrap.setup import setup_instance

    job_path = Path(args.job)
    if not job_path.is_file():
        raise InvalidInput(
            "The job description file could not be read.",
            code=Code.INVALID_INPUT,
            detail={"reason": "job_file_unreadable"},
        )
    try:
        job_text = job_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise InvalidInput(
            "The job description file could not be read as UTF-8 text.",
            code=Code.INVALID_INPUT,
            detail={"reason": type(exc).__name__},
        ) from exc

    result = setup_instance(Path(args.folder), job_text)
    warnings = list(result.warnings)
    if result.backup_path:
        warnings.append(
            "An existing database was upgraded; a verified backup was taken before migration."
        )
    env = _envelope(
        ok=True,
        code=Code.OK,
        instance_id=result.instance_id,
        data=result.to_dict(),
        warnings=warnings,
        request_id=request_id,
        state_revision=result.state_revision_after,
    )
    return env, ExitCode.OK


def _cmd_status(args: argparse.Namespace, request_id: str) -> tuple[dict[str, Any], ExitCode]:
    root, db, repo = _resolve_instance(args.instance)
    try:
        record = repo.get_instance() or {}
        counts = repo.status_counts()
        revision = db.state_revision()
        manifest: dict[str, Any] = {}
        try:
            from .bootstrap import workspace

            manifest = workspace.read_instance_manifest(root) or {}
        except Exception:  # pragma: no cover - manifest is best-effort diagnostics
            manifest = {}
        data = {
            "instance_id": str(record.get("id") or db.instance_id),
            "root_label": root.name,
            "state_revision": revision,
            "storage_mode": record.get("storage_mode"),
            "app_version": record.get("app_version"),
            "schema_version": record.get("schema_version"),
            "counts": counts,
            "manifest_schema_version": manifest.get("schema_version"),
            "lock_backend": manifest.get("lock_backend"),
        }
        env = _envelope(
            ok=True,
            code=Code.OK,
            instance_id=str(record.get("id") or db.instance_id),
            data=data,
            warnings=[],
            request_id=request_id,
            state_revision=revision,
        )
        return env, ExitCode.OK
    finally:
        db.close()


def _disabled_adapter():
    """A model client that refuses to run: scan is deterministic and needs none."""
    from .openclaw_adapter.policy import RoutePolicy

    class _NoRoute:
        def complete(self, request, *, request_id=None):  # pragma: no cover - never called
            raise DependencyUnavailable(
                "This command runs no inference, but a model call was attempted.",
                code=Code.ROUTE_UNAVAILABLE,
            )

    return _NoRoute(), RoutePolicy(route=_unavailable_route(), restricted=False)


def _unavailable_route():
    from .models import ModelRoute

    return ModelRoute.UNAVAILABLE


def _cmd_scan(args: argparse.Namespace, request_id: str) -> tuple[dict[str, Any], ExitCode]:
    from .analysis.pipeline import Pipeline

    root, db, repo = _resolve_instance(args.instance)
    try:
        adapter, policy = _disabled_adapter()
        pipeline = Pipeline(repository=repo, root=root, adapter=adapter, route_policy=policy)
        summary = pipeline.scan()
        revision = db.state_revision()
        warnings: list[str] = []
        if summary.awaiting_criteria:
            warnings.append(
                "Some documents await approved criteria; they are extracted and visible "
                "for manual review but were not assessed."
            )
        if summary.route_unavailable:
            warnings.append(
                "No approved analysis route is configured; matched documents were left "
                "for manual review. Run summarize with a configured route to assess them."
            )
        counts = repo.status_counts()
        data = {"scan": summary.to_dict(), "counts": counts}
        env = _envelope(
            ok=True,
            code=Code.OK,
            instance_id=db.instance_id,
            data=data,
            warnings=warnings,
            request_id=request_id,
            state_revision=revision,
        )
        return env, ExitCode.OK
    finally:
        db.close()


def _route_from_env():
    """Build an ``(adapter, policy)`` pair from the live route environment.

    Returns ``None`` when the route is not fully configured. Nothing here reads a
    credential into telemetry: the secret is passed by file path.
    """
    import os

    present = {name: os.environ.get(name, "").strip() for name in LIVE_ENV}
    if not all(present.values()):
        return None

    from .analysis.pipeline import AdapterCompletionClient
    from .models import ModelRoute
    from .openclaw_adapter.client import AdapterConfig, OpenClawAdapter
    from .openclaw_adapter.policy import RouteAttestation, RoutePolicy

    route = ModelRoute(present["RESUME_REVIEW_LIVE_ROUTE"])
    attestation_path = Path(present["RESUME_REVIEW_LIVE_ATTESTATION"])
    try:
        raw = json.loads(attestation_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InvalidInput(
            "The route attestation file could not be read as JSON.",
            code=Code.ROUTE_POLICY_VIOLATION,
            detail={"reason": type(exc).__name__},
        ) from exc
    if not isinstance(raw, Mapping):
        raise InvalidInput(
            "The route attestation file must contain a JSON object.",
            code=Code.ROUTE_POLICY_VIOLATION,
            detail={"reason": "attestation_not_object"},
        )

    allowed = set(RouteAttestation.__dataclass_fields__)  # type: ignore[attr-defined]
    fields = {k: raw[k] for k in raw if k in allowed}
    attestation = RouteAttestation(**fields)
    policy = RoutePolicy(
        route=route,
        restricted=True,
        attestation=attestation,
        provider_label=Path(present["RESUME_REVIEW_LIVE_PROVIDER_RECORD"]).name,
    )
    policy.assert_inference_allowed()
    config = AdapterConfig(
        base_url=present["RESUME_REVIEW_LIVE_BASE_URL"],
        agent_id=present["RESUME_REVIEW_LIVE_AGENT_ID"],
        timeout_seconds=float(os.environ.get("RESUME_REVIEW_LIVE_TIMEOUT_SECONDS", "60") or 60),
        route_policy=policy,
        secret_path=Path(present["RESUME_REVIEW_LIVE_SECRET_FILE"]),
    )
    return AdapterCompletionClient(OpenClawAdapter(config)), policy


def _cmd_summarize(args: argparse.Namespace, request_id: str) -> tuple[dict[str, Any], ExitCode]:
    from .analysis.pipeline import Pipeline
    from .analysis.queue import AnalysisQueue
    from .models import jsonable

    resolved = _route_from_env()
    if resolved is None:
        raise DependencyUnavailable(
            "No approved analysis route is configured, so no summaries can be produced. "
            "Configure the RESUME_REVIEW_LIVE_* route variables and try again. Manual "
            "review and the deterministic scan do not need a route.",
            code=Code.ROUTE_UNAVAILABLE,
            detail={"reason": "route_not_configured"},
        )
    adapter, policy = resolved

    root, db, repo = _resolve_instance(args.instance)
    try:
        pipeline = Pipeline(repository=repo, root=root, adapter=adapter, route_policy=policy)
        queue = AnalysisQueue(repository=repo, pipeline=pipeline)
        before = repo.status_counts()
        summary = queue.drain(max_jobs=int(args.limit) if getattr(args, "limit", None) else None)
        revision = db.state_revision()
        warnings: list[str] = []
        if args.changed_only:
            warnings.append(
                "Only documents whose bytes changed since their last assessment are queued; "
                "unchanged documents reuse their committed profile."
            )
        data = {
            "drain": jsonable(summary),
            "counts_before": before,
            "counts_after": repo.status_counts(),
            "changed_only": bool(args.changed_only),
        }
        env = _envelope(
            ok=True,
            code=Code.OK,
            instance_id=db.instance_id,
            data=data,
            warnings=warnings,
            request_id=request_id,
            state_revision=revision,
        )
        return env, ExitCode.OK
    finally:
        db.close()


def _cmd_render(args: argparse.Namespace, request_id: str) -> tuple[dict[str, Any], ExitCode]:
    from .bootstrap import workspace
    from .reporting.payload import build_snapshot_payload
    from .reporting.snapshot import write_snapshot

    root, db, repo = _resolve_instance(args.instance)
    try:
        target = workspace.report_path(root)
        payload = build_snapshot_payload(db, mode="snapshot")
        result = write_snapshot(target, payload)
        warnings = list(result.warnings)
        if not result.published:
            warnings.append(
                "The previous report was kept because the new one could not be published."
            )
        data = {
            "report_file": target.name,
            "published": bool(result.published),
            "stale": bool(result.stale),
            "byte_size": int(result.byte_size),
            "state_revision": result.state_revision,
            "generated_at": result.generated_at,
        }
        exit_code = ExitCode.OK if result.published else _exit_for(Code.SNAPSHOT_PUBLISH_FAILED)
        env = _envelope(
            ok=bool(result.published),
            code=Code.OK if result.published else Code.SNAPSHOT_PUBLISH_FAILED,
            instance_id=db.instance_id,
            data=data,
            warnings=warnings,
            request_id=request_id,
            state_revision=result.state_revision,
        )
        return env, exit_code
    finally:
        db.close()


def _load_request_file(path: str) -> Mapping[str, Any]:
    req_path = Path(path)
    if not req_path.is_file():
        raise InvalidInput(
            "The plan request file could not be read.",
            code=Code.INVALID_INPUT,
            detail={"reason": "request_file_unreadable"},
        )
    try:
        payload = json.loads(req_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InvalidInput(
            "The plan request file is not valid JSON.",
            code=Code.INVALID_INPUT,
            detail={"reason": type(exc).__name__},
        ) from exc
    if not isinstance(payload, Mapping):
        raise InvalidInput(
            "The plan request must be a JSON object.",
            code=Code.INVALID_INPUT,
            detail={"reason": "request_not_object"},
        )
    return payload


def _cmd_plan_actions(args: argparse.Namespace, request_id: str) -> tuple[dict[str, Any], ExitCode]:
    from .actions.planner import plan_actions
    from .models import jsonable

    payload = _load_request_file(args.request)
    for forbidden in ("actor", "requested_by", "approval", "approved_by"):
        if forbidden in payload:
            raise InvalidInput(
                "The request may not supply an actor or approval; identity comes from the "
                "authenticated session and approval from the review interface.",
                code=Code.INVALID_INPUT,
                detail={"field": forbidden},
            )

    document_ids = payload.get("document_ids", payload.get("documents"))
    if not isinstance(document_ids, (list, tuple)) or not document_ids:
        raise InvalidInput(
            "The request must name a non-empty list of document_ids.",
            code=Code.INVALID_INPUT,
            detail={"field": "document_ids"},
        )
    requested_ids = [str(d) for d in document_ids]

    intents = payload.get("intents", payload.get("intent_by_document")) or {}
    overrides = payload.get("overrides") or {}
    if not isinstance(intents, Mapping) or not isinstance(overrides, Mapping):
        raise InvalidInput(
            "intents and overrides must be JSON objects keyed by document id.",
            code=Code.INVALID_INPUT,
            detail={"reason": "intents_or_overrides_invalid"},
        )

    root, db, repo = _resolve_instance(args.instance)
    try:
        plan = plan_actions(
            repo,
            document_ids=requested_ids,
            intent_by_document=intents,
            requested_by=LOCAL_ACTOR,
            criteria_version=repo.active_criteria_version(),
            root=root,
            overrides=overrides,
        )
        batch_id = repo.create_batch(plan, created_by=LOCAL_ACTOR)
        # Write the durable per-operation rows now, before any file is touched: the
        # executor revalidates against these rows, and recovery reconciles from them.
        repo.create_file_operations(batch_id, plan.operations)
        revision = db.state_revision()
        data = {
            "plan": jsonable(plan),
            "batch_id": batch_id,
            "execution_state": "planned",
            "next_action": (
                "Review this exact plan in the review interface and record an approval, "
                "then run apply-actions with the batch id. Planning is not approval."
            ),
        }
        env = _envelope(
            ok=True,
            code=Code.OK,
            instance_id=db.instance_id,
            data=data,
            warnings=list(plan.warnings),
            request_id=request_id,
            state_revision=revision,
        )
        return env, ExitCode.OK
    finally:
        db.close()


def _cmd_apply_actions(args: argparse.Namespace, request_id: str) -> tuple[dict[str, Any], ExitCode]:
    from .actions.executor import apply_batch

    root, db, repo = _resolve_instance(args.instance)
    try:
        principal = _local_principal(db.instance_id)
        outcome = apply_batch(
            repo,
            batch_id=str(args.batch),
            actor=principal,
            root=root,
            now=now_iso(),
            dry_run=bool(getattr(args, "dry_run", False)),
        )
        revision = db.state_revision()
        result = outcome.to_dict()
        result.pop("root", None)
        env = _envelope(
            ok=bool(outcome.ok),
            code=outcome.code,
            instance_id=db.instance_id,
            data=result,
            warnings=list(outcome.warnings),
            request_id=request_id,
            state_revision=revision,
        )
        return env, _exit_for(outcome.code)
    finally:
        db.close()


def _cmd_backup(args: argparse.Namespace, request_id: str) -> tuple[dict[str, Any], ExitCode]:
    from .bootstrap import workspace

    root, db, repo = _resolve_instance(args.instance)
    try:
        db_file = workspace.db_path(root)
        backups = workspace.backups_dir(root)
        backups.mkdir(parents=True, exist_ok=True)
        stamp = now_iso().replace(":", "").replace("-", "").split(".")[0]
        target = backups / f"review-{stamp}-{new_id('tmp')[-8:]}.db"

        source = sqlite3.connect(str(db_file))
        try:
            destination = sqlite3.connect(str(target))
            try:
                source.backup(destination)
            finally:
                destination.close()
        finally:
            source.close()

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

        revision = db.state_revision()
        data = {
            "backup_file": target.name,
            "byte_size": int(target.stat().st_size),
            "verified": True,
            "integrity": "ok",
        }
        env = _envelope(
            ok=True,
            code=Code.OK,
            instance_id=db.instance_id,
            data=data,
            warnings=[],
            request_id=request_id,
            state_revision=revision,
        )
        return env, ExitCode.OK
    finally:
        db.close()


def _cmd_repair(args: argparse.Namespace, request_id: str) -> tuple[dict[str, Any], ExitCode]:
    from .actions.recovery import plan_recovery

    root, db, repo = _resolve_instance(args.instance)
    try:
        dry_run = bool(args.dry_run)
        plan = plan_recovery(repo, root=root, batch_id=args.batch, dry_run=dry_run)
        result = plan.to_dict()
        result["root"] = Path(str(result.get("root") or root)).name
        revision = db.state_revision()
        warnings: list[str] = []
        if plan.requires_human:
            warnings.append(
                "Some operations need a human decision and were not reconciled "
                "automatically."
            )
        if dry_run:
            warnings.append("This was a dry run; no journal or location state was written.")
        env = _envelope(
            ok=True,
            code=Code.OK,
            instance_id=db.instance_id,
            data=result,
            warnings=warnings,
            request_id=request_id,
            state_revision=revision,
        )
        return env, ExitCode.OK
    finally:
        db.close()


def _cmd_stop(args: argparse.Namespace, request_id: str) -> tuple[dict[str, Any], ExitCode]:
    from .bootstrap import workspace
    from .bootstrap.ownership import is_locked, read_holder

    root, db, repo = _resolve_instance(args.instance)
    try:
        lock_path = workspace.owner_lock_path(root)
        running = bool(is_locked(lock_path))
        holder = read_holder(lock_path)
        if running:
            raise Conflict(
                "A helper process still holds this instance. This command does not "
                "terminate another process; stop the helper through the entry point that "
                "started it.",
                code=Code.INSTANCE_LOCKED_BY_OTHER_OWNER,
                detail={"holder": holder.to_dict() if holder else None},
            )
        revision = db.state_revision()
        data = {"running": False, "instance_id": db.instance_id}
        env = _envelope(
            ok=True,
            code=Code.OK,
            instance_id=db.instance_id,
            data=data,
            warnings=[],
            request_id=request_id,
            state_revision=revision,
        )
        return env, ExitCode.OK
    finally:
        db.close()


def _load_api_app() -> Any:
    """Return the HTTP API application factory, or ``None`` when it is absent.

    The API package is authored separately and is not imported by the db, storage,
    actions or analysis layers. The CLI only *calls* it, and never pretends to
    serve when it is not installed. Any import or attribute failure means
    "unavailable".
    """
    import importlib

    try:
        api = importlib.import_module("resume_review.api")
    except Exception:
        return None
    factory = getattr(api, "create_app", None)
    if callable(factory):
        return factory
    if getattr(api, "app", None) is not None and not isinstance(getattr(api, "app"), type(sys)):
        return api.app
    try:
        app_mod = importlib.import_module("resume_review.api.app")
    except Exception:
        return None
    for name in ("create_app", "app"):
        candidate = getattr(app_mod, name, None)
        if candidate is not None:
            return candidate
    return None


def _serve(app: Any, *, host: str, port: int) -> None:
    """Run the ASGI application. Split out so tests can stub the blocking call."""
    import uvicorn

    import socket
    import webbrowser

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind((host, int(port)))
        listener.listen(128)
        chosen_port = listener.getsockname()[1]

        def launch():
            ticket = app.state.desktop_pairing.create(
                actor_ref=LOCAL_ACTOR, role=Role.ADMINISTRATOR, port=chosen_port,
            )
            if getattr(app.state, "desktop_open_browser", False):
                webbrowser.open(ticket.url)
                sys.stderr.write(f"RecruiterAgent is opening at http://{host}:{chosen_port}.\n")
            else:
                sys.stderr.write(f"Open this single-use local review link: {ticket.url}\n")

        from .models import Role

        app.state.desktop_launch = launch
        server = uvicorn.Server(uvicorn.Config(app, host=host, port=chosen_port, access_log=False))
        server.run(sockets=[listener])


def _cmd_start(args: argparse.Namespace, request_id: str) -> tuple[dict[str, Any], ExitCode]:
    root, db, repo = _resolve_instance(args.instance)
    try:
        factory = _load_api_app()
        if factory is None:
            raise DependencyUnavailable(
                "The HTTP API package is not available in this installation, so the "
                "connected page cannot be served. Use the generated review.html snapshot, "
                "or install the API component.",
                code=Code.ROUTE_UNAVAILABLE,
                detail={"reason": "api_package_unavailable"},
            )
        from .helper import build_desktop_app
        from .openclaw_adapter.connection import load_connection

        connection_path = getattr(args, "connection_config", None)
        connection = load_connection(Path(connection_path), root=root, instance_id=repo.instance_id) if connection_path else None
        app = build_desktop_app(repo, root, connection, factory=factory)
        app.state.desktop_open_browser = bool(getattr(args, "open_browser", False))
        host = "127.0.0.1"
        port = int(getattr(args, "port", 0) or 0)
        warnings = [
            "The helper serves only loopback. A browser must use the connected page "
            "address issued by a review session, not an ad-hoc origin."
        ]
        _serve(app, host=host, port=port)
        data = {"served": True, "host": host, "port": port, "instance_id": db.instance_id}
        env = _envelope(
            ok=True,
            code=Code.OK,
            instance_id=db.instance_id,
            data=data,
            warnings=warnings,
            request_id=request_id,
        )
        return env, ExitCode.OK
    finally:
        db.close()


def _cmd_check_connection(args: argparse.Namespace, request_id: str):
    import asyncio

    from .openclaw_adapter.client import OpenClawAdapter
    from .openclaw_adapter.connection import load_connection

    root, db, repo = _resolve_instance(args.instance)
    try:
        config = load_connection(Path(args.connection_config), root=root, instance_id=repo.instance_id)
        verification = asyncio.run(OpenClawAdapter(config).verify_route())
        return _envelope(
            ok=verification.ok, code=Code.OK if verification.ok else Code.ROUTE_UNAVAILABLE,
            instance_id=repo.instance_id, data=verification.to_dict(), request_id=request_id,
        ), ExitCode.OK if verification.ok else ExitCode.DEPENDENCY
    finally:
        db.close()


_HANDLERS = {
    "setup": _cmd_setup,
    "start": _cmd_start,
    "check-connection": _cmd_check_connection,
    "status": _cmd_status,
    "scan": _cmd_scan,
    "summarize": _cmd_summarize,
    "render": _cmd_render,
    "plan-actions": _cmd_plan_actions,
    "apply-actions": _cmd_apply_actions,
    "backup": _cmd_backup,
    "repair": _cmd_repair,
    "stop": _cmd_stop,
}


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
_EXIT_HELP = """exit codes:
  0  success
  1  unexpected internal failure
  2  invalid input, not found, or a refused downgrade
  3  permission failure
  4  conflict (stale plan, missing or expired approval, ...)
  5  dependency failure (no HTTP API package, no analysis route, ...)
  6  unsupported storage topology
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="resume-review",
        description=(
            "Folder-local, evidence-backed resume review. Each resume folder is one "
            "instance with its own database, report, and action history."
        ),
        epilog=_EXIT_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"resume-review {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    p_setup = sub.add_parser("setup", help="Provision or re-provision an instance.")
    p_setup.add_argument("--folder", required=True, help="Workspace root folder (absolute).")
    p_setup.add_argument("--job", required=True, help="Job description file (UTF-8).")
    p_setup.add_argument("--json", action="store_true", dest="json_output")

    p_start = sub.add_parser("start", help="Serve the connected review page for an instance.")
    p_start.add_argument("--instance", required=True)
    p_start.add_argument("--port", type=int, default=0)
    p_start.add_argument("--connection-config", help="Protected, instance-bound hosted HTTPS connection profile.")
    p_start.add_argument("--open-browser", action="store_true", help="Open the single-use local-owner review link.")
    p_start.add_argument("--json", action="store_true", dest="json_output")

    p_connection = sub.add_parser("check-connection", help="Probe the configured online analysis API without sending resumes.")
    p_connection.add_argument("--instance", required=True)
    p_connection.add_argument("--connection-config", required=True)
    p_connection.add_argument("--json", action="store_true", dest="json_output")

    p_status = sub.add_parser("status", help="Report instance status (JSON by default).")
    p_status.add_argument("--instance", required=True)
    p_status.add_argument("--json", action="store_true", dest="json_output")

    p_scan = sub.add_parser("scan", help="Run the deterministic discovery and extraction pass.")
    p_scan.add_argument("--instance", required=True)
    p_scan.add_argument("--json", action="store_true", dest="json_output")

    p_summarize = sub.add_parser("summarize", help="Run the configured analysis route.")
    p_summarize.add_argument("--instance", required=True)
    p_summarize.add_argument("--changed-only", action="store_true", dest="changed_only")
    p_summarize.add_argument("--limit", type=int, default=0)
    p_summarize.add_argument("--json", action="store_true", dest="json_output")

    p_render = sub.add_parser("render", help="Render the review.html snapshot.")
    p_render.add_argument("--instance", required=True)
    p_render.add_argument("--json", action="store_true", dest="json_output")

    p_plan = sub.add_parser("plan-actions", help="Build an action plan from a request file.")
    p_plan.add_argument("--instance", required=True)
    p_plan.add_argument("--request", required=True, help="JSON request naming document_ids.")
    p_plan.add_argument("--json", action="store_true", dest="json_output")

    p_apply = sub.add_parser("apply-actions", help="Apply an already-approved action batch.")
    p_apply.add_argument("--instance", required=True)
    p_apply.add_argument("--batch", required=True, help="Approved batch id.")
    p_apply.add_argument("--dry-run", action="store_true", dest="dry_run")
    p_apply.add_argument("--json", action="store_true", dest="json_output")

    p_backup = sub.add_parser("backup", help="Take a verified database backup.")
    p_backup.add_argument("--instance", required=True)
    p_backup.add_argument("--json", action="store_true", dest="json_output")

    p_repair = sub.add_parser("repair", help="Reconcile interrupted file operations.")
    p_repair.add_argument("--instance", required=True)
    p_repair.add_argument("--batch", default=None)
    p_repair.add_argument("--dry-run", action="store_true", dest="dry_run")
    p_repair.add_argument("--json", action="store_true", dest="json_output")

    p_stop = sub.add_parser("stop", help="Report or release the instance helper lock.")
    p_stop.add_argument("--instance", required=True)
    p_stop.add_argument("--json", action="store_true", dest="json_output")

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def _human_line(command: str, env: Mapping[str, Any]) -> str:
    if env.get("ok"):
        return f"{command}: ok ({env.get('code')}) instance {env.get('instance_id')}"
    error = env.get("error") or {}
    return f"{command}: error {error.get('code') or env.get('code')}: {error.get('message') or ''}"


def main(argv: Sequence[str] | None = None) -> int:
    """Run one CLI command. Returns a stable process exit code (PRD 5.3)."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if not getattr(args, "command", None):
        parser.print_help()
        return int(ExitCode.INVALID_INPUT)

    request_id = new_id("request")
    as_json = bool(getattr(args, "json_output", False)) or args.command == "status"
    instance_id = getattr(args, "instance", None)

    handler = _HANDLERS[args.command]
    try:
        env, exit_code = handler(args, request_id)
    except ResumeReviewError as exc:
        env = _error_envelope(exc, request_id=request_id, instance_id=instance_id)
        exit_code = _exit_for(exc.code)
    except (OSError, sqlite3.Error) as exc:
        env = _error_envelope(
            ResumeReviewError(
                "A filesystem or database error stopped the command.",
                code=Code.INTERNAL_ERROR,
                detail={"reason": type(exc).__name__},
            ),
            request_id=request_id,
            instance_id=instance_id,
        )
        exit_code = ExitCode.UNEXPECTED
    except Exception as exc:  # noqa: BLE001 - a CLI must not leak a raw traceback
        env = _error_envelope(
            ResumeReviewError(
                "An unexpected error stopped the command.",
                code=Code.INTERNAL_ERROR,
                detail={"reason": type(exc).__name__},
            ),
            request_id=request_id,
            instance_id=instance_id,
        )
        exit_code = ExitCode.UNEXPECTED

    if isinstance(exit_code, ExitCode):
        code_value = int(exit_code)
    else:  # pragma: no cover - handlers always return an ExitCode
        code_value = int(exit_code)

    if as_json:
        sys.stdout.write(json.dumps(env, indent=2, ensure_ascii=False) + "\n")
    else:
        line = _human_line(args.command, env)
        stream = sys.stdout if env.get("ok") else sys.stderr
        stream.write(line + "\n")

    return code_value


if __name__ == "__main__":  # pragma: no cover - module entry
    raise SystemExit(main())
