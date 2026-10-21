# Public synthetic-preview review — October 20, 2026

Review base: `3d5b0838216d4559649eddf1bcb7523038116c1c`, GitHub
`synchronic1/RecruiterAgent` main. Work is isolated from the original checkout,
including its untracked `CLAUDE.md` and video. No other agent is active in this
session's agent tree; that is not a cross-machine writer lock.

## Source boundary

`cloudflare-demo/wrangler.jsonc` maps the existing `recruiteragent-demo` static
Worker to `recruiteragent.airanger.dev`. Its README and the project README link
the existing public synthetic preview. The signed-out extensionless preview
returned HTTP 200 on October 20. The original checkout's ignored public HTML
matches that response after removing only the observed appended Cloudflare
challenge script and normalizing line endings. This establishes the existing
public package, not a pinned deployed Git revision. The tracked generator and
current report assets have evolved since that ignored package was built.

The candidate packages the current main source for review. It does not publish
it. Rebuild both synthetic and public outputs using the documented commands;
inspect the generated package before any separately authorized deployment.
The generated HTML/PDFs stay ignored, consistent with the existing repository.

## Changes

Public packaging adds the canonical for the route already linked by the portal,
an explicit synthetic-preview title/description, a main landmark covering the
preview controls and candidates, and the existing skip link targeting it.
Rendered review found saved text below 4.5:1 in light mode and phone-width decision
controls overflowing the viewport. Public-only overrides retain the status icon
color, use normal readable text and reflow the decision controls. No-JavaScript
guidance explains that the synthetic candidates need JavaScript and there is no
real applicant data or connected OpenClaw session. Existing fixture dates remain.

Only named synthetic outputs and their referenced PDF fixtures are packaged.
The packager refuses real-corpus or connected declarations. Product report
templates, authenticated helpers, applicant data and inference are outside this
change. No form is submitted during qualification.

## Qualification

- Synthetic-source build and public package build pass.
- Four focused unit checks pass, including rejection of the real-corpus variant
  and existing agent-declaration privacy boundaries. Tests generate their own
  temporary fixtures and do not require the ignored public package beforehand.
- Eight local Chrome combinations cover 360/1280 px, light/dark and JS/no-JS:
  canonical/description in initial HTML, one H1/main, keyboard skip focus,
  synthetic disclosure, no-JavaScript guidance and no document overflow.
- Rendered axe checks cover the JS pages and script-free clones of the no-JS
  DOM. JavaScript is enabled only in those separate clones to run the auditor;
  application scripts remain removed. Zero violations after the fixes.
- All browser requests are intercepted. No auth, form submissions, provider
  requests, paid inference, dataset API or real-corpus checks are run.

The sole GitHub workflow, `publish-plow-image.yml`, is `workflow_dispatch` only.
Pushing a review branch/opening a draft does not dispatch it. Its image publishing
and the separate manual Wrangler deployment were not invoked.

Independent exact-head review and publication remain unrun. This draft is separate
from HeurChain #59, portal #3 and ModelRouter #11. Full DNS completeness is unknown;
the prior Cloudflare inventory 403 has not been retried or bypassed.
