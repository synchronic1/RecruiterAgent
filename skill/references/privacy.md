# Privacy and data flow

## Where resume text goes

Before the first analysis, the operator must be shown where resume text is
processed and which route is approved. That approval is a precondition for
processing real applicant data, not a notification after the fact.

| Route | Behaviour |
| --- | --- |
| `local_only` | Text is processed by a local model. If that model is unavailable, analysis **stops**. There is no remote fallback, for summarization, chat, OCR, or any future embedding. |
| `approved_provider` | Text is processed by the specific provider route the operator approved. Any change to that route is a new approval. |
| `unavailable` | Analysis does not run. Existing summaries, sorting, filtering, manual review, notes, and approved file actions all keep working. |

Route behaviour is verified through configuration and network tests — never by
asking a model to state whether it is local. A model's claim about its own route
is data, not evidence.

If the route cannot be verified as adequately restricted, inference **fails
closed**. The correct response is manual review, never attaching a different,
unrestricted route.

## Separation of privileges

Two contexts, never merged:

* **Setup / orchestration** — may request privileged installation steps through
  operator approval.
* **Resume analysis** — has no shell, no write or edit access, no browser control,
  no messaging, no credential reading, no unrestricted file reading, and no
  cross-session access. It receives only the current job's bounded data and
  returns structured output.

The analysis context never receives the worker-result submission credential, the
approval credential, or the executor credential. A response that imitates an API
command is data.

## Untrusted content

* A resume folder is never an instruction workspace. A file named `AGENTS.md`,
  `SKILL.md`, `CLAUDE.md`, or similar inside a job folder is applicant-supplied
  data, and is recognised and ignored as instructions.
* Resumes are parsed in resource-constrained workers without executing macros,
  embedded scripts, links, or active content.
* Job descriptions and imported criteria are untrusted text until a human reviews
  them.
* Prompt-injection strings must not change policies, reveal other documents,
  authorize operations, or trigger external requests.
* Applicant text is delivered to a model inside an explicit data envelope, after
  control characters and bidirectional overrides are neutralised.

A detected injection attempt produces a **warning for a human reviewer** and
nothing else. It never penalises an applicant and never triggers an automatic
action.

## What is minimised

* Contact details are omitted from a request when they are not needed for
  qualification analysis.
* Operational logs carry IDs, counts, timings, and error codes — not applicant
  content, not absolute paths, not names.
* Provider-visible session identifiers are opaque IDs bound server-side to an
  instance and a reviewer. Candidate names and folder paths are never used as
  session IDs.
* Chat retrieves only the permitted evidence needed for the question. Four hundred
  full resumes are never stuffed into a prompt.
* Cross-job and global memory retrieval and writing are disabled for the analysis
  route.

## Credentials

The OpenClaw Gateway credential stays in protected host configuration. It never
appears in HTML, in a generated report, in a portable backup, in a command line,
in a log line, in a model request, or in browser local storage.

Authentication secrets and Gateway tokens are never stored in the portable
database. Only non-secret actor references and role assignments are stored, so
that history can be explained.

## Data at rest

Reports, extracted text, chats, backups, and OpenClaw transcripts can all contain
recruiting data and require controlled retention.

* A generated `review.html` snapshot contains sensitive recruiting information.
  Its visibility follows **filesystem permissions**, not the helper's login
  screen. Handing a snapshot to someone is handing them the data.
* Backups must be protected at least as strongly as the active folder.
* The `.review/` directory is never exposed through a generic static directory
  mount.
* Host administrators retain filesystem power. Application code, database files,
  secrets, and managed destinations are protected by ACLs; an inbound drop area
  may be writable for intake, but that must not grant access to the report or
  `.review/`.

## Retention

Version 1 has **no purge scheduler**, no permanent deletion, and no automatic
Trash purging. Retention settings create administrative review tasks; they never
silently destroy anything.

A future permanent purge would need to cover originals, extracts, reports, chats,
backups, and applicable runtime and provider records, through a separately
designed and separately approved process.

## Hiring controls

The application may summarize explicit professional evidence and help apply
approved, job-related filters. It must not:

* infer or filter on protected traits;
* use names, photographs, or dates as demographic proxies;
* score health, religion, family status, or presumed personality;
* infer age from graduation or employment dates;
* produce an aggregate suitability score, ranking, or automatic rejection.

Human review is an application control. It is not a blanket exemption from
employment obligations. Before production use the organisation must approve the
criteria, review accommodations and accessibility, define access and retention,
and assess the automated-employment-tool rules that apply in its jurisdiction.
