# RecruiterAgent: Agent Index submission

## Listing draft

**Name:** RecruiterAgent

**Slug:** recruiteragent

**Blurb:** Cut through high volumes of applications with agentic AI and
evidence-backed review, curated by humans.

RecruiterAgent turns a folder of applications and a job requisition into a
review workspace. OpenClaw can help extract relevant evidence and respond to
reviewer guidance; humans curate criteria, review findings, and decide whom to
advance, hold, or reject. Every file move requires approval of the exact plan.
There is no automatic rejection or suitability score.

The hosted design preview is an interactive demonstration. Its local browser
feedback is not submitted to OpenClaw, and its local summaries are not evidence
of live model analysis. The source package contains a separate connected helper
and restricted OpenClaw adapter. Live engagement must be demonstrated separately.

## Prepared assets

- `listing.json`: metadata draft. Empty fields require actual public URLs/YouTube ID.
- `public/recruiteragent-logo.png`: square leaderboard logo.
- `public/recruiteragent-demo.png`: synthetic-only dashboard image.
- `public/install.html`: install guide to host at a public HTTPS URL.
- `demo-script.md`: recording outline and evidence checklist.
- `prepare_index.py`: validates fields and prints registration arguments; sends nothing.

Project source is MIT. Applicant data and dependency binaries are excluded from
the clean release. Any fetched Agent Index client retains its upstream Apache
license and notices; it is not relicensed by the project MIT license.

## Bring your own OpenClaw instance

Prepare the GitHub tree from this checkout:

```powershell
python tools/build_public_source.py --output dist/RecruiterAgent-GitHub
```

Create an empty public GitHub repository, then use the generated directory as
the repository root. From that directory, initialize Git, add these clean files,
commit, and push to the new repository's ordinary HTTPS remote. Preserve your
existing Git identity and credential helper. The public tree includes tests,
source, skill, submission assets, and a content hash manifest. It excludes the
local preview deployment and applicant data. Update `submission/listing.json`
with the new repository URL before generating the registration command.

After creating an empty repository (without an initial README/license), run:

```powershell
cd dist/RecruiterAgent-GitHub
git init -b main
git add .
git commit -m "Publish RecruiterAgent MIT source and OpenClaw skill"
git remote add origin https://github.com/YOUR-ACCOUNT/RecruiterAgent.git
git push -u origin main
git rev-parse HEAD
```

Replace `YOUR-ACCOUNT` with your actual GitHub account. If Git requests a commit
identity, use your intended existing identity; authentication is handled separately
by your installed credential helper. Keep the final SHA for verification.

Use the existing agent plus the reporting client. This path does not require
rebuilding the agent on a Plow base image. Install RecruiterAgent on a dedicated
OpenClaw instance and complete a real review task before recording/reporting it.

1. Publish the clean MIT source release to a public repository. Record its commit
   SHA. Do not publish the checkout's datasets, real applicant PDFs, databases,
   credentials, or generated real-data pages.
2. Host the three `public/` assets over HTTPS. Fill `repo`, `image`, and
   `install_url` in `listing.json`. Upload the demo to YouTube and fill `video`
   with its 11-character ID, not its URL.
3. On the actual Linux OpenClaw host, copy this submission directory and fetch
   the upstream client. The downloader resolves a commit once, fetches by that
   commit, and records hashes in `agent-index/upstream-manifest.json`:

   ```bash
   cd /path/to/submission
   python3 prepare_index.py --fetch-client
   python3 agent-index/agent_index_client.py --self-check
   ```

   Fetching requires network access. The client was not downloaded or live-tested
   in this Windows checkout. Its current code uses POSIX facilities; use Linux.
   Recent compressed OpenClaw transcripts need Python 3.14 or libzstd.

4. Set `OPENCLAW_STATE_DIR` to this dedicated instance's real state directory.
   Use a dedicated reporting user/home with no unrelated Codex, Claude, or Hermes
   histories: upstream collectors merge available usage sources. Do not set
   `HERMES_HOME` to a nonexistent store. Keep the reporting user's home persistent
   across restarts to preserve the install identity.

   ```bash
   export OPENCLAW_STATE_DIR=/actual/recruiteragent/openclaw-state
   python3 agent-index/agent_index_client.py --agent recruiteragent --dry-run
   ```

5. Authenticate with Plow using its current login flow, then provision the
   agent-scoped `PLOW_AGENT_TOKEN` for this instance. Keep it out of the repository,
   screenshots, shell logs, and image layers. The current client requires an
   agent-scoped token; do not assume the account login token is interchangeable.
6. Run `python3 prepare_index.py`, inspect the printed command, and execute it on
   that host to register. Then check:

   ```bash
   python3 agent-index/agent_index_client.py status
   python3 agent-index/agent_index_client.py --agent recruiteragent
   ```

7. Schedule that last report every five minutes with cron/systemd on the same
   host, same user, same home, and same state-directory environment. A cron
   example, after securely configuring credentials/environment for that user:

   ```cron
   */5 * * * * cd /path/to/submission && /usr/bin/python3 agent-index/agent_index_client.py --agent recruiteragent >> /path/to/private/recruiteragent-index.log 2>&1
   ```

   Cron does not inherit interactive shell exports. Load the protected environment
   through your host's service configuration. Confirm the first scheduled run
   succeeds and the Index displays the correct agent's usage. Do not manufacture
   engagement or count this application's development work.

## Verification and optional one-click deployment

Post the public repository URL, exact commit SHA, and Agent Index ID in the
[verification thread](https://discord.com/channels/1519035948191449268/1549100840583700481).
The owner must send that message; no Discord message has been sent from this repo.

For one-click deployment, first build and verify a complete runnable OpenClaw
image with RecruiterAgent installed. A source archive or reporting-only image is
not a deployable agent. Push a public image using `plow-agents image push`, obtain
your UID with `plow-agents profile --show`, and give the UID, slug, and printed
image reference to an admin in [Discord](https://aiworthusing.com/discord).
Future verified image updates can use `--promote recruiteragent`.

Registration, reporting, verification, public image publication, and live
OpenClaw engagement have not been completed by these preparation scripts.

Sources: [publish requirements](https://aiworthusing.com/agent-index/publish),
[client documentation](https://github.com/plow-pbc/agent-index-client), and
[Plow CLI documentation](https://github.com/plow-pbc/plow-agents).
