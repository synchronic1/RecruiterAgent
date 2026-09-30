# RecruiterAgent Plow image

This variant uses the immutable Plow OpenClaw base from the Dockerfile, sets
`AGENT_ID=recruiteragent`, and keeps the inherited boot, gateway proxy contract,
runtime user, probe, and five-minute usage reporter. The source skill, installed
application, isolated Python 3.12.13 runtime, branding, and a preview with 200
synthetic PDFs are included. No applicant corpus, host credentials, databases,
or host virtual environments enter the build context. Dependencies are fetched
from the existing requirements files during the image build.

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
manually dispatched. It builds from the selected commit, runs both offline
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

GitHub Container Registry packages initially have private visibility, even when
the source repository is public. Change the package to **Public** in its package
settings, then check an unauthenticated pull of the exact digest. Plow must be
able to pull without your registry login. Post the digest, slug `recruiteragent`,
source commit, repository URL, and Plow account UID in the **Request 1 Click
Deploy** Discord thread. An admin tests the image from another account and admits
it; publishing alone does not enable one-click.

After admission, use `plow-agents image push ... --promote recruiteragent` for
updates. Keep the registry package public and preserve each installation's state
volume. Initial admission is not a prize-verification request.

## What a new install does

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
