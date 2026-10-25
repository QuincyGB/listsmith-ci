# listsmith-ci

**Your x402 listing has traps in it, and nobody tells you — the calls just never come.** An output sample a few hundred bytes too large and Settled's extractor silently drops it, so your receipts read `nothing_to_compare` instead of `as_advertised`. A description over 500 characters and the CDP facilitator rejects payments. A hollow `{"type", "example"}` output with no typed schema — the norm: 87.8% of catalog listings ship one, per ListSmith's Oct 2026 catalog read — and buyers' agents can't code against your route. A manifest that disagrees with your live 402 challenge, or a discovery surface that drifted since your last deploy, and every surface a buyer reads tells a different story. **listsmith-ci is a GitHub Action (and CLI) that runs these checks in your deploy pipeline and fails the build** — in seconds, for free, before an inspector or buyer ever sees the trap.

Built by [Verdict Row](https://github.com/QuincyGB) — the fleet behind [ListSmith](https://github.com/QuincyGB/list-smith) (listing grades & repairs), [RouteVerdict](https://github.com/QuincyGB/route-verdict), [Token Verdict](https://github.com/QuincyGB/token-verdict) and StallRow. Every rule below is a trap one of our own routes fell into first.

## Install (one snippet)

```yaml
# .github/workflows/listing-check.yml
name: listing-check
on: [push, pull_request]
jobs:
  listsmith:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4   # so the drift baseline persists
      - uses: QuincyGB/listsmith-ci@v1
        with:
          endpoints: |
            https://your-service.example.com/v1/your-route
```

The first run records a drift baseline (`.listsmith-ci-baseline.json`) and passes with a notice — **commit that file**. Later runs fail the build if your live discovery surface changes without the baseline being updated (re-run with `update-baseline: 'true'` when a change is intended).

Or run it anywhere, no GitHub required:

```bash
pip install "git+https://github.com/QuincyGB/listsmith-ci"
listsmith-ci check https://your-service.example.com/v1/your-route
```

## The checks

**Tier 1 — free, local, seconds.** The Action fetches your live 402 challenge and `/.well-known/x402` manifest itself. No payment, no wallet.

| Rule | What it catches |
|---|---|
| `sample-size` | Serialized output example over **1,453 bytes** fails. Measured on our fleet 2026-10-25: Settled's extractor kept a 1,453-byte sample and dropped a 1,721-byte one — receipts for the dropped route read `nothing_to_compare`. The true cap sits somewhere in between; we enforce the largest size ever observed to survive. A measured bracket, not gospel. |
| `description-length` | Description over **500 chars** (challenge or manifest) — CDP rejects payments over it. |
| `typed-schema` | Output must carry a typed schema (properties / required / enum / items), not the hollow `{"type", "example"}` that 87.8% of listings ship. |
| `example-matches-schema` | Your published output example must validate against your own published output schema (local `jsonschema` check). |
| `manifest-agreement` | The manifest entry must mirror the live 402 challenge: amount, asset, payTo, network. |
| `drift` | Discovery surface is hashed and compared to the baseline in your repo; changes fail with a readable field-by-field diff. Baselines update only via the explicit `update-baseline` input — never silently. |

**Tier 2 — paid deep checks, opt-in, OFF by default.** With your own wallet key (a GitHub secret; used only to pay, never logged), the Action buys the two inspections buyers trust: ListSmith `/v1/grade` ($0.10 per endpoint; score must meet `grade-floor`, default 80) and RouteVerdict `/v1/check` ($0.05 per endpoint; verdict must be `TRUSTED`).

## Inputs

| Input | Default | Purpose |
|---|---|---|
| `endpoints` | *(required)* | Endpoint URLs, one per line (or space/comma separated) |
| `baseline-path` | `.listsmith-ci-baseline.json` | Where the drift baseline lives in your repo |
| `update-baseline` | `false` | `'true'` re-records the baseline from current live surfaces |
| `paid-grade` | `false` | Tier 2: pay ListSmith `/v1/grade` ($0.10/endpoint) |
| `paid-route-verdict` | `false` | Tier 2: pay RouteVerdict `/v1/check` ($0.05/endpoint) |
| `grade-floor` | `80` | Minimum ListSmith grade when `paid-grade` is on |
| `wallet-key` | `""` | Your wallet private key, from a GitHub secret — Tier 2 only |
| `report-path` | `listsmith-ci-report.md` | Where the markdown report is written |

**Outputs:** `result` (`pass`/`fail`) and `report-path`. The report is also appended to the job's step summary, and the markdown file is PR-comment friendly.

Tier 2 via the CLI: set `LISTSMITH_CI_PRIVATE_KEY` (or `LISTSMITH_CI_KEY_FILE`) in the environment and pass `--paid-grade` / `--paid-route-verdict`. With no signer configured, Tier 2 refuses rather than pretending — a paid check that didn't settle never passes.

## What it can't check

Honestly: this tool reads **public discovery surfaces only** — the unpaid 402 challenge and the manifest. It cannot see your paid payloads, so it cannot verify that a delivered response actually matches your sample (that's an inspector's test purchase — Tier 2's RouteVerdict check gets you partway there), that your route stays up between runs, or that your settlement path works end-to-end. The sample-size bracket is one ecosystem participant's measured behaviour on one date, not a specification. And if your service has no `/.well-known/x402` manifest, the manifest and drift rules will say so — loudly, which is the point, but the fix is on your side of the fence.

## Dogfooding

This repo's `dogfood` workflow (manual dispatch only) runs Tier 1 against all 19 paid routes of the Verdict Row fleet — the shovel checks its own makers first. As of the v1 run (2026-10-25): 17 of 19 routes pass every rule; Token Verdict's `/v1/pre-trade` (2,519-byte sample) and `/v1/batch-verdict` (1,981-byte sample) fail `sample-size` — real traps, reported in the open, fixed by trimming the samples like `/v1/verdict` was.

## License

MIT
