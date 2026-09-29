"""Route and privacy policy for the restricted OpenClaw analysis route.

Authority: PRD sections 14.1, 14.2, 14.3 and 16.2, acceptance tests AT-35 and AT-36.

This module answers two questions, and refuses to guess at either:

1. **May inference happen at all?** The approved route is one of
   :class:`~resume_review.models.ModelRoute`. A route that processes applicant
   data is usable only when it is declared restricted *and* an operator has
   attested, out of band, that the analysis context really lacks the dangerous
   capabilities the PRD forbids. If that cannot be established the answer is no
   and manual review stays available (PRD 14.2: "The application must fail closed
   for inference when it cannot verify an adequately restricted route").

2. **Which route failed, and what must we do about it?** A lost route in
   ``LOCAL_ONLY`` mode is not an ordinary dependency failure. It is precisely the
   moment a fallback to a remote provider would be tempting, so
   :meth:`RoutePolicy.route_failure_error` translates it into
   ``LOCAL_ONLY_FALLBACK_BLOCKED`` and keeps the underlying code in ``detail``.
   Nothing in this module retries, re-targets, or opens a second route; the
   adapter holds exactly one configured route and never builds another.

The attestation is a *statement by the operator*, not proof. The only proof that
per-agent tool policy is really applied lives in the OpenClaw configuration and
in the live compatibility gate (PRD 14.3). :func:`verify_route` therefore
separates what the probe confirmed from what it could not, and marks any
non-live (mock) probe as failing that gate rather than passing it.
"""

from __future__ import annotations

import dataclasses
import ipaddress
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from ..errors import Code, ResumeReviewError
from ..models import ModelRoute
from ..util import now_iso

__all__ = [
    "REQUIRED_DENIED_TOOLS",
    "AGENT_TARGET_PREFIX",
    "RestrictedContextRequirements",
    "RouteAttestation",
    "RoutePolicy",
    "EndpointProbe",
    "RouteVerification",
    "ProbeCallable",
    "verify_route",
    "split_endpoint",
    "host_class",
]

#: Prefix OpenClaw uses to address a specific agent in the OpenAI ``model`` field
#: (docs.openclaw.ai/gateway/openai-http-api, checked 2026-09-29: ``openclaw/<agentId>``).
#: The adapter composes this itself so a caller cannot smuggle the ``agent:`` alias or
#: any other target spelling into the request.
AGENT_TARGET_PREFIX = "openclaw/"

#: Documented tool names that must be denied to the analysis context (PRD 14.2:
#: "no shell, write/edit, browser-control, messaging, credential-reading,
#: unrestricted file-reading, or cross-session access"). Names are taken from the
#: per-agent tool-permission reference so an operator can paste them into
#: ``agents.entries.<agentId>.tools.deny`` verbatim
#: (docs.openclaw.ai/gateway/security/tool-permissions, checked 2026-09-29).
#:
#: ``read`` is included because the PRD forbids *unrestricted* file reading. An
#: operator who needs the agent to read its own skill files may instead keep
#: ``read`` allowed and scope it with ``tools.fs.workspaceOnly``; that alternative
#: is recorded by the ``no_unrestricted_file_read`` attestation flag.
REQUIRED_DENIED_TOOLS: tuple[str, ...] = (
    "exec",
    "process",
    "write",
    "edit",
    "apply_patch",
    "read",
    "browser",
    "canvas",
    "nodes",
    "gateway",
    "cron",
    "sessions_spawn",
    "sessions_send",
)

#: A host that answers on the loopback interface. In LOCAL_ONLY mode this is the
#: only ingress the policy accepts.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


def split_endpoint(base_url: str) -> tuple[str, str, int]:
    """Split a base URL into ``(scheme, host, port)`` and discard everything else.

    The path, query, fragment and any userinfo are dropped rather than trusted: a
    caller-supplied endpoint path is exactly what the adapter must never forward
    (PRD 14.1). Returns the default port when the URL omits one.
    """
    parts = urlsplit(base_url)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise ResumeReviewError(
            "The OpenClaw base URL must use http or https.",
            code=Code.ROUTE_POLICY_VIOLATION,
            http_status=422,
        )
    if parts.username or parts.password:
        raise ResumeReviewError(
            "The OpenClaw base URL must not embed credentials; the shared secret is supplied separately.",
            code=Code.ROUTE_POLICY_VIOLATION,
            http_status=422,
        )
    host = parts.hostname or ""
    if not host:
        raise ResumeReviewError(
            "The OpenClaw base URL must name a host.",
            code=Code.ROUTE_POLICY_VIOLATION,
            http_status=422,
        )
    default_port = 443 if scheme == "https" else 80
    return scheme, host, parts.port or default_port


