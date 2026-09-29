# OpenClaw compatibility record

Authority: PRD section 14 (restricted OpenClaw analysis adapter), section 14.3 (the
compatibility gate), and section 23 (primary references S1-S6). This file records
the **version-sensitive integration facts the adapter depends on**, each with the
page it was read from and the date it was checked, plus what could not be
confirmed. It exists so that a later reader can tell a deliberate implementation
decision from a guess.

## Method

* Checked by fetching the live pages on **2026-09-29** with the SearxNG fetch tool.
* Appending `.md` to a `docs.openclaw.ai` path returns the page source as clean
  Markdown (`https://docs.openclaw.ai/gateway/openai-http-api.md`). The HTML form
  of the same page returns a large amount of navigation chrome first, so the `.md`
  form is what was read. Both forms were reachable for every page listed below.
* Every fact below is quoted or paraphrased from the fetched text. Where a page did
  not say something, this file says so instead of filling the gap.
* A fact marked **unconfirmable** cannot be established over the documented HTTP
  surface at all. Those are the items `verify_route()` reports in its
  `unconfirmed` list rather than pretending to pass.

## 1. Endpoint surface (S4)

Source: `https://docs.openclaw.ai/gateway/openai-http-api` (read 2026-09-29).

| Verified fact | Consequence in this code |
| --- | --- |
| The Chat Completions surface is **disabled by default**; enabling it is `gateway.http.endpoints.chatCompletions.enabled: true`. | The adapter treats a 404 on `/v1/models` as "endpoint not enabled", not as a transient fault, and the live gate fails with that explanation. |
| Paths served: `POST /v1/chat/completions`, `GET /v1/models`, `GET /v1/models/{id}`, `POST /v1/embeddings`, on the same port as the Gateway (WS + HTTP multiplex). | `CHAT_COMPLETIONS_PATH` and `MODELS_PATH` are constants; no path is ever accepted from a caller. Only the first two are used. |
| Documented example base URL `http://127.0.0.1:18789/v1`; smoke test against `http://127.0.0.1:18789/v1/models`. | The default deployment shape is loopback port 18789, matching the loopback requirement for a local-only route. |
| Built-in limit of **20 MB per request body**. | The adapter caps the serialized request at 4 MiB (`DEFAULT_MAX_REQUEST_BYTES`), well under the documented limit, because extraction is already bounded to 200k characters. |
| Auth failure rate limiting returns `429` with a `Retry-After` header when `gateway.auth.rateLimit` is configured. | `429` maps to `ROUTE_UNAVAILABLE` with `retryable=True`. |

## 2. Agent targeting and sessions (S4)

