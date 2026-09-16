# Direct measured-score contract

Supersedes target prediction for future runs. This is a contract template, not a launched experiment. Historical contracts and active frozen programs remain unchanged.

Set `domain_protocol.score_mode: raw_domain`. The controller, researcher prompt, checkpoint validator, development feedback and final scorer then use direct frozen domain scores. No predictor is required, fitted or executed. The objective is target Pearson from measured scores, averaged within domain and then equally across the three domains. Spearman and discrimination are diagnostics. The original budgets and reprompt settings are retained in config.yaml; local paths require provisioning.

Final acceptance covers every configured available candidate, including development candidates. The gateway reuses completed grades and saved responses only from the same immutable program and candidate. It preserves historical evidence and billing, blocks fresh calls for completed items, and includes unknown reservations when limiting further suite expenditure. Whole-program retries collect missing items without restarting research. Permanent provider/budget failures and a no-progress circuit breaker are reported as incomplete, never as completion.

The manifest now declares independent `questions` with actual prompts; each scoring item references a `question_id`. Several rubric items on one scenario count as one question. At least 100 distinct scored questions are required for a final snapshot. Exact duplicate prompts are rejected; semantic independence still needs audit.

Operational settings for the new run retain the original providers, model IDs, efforts, token prices and $100/$100/$120 wallets. Acceptance concurrency is four models (development two), with two requests per model. Transient retries use a 3,600-second transmission timeout and 15–180-second backoff, plus provider 429 cooldown. Kimi/Qwen reserve the native output bound without changing requested generation limits. Reservations remain distinct from actual charges.

Research remains 3 hours. Acceptance has a separate 24-hour execution window; expiry produces incomplete status and preserves all completed items for operator continuation, never a false completion. No budget is replenished.

A failed transport-only preflight may be continued explicitly with `run(..., resume_partial_preflight=True)` when every available candidate already has at least one hash-verified completed HTTP 200 response, all preflight jobs and requests have stopped, and no researcher has started. The original configuration, clock, ledger, unknown reservations and archived responses are retained. This does not mark the incomplete preflight suite complete or relax final acceptance: the final frozen benchmark still needs all declared items on every candidate. Only one preflight continuation is allowed.