def host_class(host: str) -> str:
    """Classify a host as ``loopback``, ``private``, ``global`` or ``unknown``.

    Used to enforce the documented ingress guidance (keep the endpoint on
    loopback/private ingress). A hostname that is not a literal IP is ``unknown``:
    resolving it would be network I/O and could itself leak the deployment shape,
    so the caller must decide, and an unknown host is never treated as local.
    """
    lowered = host.strip().lower()
    if lowered in _LOOPBACK_HOSTS:
        return "loopback"
    try:
        address = ipaddress.ip_address(lowered)
    except ValueError:
        return "unknown"
    if address.is_loopback:
        return "loopback"
    if address.is_global:
        return "global"
    return "private"


#: (requirement field, attestation field, human label) for every restriction that
#: must hold before applicant data may be sent to a route.
_RESTRICTION_FLAGS: tuple[tuple[str, str, str], ...] = (
    ("forbid_shell", "no_shell", "shell/exec denied"),
    ("forbid_write_or_edit", "no_write_or_edit", "write/edit/apply_patch denied"),
    ("forbid_browser_control", "no_browser_control", "browser control denied"),
    ("forbid_messaging", "no_messaging", "messaging and send tools denied"),
    ("forbid_credential_read", "no_credential_read", "credential and secret read denied"),
    ("forbid_unrestricted_file_read", "no_unrestricted_file_read", "unrestricted file read denied"),
    ("forbid_cross_session", "no_cross_session", "cross-session access denied"),
    ("forbid_agent_spawning", "no_agent_spawning", "sub-agent spawning denied"),
    (
        "trusted_instruction_workspace",
        "trusted_instruction_workspace",
        "instruction workspace is trusted, not the applicant folder",
    ),
)

_REQUIREMENT_NAMES = frozenset(flag[0] for flag in _RESTRICTION_FLAGS)


@dataclass(frozen=True)
class RestrictedContextRequirements:
    """The restricted analysis context the PRD requires (PRD 14.2, AT-35).

    Every flag defaults to ``True`` and
    :meth:`RoutePolicy.assert_inference_allowed` refuses a data-processing route
    that has disabled any of them. The flags exist so the requirement can be
    *stated, tested and reported* rather than assumed; they are not a knob for
    weakening the route.
    """

    forbid_shell: bool = True
    forbid_write_or_edit: bool = True
    forbid_browser_control: bool = True
    forbid_messaging: bool = True
    forbid_credential_read: bool = True
    forbid_unrestricted_file_read: bool = True
    forbid_cross_session: bool = True
    forbid_agent_spawning: bool = True
    trusted_instruction_workspace: bool = True
    #: Tool names the route must deny; see :data:`REQUIRED_DENIED_TOOLS`.
    denied_tools: tuple[str, ...] = REQUIRED_DENIED_TOOLS

    @property
    def disabled(self) -> tuple[str, ...]:
        """Names of requirements that have been switched off. Must stay empty."""
        return tuple(
            spec.name
            for spec in dataclasses.fields(self)
            if spec.name in _REQUIREMENT_NAMES and not getattr(self, spec.name)
        )

    def describe(self) -> dict[str, Any]:
        return {
            "enabled": [flag[2] for flag in _RESTRICTION_FLAGS if getattr(self, flag[0])],
            "disabled": list(self.disabled),
            "denied_tools": list(self.denied_tools),
        }


