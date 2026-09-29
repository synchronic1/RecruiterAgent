# Run a Plow OpenClaw instance

Plow's base runs OpenClaw in Docker with a Plow phone connection and built-in
Agent Index reporting. Run it in its own Compose project and persistent volume.
The base alone is a general OpenClaw agent; install RecruiterAgent before
describing it as a working RecruiterAgent deployment.

## Prerequisites and login

Use Linux with Git, Python 3.11 or newer, Docker, and Compose 2.24 or newer.
Install the CLI into a directory separate from any running OpenClaw instance:

```bash
git clone https://github.com/plow-pbc/plow-agents.git
export PATH="$PWD/plow-agents/bin:$PATH"
plow-agents login
plow-agents lines
```

Send the activation text displayed by `login` from your phone. Keep the ID and
phone number of an available line. The account credential stays on the host;
`mint` creates the separate line-scoped credential used by the container.

## Run the upstream base locally

```bash
git clone https://github.com/plow-pbc/plow-openclaw-agent.git
cd plow-openclaw-agent
plow-agents mint YOUR_LINE_ID
docker compose -p recruiteragent-plow up --build -d
docker compose -p recruiteragent-plow ps
```

`YOUR_LINE_ID` is the ID returned by `lines`, not a phone number. Mint writes
`plow-credentials` with private permissions. Do not commit it or put it into a
Docker build. Text the chosen line from your phone and confirm an agent reply.

The local Control UI is at `http://127.0.0.1:3001` on the Docker host. For a
remote host, forward that loopback port from your workstation:

```bash
ssh -L 3001:127.0.0.1:3001 USER@YOUR_HOST
```

Then open `http://127.0.0.1:3001` locally. The Control UI belongs to OpenClaw;
RecruiterAgent's connected review helper is a separate application endpoint.
The base's local dashboard assumes anyone with loopback access is an admin.

State persists in the Compose volume. `docker compose down` retains it;
`docker compose down -v` deletes it. Use the same `-p recruiteragent-plow`
project name for subsequent commands.

## Install RecruiterAgent and enable its listing

Build the clean source skill from this repository using
`tools/build_openclaw_package.py`, then follow
[the installation guide](../skill/references/install.md) to install it into
the new instance's trusted skills root. Install its Python dependencies and
check that the agent can discover it. Complete a real review task using the
helper before counting this deployment as demonstrated.

Set these non-secret values in the new instance's `plow-credentials`, alongside
the minted credential, once RecruiterAgent is installed:

```dotenv
AGENT_ID=recruiteragent
AGENT_NAME=RecruiterAgent
AGENT_BLURB=Cut through high volumes of applications with agentic AI and evidence-backed review, curated by humans.
AGENT_RUNTIME=OpenClaw
```

Recreate this Compose project's agent container with `docker compose
-p recruiteragent-plow up -d`. The inherited reporter registers the listing
and reports usage every five minutes. Keep its state volume to preserve the
reporting identity. The separate submission client is unnecessary when using
the base's inherited reporter.

## Publish a RecruiterAgent variant for one-click installation

Build a separate image using the upstream documented `FROM
public.ecr.aws/e1h7x4a2/plow-cloud-agents:base-<commit>@sha256:<digest>`.
Find an actually published base reference in the registry; do not guess a tag
or digest. Include the clean RecruiterAgent skill and its application setup,
set `AGENT_ID=recruiteragent` in the image, and preserve the inherited boot and
reporter. Follow the base's documented `/opt/plow/prompt` and `/opt/plow/skills`
locations. Boot rewrites workspace identity files, so use those image-level
locations for durable custom instructions.

After the image has been tested:

```bash
plow-agents image push ghcr.io/synchronic1/recruiteragent:v1
plow-agents profile --show
```

Make the registry package public. Give the printed immutable image reference,
account UID, and slug to the Plow admin for initial admission. Registration,
admission, verification, and prize qualification are separate steps. The demo
video can be added to the listing later; it is still required for qualification.

## Hosted installation checked on 2026-09-29

RecruiterAgent was installed in a running Plow-hosted OpenClaw 2026.9.6 instance.
An isolated Python 3.12.13 runtime supplies the application's dependencies;
OpenClaw's existing runtime and other agents were not restarted. The installed
skill is at `/var/lib/plow/workspace/skills/recruiteragent`. Its installer and
`--verify-only` check succeeded, and `skills.status` reported `eligible`,
`modelVisible`, and `userInvocable` as true.

The owner dashboard uses the instance's `https://<instance-uid>.plow.run` URL.
Without an owner session it redirects to Plow account login. Sign in with the
phone number or email used to activate Plow and the login code Plow sends.
CLI activation and dashboard account login are separate credentials. The raw
`exe.xyz` VM URL can show an exe.dev sign-in page; use Plow's owner dashboard
instead. Plow's account-authenticated web-launch endpoint also supplies a
single-use link that establishes the dashboard session.

### Dedicated reporter on the hosted base

The base already schedules reporting every five minutes. The following installer
preserves its original client and replaces the entry with a narrow launcher
for `recruiteragent`. Run from a trusted checkout on a dedicated recruiting
instance after installing the skill:

```bash
/var/lib/plow/workspace/skills/recruiteragent/.venv/bin/python \
  submission/install_plow_reporter.py
python3 /opt/plow/agent-index-client.py --register --agent recruiteragent
python3 /opt/plow/agent-index-client.py --agent recruiteragent --dry-run
python3 /opt/plow/agent-index-client.py --agent recruiteragent
```

Registration uses the base's protected `PLOW_AGENT_TOKEN` environment; never
put it in source, a URL, or a command argument. The installer checks the exact
upstream client hash and refuses an unknown version. The original Apache-licensed
client remains at `/opt/plow/agent-index-client.upstream.py` with its license.
Report identity and cumulative state stay in the private, persistent
`/var/lib/plow/recruiteragent-index` directory. Each invocation records only
its completion time, exit code, mode, and usage endpoint HTTP status in
`run-events.jsonl`.

The collector reads the hosted OpenClaw SQLite transcript store in read-only
mode. It counts usage after installation, excludes
`agent:main:recruiteragent-setup`, deduplicates responses, and disables unrelated
Codex/Claude/Hermes collectors. This assumes the instance is dedicated to
RecruiterAgent; future unrelated chats should not run on it. Missing, corrupt,
or unmapped usage fails closed. It sends token counters, not resume or chat
content. An empty report does not establish real recruiting engagement.

A manual report and two consecutive scheduled invocations exited zero and
received HTTP 200. The scheduled completions were five minutes apart. The
source-only skill and reporter are installed and checked;
the live resume-analysis adapter gate and a real review task remain unverified.
Recorded results are in [hosted-install-evidence.json](hosted-install-evidence.json).
The focused reporter/submission suite passed 13 tests. Rebuilding or replacing
the upstream image may restore its original reporter entry; reapply the checked
integration or include it in a tested RecruiterAgent image. A public custom image
and one-click admission have not been completed.

Sources: [OpenClaw base](https://github.com/plow-pbc/plow-openclaw-agent),
[Plow CLI](https://github.com/plow-pbc/plow-agents),
[Agent Index client](https://github.com/plow-pbc/agent-index-client).
