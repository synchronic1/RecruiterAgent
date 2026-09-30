# RecruiterAgent Plow image

This variant uses the immutable Plow OpenClaw base from the Dockerfile, sets
`AGENT_ID=recruiteragent`, and keeps the inherited boot, gateway proxy contract,
runtime user, probe, and five-minute usage reporter. The source skill, installed
application, isolated Python 3.12.13 runtime, branding, and a preview with 200
synthetic PDFs are included. No applicant corpus, host credentials, databases,
or host virtual environments enter the build context. Dependencies are fetched
from the existing requirements files during the image build.

## Install the published image on Plow

The published Linux/amd64 build is publicly pullable without registry login.
Its immutable reference is:

```text
ghcr.io/synchronic1/recruiteragent@sha256:841845e2eedf343ee04c5f3d47a86780b6f053efa8cf366eddf02d5f82256e3f
```

It was built from source commit `d918a5050d882c9089eb1089fc2198279c9a5234`.
The [publishing run](https://github.com/synchronic1/RecruiterAgent/actions/runs/36756517226)
passed application and gateway checks, verified skill discovery, and used
`plow-agents image push`. An anonymous registry request returned HTTP 200 with
the expected manifest checksum, and an anonymous Docker pull succeeded.
Full recorded scope is in [dashboard-image.json](dashboard-image.json). The
[earlier release record](published-image.json) preserves the previous digest.

With the Plow CLI installed, sign in, select an available phone line, and deploy:

```bash
plow-agents login
plow-agents lines
plow-agents deploy ghcr.io/synchronic1/recruiteragent@sha256:841845e2eedf343ee04c5f3d47a86780b6f053efa8cf366eddf02d5f82256e3f --line YOUR_AVAILABLE_LINE_UID
plow-agents agents
```

Text the chosen phone number: "Use RecruiterAgent to help set up a resume review."
The first owner message starts onboarding. Use the actual Plow dashboard link
returned for your instance and your Plow account login. Each installation needs
its own persistent `/var/lib/plow` state. The one-click button still requires
Plow admin admission after testing from a separate account.

One-click deployment is now enabled for RecruiterAgent: the [public Agent Index
record](https://agent-index-server.vercel.app/v1/agent?agent_id=recruiteragent)
reports `deployable_at=2026-09-30T20:48:11.549Z`. The listing still appears
grey with a WIP badge because its demo video is absent (`has_video=false`).
The site's [readiness rule](https://aiworthusing.com/assets/agent-status.js)
requires its approval mark, a video, at least one
image, and either one-click deployment or an HTTPS install guide. The other
fields are present. GitHub sign-in on the listing is for comments and does not
control this WIP styling. Its separate public builder name is currently blank;
that name is resolved from the owner's Plow profile.

## Build and test locally

Run from a clean trusted repository checkout on a Linux Docker host:

```bash
python3 tools/build_plow_image.py --output dist/plow-image
docker build --platform linux/amd64 \
  --build-arg SOURCE_REVISION="$(git rev-parse HEAD)" \
  --tag ghcr.io/synchronic1/recruiteragent:v1 dist/plow-image
docker run --rm --network none \
  --entrypoint /opt/plow/skills/recruiteragent/.venv/bin/python \
  ghcr.io/synchronic1/recruiteragent:v1 /opt/recruiteragent/smoke.py
docker run --rm --network none \
  --entrypoint /opt/plow/skills/recruiteragent/.venv/bin/python \
  ghcr.io/synchronic1/recruiteragent:v1 /opt/recruiteragent/smoke.py --gateway
```

The application check provisions a disposable local workspace, ingests two
synthetic PDFs, renders a report, checks repeat setup, and verifies that originals
are unchanged. It also verifies the packaged source manifest, preview count,
dependencies, and inherited reporter checksum. The gateway check runs Plow's
bounded offline probe and checks actual OpenClaw skill discovery. Neither sends
messages, runs inference, or reports fabricated usage.

These application, gateway, and skill-discovery checks passed on the Linux Docker
builder on 2026-09-29. They do not demonstrate a new owner's first SMS or live
resume-analysis route. Those remain separate deployment checks.

## Publish and request one-click deployment

The repository's **Publish Plow RecruiterAgent image** GitHub Actions workflow is
manually dispatched. It builds from the selected commit, runs all three offline
checks, and publishes `ghcr.io/synchronic1/recruiteragent:<commit>` using
`plow-agents image push`. Its job summary and log contain the immutable
`ghcr.io/synchronic1/recruiteragent@sha256:<digest>` reference. The workflow uses
its short-lived package credential; no personal credential is baked into the
image.

The workflow defaults to GitHub's `ubuntu-24.04` builder. If ECR rate-limits that
builder, select `recruiteragent-image-builder` only after registering a private,
single-job ephemeral Linux runner with that label. No permanent runner is needed;
GitHub de-registers it after the job. Registry login uses a private job-specific
Docker configuration and is cleared at the end. For a manual publish,
authenticate Docker to your registry and run:

```bash
plow-agents image push ghcr.io/synchronic1/recruiteragent:v1
plow-agents profile --show
```

Check GitHub Container Registry package visibility explicitly; new packages can
be private even when the source repository is public. Set **Public** if needed,
then check an unauthenticated pull of the exact digest. Plow must be
able to pull without your registry login. Post the digest, slug `recruiteragent`,
source commit, repository URL, and Plow account UID in the **Request 1 Click
Deploy** Discord thread. An admin tests the image from another account and admits
it; publishing alone does not enable one-click.

After admission, use `plow-agents image push ... --promote recruiteragent` for
updates. Keep the registry package public and preserve each installation's state
volume. Initial admission is not a prize-verification request.

## What a new install does

The dashboard update adds an authenticated full-page companion at
`https://YOUR_INSTANCE.plow.run/recruiteragent/`. Open it directly in a browser
after signing into Plow. It lists registered job workspaces and links each live
review page. The Gateway plugin starts and supervises the loopback helper;
no additional public port or widget sandbox is needed. The inherited Gateway,
phone channel and usage reporter remain in place. See
[ADR 0004](../../docs/adr/0004-hosted-owner-dashboard.md) for the trust boundary.
The immutable image above includes this dashboard update. A separate live
installation test is recorded alongside the build checks.

The live test instance is
[RecruiterAgent dashboard test](https://1fd35ad3110b3bca5a66ea43f4ebc3f6.plow.run/recruiteragent/).
Sign in as its Plow owner. Its landing page links a three-applicant synthetic
workspace and the 200-candidate design demo. Owner ingress, connected assets,
durable scanning, original downloads, and a persisted Keep decision passed.
Originals remained unchanged. The inherited reporter also delivered actual
collected usage with HTTP 200. Restricted applicant analysis is not configured;
phone delivery, recurring reporting cadence and one-click admission require
their own checks. Existing older instances were left intact.

An additional offline check runs the actual pinned Gateway plugin, service,
authenticated landing/demo/review/assets, session and CSRF flow, and scan API:

```bash
docker run --rm --network none --entrypoint node \
  ghcr.io/synchronic1/recruiteragent:v2 /opt/recruiteragent/hosted_probe.mjs
```

Plow injects the new owner's runtime credential and resolves their phone line at
boot. The skill is already installed and visible; no pip command is needed from
the owner. The first owner message starts onboarding for a requisition and resume
folder. Workspaces and SQLite databases belong under persistent local
`/var/lib/plow/recruiteragent/jobs`, one folder per job. The index identity and
usage ledger also persist in `/var/lib/plow` through Plow's inherited reporter.

The OpenClaw dashboard and companion static HTML design demo are distinct. The
image includes the real review helper, but each job's connected page and its
authenticated session are provisioned by the helper. Inference stays off until
the owner approves and configures a restricted model/privacy route. Review
decisions never move files; the helper requires an exact human-approved plan.

Sources: [Plow variant-image contract](https://github.com/plow-pbc/plow-openclaw-agent#building-a-variant-image),
[Plow publishing CLI](https://github.com/plow-pbc/plow-agents), and
[GitHub package visibility](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility).
