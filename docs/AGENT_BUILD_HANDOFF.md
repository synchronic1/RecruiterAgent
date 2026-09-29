# Coding-agent handoff: OpenClaw Resume Review

**Owner:** Peter  
**Date:** September 29, 2026  
**Authority:** `OpenClaw_Resume_Review_PRD_v1.0.md`  
**Package status:** Specifications only. No application, helper binary, or installable skill is included yet.

## Mission

Implement the PRD as a reusable OpenClaw skill and a deterministic, folder-local review application. One resume folder is one instance with its own database, generated HTML, review state, and file-action history. The skill provisions tested components; it does not create new application logic during every setup.

Start by reading the full PRD. The Markdown is canonical for coding; the PDF and Word copies are reading/editing formats of the same specification. Record any necessary design departure in an architecture decision record rather than silently changing scope.

## Non-negotiable constraints

1. Preserve local operation, per-folder ownership, repeatable setup, and the shared-drive storage-host-local deployment.
2. Keep the user's original page-bound chat requirement. Deterministic review must also work without inference.
3. Separate reviewer decisions from pending file intentions and actual file locations.
4. Never move files because a model suggested Reject. Require an exact human-approved plan and execute through the helper.
5. Use recoverable Trash only. No permanent deletion, automatic purging, applicant messaging, or ATS integration in version 1.
6. Keep SQLite on supported local storage at the storage host. One writer on another machine does not make a mapped-drive database acceptable.
7. Do not put Gateway credentials in HTML or expose an unrestricted Gateway proxy. Resume analysis must use a restricted OpenClaw route with no dangerous tools.
8. Keep authoritative human state and evidence across rescans, restarts, template regeneration, and migrations.
9. Never infer sensitive characteristics, introduce an opaque suitability score, or automatically reject applicants.
10. Clearly distinguish fixtures/mocks, implemented features, live-tested integrations, and unfinished work.

## First implementation pass

Create the repository and dependency lockfile, then implement the instance schema, stable IDs, revision checks, file-action contract, and synthetic fixture generator. Establish a storage adapter that can perform tested same-volume no-clobber moves.

Build one vertical slice with no inference: setup a synthetic folder, start the helper, show the list, save a human decision, preview an action plan, record reviewer approval, apply one move, refresh the report, and restore the file through another approved plan.

Next prove crash recovery, stale-plan rejection, destination collision protection, idempotency, instance isolation, and repeat setup. Only after these gates pass should the build process real resumes or call an inference route.

Follow milestones 0–5 in PRD section 19. The local demonstration does not fulfill page-bound chat and shared-host requirements by itself.

## Required contracts and test approach

Derive versioned JSON schemas for analysis, filters, API errors, action plans, and manifests from PRD sections 7, 9, and 12. Treat the example IDs and hashes as placeholders, not valid runtime values. Generate API documentation from actual implemented types and validate contract examples in tests.

Implement all forty acceptance tests in section 18 or map each to an equivalent test with recorded evidence. Use synthetic data by default. Separate deterministic tests from live OpenClaw integration and evidence-quality evaluation. Report unsupported host/storage combinations honestly.

Read current official OpenClaw documentation before coding version-sensitive configuration. Verify the installed runtime's actual skill loading, auth, agent routing, session behavior, and tool-denial controls. The PRD's new `resume-review` commands are product commands to build, not existing OpenClaw CLI functions.

## Skill contract to implement

The final skill must have valid `SKILL.md` frontmatter, for example:

```yaml
---
name: resume-review
description: Set up and operate a folder-local resume review workspace with evidence-backed summaries and human-approved file organization.
---
```

Its body should define supported triggers, prerequisite checks, instance binding, approved helper entry points, result interpretation, privacy boundaries, and safe failure handling. Reference bundled routines through the installed skill's supported path convention. Do not include commands that point to nonexistent scripts in a released skill.

The setup/orchestration context may request privileged installation steps through operator approval. The resume-analysis context must not inherit that capability. The final skill must never instruct the agent to execute applicant-provided code or to bypass the helper with shell-based file moves.

## Completion report

At each milestone, report implemented features, exact test commands/results, live versus mocked integrations, dependency versions, and remaining blockers. At final delivery, include the installable skill, packaged helper, migrations, frontend assets, schemas, fixtures, tests, OS/storage compatibility matrix, operator runbooks, and a synthetic example instance.

Do not declare production readiness from a demo or passing UI screenshots. Full completion requires the P0/P1 workflow, recovery/security gates, live restricted OpenClaw tests, and documented operational limits.