@dataclass(frozen=True)
class RouteAttestation:
    """The operator's out-of-band confirmation that the route is restricted.

    Filled in by the trusted setup/orchestration context after checking the actual
    OpenClaw configuration, never by the analysis flow and never by a model. An
    incomplete attestation makes a data route unusable: the policy fails closed
    rather than sending applicant text to a route nobody verified.
    """

    attested_by: str
    attested_at: str
    no_shell: bool = False
    no_write_or_edit: bool = False
    no_browser_control: bool = False
    no_messaging: bool = False
    no_credential_read: bool = False
    no_unrestricted_file_read: bool = False
    no_cross_session: bool = False
    no_agent_spawning: bool = False
    trusted_instruction_workspace: bool = False
    #: Free text for the operator (tool-policy file revision, ticket, review note).
    #: Never contains a credential or an applicant name.
    notes: str = ""

    def uncovered(self, requirements: RestrictedContextRequirements) -> tuple[str, ...]:
        """Requirement names that are enabled but not attested."""
        missing: list[str] = []
        for requirement_name, attestation_name, _label in _RESTRICTION_FLAGS:
            if getattr(requirements, requirement_name) and not getattr(self, attestation_name):
                missing.append(requirement_name)
        return tuple(missing)

    @property
    def complete(self) -> bool:
        return not self.uncovered(RestrictedContextRequirements())

    def covers(self, requirements: RestrictedContextRequirements) -> bool:
        return not self.uncovered(requirements)

    def describe(self) -> dict[str, Any]:
        return {
            "attested_by": self.attested_by,
            "attested_at": self.attested_at,
            "confirmed": [flag[2] for flag in _RESTRICTION_FLAGS if getattr(self, flag[1])],
            "unconfirmed": [
                flag[2] for flag in _RESTRICTION_FLAGS if not getattr(self, flag[1])
            ],
            "notes": self.notes,
        }


