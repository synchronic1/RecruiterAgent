"""Desktop helper composition and durable outbound worker. No remote file commands."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

from .analysis.pipeline import AdapterCompletionClient, AnalysisOutcome, Pipeline
from .analysis.queue import AnalysisQueue
from .api.chat import FolderChatService
from .db import Database, Repository
from .db.connection import DbConfig
from .errors import Code, ResumeReviewError
from .models import ModelRoute
from .openclaw_adapter.client import AdapterConfig, OpenClawAdapter
from .openclaw_adapter.policy import RoutePolicy


class _NoInference:
    def complete(self, *args: Any, **kwargs: Any) -> Any:
        raise ResumeReviewError("No analysis connection is configured.", code=Code.ROUTE_UNAVAILABLE)


class DesktopWorker:
    """One serial durable worker; chat, scan and analysis have separate retry clocks."""

    def __init__(self, repository: Repository, root: Path, config: AdapterConfig | None):
        self.repository = repository
        self.root = root
        self.config = config
        self.adapter = OpenClawAdapter(config) if config is not None else None
        self.policy = config.route_policy if config else RoutePolicy(ModelRoute.UNAVAILABLE, False)
        self.chat = FolderChatService(repository, self.adapter, route_policy=self.policy) if self.adapter else None
        pipeline = Pipeline(
            repository=repository, root=root,
            adapter=AdapterCompletionClient(self.adapter) if self.adapter else _NoInference(),
            route_policy=self.policy,
        )
        # Scan remains deterministic and does not automatically schedule inference.
        self.scan_pipeline = Pipeline(
            repository=repository, root=root, adapter=_NoInference(),
            route_policy=RoutePolicy(ModelRoute.UNAVAILABLE, False),
        )
        self.queue = AnalysisQueue(
            repository=repository, pipeline=pipeline,
            lease_owner="worker:desktop",
            # Analysis can make its first call and one repair call before commit.
            lease_seconds=max(300, 2 * config.timeout_seconds + 120) if config else 300,
            handlers={"scan": self._scan, "chat": self._chat},
        )
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self._status_lock = threading.Lock()
        self._status: dict[str, Any] = {
            "configured": config is not None, "state": "configured" if config else "not_configured",
            "last_error_code": None, "last_job_id": None,
            "endpoint_label": config.endpoint_label if config else None,
            "agent_target": config.agent_target if config else None,
        }
        self._next_attempt: dict[str, float] = {}

    def _scan(self, job: Any) -> AnalysisOutcome:
        self.scan_pipeline.scan()
        return AnalysisOutcome(status="committed")

    def _chat(self, job: Any) -> AnalysisOutcome:
        if self.chat is None:
            raise ResumeReviewError("Chat is not configured.", code=Code.ROUTE_UNAVAILABLE)
        answer = asyncio.run(self.chat.run_job(job))
        return AnalysisOutcome(status="committed", profile_id=answer["conversation_id"], model_calls=1)

    def describe(self) -> dict[str, Any]:
        with self._status_lock:
            return {**self._status, "worker_running": bool(self.thread and self.thread.is_alive())}

    def step(self) -> bool:
        """Process at most one job. Without a route, preserve queued model work."""
        import time

        for kind in (("chat", "scan", "analysis") if self.adapter else ("scan",)):
            if time.monotonic() < self._next_attempt.get(kind, 0):
                continue
            outcome = self.queue.process_next(kind=kind)
            if outcome.status in ("idle", "no_capacity", "unsupported_kind"):
                continue
            with self._status_lock:
                self._status["last_job_id"] = outcome.job_id
                if kind != "scan":
                    self._status.update(
                        state="retrying" if outcome.status == "retry" else "error" if outcome.error_code else "ready",
                        last_error_code=outcome.error_code,
                    )
            if outcome.retry_after_seconds:
                self._next_attempt[kind] = time.monotonic() + outcome.retry_after_seconds
            return True
        return False

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._run, name="recruiteragent-desktop", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        try:
            while not self.stop_event.is_set():
                try:
                    worked = self.step()
                except Exception:
                    # Never place arbitrary exception text or applicant data in health output.
                    with self._status_lock:
                        self._status.update(state="error", last_error_code=Code.INTERNAL_ERROR)
                    worked = False
                if not worked:
                    self.stop_event.wait(0.5)
        finally:
            self.repository.db.close()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join()


def build_desktop_app(repository: Repository, root: Path, config: AdapterConfig | None = None, *, factory=None, api_config=None):
    """Compose the existing HTTP contracts with the outbound model worker."""
    from contextlib import asynccontextmanager

    from .api.app import DEFAULT_ROUTE_MODULES, create_app
    from .api.deps import ApiConfig
    from .api.desktop import install_desktop_routes
    from .bootstrap.ownership import InstanceLock
    from .bootstrap.workspace import owner_lock_path

    worker_db = Database(DbConfig(path=repository.db.path), instance_id=repository.instance_id)
    worker = DesktopWorker(Repository(worker_db), root, config)
    browser_chat = FolderChatService(repository, worker.adapter, route_policy=worker.policy) if config else None
    app = (factory or create_app)(
        repository, chat_adapter=browser_chat,
        config=api_config or ApiConfig(allowed_hosts=("127.0.0.1", "localhost", "::1")),
        route_modules=(*DEFAULT_ROUTE_MODULES, "resume_review.api.scan", "resume_review.api.analysis", "resume_review.api.jobs"),
    )
    install_desktop_routes(app, worker)
    app.state.desktop_worker = worker

    @asynccontextmanager
    async def lifespan(app):
        with InstanceLock(owner_lock_path(root), instance_id=repository.instance_id):
            worker.start()
            try:
                launch = getattr(app.state, "desktop_launch", None)
                if launch is not None:
                    launch()
                yield
            finally:
                await asyncio.to_thread(worker.stop)

    app.router.lifespan_context = lifespan
    return app
