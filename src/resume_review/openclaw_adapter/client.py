"""Adapter around OpenClaw's documented, disabled-by-default Chat Completions surface.

Authority: PRD sections 14.1, 14.2, 14.3, 16.2, and acceptance tests AT-35/AT-36.

The surface used is the optional OpenAI-compatible endpoint that OpenClaw serves
from the Gateway (``POST /v1/chat/completions``, plus ``GET /v1/models`` for the
cheap probe). The compatibility facts this module depends on were checked against
the live documentation on 2026-09-29 and are recorded, with their URLs, in
``docs/compatibility.md``. The four that shape the code most:

* The ``model`` field is an **agent target**, not a provider model id
  (``openclaw/<agentId>``). The adapter composes that string from the allowlisted
  agent id in its own config, so nothing a browser sends can influence it.
* A ``user`` string yields a stable session key; without one every call is
  stateless. That is the whole conversation-continuity mechanism the adapter uses.
* A valid shared secret is an **owner/operator** credential, not a narrow
  per-user scope. The adapter therefore never forwards a caller-supplied header,
  model override, session key or endpoint path, and it fails closed when the route
  cannot be shown to be restricted.
* The endpoint's supported request fields do not include ``response_format``, so
  JSON output is requested in the prompt and parsed defensively instead.

Failures are translated by :mod:`resume_review.openclaw_adapter.policy`: transport
and route-availability failures become ``ROUTE_UNAVAILABLE`` (or
``LOCAL_ONLY_FALLBACK_BLOCKED`` in local-only mode), silence becomes
``ADAPTER_TIMEOUT``, and an answer that is not a usable completion becomes
``ADAPTER_BAD_RESPONSE``. A response that asks for a tool is treated as proof that
the route is not adequately restricted and is refused with
``ROUTE_POLICY_VIOLATION``.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import httpx

from .. import APP_NAME, __version__
from ..errors import Code, InvalidInput, ResumeReviewError
from ..models import ModelRoute
from ..util import new_id, now_iso
from .policy import (
    AGENT_TARGET_PREFIX,
    EndpointProbe,
    RoutePolicy,
    RouteVerification,
    host_class,
    split_endpoint,
)
from .policy import verify_route as _verify_route
from .prompts import PROMPT_VERSION, AnalysisRequest

__all__ = [
    "CHAT_COMPLETIONS_PATH",
    "MODELS_PATH",
    "AdapterConfig",
    "AdapterResult",
    "OpenClawAdapter",
    "probe_route",
]

#: Fixed, documented paths. The adapter never accepts a path from its caller.
CHAT_COMPLETIONS_PATH = "/v1/chat/completions"
MODELS_PATH = "/v1/models"

#: Documented request-size guidance is 20 MB per body; we stay well under it because
#: extraction is already bounded to 200k characters.
DEFAULT_MAX_REQUEST_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_RESPONSE_BYTES = 8 * 1024 * 1024

#: An allowlisted agent id is a single path segment. Refusing separators means
#: ``openclaw/<id>`` cannot be re-pointed by a value containing ``/`` or ``:``.
_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: A conversation ``user`` value is an opaque application-owned id. The pattern
#: rejects email addresses, paths and display names, none of which may become a
#: provider-visible session id (PRD 9.4).
_CONVERSATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{7,127}$")

_SAFE_TOKEN_RE = re.compile(r"[^A-Za-z0-9._:-]+")

_LOGGER = logging.getLogger(__name__)


def _fail(
    message: str,
    code: str,
    *,
    http_status: int = 502,
    retryable: bool = False,
    detail: Mapping[str, Any] | None = None,
) -> ResumeReviewError:
    """Build an error whose text is safe for a log and for a reviewer."""
    return ResumeReviewError(
        message,
        code=code,
        http_status=http_status,
        retryable=retryable,
        detail=dict(detail or {}),
    )


def _sanitize_token(value: Any, *, limit: int = 64) -> str | None:
    """Reduce a provider-supplied string to a short, safe token.

    Provider error text can quote request content, which may be applicant text, so
    the adapter keeps only a type-like token and never the message body.
    """
    if not isinstance(value, str):
        return None
    token = _SAFE_TOKEN_RE.sub("", value)[:limit]
    return token or None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AdapterConfig:
    """Everything the adapter needs, supplied explicitly by the caller.

    The shared secret is either read from ``secret_path`` (a protected host
    location outside the job folder, as the PRD requires) or passed as ``secret``
    for tests. Both fields are excluded from ``repr`` and from
    :meth:`describe`; the adapter never logs the value, never puts it in an error
    message, never writes it to a file and never renders it into HTML.
    """

    base_url: str
    agent_id: str
    timeout_seconds: float
    route_policy: RoutePolicy
    secret: str | None = field(default=None, repr=False)
    secret_path: Path | None = field(default=None, repr=False)
    max_output_tokens: int | None = None
    max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES
    user_agent: str = f"{APP_NAME}-openclaw-adapter/{__version__}"
    #: Injected transport, used only by tests. Never set in production: the probe
    #: reports such a call as non-live so a mock can never pass the PRD 14.3 gate.
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not _AGENT_ID_RE.match(self.agent_id or ""):
            raise _fail(
                "The configured OpenClaw agent target is not a valid single-segment agent id.",
                Code.ROUTE_POLICY_VIOLATION,
                http_status=422,
                detail={"reason": "agent_id_invalid"},
            )
        scheme, host, port = split_endpoint(self.base_url)
        # ``port`` is part of the endpoint identity; endpoint_label re-parses base_url.
        del port
        classification = host_class(host)
        if self.route_policy.is_local_only and classification != "loopback":
            # A local-only route must not be able to reach anything but the loopback
            # interface; a non-loopback endpoint is how a remote fallback would
            # arrive, so it is refused at construction rather than at call time.
            raise _fail(
                "A local-only analysis route must use a loopback endpoint.",
                Code.ROUTE_POLICY_VIOLATION,
                http_status=422,
                detail={"route": str(self.route_policy.route.value)},
            )
        if classification == "global":
            raise _fail(
                "The OpenClaw endpoint resolves to a public address; keep it on loopback or private ingress.",
                Code.ROUTE_POLICY_VIOLATION,
                http_status=422,
                detail={"reason": "public_ingress"},
            )
        if not 0 < float(self.timeout_seconds) <= 600:
            raise _fail(
                "The OpenClaw timeout must be greater than zero and at most 600 seconds.",
                Code.INVALID_INPUT,
                http_status=422,
                detail={"reason": "timeout_invalid"},
            )
        if self.max_output_tokens is not None and int(self.max_output_tokens) < 1:
            raise _fail(
                "The completion token cap must be a positive integer.",
                Code.INVALID_INPUT,
                http_status=422,
                detail={"reason": "max_output_tokens_invalid"},
            )
        if (self.secret is None) == (self.secret_path is None):
            raise _fail(
                "Configure exactly one OpenClaw shared secret source: a protected file path or a supplied value.",
                Code.VALIDATION_FAILED,
                http_status=422,
                detail={"reason": "secret_source_invalid"},
            )
        if self.secret is not None and not self.secret.strip():
            raise _fail(
                "The configured OpenClaw shared secret is empty.",
                Code.VALIDATION_FAILED,
                http_status=422,
                detail={"reason": "secret_empty"},
            )

    # -- endpoint ----------------------------------------------------------
    @property
    def endpoint_label(self) -> str:
        """``scheme://host:port`` only. The adapter never forwards a path."""
        scheme, host, port = split_endpoint(self.base_url)
        return f"{scheme}://{host}:{port}"

    @property
    def agent_target(self) -> str:
        """The documented agent-target spelling used as the request ``model``."""
        return f"{AGENT_TARGET_PREFIX}{self.agent_id}"

    def chat_completions_url(self) -> str:
        return f"{self.endpoint_label}{CHAT_COMPLETIONS_PATH}"

    def models_url(self) -> str:
        return f"{self.endpoint_label}{MODELS_PATH}"

    # -- secret ------------------------------------------------------------
    def read_secret(self) -> str:
        """Return the shared secret without caching or echoing it.

        Read on each use so a rotated secret file takes effect without a restart.
        Failures never name the path (an error message must not contain an absolute
        path) and never contain any part of the stored value.
        """
        if self.secret is not None:
            return self.secret
        path = self.secret_path
        try:
            raw = Path(path).read_text(encoding="utf-8") if path is not None else ""
        except OSError as exc:
            raise _fail(
                "The OpenClaw shared secret file could not be read.",
                Code.VALIDATION_FAILED,
                http_status=422,
                detail={"reason": "secret_unreadable", "os_error": exc.__class__.__name__},
            ) from None
        secret = raw.strip()
        if not secret:
            raise _fail(
                "The OpenClaw shared secret file is empty.",
                Code.VALIDATION_FAILED,
                http_status=422,
                detail={"reason": "secret_empty"},
            )
        return secret

    # -- reporting ---------------------------------------------------------
    def describe(self) -> dict[str, Any]:
        """Safe operational summary. Contains no secret and no absolute path."""
        from urllib.parse import urlsplit

        configured_path = urlsplit(self.base_url).path
        return {
            "endpoint_label": self.endpoint_label,
            "agent_target": self.agent_target,
            # A path in base_url is discarded rather than forwarded; report that so a
            # misconfigured reverse-proxy prefix is visible instead of silently ignored.
            "base_url_path_ignored": configured_path not in ("", "/"),
            "timeout_seconds": float(self.timeout_seconds),
            "user_agent": self.user_agent,
            "max_output_tokens": self.max_output_tokens,
            "max_request_bytes": self.max_request_bytes,
            "secret_source": "inline" if self.secret is not None else "protected_file",
            "injected_transport": self.transport is not None,
            "route_policy": self.route_policy.describe(),
        }


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------
@dataclass
class AdapterResult:
    """Model text plus the trusted run metadata the helper attaches itself.

    ``route`` is set by this module from its own configuration. Nothing the model
    or the gateway reports about routing is trusted (PRD 7.3: "Do not trust the
    model to report which route actually processed the data"). ``upstream_model_echo``
    exists only so the helper can record what the provider claimed, labelled as an
    unverified echo; policy never reads it.
    """

    text: str
    request_id: str
    document_id: str
    prompt_version: str
    schema_version: str
    route: ModelRoute
    provider_label: str | None
    agent_target: str
    endpoint_label: str
    started_at: str
    ended_at: str
    duration_ms: int
    http_status: int
    finish_reason: str | None = None
    token_usage: int | None = None
    upstream_response_id: str | None = None
    upstream_model_echo: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Trusted metadata for ``profiles`` plus the raw model text.

        Keys match the analysis profile columns the helper writes: ``model_route``,
        ``run_request_id``, ``run_started_at``, ``run_ended_at``, ``token_usage``.
        Contains no secret; the endpoint label is host:port only.
        """
        return {
            "schema_version": self.schema_version,
            "prompt_version": self.prompt_version,
            "document_id": self.document_id,
            "model_route": str(self.route.value),
            "model_route_attached_by": "adapter",
            "provider_label": self.provider_label,
            "agent_target": self.agent_target,
            "endpoint_label": self.endpoint_label,
            "run_request_id": self.request_id,
            "run_started_at": self.started_at,
            "run_ended_at": self.ended_at,
            "duration_ms": self.duration_ms,
            "token_usage": self.token_usage,
            "http_status": self.http_status,
            "finish_reason": self.finish_reason,
            "upstream_response_id": self.upstream_response_id,
            # Informational only: never used for policy or routing decisions.
            "upstream_model_echo_untrusted": self.upstream_model_echo,
            "text": self.text,
        }


# ---------------------------------------------------------------------------
# Transport helpers
# ---------------------------------------------------------------------------
def _auth_headers(config: AdapterConfig) -> dict[str, str]:
    """The complete header set. Nothing here is caller-supplied.

    ``x-openclaw-model``, ``x-openclaw-agent-id`` and ``x-openclaw-session-key`` are
    deliberately absent: the first two are model/agent overrides the adapter must
    never send (PRD 14.1), and the third takes an operator-admin session route the
    analysis flow has no business selecting.
    """
    return {
        "Authorization": f"Bearer {config.read_secret()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": config.user_agent,
    }


def _classify_status(status: int) -> tuple[str, bool]:
    """Map an HTTP status onto the adapter's error codes.

    Route/transport problems (auth refused, endpoint absent, rate limited, gateway
    fault) are ``ROUTE_UNAVAILABLE``. Content problems (a rejected request, a
    response we cannot use) are ``ADAPTER_BAD_RESPONSE``. A 408 or 504 is a
    timeout even though it arrived over HTTP.
    """
    if status == 408 or status == 504:
        return Code.ADAPTER_TIMEOUT, True
    if status in (401, 403, 404):
        return Code.ROUTE_UNAVAILABLE, False
    if status == 429:
        return Code.ROUTE_UNAVAILABLE, True
    if 500 <= status <= 599:
        return Code.ROUTE_UNAVAILABLE, status != 501
    return Code.ADAPTER_BAD_RESPONSE, False


def _parse_json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
        raise _fail(
            "The analysis route returned a body that is not JSON.",
            Code.ADAPTER_BAD_RESPONSE,
            http_status=502,
            retryable=True,
            detail={"reason": "malformed_json", "http_status": response.status_code},
        ) from None
    if not isinstance(payload, dict):
        raise _fail(
            "The analysis route returned JSON that is not an object.",
            Code.ADAPTER_BAD_RESPONSE,
            http_status=502,
            retryable=True,
            detail={"reason": "unexpected_json_shape", "http_status": response.status_code},
        )
    return payload


def _content_text(message: Mapping[str, Any]) -> str | None:
    """Read ``message.content`` as text, accepting the documented part form."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        if parts:
            return "\n".join(parts)
    return None