@dataclass(frozen=True)
class RoutePolicy:
    """The approved analysis route and the restrictions it must satisfy."""

    route: ModelRoute
    restricted: bool
    requirements: RestrictedContextRequirements = field(default_factory=RestrictedContextRequirements)
    attestation: RouteAttestation | None = None
    #: Human-readable description of where inference runs, e.g. "local llama.cpp
    #: server on the storage host". No credential, no applicant data.
    provider_label: str | None = None
    #: Backend model identifier, informational only. Never sent as the request's
    #: ``model`` field (that field carries the agent target) and never a caller input.
    model_identifier: str | None = None
    notes: str = ""

    # -- classification ----------------------------------------------------
    @property
    def processes_applicant_data(self) -> bool:
        """True for routes that actually run inference over applicant text."""
        return self.route in (ModelRoute.LOCAL_ONLY, ModelRoute.APPROVED_PROVIDER)

    @property
    def is_local_only(self) -> bool:
        return self.route is ModelRoute.LOCAL_ONLY

    @property
    def is_fixture(self) -> bool:
        return self.route is ModelRoute.FIXTURE

    def missing_restrictions(self) -> tuple[str, ...]:
        """Everything that is not yet established. Empty means the route is usable."""
        if not self.processes_applicant_data:
            return ()
        missing: list[str] = []
        if not self.restricted:
            missing.append("route_declared_restricted")
        missing.extend(self.requirements.disabled)
        if self.attestation is None:
            missing.extend(
                name for name, _att, _label in _RESTRICTION_FLAGS if getattr(self.requirements, name)
            )
        else:
            missing.extend(self.attestation.uncovered(self.requirements))
        return tuple(dict.fromkeys(missing))

    # -- the fail-closed gate ---------------------------------------------
    def assert_inference_allowed(self) -> None:
        """Refuse inference unless the route is verifiably restricted.

        Raises :class:`~resume_review.errors.ResumeReviewError` with one of
        ``ROUTE_UNAVAILABLE``, ``ROUTE_NOT_RESTRICTED`` or
        ``ROUTE_POLICY_VIOLATION``. The messages carry identifiers only: no
        endpoint path, no secret, no applicant data. Manual review is unaffected
        by every one of these refusals.
        """
        if self.route is ModelRoute.UNAVAILABLE:
            raise ResumeReviewError(
                "The approved analysis route is unavailable. Manual review remains available.",
                code=Code.ROUTE_UNAVAILABLE,
                http_status=503,
                detail={"route": str(self.route.value)},
            )
        if self.route is ModelRoute.FIXTURE:
            # A fixture is precomputed data, not a model call. Reaching the adapter
            # in fixture mode means something upstream mistook a fixture for a live
            # route; label it rather than serve fixture text as an analysis result.
            raise ResumeReviewError(
                "The fixture route performs no inference; use the deterministic fixture profiles directly.",
                code=Code.ROUTE_POLICY_VIOLATION,
                http_status=409,
                detail={"route": str(self.route.value)},
            )
        missing = self.missing_restrictions()
        if not missing:
            return
        if not self.restricted or self.requirements.disabled:
            raise ResumeReviewError(
                "This route is not a restricted analysis context, so applicant text is not sent to it. "
                "Manual review remains available.",
                code=Code.ROUTE_NOT_RESTRICTED,
                http_status=503,
                detail={
                    "route": str(self.route.value),
                    "missing": list(missing),
                    "disabled_requirements": list(self.requirements.disabled),
                },
            )
        raise ResumeReviewError(
            "The restricted analysis route has not been attested by an operator, so inference is refused. "
            "Manual review remains available.",
            code=Code.ROUTE_NOT_RESTRICTED,
            http_status=503,
            detail={"route": str(self.route.value), "missing": list(missing)},
        )

    # -- failure translation ----------------------------------------------
    def route_failure_error(
        self,
        underlying_code: str,
        *,
        message: str,
        retryable: bool = False,
        http_status: int = 503,
    ) -> ResumeReviewError:
        """Translate a transport failure into the code this policy requires.

        In ``LOCAL_ONLY`` mode the loss of the route is reported as
        ``LOCAL_ONLY_FALLBACK_BLOCKED``: the failure is not merely a dependency
        failure, it is the point at which a remote fallback must be refused
        (PRD 14.2, AT-36). The underlying transport code is preserved in ``detail``
        so the operator can still diagnose the cause. No caller may retry a
        different route from here; there is no second route to retry.
        """
        if self.is_local_only:
            return ResumeReviewError(
                message,
                code=Code.LOCAL_ONLY_FALLBACK_BLOCKED,
                http_status=http_status,
                retryable=False,
                detail={"route": str(self.route.value), "underlying_code": underlying_code},
            )
        return ResumeReviewError(
            message,
            code=underlying_code,
            http_status=http_status,
            retryable=retryable,
            detail={"route": str(self.route.value)},
        )

    def describe(self) -> dict[str, Any]:
        """Safe summary for status output and docs. Never contains a secret."""
        return {
            "route": str(self.route.value),
            "restricted": self.restricted,
            "provider_label": self.provider_label,
            "model_identifier": self.model_identifier,
            "processes_applicant_data": self.processes_applicant_data,
            "requirements": self.requirements.describe(),
            "attestation": self.attestation.describe() if self.attestation else None,
            "missing_restrictions": list(self.missing_restrictions()),
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# Live route verification (PRD 14.3 gate)
# ---------------------------------------------------------------------------
@dataclass
class EndpointProbe:
    """Mechanical result of the cheap live probe. Data only, never a judgement.

    Produced by :func:`resume_review.openclaw_adapter.client.probe_route`; consumed
    by :func:`verify_route`, which decides whether it satisfies the PRD 14.3 gate.
    """

    endpoint_label: str
    reachable: bool = False
    authenticated: bool = False
    http_status: int | None = None
    agent_targets: list[str] = field(default_factory=list)
    agent_target_present: bool = False
    #: False when the call was made through an injected (mock) transport. A mock
    #: result can never pass the live compatibility gate.
    is_live: bool = True
    host_class: str = "unknown"
    elapsed_ms: int = 0
    error_code: str | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "endpoint_label": self.endpoint_label,
            "reachable": self.reachable,
            "authenticated": self.authenticated,
            "http_status": self.http_status,
            "agent_targets": list(self.agent_targets),
            "agent_target_present": self.agent_target_present,
            "is_live": self.is_live,
            "host_class": self.host_class,
            "elapsed_ms": self.elapsed_ms,
            "error_code": self.error_code,
            "detail": self.detail,
        }


@dataclass
class RouteVerification:
    """What a live probe could and could not confirm about the route (PRD 14.3).

    ``ok`` is true only for a genuinely live probe of a restricted data route whose
    allowlisted agent target is listed by the gateway and whose restrictions an
    operator has attested. Everything else is reported as unconfirmed, and
    :attr:`detail` says plainly why a mock result does not satisfy the gate.
    """

    route: ModelRoute
    ok: bool
    is_live: bool
    checked_at: str
    endpoint_label: str
    confirmed: list[str] = field(default_factory=list)
    unconfirmed: list[str] = field(default_factory=list)
    detail: str = ""
    http_status: int | None = None
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "route": str(self.route.value),
            "ok": self.ok,
            "is_live": self.is_live,
            "checked_at": self.checked_at,
            "endpoint_label": self.endpoint_label,
            "confirmed": list(self.confirmed),
            "unconfirmed": list(self.unconfirmed),
            "detail": self.detail,
            "http_status": self.http_status,
            "error_code": self.error_code,
        }


