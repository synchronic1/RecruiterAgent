# RecruiterAgent source installation

For a Plow-hosted instance with RecruiterAgent and its dependencies already
installed, use the [published image guide](https://github.com/synchronic1/RecruiterAgent/blob/main/submission/plow-image/README.md).
It includes the immutable image digest, install commands, and recorded checks.
The instructions below install the source skill into an existing OpenClaw.

## Prepare the package from a repository checkout

```powershell
python tools/build_openclaw_package.py --output dist/RecruiterAgent
```

The builder refuses an existing output directory. Choose a fresh directory for
another build. It includes only reviewed source, runtime assets, schemas,
migrations, skill instructions, and operational Markdown. It excludes `resume/`,
databases, generated real-data reports, credentials, environments, compiled
extensions, caches, and downloaded dependencies. A hash manifest records contents.

## Install into OpenClaw

For an OpenClaw version supporting local skill installs:

```text
openclaw skills install ./dist/RecruiterAgent --as recruiteragent
openclaw skills info recruiteragent
openclaw skills check
```

Use the installed skill directory reported by OpenClaw as `{baseDir}` for the
installer. If that OpenClaw version lacks local installation, copy the prepared
directory into a trusted configured skills root and check that the intended agent
can see it. Never install the whole repository as a skill: the checkout may
contain real applicant data. Per-agent visibility/allowlists remain operator
configuration. OpenClaw skill discovery/installation is version dependent and
has not been verified live by this source package.

## Install the Python application

```text
python "{baseDir}/scripts/install.py"
python "{baseDir}/scripts/install.py" --verify-only
python "{baseDir}/scripts/run.py" -- --help
```

The installer uses a dedicated skill-local virtual environment. No system-wide
Python packages, PATH changes, OpenClaw configuration changes, or model credentials
are required. Internet/package-registry access is needed for the first install;
this is source-contained, not an offline binary distribution. Python and OpenClaw
are external prerequisites. Manual equivalent from `application/`:

```text
python -m venv ../.venv
../.venv/Scripts/python.exe -m pip install -r requirements.txt
```

On Linux/macOS use `../.venv/bin/python`; these hosts require their own compatibility
verification. Exact runtime versions are constrained by `requirements.lock`.
Test dependencies are not installed by `requirements.txt`.

## Optional synthetic demo

```text
python "{baseDir}/scripts/install.py" --demo
```

Serve only `{baseDir}/application/docs/review-artifacts` using the skill's Python:

```text
"{baseDir}/.venv/Scripts/python.exe" -m http.server 8766 --bind 127.0.0.1 --directory "{baseDir}/application/docs/review-artifacts"
```

Use another free port when 8766 is occupied. Open
`http://127.0.0.1:8766/recruiteragent-design-preview.html`. This preview has local
synthetic originals and hidden agent declarations. Edits reset on reload and
feedback is never sent. Do not serve the entire application or dataset directory.

## Provision the connected product

With an existing local job folder and job-description text file:

```text
python "{baseDir}/scripts/run.py" -- setup --folder <job-folder> --job <job-description.txt> --json
python "{baseDir}/scripts/run.py" -- status --instance <returned-instance-id> --json
python "{baseDir}/scripts/run.py" -- start --instance <returned-instance-id>
```

Read the result envelope. Follow the helper's reported connected-page and session
instructions. Do not use the static demo server as the product backend. Consult
`application/docs/installation.md` and `application/docs/local-operation.md` for
the detailed runbook. A real approved restricted OpenClaw route, credentials,
privacy settings, and approved criteria remain operator setup; an installer must
not fabricate these or silently start inference.

For a desktop folder paired with an online-only agent, follow
`application/docs/desktop-companion.md`. Use an operator-owned HTTPS connection
profile outside the job folder and launch with `start --connection-config
<protected-profile.json> --open-browser`. The packaged example lives at
`application/examples/desktop-connection.example.json`; approvals start false.
The desktop needs the Python helper, not a second OpenClaw gateway. A hosted
browser dashboard URL alone is not a machine-authenticated restricted API.