| Verified fact | Consequence in this code |
| --- | --- |
| "OpenClaw treats the OpenAI `model` field as an **agent target**, not a raw provider model id." Accepted forms: `openclaw` (default agent), `openclaw/default` (stable alias), `openclaw/<agentId>` or `openclaw:<agentId>`, and `agent:<agentId>`. | The adapter composes `openclaw/<agentId>` itself from its own allowlisted `agent_id`. No other spelling is reachable: the id must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`, so a value containing `/`, `:` or whitespace cannot re-point the target. |
| `GET /v1/models` lists "top-level agent targets ... not backend provider models and not sub-agents". The list and detail endpoints require `operator.read` or a scope including it. | The probe uses `/v1/models` and checks that the configured agent target appears in the returned ids. A missing target is reported as unconfirmed, never assumed present. |
| "By default the endpoint is **stateless per request** (a new session key is generated each call)." If the request includes an OpenAI `user` string, "the Gateway derives a stable session key from it so repeated calls can share an agent session." The documented example value is `conv:YOUR_CONVERSATION_ID`. | `OpenClawAdapter.analyze(..., conversation_user=...)` maps to the request's `user` field, and nothing else. The adapter narrows the accepted shape to `^[A-Za-z0-9][A-Za-z0-9._~-]{7,127}$`, which **excludes the documented example's `conv:` prefix**: a colon is refused so that an operator-looking or reserved-namespace value cannot be supplied by a browser, and so a name, path or email address cannot become a provider-visible session id (PRD 9.4). This is a deliberate narrowing, not a documented restriction. |

## 3. Supported request fields (S4)

The documented supported-field table for `/v1/chat/completions` lists `tools`,
`tool_choice`, `messages[*].role: "tool"`, `messages[*].tool_call_id`,
`max_completion_tokens`, `max_tokens`, `temperature`, `top_p`,
`frequency_penalty`, `presence_penalty`, `seed`, and `stop`.

**`response_format` is not in that table.** Consequence: the adapter does not send
it. JSON output is requested in the prompt (`OUTPUT_SKELETON`) and the response is
parsed defensively: a fenced or non-JSON answer is reported as
`ADAPTER_BAD_RESPONSE` rather than silently unwrapped, and the caller may spend its
one permitted repair turn (PRD 6.4). The requested shape is kept in step with
`schemas/analysis_result.schema.json` by a unit test rather than by a
schema-enforcing request parameter.

The adapter sends only `model`, `messages`, `stream: false`, `temperature: 0.0`,
and optionally `max_completion_tokens` and `user`. `tools` is never sent: a
restricted analysis route has no tools to offer, and the endpoint would otherwise
expect the caller to run the tool loop.

| Verified fact | Consequence in this code |
| --- | --- |
| When the agent calls tools, the non-streaming response uses `choices[0].finish_reason = "tool_calls"` plus `choices[0].message.tool_calls[]`. | Either signal makes the adapter raise `ROUTE_POLICY_VIOLATION`. A route that returns a tool call is not the restricted context the policy believed it was, so it fails closed rather than continuing. |
| `temperature` accepts 0-2 and returns `400 invalid_request_error` out of range. | `temperature: 0.0` is inside the documented range. |

## 4. Authentication and the security boundary (S4, S5)

Source (S5): `https://docs.openclaw.ai/gateway/security` (read 2026-09-29).

| Verified fact | Consequence in this code |
| --- | --- |
| "A valid Gateway token/password for this endpoint is equivalent to an owner/operator credential, not a narrow per-user scope." With `gateway.auth.mode="token"` or `"password"`, a bearer call "restores the full default operator scope set: `operator.admin`, `operator.approvals`, `operator.pairing`, `operator.read`, `operator.talk.secrets`, `operator.write`" and "Treats chat turns as owner-sender turns." | This is the single most load-bearing fact in the module. The secret is a full operator credential, so the adapter treats it as one: it is read per call from a protected host path, never logged, never placed in an exception message, never written to a file, and never rendered. An applicant-facing surface must never be able to reach it. |
| "Keep it on loopback/tailnet/private ingress only. Do not expose it to the public internet." | A `base_url` resolving to a global IP is refused at construction with `ROUTE_POLICY_VIOLATION`. A local-only route additionally requires a loopback host. A hostname that is not an IP literal is classified `unknown` and is never treated as local, because resolving it would be network I/O. |
| Optional request headers include `x-openclaw-model: <provider/model>`, which "Overrides the backend model for the selected agent. **Shared-secret bearer callers can use this directly**", plus `x-openclaw-agent-id`, `x-openclaw-session-key` and `x-openclaw-message-channel`. | Because the adapter's credential already carries owner scope, forwarding a caller-supplied header would hand a browser an operator-level model override. The adapter sends exactly four headers (`Authorization`, `Content-Type`, `Accept`, `User-Agent`) and has no parameter that could carry another one. `x-openclaw-session-key` is also refused a value: a documented reserved namespace (`subagent:`, `cron:`, `acp:`) would otherwise be selectable from outside. |
| `x-openclaw-session-key` with a reserved internal namespace returns `400 invalid_request_error`; explicitly continuing an incognito session requires `operator.admin` authority. | No code path in the adapter requests an incognito or explicitly-routed session. Continuity is only ever the opaque `user` value. |
| "**One trust boundary per gateway.** ... OpenClaw is not a hostile multi-tenant security boundary for mutually adversarial users sharing one agent or gateway. For mixed-trust or adversarial-user operation, split trust boundaries: separate gateway + credentials, ideally separate OS users or hosts." | The restricted analysis route is a separate trust boundary from the reviewer-facing application, and the PRD's route policy is what enforces that in this application. The adapter does not attempt to synthesise per-user isolation from a shared operator credential. |
| On a regular host install "the Gateway binds to loopback" by default. | A loopback default is compatible with the local-only route requirement and with the negative-case tests in the live gate. |