#: A zero-argument awaitable returning one :class:`EndpointProbe`.
ProbeCallable = Callable[[], Awaitable[EndpointProbe]]

_ALWAYS_UNCONFIRMED = (
    "per-agent tool policy is not exposed by the HTTP endpoint; it is asserted by operator "
    "attestation and must be checked in the OpenClaw configuration",
    "host sandbox and OS-level isolation are not observable from an HTTP response",
    "whether the backend model runs on the storage host is not observable from an HTTP response",
)


async def verify_route(route_policy: RoutePolicy, probe: ProbeCallable) -> RouteVerification:
    """Run a cheap live probe and report honestly what it established.

    A mock or injected-transport probe never passes: the PRD 14.3 gate says
    "Mock results do not count as passing this gate", and this function says so in
    the returned ``detail`` as well as failing ``ok``. No exception is raised for a
    failed probe; the caller decides whether to refuse inference using
    :meth:`RoutePolicy.assert_inference_allowed`.
    """
    result = await probe()
    checked_at = now_iso()
    confirmed: list[str] = []
    unconfirmed: list[str] = list(_ALWAYS_UNCONFIRMED)
    # Conditions the probe *can* observe and that the gate requires. Kept separate
    # from ``unconfirmed`` because the always-unconfirmed items above are not
    # observable over HTTP and so cannot be part of the pass condition.
    blockers: list[str] = []

    if result.reachable:
        confirmed.append(f"endpoint reachable at {result.endpoint_label}")
    else:
        unconfirmed.append(f"endpoint not reachable at {result.endpoint_label}")
    if result.authenticated:
        confirmed.append("gateway accepted the shared secret")
    elif result.reachable:
        unconfirmed.append("gateway did not accept the shared secret")

    if route_policy.processes_applicant_data:
        if result.agent_target_present:
            confirmed.append("allowlisted agent target is listed by the gateway")
        else:
            unconfirmed.append("allowlisted agent target was not confirmed by the gateway")
        if route_policy.attestation is None:
            unconfirmed.append("no operator attestation of the restricted analysis context")
        else:
            missing = result_attestation_missing(route_policy)
            if missing:
                unconfirmed.append(
                    "operator attestation is incomplete: " + ", ".join(missing)
                )
            else:
                confirmed.append(
                    "operator attestation recorded by "
                    f"{route_policy.attestation.attested_by} at {route_policy.attestation.attested_at}"
                )
        if route_policy.is_local_only:
            if result.host_class == "loopback":
                confirmed.append("local-only route is bound to a loopback endpoint")
            else:
                # A local-only route that answers off-loopback is the shape a remote
                # fallback would take, so it can never pass, whatever else the probe
                # confirmed.
                blockers.append("local_only_endpoint_not_loopback")
                unconfirmed.append(
                    "local-only route endpoint is not loopback; remote fallback cannot be excluded"
                )
    else:
        unconfirmed.append("route does not process applicant data, so no restriction is verified")

    ok = (
        result.is_live
        and result.reachable
        and result.authenticated
        and result.agent_target_present
        and route_policy.processes_applicant_data
        and not route_policy.missing_restrictions()
        and not blockers
    )

    if not result.is_live:
        detail = (
            "The probe ran through an injected transport, so this is a mock result. "
            "Mock results do not satisfy the PRD 14.3 compatibility gate and must not be "
            "recorded as live verification."
        )
    elif ok:
        detail = "Live probe confirmed the endpoint, authentication, agent targeting and attested restrictions."
    elif not result.reachable:
        detail = "The probe could not reach the endpoint; the route is not verified."
    else:
        detail = "The probe reached the endpoint but could not confirm every restriction; the route is not verified."

    return RouteVerification(
        route=route_policy.route,
        ok=ok,
        is_live=result.is_live,
        checked_at=checked_at,
        endpoint_label=result.endpoint_label,
        confirmed=confirmed,
        unconfirmed=unconfirmed,
        detail=detail,
        http_status=result.http_status,
        error_code=result.error_code,
    )


def result_attestation_missing(route_policy: RoutePolicy) -> tuple[str, ...]:
    """Requirement names an attestation leaves uncovered. Empty when complete."""
    attestation = route_policy.attestation
    if attestation is None:
        return tuple(name for name, _a, _l in _RESTRICTION_FLAGS if getattr(route_policy.requirements, name))
    return attestation.uncovered(route_policy.requirements)
