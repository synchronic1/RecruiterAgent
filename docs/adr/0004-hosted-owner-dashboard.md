# ADR 0004: Authenticated companion on the hosted Gateway origin

Status: Implemented; live verification recorded separately.

## Decision

Expose the companion as a full browser page at `/recruiteragent` on the existing
Plow Gateway origin. OpenClaw's widget preview depends on a separate sandbox host
that is unavailable on the tested Plow deployment. The dashboard does not depend
on that sandbox, a new public port, or a replacement Gateway boot command.

The pinned Plow channel plugin registers Gateway-authenticated HTTP routes and a
supervised Python service on loopback. Its existing channel registration remains
intact. A build-time checksum rejects changes to the upstream registration file.
The Plow proxy authenticates the owner and supplies the verified owner identity.
The plugin accepts only companion paths, overwrites bridge identity headers,
limits bodies, and forwards a narrow set of headers to the fixed loopback port.

A private random bridge secret outside applicant folders protects that service.
Owner navigation issues an instance-bound HttpOnly session. Mutations retain
exact-origin validation, CSRF, operation authorization, idempotency and auditing.
The owner actor is derived from verified ingress identity, never request data.
The fixed host registry selects workspaces; request bodies cannot select paths.

Each opened workspace acquires the existing local instance lock and runs the
durable deterministic worker. Databases and files remain on this storage host.
No inference route is implicitly approved. Analysis and page chat remain
unconfigured until a restricted model route is explicitly provisioned. Existing
human-approved file plans remain the only mechanism for file operations.

## Consequences

The authenticated landing page links registered job workspaces and a clearly
labeled synthetic demo. It is opened as a full page, not an embedded widget.
Uploaded resumes remain on the hosted instance; desktop resumes still require
the local helper described in ADR 0003. Gateway and bridge credentials never
enter HTML. Applicant data is excluded from published image build contexts.

Tests in `tests/unit/test_hosted_companion.py` drive owner sessions, connected
assets, origin/CSRF enforcement, scan queuing and owner isolation against the
real application. Image and live installation checks are separate evidence.
