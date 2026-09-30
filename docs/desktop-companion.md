# Desktop companion and an online OpenClaw agent

The local helper serves the RecruiterAgent dashboard and owns the selected resume
folder. It sends bounded extracted text and evidence to an explicitly approved
hosted analysis agent. Originals and SQLite stay on the desktop. Closing the
browser leaves the helper running; closing the terminal stops it. Restarting
resumes queued work and reclaims expired leases. Retry attempts are bounded.

Implemented: authenticated loopback dashboard, local-owner launch, deterministic
scan worker, queued analysis and chat worker, direct page-bound chat, protected
instance-bound HTTPS connection profiles, and a no-resume connection probe.

Not live-verified: the existing Plow owner's browser dashboard as a machine API,
production hosted credentials, restricted agent configuration, provider retention,
or an end-to-end real-applicant run. A Plow dashboard URL ending in
`/new?agent=main` is not a connection profile. A usable deployment must expose a
supported authenticated OpenClaw API origin and a restricted analysis agent.
Do not copy browser cookies or use an unrestricted personal agent as a shortcut.

## Install and provision

Python 3.12 or newer is required. From the repository, in PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\resume-review.exe setup --folder "C:\Recruiting\Engineering" --job "C:\Recruiting\engineering-role.txt" --json
```

Use the returned `instance_id` below. The requisition is a UTF-8 text file.
For an installed OpenClaw skill, its `scripts/run.py --` wrapper provides the same
CLI commands without requiring a checkout or a desktop OpenClaw installation.

## Approve and configure the hosted route

The hosted operator must enable `/v1/chat/completions` and `/v1/models`, arrange
machine authentication through their ingress, and provision an analysis agent
whose actual tool policy denies shell, edits, browser control, messaging,
credentials, unrestricted reads, other sessions, and agent spawning. Its trusted
instruction workspace must be separate from applicant folders.

Copy [the connection example](../examples/desktop-connection.example.json) to an
operator-owned directory outside every resume folder, such as
`%LOCALAPPDATA%\RecruiterAgent\connections`. Set:

- `instance_id`: the workspace returned by setup.
- `base_url`: the HTTPS API origin only, with no path, query, userinfo, or fragment.
- `agent_id`: the restricted analysis agent, not the unrestricted `main` agent.
- `secret_file`: an absolute path to a separate protected UTF-8 bearer token file.
- `provider_label`: the approved inference provider and retention policy reference.
- `privacy_approved`: true only after approving applicant text leaving this desktop.
- `attestation`: the actual operator, an ISO timestamp with timezone, and each
  restriction confirmed true only after checking the deployed configuration.

The example deliberately leaves approvals false. The helper refuses it until
configured. Protect both files with permissions for your operating-system account;
on Linux/macOS the credential file must have no group/other permissions (0600).
On Windows use a user-private directory and restrict its ACL to the owner and
required system administrators. No token should appear in a shell command, repo,
resume folder, browser, dashboard URL, or portable database.

TLS verification is enabled; redirects and environment proxy forwarding are
disabled. Each profile approves one exact origin and agent target. Credential
rotation takes effect on the next request. Changing the profile requires a helper
restart. Multiple profiles can pair different job instances with different agents.

## Probe and launch

```powershell
.\.venv\Scripts\resume-review.exe check-connection --instance INSTANCE_ID --connection-config "$env:LOCALAPPDATA\RecruiterAgent\connections\engineering.json" --json
.\.venv\Scripts\resume-review.exe start --instance INSTANCE_ID --connection-config "$env:LOCALAPPDATA\RecruiterAgent\connections\engineering.json" --open-browser
```

The probe sends no resumes and makes no inference request. It checks authentication
and the listed target; it cannot prove tool restrictions or provider location.
Keep its result with the operator's deployed-version and tool-policy evidence.
A successful probe is only part of live verification, not the whole gate.

The helper binds `127.0.0.1` on an available port and opens a short-lived launch
ticket. It exchanges that ticket once for an instance-bound HttpOnly session,
then redirects to a credential-free review URL. Browser mutations require the
session's CSRF token. Foreign origins and hosts are refused. Start accepts
`--port PORT` when you need a fixed port. Without `--open-browser`, the terminal
prints the single-use launch link; access logging is disabled so tickets are not
recorded in HTTP logs. An expired ticket requires relaunching the helper.

To use manual review without hosted inference, omit `--connection-config`.

## Use the dashboard

1. Scan the folder. Extraction and document registration run locally with no
   model call; scans do not automatically start analysis.
2. In the Requisition tab, expand **Review and approve screening criteria**. Add
   requirements one per line, save a draft, review the displayed active and draft
   requirements, then approve it. Criteria proposed by chat also appear here for
   review. Saving a requisition reference alone does not approve criteria.
3. Select candidates and choose **Summarize selected candidates**. Jobs bind the
   document revision and criteria version. The worker sends bounded prompts,
   validates structured evidence, and preserves human decisions. Use **Refresh**
   to load newly committed results. The connection indicator updates periodically.
4. Use **OpenClaw feedback** for a bounded folder-scoped question or correction.
   The page's direct chat returns the answer; API clients may use `mode: "queue"`
   to defer a turn and poll `/jobs/{job_id}`. Queued exchanges persist in the local
   conversation store and do not grant approval or change decisions.
5. Curate Keep / advance, Hold, or Reject. File organization still requires a
   separately displayed exact plan, human approval, and helper execution.

No public desktop port, permanent tunnel, shared-drive SQLite, desktop shell
access, or full-folder cloud synchronization is needed. The cloud agent cannot
request arbitrary desktop files or send commands to execute locally.

## Verification

Deterministic integration tests are in `tests/unit/test_desktop_helper.py`. They
use synthetic resumes and injected HTTP envelopes, including the full local
dashboard → queued scan → analysis → persisted profile → direct/queued feedback
path. These tests also assert originals and human decisions remain unchanged.
Injected transport is explicitly reported as non-live by the connection probe.
Run them with the CLI, adapter, API chat, queue, layering, and packaging tests.

The desktop implementation does not change the published Plow image, its usage
reporter, or the original hosted agent installation.
