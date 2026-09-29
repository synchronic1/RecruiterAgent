"""Authentication, session, pairing, CSRF, and guard tests.

Authority: PRD sections 15.1, 15.3 and 16.1; acceptance test AT-33.

    "AT-33 - Authentication. Unauthenticated, wrong-role, wrong-instance,
     forged-Origin, and CSRF attempts fail. Shared identity comes from
     authentication, not request text."

All identities, names, and passwords here are synthetic. This file proves the
negative cases the acceptance test names, and proves that a stored account never
contains a plaintext password.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from resume_review import errors, util
from resume_review.models import Principal, Role
from resume_review.auth import (
    DEFAULT_PAIRING_TTL_SECONDS,
    AccountStore,
    CsrfFailed,
    CsrfStore,
    HostRejected,
    InstanceMismatch,
    OriginRejected,
    PairingManager,
    PairingTokenExpired,
    PairingTokenInvalid,
    RateLimited,
    RateLimiter,
    SessionExpired,
    SessionStore,
    check_csrf,
    check_host,
    check_mutation,
    check_origin,
    check_rate_limit,
    require_role,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------
class FakeClock:
    """A controllable ISO-8601 clock so expiry is tested without sleeping."""

    def __init__(self, start: str = "2026-01-01T00:00:00+00:00") -> None:
        self._now = util.parse_iso(start)

    def __call__(self) -> str:
        return self._now.isoformat()

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)


class NumClock:
    """A monotonic-style float clock for the rate limiter."""

    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def account_store(tmp_path: Path, **kwargs: object) -> AccountStore:
    return AccountStore(tmp_path / "host-secrets" / "accounts.json", **kwargs)  # type: ignore[arg-type]


def principal(actor: str, role: Role) -> Principal:
    return Principal(
        actor_ref=actor,
        role=role,
        session_id=f"sess_{actor}",
        instance_id="inst_A",
    )


# ---------------------------------------------------------------------------
# Store: wrong password, unknown user, tampered session
# ---------------------------------------------------------------------------
def test_wrong_password_unknown_user_and_tampered_session_fail(tmp_path: Path) -> None:
    store = account_store(tmp_path)
    user = store.create_user("Dana Reviewer", "correct-horse-battery", role=Role.REVIEWER)

    with pytest.raises(errors.Unauthenticated) as bad_password:
        store.authenticate(user.actor_ref, "wrong-password")
    assert bad_password.value.code == "UNAUTHENTICATED"

    with pytest.raises(errors.Unauthenticated) as unknown:
        store.authenticate("user_does_not_exist", "correct-horse-battery")
    assert unknown.value.code == "UNAUTHENTICATED"

    # The right credentials do produce the account and its role.
    authenticated = store.authenticate(user.actor_ref, "correct-horse-battery")
    assert authenticated.role is Role.REVIEWER
    # A typed display name is never the identity.
    assert authenticated.actor_ref != "Dana Reviewer"
    assert authenticated.display_name == "Dana Reviewer"

    sessions = SessionStore()
    session = sessions.issue("inst_A", user.actor_ref, user.role)
    with pytest.raises(errors.Unauthenticated) as tampered:
        sessions.resolve("sess_tampered_value")
    assert tampered.value.code == "UNAUTHENTICATED"
    with pytest.raises(errors.Unauthenticated):
        sessions.resolve(session.session_id + "x")


def test_authentication_is_neutral_for_unknown_and_wrong_password(tmp_path: Path) -> None:
    """Both failures raise the same code and message, so neither enumerates users."""
    store = account_store(tmp_path)
    user = store.create_user("Pat", "a-long-enough-password", role=Role.VIEWER)

    with pytest.raises(errors.Unauthenticated) as wrong:
        store.authenticate(user.actor_ref, "nope-nope-nope")
    with pytest.raises(errors.Unauthenticated) as missing:
        store.authenticate("user_absent", "nope-nope-nope")
    assert wrong.value.message == missing.value.message
    assert wrong.value.detail == missing.value.detail


def test_set_role_is_persisted_and_returned(tmp_path: Path) -> None:
    store = account_store(tmp_path)
    user = store.create_user("Sam", "a-long-enough-password", role=Role.VIEWER)

    updated = store.set_role(user.actor_ref, Role.ADMINISTRATOR)
    assert updated.role is Role.ADMINISTRATOR
    assert store.get_user(user.actor_ref).role is Role.ADMINISTRATOR
    assert store.authenticate(user.actor_ref, "a-long-enough-password").role is Role.ADMINISTRATOR


def test_short_password_and_duplicate_account_are_rejected(tmp_path: Path) -> None:
    store = account_store(tmp_path)
    with pytest.raises(errors.InvalidInput):
        store.create_user("Short", "abc", role=Role.VIEWER)

    store.create_user("First", "a-long-enough-password", role=Role.VIEWER, actor_ref="user_fixed")
    with pytest.raises(errors.InvalidInput) as dup:
        store.create_user("Second", "another-long-password", role=Role.VIEWER, actor_ref="user_fixed")
    assert dup.value.code == "INVALID_INPUT"


# ---------------------------------------------------------------------------
# Store: a plaintext password never reaches the file
# ---------------------------------------------------------------------------
def test_account_file_never_contains_plaintext_password(tmp_path: Path) -> None:
    password = "Correct-Horse-Battery-9"
    store = account_store(tmp_path)
    user = store.create_user("Dana Reviewer", password, role=Role.ADMINISTRATOR)

    path = store.path
    assert path == tmp_path / "host-secrets" / "accounts.json"
    text = path.read_text(encoding="utf-8")
    assert password not in text

    payload = json.loads(text)
    assert payload["schema_version"] == "1.0"
    assert payload["users"][0]["role"] == "administrator"
    verifier = payload["users"][0]["password_verifier"]
    assert verifier.startswith("scrypt$")
    assert password not in verifier

    # The verifier never leaks through a repr, which is what reaches a traceback.
    assert "scrypt$" not in repr(user)
    assert password not in repr(user)


def test_account_store_refuses_workspace_and_review_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "Job - Operations Manager"
    workspace.mkdir()

    with pytest.raises(errors.InvalidInput) as inside_workspace:
        AccountStore(workspace / "accounts.json", workspace_root=workspace)
    assert inside_workspace.value.code == "INVALID_INPUT"

    with pytest.raises(errors.InvalidInput) as inside_review:
        AccountStore(workspace / ".review" / "accounts.json")
    assert inside_review.value.code == "INVALID_INPUT"


# ---------------------------------------------------------------------------
# Sessions: instance binding
# ---------------------------------------------------------------------------
def test_session_for_one_instance_is_refused_for_another() -> None:
    sessions = SessionStore()
    session = sessions.issue("inst_A", "user_1", Role.REVIEWER)

    assert sessions.resolve(session.session_id, instance_id="inst_A").instance_id == "inst_A"

    with pytest.raises(InstanceMismatch) as mismatch:
        sessions.resolve(session.session_id, instance_id="inst_B")
    assert mismatch.value.code == "INSTANCE_MISMATCH"
    # The session is still usable for its own instance afterwards.
    assert sessions.resolve(session.session_id, instance_id="inst_A").actor_ref == "user_1"


def test_session_cookie_name_is_instance_scoped() -> None:
    from resume_review.auth import session_cookie_name

    first = session_cookie_name("inst_A")
    second = session_cookie_name("inst_B")
    assert first != second
    assert first.startswith("rr_sess_")


# ---------------------------------------------------------------------------
# Sessions: absolute and idle expiry
# ---------------------------------------------------------------------------
def test_expired_idle_and_absolute_sessions_are_rejected() -> None:
    idle_clock = FakeClock()
    # idle (30s) expires before absolute (600s) so the idle path is exercised first.
    idle_store = SessionStore(clock=idle_clock, absolute_ttl_seconds=600, idle_ttl_seconds=30)
    session = idle_store.issue("inst_A", "user_1", Role.VIEWER)

    idle_clock.advance(20)
    assert idle_store.resolve(session.session_id).last_seen_at  # touch refreshes idle
    idle_clock.advance(20)  # 40s since issue, 20s since last touch
    idle_store.resolve(session.session_id)
    idle_clock.advance(31)  # 31s since last touch
    with pytest.raises(SessionExpired) as idle_expired:
        idle_store.resolve(session.session_id)
    assert idle_expired.value.code == "SESSION_EXPIRED"
    assert idle_expired.value.detail["reason"] == "idle"

    absolute_clock = FakeClock()
    # Idle is long so only the absolute bound can fire.
    absolute_store = SessionStore(clock=absolute_clock, absolute_ttl_seconds=60, idle_ttl_seconds=600)
    absolute = absolute_store.issue("inst_A", "user_1", Role.VIEWER)
    absolute_clock.advance(61)
    with pytest.raises(SessionExpired) as absolute_expired:
        absolute_store.resolve(absolute.session_id)
    assert absolute_expired.value.detail["reason"] == "absolute"

    # Sweeping drops a dead session that was never resolved.
    absolute_store.issue("inst_A", "user_2", Role.VIEWER)
    absolute_clock.advance(61)
    assert absolute_store.sweep_expired() >= 1
    assert absolute_store.active_count() == 0


def test_revoke_and_revoke_actor_end_sessions() -> None:
    sessions = SessionStore()
    first = sessions.issue("inst_A", "user_1", Role.VIEWER)
    second = sessions.issue("inst_A", "user_1", Role.VIEWER)

    assert sessions.revoke(first.session_id) is True
    assert sessions.revoke(first.session_id) is False
    with pytest.raises(errors.Unauthenticated):
        sessions.resolve(first.session_id)

    assert sessions.revoke_actor("user_1") == 1
    with pytest.raises(errors.Unauthenticated):
        sessions.resolve(second.session_id)


# ---------------------------------------------------------------------------
# Pairing
# ---------------------------------------------------------------------------
def test_pairing_token_works_exactly_once() -> None:
    clock = FakeClock()
    sessions = SessionStore(clock=clock)
    pairing = PairingManager("inst_A", sessions, default_port=8765, clock=clock, operating_user="alice")

    ticket = pairing.create(actor_ref="user_1", role=Role.REVIEWER, display_name="Alice")
    assert ticket.url.startswith("http://127.0.0.1:8765/pair?token=")
    assert ticket.token in ticket.url
    # A loggable summary never carries the token.
    assert ticket.token not in json.dumps(pairing.describe(ticket))

    session = pairing.exchange(ticket.token, operating_user="alice")
    assert session.instance_id == "inst_A"
    assert session.actor_ref == "user_1"
    assert session.is_local_owner is True
    assert sessions.resolve(session.session_id).role is Role.REVIEWER

    with pytest.raises(PairingTokenInvalid) as reused:
        pairing.exchange(ticket.token, operating_user="alice")
    assert reused.value.code == "PAIRING_TOKEN_INVALID"
    assert pairing.active_count() == 0


def test_pairing_token_expires_and_is_os_user_bound() -> None:
    clock = FakeClock()
    sessions = SessionStore(clock=clock)
    pairing = PairingManager("inst_A", sessions, default_port=8765, clock=clock, operating_user="alice")

    expiring = pairing.create(actor_ref="user_1", role=Role.REVIEWER)
    clock.advance(DEFAULT_PAIRING_TTL_SECONDS + 1)
    with pytest.raises(PairingTokenExpired) as expired:
        pairing.exchange(expiring.token, operating_user="alice")
    assert expired.value.code == "PAIRING_TOKEN_EXPIRED"

    other_user = pairing.create(actor_ref="user_1", role=Role.REVIEWER)
    with pytest.raises(PairingTokenInvalid) as wrong_user:
        pairing.exchange(other_user.token, operating_user="mallory")
    assert wrong_user.value.code == "PAIRING_TOKEN_INVALID"

    with pytest.raises(PairingTokenInvalid) as unknown:
        pairing.exchange("not-a-real-token", operating_user="alice")
    assert unknown.value.code == "PAIRING_TOKEN_INVALID"

    # Sweeping removes an expired, never-exchanged token.
    pairing.create(actor_ref="user_1", role=Role.REVIEWER)
    assert pairing.active_count() == 1
    clock.advance(DEFAULT_PAIRING_TTL_SECONDS + 1)
    assert pairing.sweep_expired() == 1
    assert pairing.active_count() == 0


def test_pairing_requires_a_port() -> None:
    sessions = SessionStore()
    pairing = PairingManager("inst_A", sessions, operating_user="alice")
    with pytest.raises(errors.InvalidInput):
        pairing.create(actor_ref="user_1")


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------
def test_csrf_missing_and_wrong_tokens_fail() -> None:
    store = CsrfStore()
    session_id = "sess_csrf"
    token = store.issue(session_id)

    assert store.verify(session_id, token) is True
    assert store.verify(session_id, "wrong-token") is False
    assert store.verify("sess_absent", token) is False
    assert store.verify(session_id, None) is False
    assert store.verify(None, token) is False

    check_csrf(session_id, token, store=store)  # no raise

    with pytest.raises(CsrfFailed) as missing:
        check_csrf(session_id, None, store=store)
    assert missing.value.code == "CSRF_FAILED"

    with pytest.raises(CsrfFailed) as wrong:
        check_csrf(session_id, "wrong-token", store=store)
    assert wrong.value.code == "CSRF_FAILED"

    # Rotating invalidates the old token.
    rotated = store.rotate(session_id)
    assert rotated != token
    assert store.verify(session_id, token) is False
    assert store.verify(session_id, rotated) is True

    store.discard(session_id)
    assert store.verify(session_id, rotated) is False


# ---------------------------------------------------------------------------
# Origin and Host
# ---------------------------------------------------------------------------
def test_forged_origin_is_refused() -> None:
    allowed = ["http://127.0.0.1:8765"]

    check_origin({"Origin": "http://127.0.0.1:8765"}, allowed)
    check_origin({"Origin": "http://127.0.0.1:8765/"}, allowed)

    with pytest.raises(OriginRejected) as forged:
        check_origin({"Origin": "http://evil.example"}, allowed)
    assert forged.value.code == "ORIGIN_REJECTED"

    # The opaque null origin is never same-origin.
    with pytest.raises(OriginRejected):
        check_origin({"Origin": "null"}, allowed)

    # Mutations must not accept an absent Origin.
    with pytest.raises(OriginRejected) as required:
        check_origin({}, allowed, require=True)
    assert required.value.code == "ORIGIN_REJECTED"
    check_origin({}, allowed)  # read-only request without an Origin is fine


def test_unexpected_host_is_refused() -> None:
    allowed = ["127.0.0.1:8765", "localhost:8765"]

    check_host({"Host": "127.0.0.1:8765"}, allowed)
    check_host({"Host": "LOCALHOST:8765"}, allowed)

    with pytest.raises(HostRejected) as unexpected:
        check_host({"Host": "evil.example"}, allowed)
    assert unexpected.value.code == "HOST_REJECTED"

    with pytest.raises(HostRejected):
        check_host({}, allowed)

    with pytest.raises(HostRejected):
        check_host({"Host": "127.0.0.1:8765@evil.example"}, allowed)


def test_check_mutation_runs_all_three_checks() -> None:
    store = CsrfStore()
    token = store.issue("sess_m")
    allowed_origins = ["http://127.0.0.1:8765"]
    allowed_hosts = ["127.0.0.1:8765"]

    check_mutation(
        headers={"Host": "127.0.0.1:8765", "Origin": "http://127.0.0.1:8765"},
        allowed_origins=allowed_origins,
        allowed_hosts=allowed_hosts,
        session_id="sess_m",
        csrf_token=token,
        csrf_store=store,
    )

    with pytest.raises(HostRejected):
        check_mutation(
            headers={"Host": "evil.example", "Origin": "http://127.0.0.1:8765"},
            allowed_origins=allowed_origins,
            allowed_hosts=allowed_hosts,
            session_id="sess_m",
            csrf_token=token,
            csrf_store=store,
        )

    with pytest.raises(OriginRejected):
        check_mutation(
            headers={"Host": "127.0.0.1:8765", "Origin": "http://evil.example"},
            allowed_origins=allowed_origins,
            allowed_hosts=allowed_hosts,
            session_id="sess_m",
            csrf_token=token,
            csrf_store=store,
        )

    with pytest.raises(CsrfFailed):
        check_mutation(
            headers={"Host": "127.0.0.1:8765", "Origin": "http://127.0.0.1:8765"},
            allowed_origins=allowed_origins,
            allowed_hosts=allowed_hosts,
            session_id="sess_m",
            csrf_token=None,
            csrf_store=store,
        )


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------
def test_viewer_cannot_review_and_reviewer_cannot_administer() -> None:
    viewer = principal("u_view", Role.VIEWER)
    reviewer = principal("u_rev", Role.REVIEWER)
    admin = principal("u_admin", Role.ADMINISTRATOR)

    with pytest.raises(errors.Forbidden) as viewer_denied:
        require_role(viewer, Role.REVIEWER)
    assert viewer_denied.value.code == "ROLE_INSUFFICIENT"

    with pytest.raises(errors.Forbidden) as reviewer_denied:
        require_role(reviewer, Role.ADMINISTRATOR)
    assert reviewer_denied.value.code == "ROLE_INSUFFICIENT"

    check_role = require_role(reviewer, Role.REVIEWER)
    assert check_role is reviewer
    assert require_role(admin, Role.ADMINISTRATOR) is admin

    with pytest.raises(errors.Unauthenticated):
        require_role(None, Role.VIEWER)


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
def test_rate_limiter_bounds_and_recovers() -> None:
    clock = NumClock()
    limiter = RateLimiter(limit=3, window_seconds=60.0, clock=clock)

    assert limiter.allow("pairing")
    assert limiter.allow("pairing")
    assert limiter.allow("pairing")
    assert limiter.allow("pairing") is False
    assert limiter.remaining("pairing") == 0

    with pytest.raises(RateLimited) as limited:
        check_rate_limit("pairing", limiter=limiter)
    assert limited.value.code == "RATE_LIMITED"
    assert limited.value.http_status == 429

    clock.value = 61.0
    assert limiter.allow("pairing") is True
    assert limiter.remaining("pairing") == 2

    # A separate key is unaffected.
    assert limiter.allow("chat") is True
