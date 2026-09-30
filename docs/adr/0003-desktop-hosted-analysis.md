# ADR 0003: Desktop companion with an explicitly approved hosted analysis origin

Status: Accepted for implementation; hosted Plow compatibility remains unverified.

## Context

PRD section 14.1 assumes a private Gateway. Users also need a desktop folder to
work with an online-only OpenClaw installation. A browser dashboard URL provides
neither access to desktop files nor machine authentication to the model API.

## Decision

Keep files, the instance database, sessions, decisions, and execution on the
desktop. The helper makes outbound bounded requests through the existing
OpenClaw adapter. An operator-owned connection profile outside the applicant
folder binds one instance, one HTTPS origin, one analysis agent target, explicit
privacy approval, and a complete restricted-route attestation. The bearer secret
is read from a separate protected host file. It never enters HTML or the database.

An explicit exact-origin approval permits public HTTPS ingress for an
approved-provider route. Default adapter configurations continue rejecting public
IP ingress. Local-only configurations cannot opt into hosted access. TLS
certificate verification remains enabled, redirects remain disabled, and ambient
HTTP proxy environment variables are ignored. The browser cannot choose an
endpoint, agent, tools, model override, or credentials. No unrestricted Gateway
proxy, desktop shell, or remote filesystem command is introduced.

The desktop helper acquires the existing OS instance lock, serves its dashboard
on loopback, exchanges a short-lived local launch ticket for an instance-bound
HttpOnly session, and drives durable scan, chat, and analysis jobs. Scans do not
automatically request inference. Approved criteria and an explicit analysis job
remain required. Existing pipeline validation, stale-result checks, audit writes,
and exact human-approved file plans remain authoritative.

## Consequences and evidence boundary

Closing the browser does not stop the helper. Closing the helper pauses queued
work; expired leases can be reclaimed by a restarted helper. Retry uses the
existing bounded attempt budget. No live database or raw PDFs are uploaded.
Approved extracted text and evidence do leave the desktop for hosted inference.

This integration targets a machine-authenticated OpenClaw Chat Completions API.
Plow's owner-proxy dashboard does not automatically provide that API or a desktop
credential. Configure a supported machine-authenticated ingress and a genuinely
restricted analysis agent before using real applicant data. Browser session
cookies are not copied into the connection profile. Hosted setup, provider
retention, tool denial and installed-version compatibility require separate live
verification; deterministic transport tests are not that verification.
