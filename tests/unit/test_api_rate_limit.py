"""Request limits on the HTTP surface (PRD section 16.1).

The limiter itself is tested in the auth suite. What was missing -- and what these
tests establish -- is that the limiter is actually REACHABLE from a route. The
error contract mapped ``RATE_LIMITED`` to 429 and the auth module defined the
limiters, but nothing in ``src/resume_review/api/`` ever called one, so a route
could not emit 429 at all. A control that cannot fire is not a control, so these
tests drive real requests through the real app and assert the observed status.

``AT-33`` (authentication) and the PRD 16.1 request-limit requirement are the
authority. Everything runs through ``fastapi.testclient.TestClient`` against a real
migrated database; synthetic data only.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from resume_review import SCHEMA_VERSION, __version__
from resume_review.api import ApiConfig, ApiRuntime, create_app
from resume_review.api.deps import MUTATING_METHODS, require_mutation
from resume_review.auth import CsrfStore, RateLimiter, SessionStore, session_cookie_name
from resume_review.db import Repository
from resume_review.db.connection import Database, DbConfig
from resume_review.db.migrations import apply_migrations
from resume_review.models import Role

INSTANCE_ID = "inst_rate"
ORIGIN = "http://testserver"

#: PRD 12.1 lists roughly twenty instance-scoped endpoints, about half mutating.
#: A walk that finds fewer than this is not exercising the real route table, and a
#: "no offenders" result from such a walk would be meaningless.
MIN_EXPECTED_MUTATING_ROUTES = 8


class _StubChat:
    """A minimal chat adapter so the ``/chat`` route is registered and callable."""

    def __call__(self, payload, *, principal, instance_id):  # noqa: ANN001
        return {"answer": "stub", "documents_inspected": 0}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "job"
    (root / ".review").mkdir(parents=True)
    (root / "resumes").mkdir(parents=True)
    return root


@pytest.fixture
def db(workspace: Path):
    database = Database(DbConfig(path=workspace / ".review" / "review.db"))
    apply_migrations(database.connect())
    yield database
    database.close()


@pytest.fixture
def repo(db: Database) -> Repository:
    repository = Repository(db)
    repository.create_instance(INSTANCE_ID, __version__, SCHEMA_VERSION)
    return repository


@pytest.fixture
def sessions() -> SessionStore:
    return SessionStore()


@pytest.fixture
def csrf() -> CsrfStore:
    return CsrfStore()


@pytest.fixture
def app(repo, sessions, csrf):
    """The full application, every default route module registered.

    Unlike the per-module suites this deliberately does NOT narrow ``route_modules``:
    the route-coverage test below has to see the whole table to mean anything.
    """
    return create_app(
        repo,
        sessions=sessions,
        csrf_store=csrf,
        config=ApiConfig(allowed_origins=(ORIGIN,)),
        chat_adapter=_StubChat(),
    )


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


def auth(sessions: SessionStore, csrf: CsrfStore, actor: str = "reviewer_1"):
    session = sessions.issue(INSTANCE_ID, actor, Role.REVIEWER)
    headers = {"X-CSRF-Token": csrf.issue(session.session_id), "Origin": ORIGIN}
    cookies = {session_cookie_name(INSTANCE_ID): session.session_id}
    return session, headers, cookies


def url(suffix: str) -> str:
    return f"/api/v1/instances/{INSTANCE_ID}{suffix}"


def narrow_limit(app, *, limit: int, chat_limit: int = 1000) -> None:
    """Shrink the limiter windows so a test can reach the limit in a few requests.

    Replaces the runtime with a copy carrying fresh limiters. Fresh instances matter:
    the module-level defaults are process-global, so mutating them would leak
    counters into every other test in the session.
    """
    app.state.runtime = dataclasses.replace(
        app.state.runtime,
        rate_limiter=RateLimiter(limit=limit, window_seconds=60.0),
        chat_rate_limiter=RateLimiter(limit=chat_limit, window_seconds=60.0),
    )


def make_doc(repo: Repository, filename: str):
    return repo.create_document(
        original_filename=filename,
        rel_path=f"resumes/{filename}",
        media_type="pdf",
        size_bytes=12,
        content_sha256=f"sha_{filename}",
        fs_identity=f"fs_{filename}",
    )


def decide(client, repo, headers, cookies, doc, revision: int):
    """One valid decision mutation with the expected revision for that write."""
    return client.patch(
        url(f"/documents/{doc.id}/decision"),
        json={"disposition": "keep", "expected_revision": revision},
        headers=headers,
        cookies=cookies,
    )


# ---------------------------------------------------------------------------
# The limit is reachable
# ---------------------------------------------------------------------------
def test_a_burst_of_mutations_is_refused_with_429(client, app, sessions, csrf, repo):
    """The behaviour that could not previously happen: a route emitting 429."""
    narrow_limit(app, limit=3)
    doc = make_doc(repo, "alpha.pdf")
    _, headers, cookies = auth(sessions, csrf)

    statuses = [
        decide(client, repo, headers, cookies, doc, revision).status_code
        for revision in range(6)
    ]

    assert statuses[:3] == [200, 200, 200], statuses
    assert statuses[3:] == [429, 429, 429], statuses

    refused = decide(client, repo, headers, cookies, doc, revision=99)
    assert refused.status_code == 429
    body = refused.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "RATE_LIMITED"
    assert body["error"]["retryable"] is True
    # The refusal is an envelope like any other failure, and it leaks nothing.
    assert body["instance_id"] == INSTANCE_ID
    assert body["request_id"]


def test_reads_are_not_limited(client, app, sessions, csrf, repo):
    """A reviewer paging and sorting must not be throttled by the mutation budget."""
    narrow_limit(app, limit=1)
    make_doc(repo, "alpha.pdf")
    _, headers, cookies = auth(sessions, csrf)

    for _ in range(12):
        response = client.get(url("/documents"), headers=headers, cookies=cookies)
        assert response.status_code == 200, response.text


def test_the_budget_is_per_actor_not_global(client, app, sessions, csrf, repo):
    """One reviewer's runaway tab must not spend another reviewer's budget."""
    narrow_limit(app, limit=2)
    doc = make_doc(repo, "alpha.pdf")
    _, first_headers, first_cookies = auth(sessions, csrf, actor="reviewer_1")

    assert decide(client, repo, first_headers, first_cookies, doc, 0).status_code == 200
    assert decide(client, repo, first_headers, first_cookies, doc, 1).status_code == 200
    assert decide(client, repo, first_headers, first_cookies, doc, 2).status_code == 429

    _, second_headers, second_cookies = auth(sessions, csrf, actor="reviewer_2")
    still_allowed = decide(client, repo, second_headers, second_cookies, doc, 2)
    assert still_allowed.status_code == 200, still_allowed.text


def test_chat_has_its_own_budget(client, app, sessions, csrf):
    """Chat is interactive, so it carries a tighter limit than other mutations."""
    narrow_limit(app, limit=1000, chat_limit=2)
    _, headers, cookies = auth(sessions, csrf)

    statuses = [
        client.post(
            url("/chat"),
            json={"message": "how many submissions are there?"},
            headers=headers,
            cookies=cookies,
        ).status_code
        for _ in range(4)
    ]

    assert statuses[:2] == [200, 200], statuses
    assert statuses[2:] == [429, 429], statuses


# ---------------------------------------------------------------------------
# Coverage: no mutating route may omit the guard
# ---------------------------------------------------------------------------
def _flat_routes(router) -> list:
    """Every leaf route under ``router``, descending through included routers.

    ``include_router`` nests rather than flattens in this Starlette version: the
    entry on ``app.routes`` is a ``_IncludedRouter`` whose real routes hang off
    ``.original_router`` (and a nested include adds another layer). Walking
    ``app.routes`` alone finds no endpoint at all, which would make the coverage
    assertion below vacuously true. Both shapes are handled so the walk survives a
    Starlette that flattens instead.
    """
    found: list = []
    stack = list(getattr(router, "routes", ()) or ())
    while stack:
        route = stack.pop()
        nested = getattr(route, "original_router", None)
        children = getattr(nested, "routes", None) if nested is not None else None
        if children is None:
            children = getattr(route, "routes", None)
        if children:
            stack.extend(children)
            continue
        found.append(route)
    return found


def _mutating(routes) -> list:
    return [
        route
        for route in routes
        if (getattr(route, "methods", None) or set()) & MUTATING_METHODS
    ]


def _dependency_calls(route) -> set:
    """Every callable in a route's dependency tree, transitively."""
    calls = set()
    stack = list(getattr(route.dependant, "dependencies", []) or [])
    while stack:
        dependency = stack.pop()
        calls.add(dependency.call)
        stack.extend(getattr(dependency, "dependencies", []) or [])
    return calls


