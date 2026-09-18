# Broad random baseline utilities

`build_random_broad.py ARTIFACT_ROOT` builds a source-balanced 120-item text suite from externally staged, pinned source data. Source datasets, original PACE vendor code, private graders, keys, response artifacts and ledger remain outside this repository. Inspect the source adapters and eligibility manifest before use; these are adapted proxy protocols, not native leaderboard reproductions.

The builder expects the existing source files under `data/`, the pinned PACE checkout under `pace-source/`, and the DebugBench oracle/bug audit under `debug-audit.json`. It writes the full eligible `broad/pool.json`, sampled `broad/suite.json`, separate LiveCodeBench test packs and `broad/eligibility.json`. Do not rebuild or change a suite after measurement starts.

Run `python -m seb.random_run ARTIFACT_ROOT/broad` with an external `run-config.json`. Set `ledger_path` to the original ledger to preserve all spending and unknown reservations, `vendor_path` to the IFEval package, and `grader_vendor`/`numpy_vendor` to the isolated LIFBench/LiveCodeBench dependencies. The configured rootfs needs Python 3.12 and bubblewrap on the host; tested NumPy vendor is 2.2.6 for CPython 3.12. Set `pilot_each_source: true` to validate one real response per source/model before continuing that model. Completed wrong/empty answers are not retried.

InFoBench uses one fixed external judge and one batch of decomposed criteria per candidate response. Auxiliary requests use the same ledger and OpenAI provider scope, with an additional $3 auxiliary cap. This differs from PACE's candidate self-judging adapter. Infra/judge errors remain missing, not zero.

`reuse_roots` permits reuse only when the entire item, model configuration, and output cap match and the prior result is completed. No new provider charge is created for replay; original usage and receipt paths remain attached. All reuse roots must share the ledger and grader protocol; do not point at unrelated experiments. Cross-provider scores and default versus explicit reasoning configurations must be disclosed by the experiment report.
