"""Restricted OpenClaw analysis adapter (PRD section 14).

The three modules here have separate jobs, and the separation is the control:

* :mod:`prompts` builds the bounded criterion-assessment request. It never performs
  neutral factual extraction, it frames every span of applicant text as untrusted
  data, and it asks for exactly the frozen ``analysis_result`` schema.
* :mod:`policy` states the approved route and fails closed when the restricted
  analysis context cannot be established. It is the only place that decides whether
  inference may happen, and the only place that decides what a lost route means.
* :mod:`client` talks to the documented, disabled-by-default Chat Completions
  surface, attaches trusted run metadata itself, and never forwards a
  caller-supplied model id, agent name, header, tool definition or endpoint path.

Importing this package performs no I/O and reads no secret.
"""

from __future__ import annotations

from .client import (
    CHAT_COMPLETIONS_PATH,
    MODELS_PATH,
    AdapterConfig,
    AdapterResult,
    OpenClawAdapter,
    probe_route,
)
from .policy import (
    REQUIRED_DENIED_TOOLS,
    EndpointProbe,
    RestrictedContextRequirements,
    RouteAttestation,
    RoutePolicy,
    RouteVerification,
    verify_route,
)
from .prompts import (
    OUTPUT_SKELETON,
    PROMPT_SCHEMA_VERSION,
    PROMPT_VERSION,
    AnalysisRequest,
    ChatMessage,
    build_analysis_request,
    build_repair_turn,
)

__all__ = [
    # client
    "CHAT_COMPLETIONS_PATH",
    "MODELS_PATH",
    "AdapterConfig",
    "AdapterResult",
    "OpenClawAdapter",
    "probe_route",
    # policy
    "REQUIRED_DENIED_TOOLS",
    "EndpointProbe",
    "RestrictedContextRequirements",
    "RouteAttestation",
    "RoutePolicy",
    "RouteVerification",
    "verify_route",
    # prompts
    "OUTPUT_SKELETON",
    "PROMPT_SCHEMA_VERSION",
    "PROMPT_VERSION",
    "AnalysisRequest",
    "ChatMessage",
    "build_analysis_request",
    "build_repair_turn",
]