def test_every_mutating_route_declares_the_mutation_guard(app):
    """The limit lives in ``require_mutation``, so every mutating route must use it.

    Middleware covers CSRF for a route that forgets this dependency; rate limiting
    has no such backstop, so the gap is closed here instead -- by asserting the
    property over the real route table rather than trusting each module.
    """
    mutating = _mutating(_flat_routes(app))
    assert len(mutating) >= MIN_EXPECTED_MUTATING_ROUTES, (
        f"only {len(mutating)} mutating routes were found; the route table was not "
        "walked properly, so this test would pass vacuously"
    )

    offenders = sorted(
        (route.path, tuple(sorted((getattr(route, "methods", None) or set()) & MUTATING_METHODS)))
        for route in mutating
        if require_mutation not in _dependency_calls(route)
    )
    assert not offenders, (
        "these mutating routes do not declare require_mutation, so they are exempt "
        f"from the PRD 16.1 request limit: {offenders}"
    )


def test_no_path_is_registered_twice(app):
    """Two handlers on one path means the first registered silently wins.

    That is not hypothetical: ``POST /chat`` was registered both by the factory's
    fallback bridge and by :mod:`resume_review.api.chat`, and FastAPI dispatched the
    bridge -- so the module's route-policy check, its resolution of caller-supplied
    document ids against this instance, and its own request budget were all dead
    code in the assembled application. The chat tests never saw it because they
    pin ``route_modules`` and set ``chat_adapter`` on the runtime afterwards, so
    they exercised the module's route alone.
    """
    seen: dict = {}
    for route in _flat_routes(app):
        for method in sorted((getattr(route, "methods", None) or set()) - {"HEAD", "OPTIONS"}):
            key = (route.path, method)
            seen.setdefault(key, []).append(getattr(route, "name", None))

    # Anti-vacuity: a walk that finds almost nothing has not inspected the table,
    # and "no duplicates" from such a walk would mean nothing.
    assert len(seen) >= MIN_EXPECTED_MUTATING_ROUTES, (
        f"only {len(seen)} path/method pairs were found; the route table was not "
        "walked properly, so this test would pass vacuously"
    )

    duplicated = {key: names for key, names in seen.items() if len(names) > 1}
    assert not duplicated, (
        "these path/method pairs are registered more than once, so all but the "
        f"first handler are unreachable: {duplicated}"
    )


