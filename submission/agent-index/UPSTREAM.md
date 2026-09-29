# Upstream reporting client

Source: https://github.com/plow-pbc/agent-index-client

Run `python3 ../prepare_index.py --fetch-client` from this directory or run the
script from the submission directory. It resolves the upstream commit once and
retrieves source and LICENSE by that exact commit, plus NOTICE if upstream has
one. A manifest records the commit and content hashes. It requires the OpenClaw
collector to be present. The upstream client is
Apache licensed, separately from RecruiterAgent's MIT license.

The client has not been downloaded in this checkout because the shell's network
proxy was unavailable. The upstream collector has not been tested against your
OpenClaw state. Run its self-check and inspect a real dry-run before reporting.