## 5. Tool and sandbox restrictions (S6, S3)

Source (S6): `https://docs.openclaw.ai/gateway/security/tool-permissions` (read
2026-09-29). Source (S3): `https://docs.openclaw.ai/tools/exec` (read 2026-09-29).

| Verified fact | Consequence in this code |
| --- | --- |
| For any agent or surface handling untrusted content the page says to deny `gateway`, `cron`, `sessions_spawn` and `sessions_send` by default. | All four are in `REQUIRED_DENIED_TOOLS`. |
| The page's own "no filesystem/shell access" example denies `apply_patch`, `browser`, `canvas`, `cron`, `edit`, `exec`, `gateway`, `image`, `nodes`, `process`, `read`, `write`. | `REQUIRED_DENIED_TOOLS` is exactly that list, plus `sessions_spawn` and `sessions_send`. Operators can paste the names straight into `agents.entries.<agentId>.tools.deny`. |
| "`exec` is a mutating shell surface ... Disabling OpenClaw filesystem tools such as `write`, `edit`, or `apply_patch` does not make `exec` read-only." | Denying `exec` and `process` is required independently of denying the filesystem tools; one does not imply the other. |
| `agents.defaults.sandbox.workspaceAccess` is `"none"` (default), `"ro"` (mounts the agent workspace read-only at `/agent`, which disables `write`/`edit`/`apply_patch`) or `"rw"`. Sandbox `scope` should stay `"agent"` or `"session"` to prevent cross-agent access. | A restricted analysis agent uses `"none"` or `"ro"`. The alternative for an agent that must read its own skill files is `tools.fs.workspaceOnly: true`, which is what the `no_unrestricted_file_read` attestation flag records. |
| `tools.elevated` is "the global baseline escape hatch that runs exec outside the sandbox". `tools.exec.applyPatch.workspaceOnly` defaults to `true`. | The attestation requires that elevated access is not enabled for the analysis agent; the probe cannot see this, so it is operator-attested and reported as unconfirmable. |
| "Tool profiles do not narrow session-tool reach ... Session visibility is Gateway-wide and agent-to-agent messaging is on by default." | A tool allow/deny list alone does not bound session reach, which is why `sessions_spawn`/`sessions_send` are denied and why cross-session access is an explicit attestation flag rather than an assumption. |
| Skill folders are refreshed mid-session by a watcher, and "Treat skill folders as trusted code and restrict who can modify them." | The instruction workspace must be a trusted folder (see section 6); the applicant folder must not be it. |

## 6. Agent runtime, workspace and skills (S1, S2)

Source (S1): `https://docs.openclaw.ai/concepts/agent` (read 2026-09-29). Source
(S2): `https://docs.openclaw.ai/tools/skills` (read 2026-09-29).

