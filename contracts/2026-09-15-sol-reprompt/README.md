# Sol three-hour reprompt pilot

This is a new, separately metered Sol run authorized on 2026-09-15. The [base interface](../2026-09-15-mixed-v3/CONTRACT.txt), materials, candidate split and 100-item minimum remain applicable. See [config.yaml](config.yaml) and [instructions.txt](instructions.txt) for the exact new launch specification; paths refer to the operator host and require local provisioning elsewhere. No keys or datasets are included.

- Research window: 10,800 seconds, including candidate preflight.
- Normal return with at least 5,400 seconds remaining: neutral reprompt in the same SDK thread. Otherwise permit early completion.
- At most two terminal transport recoveries, preserving the original deadline, artifacts and wallets. No retry of completed candidate answers. Failures remain separately recorded.
- Researcher $100; development $100; acceptance $120 (four candidates, at most $30 per suite). Responses researcher usage includes configured cache discounts. Reservations are not invoices.
- Qwen reserves its native maximum output allowance because earlier usage exceeded the requested limit. Actual request limits and usage-based billing remain unchanged.
- Kimi, like GLM5.2, may deliver explicit input/output usage when totals disagree; the raw ledger charge remains unknown and its reservation is retained.
- Any closed research/development accounting wallet is a fault, not a normal successful early stop.

Full immutable attempt traces and the continuation decision journal live outside Git. Parent trace files project the final attempt; earlier failure events are never deleted. Research reports belong in MyContext.

User update after launch: subsequent acceptance panels should set `evaluation.acceptance_model_concurrency: 4`, retaining development concurrency 2. The archived config here is the exact original running configuration and is not silently edited. This pilot has eight candidates (four development, four holdout); it is not the earlier expanded model panel.
