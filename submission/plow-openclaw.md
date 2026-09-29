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

This guide has been checked against upstream documentation. A Plow container
with RecruiterAgent installed has not yet been booted and verified here.

Sources: [OpenClaw base](https://github.com/plow-pbc/plow-openclaw-agent),
[Plow CLI](https://github.com/plow-pbc/plow-agents),
[Agent Index client](https://github.com/plow-pbc/agent-index-client).
