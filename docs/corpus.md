# Real resume corpus (opt-in)

This document describes the opt-in real-resume corpus used by the
`tests/corpus/` suite. The repository's `.gitignore` already refers here; this
file is the document that comment promises.

## What this corpus is

`resume/` holds roughly 1.1 GB of **real, public HuggingFace resume datasets**
(eight datasets, about 8,900 files) fetched for optional local testing of the
deterministic pipeline: discovery, type sniffing, extraction, and the
anti-fabrication validator.

**These are real people's resumes.** They contain names, contact details, and
employment history. Treat the whole tree as sensitive personal data (PII).

They are **third-party, publicly published datasets**, not data this repository
produces or owns. **This repository does not redistribute them**: `resume/` is
gitignored and must stay out of the repository, its history, and any remote.

## The rule the harness enforces

* The **default `pytest` run is synthetic-only** and never reads `resume/`.
* The corpus is **opt-in per run**, by an explicit environment flag.
* Real applicant text must **never** be written into a committed fixture, golden
  file, snapshot, or report. The corpus suite reads it at runtime, in memory.

`tests/corpus/conftest.py` is the single gate:

| State | Result |
| --- | --- |
| `RESUME_REVIEW_REAL_CORPUS` unset | every `tests/corpus/` test is collected and **skipped** with a visible reason; the corpus is never opened |
| flag set, corpus present | the suite runs, bounded (see below) |
| flag set, corpus absent | the corpus tests **fail loudly**, telling the operator to fetch the corpus first |

## Fetching the corpus

The downloader is idempotent; re-running it fetches or repairs only what is
missing.

```bash
python resume/_download.py
```

One dataset (`popresume-text`) is manually gated and may remain incomplete; the
rest download without authentication. See `resume/README.md` for per-dataset
provenance and sizes.

## Running the opt-in suite

The tests carry the `corpus` marker. With the corpus present:

POSIX shell:

```bash
RESUME_REVIEW_REAL_CORPUS=1 python -m pytest -m corpus
```

Windows PowerShell:

```powershell
$env:RESUME_REVIEW_REAL_CORPUS = "1"; .venv/Scripts/python.exe -m pytest -m corpus
```

The directory directly:

```bash
python -m pytest tests/corpus          # skips cleanly when the flag is unset
```

With the flag unset this reports the tests as skipped, never as failures and
never as a silent zero-collection.

## What is bounded and sampled

The suite deliberately does not walk or parse the whole corpus. Bounds live in
`tests/corpus/test_corpus_smoke.py` as named constants, each reported when it
bites:

| Bound | Value | What it caps |
| --- | --- | --- |
| `DISCOVERY_SAMPLE` | 200 | candidate files from the incremental discovery pass |
| `SNIFF_SAMPLE` | 24 | files type-sniffed |
| `EXTRACT_CANDIDATE_POOL` / `EXTRACT_SAMPLE` | 40 / 6 | candidates considered / documents actually extracted |
| `EXTRACT_MAX_BYTES` | 2 MiB | largest file considered for extraction |
| `TIME_BUDGET_SECONDS` | 90 | wall clock for the extraction loop |

The extraction sample is the smallest candidates, so a run stays fast. When a
bound bites, the suite prints what it skipped and can skip a check rather than
fail it.

## Licence and provenance caveat

The datasets are third-party public HuggingFace datasets, each with its own
licence and terms (for example, `resume-real-pdfs-2480` is CC0 and
`resume-mixed-real-synthetic` is MIT; some are gated). Downloading and using
them is the operator's responsibility. Because the repository does not
redistribute them, their licences attach to the local copies, not to this
repository's code.

## What must not be committed

* The corpus itself (`resume/`), any copy of it, or any subset of it.
* Any real resume text, excerpt, or quote derived from it.
* Any fixture, golden file, snapshot, report, or profile generated from real
  corpus text.

Only synthetic data belongs in committed tests, fixtures, and examples.