| Verified fact | Consequence in this code and in the deployment |
| --- | --- |
| Each agent has one workspace directory used as its **only** working directory for tools and context. | The analysis agent's workspace is a dedicated trusted folder. |
| On the first turn of a new session OpenClaw injects the workspace's `AGENTS.md`, `SOUL.md`, `IDENTITY.md`, `USER.md`, `BOOTSTRAP.md` and (if present) `MEMORY.md` into the system prompt's Project Context. | This is the concrete mechanism behind PRD AT-34: an applicant-controlled folder used as the workspace would inject applicant-authored `AGENTS.md` text into the system prompt as operating instructions. It must never be the workspace. |
| "The `## Tools` section of `AGENTS.md` does **not** control which tools exist. It is guidance for how _you_ want them used." | Tool denial is enforced in `agents.entries.<agentId>.tools.deny`, not by prompt text. The adapter's own prompt cannot restrict tool availability, which is why the tool policy is attested and probed separately. |
| Skills are directories containing a `SKILL.md` with YAML frontmatter and a markdown body. Highest-precedence source is **workspace** `<workspace>/skills`, then project agent skills, personal agent skills, managed/local, bundled, extra dirs. | The analysis skill lives in the agent's own workspace and is loaded by the agent, not by this application. A resume folder used as a workspace would have had its `skills/` directory at the highest precedence tier. |
| Core tools (read/exec/edit/write and related) are "always available, subject to tool policy". | A restricted route is achieved by the configuration's deny lists and sandbox, never by asking the model to behave. |

The live gate asserts the observable proxy for skill loading: an agent whose
analysis skill and profile are not loaded cannot answer with the bound revisions
echoed and a schema-conformant result. Skill state itself is not exposed over HTTP,
so the gate reports it as asserted-by-proxy rather than claiming it observed the
skill list.

## 7. Prompt injection and untrusted-content wrapping (S5 subpage)

Source: `https://docs.openclaw.ai/gateway/security/prompt-injection` (read
2026-09-29).

| Verified fact | Consequence in this code |
| --- | --- |
| The page's own guidance is that untrusted content is delivered with boundary markers plus `Source: External` metadata. | The application mirrors that pattern with `security.untrusted.wrap_untrusted` (`<<<BEGIN-DOCUMENT>>>` ... `<<<END-DOCUMENT>>>`) plus a data-not-instructions preamble. **The wrapping OpenClaw performs applies to content OpenClaw fetches itself**; applicant text sent as a normal message body is not wrapped for us, so the application must do it. |
| OpenClaw strips common self-hosted chat-template special-token literals (Qwen/ChatML, Llama, Gemma, Mistral, Phi, GPT-OSS) from the external content **it** wraps, because "without this sanitization, untrusted text ... could forge a synthetic `assistant`/`system` role boundary". | This is a tokenizer-layer forgery, below the level any envelope can protect. Since our message body is not wrapped by OpenClaw, the adapter defangs the same class of literal itself before framing (`_defang_role_tokens` in `prompts.py`). Covered by unit tests. |
| "Do not use older/weaker/smaller tiers for tool-enabled agents or untrusted inboxes." Model choice is described as the first and cheapest layer, with hard enforcement (tool policy, exec approvals, sandboxing, allowlists) kept for anything whose blast radius would not be acceptable. | The route policy's operator attestation covers model choice; the adapter itself never selects a model (the `model` field carries the agent target, and the backend model is chosen by the agent's configuration). |

## 8. Pages that could not be read

| Page | Result (2026-09-29) |
| --- | --- |
| `https://docs.openclaw.ai/release-notes.md` | **HTTP 404.** The release list is published at `https://docs.openclaw.ai/releases` instead. |
| `https://docs.openclaw.ai/releases` | Readable. Lists versions v2026.9.6 (newest) down to v2026.6.11, with `v2026.8.1` labelled "AKA OpenClaw 2.0". **The page publishes no release dates**, so no date is claimed here. |
| Every page listed in sections 1-7 | Readable in both `.md` and HTML form. |

No fact in this file is taken from an unreachable page. Nothing above is inferred
from a version number: the pages do not state which release a behaviour landed in,
so the adapter does not gate on a version string.

## 9. What the HTTP surface cannot confirm

These are reported in `RouteVerification.unconfirmed` and are why
`verify_route()` is not a security proof:

1. Per-agent tool policy. The endpoint exposes agent targets, not tool
   configuration. It is asserted by the operator attestation
   (`RouteAttestation`) and must be checked in the OpenClaw configuration
   (`agents.entries.<agentId>.tools`, sandbox settings) and with
   `openclaw policy` / `openclaw security audit`.
