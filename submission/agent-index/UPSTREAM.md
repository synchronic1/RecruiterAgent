# Upstream reporting client

Source: https://github.com/plow-pbc/agent-index-client

Run `python3 ../prepare_index.py --fetch-client` from this directory or run the
script from the submission directory. It resolves the upstream commit once and
retrieves source and LICENSE by that exact commit, plus NOTICE if upstream has
one. A manifest records the commit and content hashes. It requires the OpenClaw
collector to be present. The upstream client is
Apache licensed, separately from RecruiterAgent's MIT license.

On 2026-09-29, upstream revision
`fbfe8b635c1f20ce1f0152497abb419623f53329` was cloned into a separate Linux
submission-preparation directory. Its `--self-check` passed under Python 3.12.13;
that check covers merge behavior, flag parsing, and the Hermes delta collector.
It does not demonstrate collection from the actual OpenClaw instance. No live
usage report was sent. Inspect a real dry-run before enabling reporting.
