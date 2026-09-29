"""Host-local account store for authenticated reviewer identity.

Authority: PRD sections 15.1 and 15.3, acceptance test AT-33.

    "Use distinct authenticated reviewer identities with Viewer, Reviewer, and
     Administrator roles. A display name typed into the page is not
     authentication. The shared helper may use a tested host-local account store
     or an existing identity-aware proxy."

The account file lives in a protected host directory the caller supplies. It must
never live inside the portable job folder and never inside the portable database
(PRD section 16.2: "Keep tokens and login secrets outside shared folders, generated
HTML, and portable backups"). :func:`assert_host_local_secret_path` enforces the
two mechanically checkable parts of that rule - containment in the workspace and
network-filesystem storage - and raises rather than warning.

Passwords are hashed with :func:`hashlib.scrypt` from the standard library, so the
application needs no external ``bcrypt``/``argon2`` wheel. The cost parameters are
module constants and are recorded inside each verifier string, so a future cost
increase can re-hash on next successful sign-in without invalidating old rows.

Nothing here logs or returns a plaintext password. The verifier is excluded from
the dataclass ``repr`` so it cannot reach a traceback or a log line by accident.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..errors import Code, InvalidInput, NotFound, ResumeReviewError, Unauthenticated
from ..models import Role
from ..storage.topology import TopologyKind, probe_topology
from ..util import constant_time_equals, new_id, now_iso

__all__ = [
    "AccountStore",
    "AccountStoreCorrupt",
    "User",
    "MIN_PASSWORD_LENGTH",
    "SCRYPT_N",
    "SCRYPT_R",
    "SCRYPT_P",
    "SCRYPT_DKLEN",
    "hash_password",
    "verify_password",
    "assert_host_local_secret_path",
]

# scrypt cost. 128 * N * r bytes of memory (about 16 MiB) and a few tens of
# milliseconds on a modern host. Tests construct many users, so this is
# deliberately at the low end of "clearly memory-hard" rather than maximal; raising
# it only makes existing verifiers slower to check, never invalid.
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32

#: scrypt needs an explicit arena larger than 128*N*r; the OpenSSL default (32 MiB)
#: is only just above our working set, so ask for room to spare.
_SCRYPT_MAXMEM = 64 * 1024 * 1024

MIN_PASSWORD_LENGTH = 8

_VERIFIER_SCHEME = "scrypt"

#: Fixed, non-secret salt for the timing-equalising verifier below.
_FIXED_SALT = bytes(range(16))


class AccountStoreCorrupt(ResumeReviewError):
    """The host account file exists but cannot be parsed. Auth fails closed."""

    code = Code.INTERNAL_ERROR
    http_status = 500


def hash_password(
    password: str,
    *,
    salt: bytes | None = None,
    n: int = SCRYPT_N,
    r: int = SCRYPT_R,
    p: int = SCRYPT_P,
    dklen: int = SCRYPT_DKLEN,
) -> str:
    """Derive a self-describing verifier string for ``password``.

    The returned value has the form ``scrypt$N$r$p$<salt-hex>$<hash-hex>`` so the
    parameters travel with the hash and a cost change is a per-row concern. The
    salt is random per call unless one is supplied for a deterministic test.
    """
    if not isinstance(password, str) or password == "":
        raise InvalidInput("A password is required.")
    if salt is None:
        salt = os.urandom(16)
    digest = _derive(password, salt, n, r, p, dklen)
    return f"{_VERIFIER_SCHEME}${n}${r}${p}${salt.hex()}${digest.hex()}"


def verify_password(password: str, verifier: str) -> bool:
    """Constant-time check of ``password`` against a stored verifier.

    A malformed verifier returns ``False`` rather than raising; a corrupt row must
    fail the sign-in, never the process.
    """
    if not isinstance(password, str) or not password:
        return False
    try:
        scheme, raw_n, raw_r, raw_p, salt_hex, hash_hex = verifier.split("$")
        if scheme != _VERIFIER_SCHEME:
            return False
        n, r, p = int(raw_n), int(raw_r), int(raw_p)
        salt = bytes.fromhex(salt_hex)
        # The derived length is read back from the stored hash so a verifier made
        # with a non-default dklen still validates instead of silently failing.
        dklen = len(hash_hex) // 2
        if dklen <= 0:
            return False
    except (AttributeError, ValueError, TypeError):
        return False
    digest = _derive(password, salt, n, r, p, dklen)
    return constant_time_equals(digest.hex(), hash_hex)


def _derive(password: str, salt: bytes, n: int, r: int, p: int, dklen: int) -> bytes:
    import hashlib

    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=dklen,
        maxmem=_SCRYPT_MAXMEM,
    )


# Populate the timing-equalising verifier once at import. Derived from a fixed,
# non-secret input; it is never accepted as a real credential because no account
# row can carry it.
_DUMMY_VERIFIER = hash_password("timing-equaliser-not-a-credential", salt=_FIXED_SALT)


# ---------------------------------------------------------------------------
# Location guard
# ---------------------------------------------------------------------------
def assert_host_local_secret_path(
    path: str | os.PathLike[str], *, workspace_root: str | os.PathLike[str] | None = None
) -> Path:
    """Return ``path`` after confirming it may hold a host-local secret.

    Rejects, in order: a path inside the portable workspace, a path anywhere under
    a ``.review`` directory (the portable database and its siblings), and a path on
    a network filesystem. ``workspace_root`` is optional because a caller may not
    have the job folder at hand, but it should be supplied whenever it is known.
    """
    candidate = Path(os.path.abspath(os.fspath(Path(path).expanduser())))

    if ".review" in candidate.parts:
        raise InvalidInput(
            "The account store must not live inside the portable reserved directory.",
            detail={"reason": "inside_review_directory"},
        )

    if workspace_root is not None:
        root = Path(os.path.realpath(os.path.abspath(os.fspath(workspace_root))))
        target = Path(os.path.realpath(candidate))
        try:
            target.relative_to(root)
        except ValueError:
            pass
        else:
            raise InvalidInput(
                "The account store must live outside the portable job folder.",
                detail={"reason": "inside_workspace"},
            )

    report = probe_topology(candidate.parent if candidate.parent.exists() else candidate)
    if report.kind is TopologyKind.NETWORK:
        raise InvalidInput(
            "The account store must live on storage local to the host; a shared "
            "folder would export sign-in secrets with the job folder.",
            detail={"reason": "network_filesystem", "topology": report.to_dict()},
        )
    return candidate


# ---------------------------------------------------------------------------
# Value type
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class User:
    """One authenticated host-local account.

    ``password_verifier`` is excluded from ``repr`` so it cannot reach a log line,
    a traceback frame, or an error envelope. ``role`` is the only thing that grants
    privilege; there is no separate permission list to drift out of sync.
    """

    actor_ref: str
    display_name: str
    role: Role
    password_verifier: str = field(default="", repr=False)
    created_at: str = ""
    updated_at: str = ""

    def to_public_dict(self) -> dict[str, Any]:
        """A serialisable view with no secret material, safe to return over the API."""
        return {
            "actor_ref": self.actor_ref,
            "display_name": self.display_name,
            "role": str(self.role.value),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------
class AccountStore:
    """A JSON-backed, process-serialised host account store.

    The file is small (a handful of reviewers) and written whole, so there is no
    need for a database engine. Every mutation rewrites the file through an atomic
    replace so a crash mid-write cannot leave a half-parsed credential file.
    """

    SCHEMA_VERSION = "1.0"

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        workspace_root: str | os.PathLike[str] | None = None,
        clock: Callable[[], str] = now_iso,
    ) -> None:
        self._path = assert_host_local_secret_path(path, workspace_root=workspace_root)
        self._clock = clock
        self._lock = threading.RLock()
        self._users: dict[str, User] | None = None

    # -- introspection -----------------------------------------------------
    @property
    def path(self) -> Path:
        return self._path

    def exists(self) -> bool:
        return self._path.exists()

    def user_count(self) -> int:
        return len(self._load())

    # -- reads -------------------------------------------------------------
    def get_user(self, actor_ref: str) -> User | None:
        """Return the account, or ``None`` when no such account exists."""
        return self._load().get(actor_ref)

    def require_user(self, actor_ref: str) -> User:
        user = self._load().get(actor_ref)
        if user is None:
            raise NotFound("That account was not found.", detail={"reason": "unknown_account"})
        return user

    def list_users(self) -> list[User]:
        return [self._load()[ref] for ref in sorted(self._load())]

    # -- writes ------------------------------------------------------------
    def create_user(
        self,
        display_name: str,
        password: str,
        *,
        role: Role = Role.VIEWER,
        actor_ref: str | None = None,
    ) -> User:
        """Create an account and return it.

        ``actor_ref`` defaults to a freshly minted opaque identifier. The display
        name is a label only; it never becomes the actor reference, because a name
        typed into a page is not authentication (PRD section 15.3).
        """
        role = _coerce_role(role)
        name = (display_name or "").strip()
        if not name:
            raise InvalidInput("A display name is required.")
        if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
            raise InvalidInput(
                f"The password must be at least {MIN_PASSWORD_LENGTH} characters.",
                detail={"reason": "password_too_short"},
            )
        ref = actor_ref or new_id("user")
        if not ref or not isinstance(ref, str):
            raise InvalidInput("An actor reference must be a non-empty string.")

        with self._lock:
            users = self._load()
            if ref in users:
                raise InvalidInput(
                    "An account with that identifier already exists.",
                    detail={"reason": "account_exists"},
                )
            timestamp = self._clock()
            user = User(
                actor_ref=ref,
                display_name=name,
                role=role,
                password_verifier=hash_password(password),
                created_at=timestamp,
                updated_at=timestamp,
            )
            users[ref] = user
            self._persist(users)
            return user

    def set_role(self, actor_ref: str, role: Role) -> User:
        """Change a user's role and return the updated account."""
        role = _coerce_role(role)
        with self._lock:
            users = self._load()
            current = users.get(actor_ref)
            if current is None:
                raise NotFound("That account was not found.", detail={"reason": "unknown_account"})
            updated = User(
                actor_ref=current.actor_ref,
                display_name=current.display_name,
                role=role,
                password_verifier=current.password_verifier,
                created_at=current.created_at,
                updated_at=self._clock(),
            )
            users[actor_ref] = updated
            self._persist(users)
            return updated

    # -- authentication ----------------------------------------------------
    def authenticate(self, actor_ref: str, password: str) -> User:
        """Verify credentials and return the account, or raise ``Unauthenticated``.

        An unknown account and a wrong password produce the same error and spend
        comparable time, so the response cannot be used to enumerate valid accounts.
        """
        if not isinstance(actor_ref, str) or actor_ref == "":
            _burn_unknown_account_time(password)
            raise Unauthenticated(
                "Those sign-in details were not accepted.",
                detail={"reason": "credentials"},
            )
        user = self._load().get(actor_ref)
        if user is None:
            _burn_unknown_account_time(password)
            raise Unauthenticated(
                "Those sign-in details were not accepted.",
                detail={"reason": "credentials"},
            )
        if not verify_password(password, user.password_verifier):
            raise Unauthenticated(
                "Those sign-in details were not accepted.",
                detail={"reason": "credentials"},
            )
        return user

    # -- persistence -------------------------------------------------------
    def _load(self) -> dict[str, User]:
        with self._lock:
            if self._users is not None:
                return self._users
            users: dict[str, User] = {}
            if self._path.exists():
                try:
                    raw = json.loads(self._path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise AccountStoreCorrupt(
                        "The host account store could not be read; sign-in is disabled "
                        "until an administrator repairs or removes it.",
                        detail={"reason": "unreadable"},
                    ) from exc
                if not isinstance(raw, dict):
                    raise AccountStoreCorrupt(
                        "The host account store is not in the expected format.",
                        detail={"reason": "shape"},
                    )
                for row in raw.get("users", []):
                    user = _user_from_row(row)
                    if user is not None:
                        users[user.actor_ref] = user
            self._users = users
            return users

    def _persist(self, users: dict[str, User]) -> None:
        payload = {
            "schema_version": self.SCHEMA_VERSION,
            "updated_at": self._clock(),
            "users": [
                {
                    "actor_ref": u.actor_ref,
                    "display_name": u.display_name,
                    "role": str(u.role.value),
                    "password_verifier": u.password_verifier,
                    "created_at": u.created_at,
                    "updated_at": u.updated_at,
                }
                for u in (users[ref] for ref in sorted(users))
            ],
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # A credential file is only ever readable by the operating user. The
        # directory mode is best-effort because Windows uses ACLs, not POSIX bits.
        try:
            os.chmod(self._path.parent, 0o700)
        except OSError:
            pass

        text = json.dumps(payload, indent=2, sort_keys=True)
        handle_fd, tmp_name = tempfile.mkstemp(prefix=".accounts-", dir=str(self._path.parent))
        try:
            with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(tmp_name, 0o600)
            except OSError:
                pass
            # An atomic replace of a small config file, deliberately not a managed
            # applicant move: this file sits outside the workspace and no-clobber
            # semantics do not apply. The replace is what makes a crash harmless.
            os.replace(tmp_name, self._path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise


def _burn_unknown_account_time(password: object) -> None:
    """Spend one scrypt derivation so unknown-account timing resembles a real check."""
    candidate = password if isinstance(password, str) and password else "unknown-account"
    verify_password(candidate, _DUMMY_VERIFIER)


def _coerce_role(role: Role | str) -> Role:
    if isinstance(role, Role):
        return role
    try:
        return Role(str(role))
    except ValueError as exc:
        raise InvalidInput("That role is not recognised.", detail={"reason": "role"}) from exc


def _user_from_row(row: Any) -> User | None:
    if not isinstance(row, dict):
        return None
    actor_ref = row.get("actor_ref")
    verifier = row.get("password_verifier")
    if not isinstance(actor_ref, str) or not actor_ref:
        return None
    try:
        role = Role(str(row.get("role", "viewer")))
    except ValueError:
        return None
    return User(
        actor_ref=actor_ref,
        display_name=str(row.get("display_name", "")),
        role=role,
        password_verifier=verifier if isinstance(verifier, str) else "",
        created_at=str(row.get("created_at", "")),
        updated_at=str(row.get("updated_at", "")),
    )