2. Host sandbox and OS-level isolation. Not observable from an HTTP response.
3. Whether the backend model runs on the storage host. Not observable; recorded by
   the operator in the provider record.

`verify_route()` also fails the PRD 14.3 gate outright when the probe ran through
an injected transport, and says so in its `detail`: **mock results do not count as
passing the compatibility gate.** `tests/unit/test_openclaw_adapter.py` asserts
that a mock probe is reported as failing; `tests/integration/test_openclaw_live.py`
runs the gate only against a configured live route and skips with a stated reason
otherwise.

## 10. Status-code and failure mapping

The mapping below is a deliberate choice made against the documented error shape
(`{"error": {"message": ..., "type": ...}}`) and the documented auth matrix.

| Situation | Code | Rationale |
| --- | --- | --- |
| `408`, `504` | `ADAPTER_TIMEOUT`, retryable | A timeout that arrived over HTTP is still a timeout. |
| `401`, `403`, `404` | `ROUTE_UNAVAILABLE`, not retryable | Absent endpoint, refused credential, or forbidden scope. Retrying cannot fix it. A `404` on `/v1/models` most often means the endpoint is still disabled by default. |
| `429` | `ROUTE_UNAVAILABLE`, retryable | Documented auth-failure rate limiting with `Retry-After`. |
| other `5xx` (not `501`) | `ROUTE_UNAVAILABLE`, retryable | Gateway fault, not a content problem. |
| `501` | `ROUTE_UNAVAILABLE`, not retryable | Not implemented is a configuration fact, not a transient fault. |
| other `4xx` | `ADAPTER_BAD_RESPONSE`, not retryable | A rejected request is a content or request problem. |
| Unparseable body, non-object JSON, missing or malformed `choices`, non-text or empty completion | `ADAPTER_BAD_RESPONSE`, retryable | The route answered but not usably. |
| A provider error object in a `200` body | `ADAPTER_BAD_RESPONSE` | The provider's message text can quote request content, so only a sanitized `error.type` token is recorded, never the message. |
| `choices[0].message.tool_calls` non-empty, or `finish_reason == "tool_calls"` | `ROUTE_POLICY_VIOLATION` | Proof the route is not the restricted context the policy believed. Fail closed. |
| Transport failure or timeout while the route is `LOCAL_ONLY` | `LOCAL_ONLY_FALLBACK_BLOCKED`, not retryable, with the underlying code preserved in `detail.underlying_code` | PRD 14.2 / AT-36. A lost local route is exactly the moment a remote fallback would be tempting, so it is refused rather than re-tried. There is no second route to retry: the adapter holds one configured route. |

`ADAPTER_BAD_RESPONSE` is deliberately **not** translated into
`LOCAL_ONLY_FALLBACK_BLOCKED`: a malformed body is a content failure, not a lost
route, and collapsing the two would hide which one happened.

## 11. Re-verification checklist

Before trusting this record for a new deployment, re-read the six pages in
sections 1-7 and confirm:

1. `response_format` is still absent from the supported-request-field table (if it
   appears, the prompt-side schema request could be replaced by a real parameter).
2. The `model` field is still an agent target and `/v1/models` still lists agent
   targets rather than provider models.
3. The `user` field still derives a stable session key and the endpoint is still
   stateless without it.
4. A shared-secret bearer caller still receives the full operator scope set, and
   `x-openclaw-model` is still usable by such a caller.
5. The documented deny-list names still match the per-agent tool names.
6. The tool-call response shape is still `finish_reason == "tool_calls"` plus
   `message.tool_calls[]`.
7. The 20 MB body limit is still documented.
8. Whether the pages now carry release dates or state which release a behaviour
   landed in.

Then run the live gate:

```
cd C:/Users/NM2/Documents/RecruiterAgent
.venv/Scripts/python.exe -m pytest tests/integration/test_openclaw_live.py -q
```

with the `RESUME_REVIEW_LIVE_*` variables documented at the top of that file. An
unconfigured run skips with a stated reason; a partly configured run fails, so a
half-set route is never mistaken for "no route configured".