def test_chat_is_served_by_the_full_route_module(app):
    """``/chat`` must be the module's route, not the factory's simpler bridge.

    The bridge is a fallback for a deployment that does not load ``api.chat``. When
    both exist the served handler decides which guarantees are real, so this asserts
    the module's handler (``post_chat``) is the one on the path.
    """
    chat_routes = [
        route
        for route in _flat_routes(app)
        if route.path.endswith("/chat") and "POST" in (getattr(route, "methods", None) or set())
    ]
    assert len(chat_routes) == 1, f"expected one POST /chat route, found {len(chat_routes)}"
    assert chat_routes[0].name == "post_chat", (
        "POST /chat is served by the factory fallback "
        f"({chat_routes[0].name!r}); the full endpoint in resume_review.api.chat is "
        "shadowed and its route policy, scope resolution and request budget are inert"
    )


def test_the_fallback_bridge_still_serves_when_the_chat_module_is_absent(
    repo, sessions, csrf
):
    """A deployment that narrows ``route_modules`` keeps the simple route."""
    app = create_app(
        repo,
        sessions=sessions,
        csrf_store=csrf,
        config=ApiConfig(allowed_origins=(ORIGIN,)),
        chat_adapter=_StubChat(),
        route_modules=("resume_review.api.documents",),
    )
    chat_routes = [
        route
        for route in _flat_routes(app)
        if route.path.endswith("/chat") and "POST" in (getattr(route, "methods", None) or set())
    ]
    assert len(chat_routes) == 1
    assert chat_routes[0].name == "chat"



def test_the_runtime_carries_both_limiters(app):
    """The limiters are runtime state, so a deployment can widen them without code."""
    runtime = app.state.runtime
    assert isinstance(runtime, ApiRuntime)
    assert isinstance(runtime.rate_limiter, RateLimiter)
    assert isinstance(runtime.chat_rate_limiter, RateLimiter)
    # The chat budget is the tighter of the two.
    assert runtime.chat_rate_limiter.limit <= runtime.rate_limiter.limit
