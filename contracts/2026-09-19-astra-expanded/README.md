# Astra expanded executable contract

This is operator configuration, never researcher input. `config.yaml` fixes four development and ten sealed candidates (one endpoint pending), and eight visible/eight sealed targets. It retains a three-hour researcher window, 90-minute continuation threshold, at least 100 distinct questions, and direct measured-score Pearson. The candidate output budget is chosen in submitted code, not fixed to Sol's 2500.

Set `SEB_ASTRA_INPUTS` to a private input bundle containing `references/<target>.json` and `materials/<visible-target>/`. Target references map candidate IDs to numeric scores. Curated materials must omit sealed targets, held-out labels, credentials and aggregate leaderboard tables. Configuration validation checks label coverage, paths and frozen visibility membership. Local material availability is documented in the MyContext report; the four added visible sources currently have descriptions/labels rather than preinstalled task archives.

```bash
export SEB_ASTRA_INPUTS=/absolute/path/to/private/inputs
seb experiment validate --config contracts/2026-09-19-astra-expanded/config.yaml
# Supply SEB_OPENAI_KEY and SEB_DEEPINFRA_KEY through private environment handling.
# A new output directory is required; do not reuse an existing ledger.
seb experiment run --config contracts/2026-09-19-astra-expanded/config.yaml \
  --researcher r-gpt-6-astra --output /absolute/path/to/new-run
```

The enabled routes use OpenAI directly and DeepInfra. The VectorEngine provider definition is retained as an unused operator option, not an automatic fallback. Exact backend/tool readiness must pass the run's metered preflight before research. New-provider availability is not inferred from this static configuration.

All three domains share the existing $100 researcher / $100 development / $120 acceptance caps; the whole frozen program is capped at $30 per candidate. These are caps, not projected costs or a guarantee that the full panel fits. Pending endpoints and sparse references remain explicit missing coverage. Original experiment wallets are never reused or reset.

Final acceptance measures all available development and sealed candidates. Sealed labels join their measured outputs only in host-side scoring after researcher access is revoked. Visible, sealed and combined raw-score reports are operator-only; pending labels/measurements must not be represented as complete correlations.