def _token_usage(payload: Mapping[str, Any]) -> int | None:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    total = usage.get("total_tokens")
    if isinstance(total, int) and total >= 0:
        return total
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if isinstance(prompt, int) and isinstance(completion, int):
        return prompt + completion
    return None


# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------
async def probe_route(
    config: AdapterConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> EndpointProbe:
    """Cheap live check: is the endpoint up, does it accept the secret, is the agent listed.

    Deliberately read-only and tiny: ``GET /v1/models``. It cannot confirm tool
    policy, sandboxing or where the backend model runs, and it does not pretend to
    (see :func:`resume_review.openclaw_adapter.policy.verify_route`). A transport
    passed here marks the result non-live so a mock can never satisfy PRD 14.3.
    """
    effective_transport = transport if transport is not None else config.transport
    is_live = effective_transport is None
    probe = EndpointProbe(
        endpoint_label=config.endpoint_label,
        is_live=is_live,
        host_class=host_class(split_endpoint(config.base_url)[1]),
    )
    started = time.monotonic()
    try:
        secret = config.read_secret()
        async with httpx.AsyncClient(
            transport=effective_transport,
            timeout=httpx.Timeout(float(config.timeout_seconds)),
        ) as client:
            response = await client.get(
                config.models_url(),
                headers={
                    "Authorization": f"Bearer {secret}",
                    "Accept": "application/json",
                    "User-Agent": config.user_agent,
                },
            )
    except httpx.TimeoutException:
        probe.error_code = Code.ADAPTER_TIMEOUT
        probe.detail = "The route probe timed out."
    except (httpx.ConnectError, httpx.TransportError):
        probe.error_code = Code.ROUTE_UNAVAILABLE
        probe.detail = "The route probe could not connect."
    except ResumeReviewError as exc:
        probe.error_code = exc.code
        probe.detail = "The route probe could not read the configured shared secret."
    else:
        probe.elapsed_ms = int((time.monotonic() - started) * 1000)
        probe.reachable = True
        probe.http_status = response.status_code
        if response.status_code == 200:
            probe.authenticated = True
            try:
                payload = response.json()
            except (json.JSONDecodeError, ValueError):
                probe.detail = "The model list was not JSON."
            else:
                probe.agent_targets = _model_ids(payload)
                probe.agent_target_present = config.agent_target in probe.agent_targets
                if not probe.agent_target_present:
                    probe.detail = "The allowlisted agent target is not listed by the gateway."
        else:
            code, _retryable = _classify_status(response.status_code)
            probe.error_code = code
            probe.detail = "The gateway refused the route probe."
    return probe


def _model_ids(payload: Any) -> list[str]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    ids: list[str] = []
    for entry in data:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            ids.append(entry["id"])
    return ids


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------
class OpenClawAdapter:
    """Sends bounded assessment requests to one allowlisted OpenClaw agent target."""

    def __init__(self, config: AdapterConfig, *, logger: logging.Logger | None = None) -> None:
        self._config = config
        self._log = logger or _LOGGER

    @property
    def config(self) -> AdapterConfig:
        return self._config

    def describe(self) -> dict[str, Any]:
        """Safe summary for status output and the compatibility matrix."""
        return self._config.describe()

    # -- request -----------------------------------------------------------
    async def analyze(
        self,
        request: AnalysisRequest,
        *,
        conversation_user: str | None = None,
        request_id: str | None = None,
    ) -> AdapterResult:
        """Run one assessment request and return the text with trusted run metadata.

        There is deliberately no way to pass a model id, agent name, header, tool
        list or endpoint path through this call: the only inputs are the prepared
        prompt, an opaque conversation id and an optional request id. The route is
        checked first, so an unusable or unverified route refuses before any
        applicant text is serialized, let alone sent.
        """
        policy = self._config.route_policy
        policy.assert_inference_allowed()

        if not isinstance(request, AnalysisRequest):
            raise InvalidInput(
                "The adapter accepts only a prepared analysis request.",
                code=Code.INVALID_INPUT,
                detail={"reason": "request_type_invalid"},
            )
        if request.prompt_version != PROMPT_VERSION:
            raise _fail(
                "The analysis request was built by a different prompt version.",
                Code.VALIDATION_FAILED,
                http_status=422,
                detail={"reason": "prompt_version_mismatch"},
            )

        payload = self._build_payload(request, conversation_user=conversation_user)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(body) > self._config.max_request_bytes:
            raise _fail(
                "The analysis request exceeds the configured size limit.",
                Code.VALIDATION_FAILED,
                http_status=422,
                detail={"reason": "request_too_large", "bytes": len(body)},
            )

        call_id = request_id or new_id("request")
        self._log.info(
            "openclaw analysis request %s -> %s (agent=%s, doc=%s, bytes=%d)",
            call_id,
            self._config.endpoint_label,
            self._config.agent_target,
            request.document_id,
            len(body),
        )

        started_at = now_iso()
        started = time.monotonic()
        try:
            response = await self._post(body)
        except httpx.TimeoutException:
            raise policy.route_failure_error(
                Code.ADAPTER_TIMEOUT,
                message="The analysis route did not answer within the configured timeout.",
                retryable=True,
                http_status=504,
            ) from None
        except (httpx.ConnectError, httpx.TransportError):
            raise policy.route_failure_error(
                Code.ROUTE_UNAVAILABLE,
                message="The analysis route could not be reached.",
                retryable=True,
            ) from None
        ended_at = now_iso()
        duration_ms = int((time.monotonic() - started) * 1000)

        if response.status_code != 200:
            code, retryable = _classify_status(response.status_code)
            raise policy.route_failure_error(
                code,
                message="The analysis route returned an error response.",
                retryable=retryable,
                http_status=response.status_code,
            )
        if len(response.content) > self._config.max_response_bytes:
            raise _fail(
                "The analysis route returned a response larger than the configured limit.",
                Code.ADAPTER_BAD_RESPONSE,
                http_status=502,
                retryable=True,
                detail={"reason": "response_too_large"},
            )

        payload_json = _parse_json_object(response)
        text, finish_reason, upstream_id, upstream_model = self._extract_completion(payload_json)
        return AdapterResult(
            text=text,
            request_id=call_id,
            document_id=request.document_id,
            prompt_version=request.prompt_version,
            schema_version=request.schema_version,
            # Attached here from our own configuration; never read from the response.
            route=policy.route,
            provider_label=policy.provider_label,
            agent_target=self._config.agent_target,
            endpoint_label=self._config.endpoint_label,
            started_at=started_at,
            ended_at=ended_at,
            duration_ms=duration_ms,
            http_status=response.status_code,
            finish_reason=finish_reason,
            token_usage=_token_usage(payload_json),
            upstream_response_id=upstream_id,
            upstream_model_echo=upstream_model,
        )

    # -- verification ------------------------------------------------------
    async def verify_route(self) -> RouteVerification:
        """Live, cheap probe of this adapter's one route (PRD 14.3)."""

        async def probe() -> EndpointProbe:
            return await probe_route(self._config)

        return await _verify_route(self._config.route_policy, probe)

    # -- internals ---------------------------------------------------------
    def _build_payload(
        self, request: AnalysisRequest, *, conversation_user: str | None
    ) -> dict[str, Any]:
        """Compose the request body from fixed fields only.

        ``response_format`` is not sent because it is not in the endpoint's
        documented supported-field set; the prompt requests JSON and the parser
        tolerates a wrapped or fenced answer by rejecting it honestly. ``tools`` is
        never sent: a restricted route has no tools to offer, and a tool call that
        arrives anyway is refused as a policy violation.
        """
        payload: dict[str, Any] = {
            "model": self._config.agent_target,
            "messages": request.to_messages_payload(),
            "stream": False,
            "temperature": 0.0,
        }
        if self._config.max_output_tokens is not None:
            payload["max_completion_tokens"] = int(self._config.max_output_tokens)
        if conversation_user is not None:
            payload["user"] = _validated_conversation_id(conversation_user)
        return payload

    async def _post(self, body: bytes) -> httpx.Response:
        async with httpx.AsyncClient(
            transport=self._config.transport,
            timeout=httpx.Timeout(float(self._config.timeout_seconds)),
            follow_redirects=False,
        ) as client:
            return await client.post(
                self._config.chat_completions_url(),
                headers=_auth_headers(self._config),
                content=body,
            )

    def _extract_completion(
        self, payload: Mapping[str, Any]
    ) -> tuple[str, str | None, str | None, str | None]:
        if isinstance(payload.get("error"), dict):
            error = payload["error"]
            raise _fail(
                "The analysis route reported a provider error.",
                Code.ADAPTER_BAD_RESPONSE,
                http_status=502,
                detail={
                    "reason": "provider_error",
                    "error_type": _sanitize_token(error.get("type")),
                },
            )

        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise _fail(
                "The analysis route returned no completion choices.",
                Code.ADAPTER_BAD_RESPONSE,
                http_status=502,
                retryable=True,
                detail={"reason": "no_choices"},
            )
        first = choices[0]
        if not isinstance(first, dict):
            raise _fail(
                "The analysis route returned a malformed choice.",
                Code.ADAPTER_BAD_RESPONSE,
                http_status=502,
                retryable=True,
                detail={"reason": "malformed_choice"},
            )
        message = first.get("message")
        if not isinstance(message, dict):
            raise _fail(
                "The analysis route returned a choice without a message.",
                Code.ADAPTER_BAD_RESPONSE,
                http_status=502,
                retryable=True,
                detail={"reason": "missing_message"},
            )

        finish_reason = first.get("finish_reason")
        tool_calls = message.get("tool_calls")
        if (isinstance(tool_calls, list) and tool_calls) or finish_reason == "tool_calls":
            # Fail closed. A restricted route has no tools, so a tool call means the
            # route is not the restricted context the policy believed it was.
            raise _fail(
                "The analysis route attempted a tool call; the route is not an adequately "
                "restricted analysis context.",
                Code.ROUTE_POLICY_VIOLATION,
                http_status=503,
                detail={"reason": "tool_call_refused", "agent_target": self._config.agent_target},
            )

        text = _content_text(message)
        if text is None:
            raise _fail(
                "The analysis route returned non-text content.",
                Code.ADAPTER_BAD_RESPONSE,
                http_status=502,
                retryable=True,
                detail={"reason": "non_text_content"},
            )
        if not text.strip():
            raise _fail(
                "The analysis route returned an empty completion.",
                Code.ADAPTER_BAD_RESPONSE,
                http_status=502,
                retryable=True,
                detail={"reason": "empty_completion"},
            )

        upstream_id = payload.get("id") if isinstance(payload.get("id"), str) else None
        upstream_model = payload.get("model") if isinstance(payload.get("model"), str) else None
        return text, finish_reason if isinstance(finish_reason, str) else None, upstream_id, upstream_model


def _validated_conversation_id(value: str) -> str:
    """Refuse a conversation value that could be a name, path or address (PRD 9.4)."""
    if not isinstance(value, str) or not _CONVERSATION_ID_RE.match(value):
        raise _fail(
            "The conversation id must be an opaque application-generated value, not a name, "
            "path or address.",
            Code.CHAT_SCOPE_NOT_BOUND,
            http_status=422,
            detail={"reason": "conversation_id_not_opaque"},
        )
    return value
