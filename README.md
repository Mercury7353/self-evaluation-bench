# Self-Evaluation Bench

Evaluate an **agent researcher's ability to build an inexpensive benchmark**. A YAML experiment selects the researcher, candidate models and model families, visible/hidden target benchmarks, resource access, budgets, and execution policy.

The pipeline runs **researcher → development feedback → frozen submission → independent model panel → target correlations + overall**. This repository contains reusable infrastructure and synthetic fixtures. Supply your own task resources, target scores, provider credentials, and experiment settings.

## Install

Use Linux with working unprivileged user/network namespaces, Bubblewrap (`bwrap`), and Python 3.12. Paid researcher runs require a **native Linux Claude Code executable** on `PATH`; the harness runs inside an isolated root filesystem. Optional Harbor agent tasks also require Apptainer and the `agent` extra.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev,agent]'
# Apptainer extracts a Python rootfs; alternatively supply an existing compatible rootfs.
export SEB_ROOTFS="$(seb init-rootfs --image python:3.12-slim --cache ./images)"
export SEB_SCIENCE_PACKAGES="$(python -c 'import sysconfig; print(sysconfig.get_path("purelib"))')"
seb doctor --rootfs "$SEB_ROOTFS"
```

The rootfs must provide `/usr/local/bin/python` with the same Python ABI as the scientific packages. Dependencies are mounted read-only. Task execution supports a subset of single-container Dockerfiles; unsupported instructions fail explicitly. This is a Linux namespace backend, not a Docker/Compose replacement. Keep rootfs and environment versions fixed across comparisons.

## First run: no API cost

```bash
seb experiment validate --config examples/mock.yaml
seb experiment run --config examples/mock.yaml --mock --output runs/mock-001
```

This uses a deterministic fixture writer and a local fake provider. It exercises real sandboxed suite execution, metering, snapshots, family CV, and aggregation. Synthetic scores and the toy two-item suite are **not evidence of researcher quality**. Each output directory must be new; the CLI refuses to overwrite or reset an existing run.

## Configure a researcher experiment

Start from [examples/experiment.yaml](examples/experiment.yaml). Replace every provider/model/price placeholder and both synthetic reference files before a real experiment. Set credentials in the environment named by each provider's `key_env`; never put API keys in YAML or Git.

| YAML field | Controls |
| --- | --- |
| `researchers` | Researcher IDs, model/provider, `claude_code` harness, optional effort and task-instruction file |
| `models` | Candidate IDs, provider/model, family, development/holdout split, prices and optional effort |
| `benchmarks.whitebox` | Visible target IDs, reference JSON, scale and resource paths |
| `benchmarks.blackbox` | Hidden target IDs and reference JSON; never copied into the researcher workspace |
| `budgets` | Separate researcher, development and acceptance wallets; per-suite and per-item limits |
| `design` | Maximum rounds, per-checkpoint/total development seconds, minimum final item count |
| `evaluation` | Acceptance deadline, optional model preflight, concurrency and candidate output/retry policy |
| `overall` | Aggregate definition, source and completeness rules |

Reference JSON maps candidate IDs to finite numeric target scores, e.g. `{"dev-a": 0.42, "dev-b": 0.61}`. Target scale is the reference's native range (e.g. `1` or `100`). Missing references are frozen exclusions, reported explicitly. Use a consistent scoring direction where higher is better. Actual datasets and labels belong outside Git. Each supplied white-box resource is copied to the researcher's isolated workspace; curate it so it contains only information you intend to expose.

At least three development models and two development families are required. Holdout families must be disjoint from development families. More models/targets are needed for reliable scientific comparisons; a valid run is not a statistical significance claim.

```bash
# Export the API key through your usual secret-management mechanism first.
seb experiment validate --config /path/to/experiment.yaml
seb experiment run --config /path/to/experiment.yaml \
  --researcher researcher-a --output runs/researcher-a-001
seb budget runs/researcher-a-001
```

`validate` resolves local inputs without making model calls. `run` makes paid calls unless `--mock` is explicitly paired with the mock harness. Multiple researcher entries use the same experiment specification; run one selected researcher per new output directory. Budgets are **per invocation**, not a shared cap across concurrent experiments. Use an external scheduler or shell loop for multiple independent runs.

Providers must implement the Anthropic Messages protocol at `upstream + /anthropic/v1/messages`, including streaming/tool use for Claude Code. The supplied routing supports distinct providers for different models. Plain OpenAI-only endpoints require an adapter; changing the URL alone does not convert the protocol.

## Researcher interface and isolation

The researcher receives the neutral task/contract, SDK, development model IDs, visible development labels, and explicitly supplied resources. It can synthesize tasks, select existing tasks, or combine methods. It can run small pilots, inspect original job evidence, and request white-box feedback. It cannot access the host run directory, provider keys, hidden targets, holdout models, or external network. This is scoped resource isolation for cooperative research agents, not a hardened service for arbitrary hostile tenants.

The submission contains `run.py`, `evaluation.json`, `README.md`, and any required assets. A suite runs once for each candidate under a scoped context, produces scored items with call/agent evidence, and declares its fixed aggregation or adaptive selection rule. See [the executable toy example](examples/arithmetic/) and `seb/research_sdk.py`. An optional `predictor.py` implements `fit(training_rows, target_metadata)` and `predict(fitted, observations)`; it receives anonymous numeric item measurements rather than candidate identities. Optional predictor assets go in `predictor_assets/`.

`evaluation.preflight: true` checks all configured candidate transports through the same metered pipeline before researcher launch. These calls use the development wallet and deadline. Wrong/empty delivered answers do not fail transport readiness. Optional `budgets.judge_usd` reserves a separate, currently inaccessible judge wallet.

Every round shares the original development wallet and total development deadline; `design.checkpoint_seconds` bounds each researcher turn (default at most two hours). The controller snapshots the submission, evaluates development models, and provides visible-target feedback for the next configured round. Exact unchanged snapshots reuse their earliest development job IDs, including wrong and empty answers. Each round must produce a valid submission. The predeclared selection policy is the **last valid round**; acceptance results never select a checkpoint. If the researcher consumes the full development time, controller feedback can be incomplete; the frozen valid submission can still enter acceptance.

Final acceptance measures all configured candidates afresh under its separate wallet. Submission files, reference/config hashes, and job IDs are persisted. No automatic researcher restart, budget reset, or answer-dependent retry occurs. Infrastructure retries are bounded by the frozen execution policy. Empty/refused/invalid delivered answers score zero; missing execution is unscored and marks coverage incomplete.

## Overall metric

For each eligible black-box target, compute Spearman correlation between its reference scores and frozen-benchmark predictions. **Overall is the unweighted mean across black-box targets**, on `[-1,1]`.

- Default `source: family_cv`: freeze predictor code, then fit it separately while holding out each entire candidate family. A submitted predictor is used if present; otherwise a fixed mean-feature ridge baseline is identified in the result.
- `source: raw_mean`: correlate the suite's aggregate score directly, without fitting a target mapping. Do not compare different overall sources as the same metric.
- Target reference coverage is fixed before running. Targets with fewer than `minimum_models` references are listed as exclusions. Missing predictions for an eligible target make the official overall undefined, instead of silently removing that target/model.
- By default, a constant prediction on a nonconstant reference contributes zero rank information to the aggregate, while its per-target Spearman remains undefined. Set `constant_prediction: undefined` to make the overall undefined too. Constant references remain undefined.

Also retain Pearson/Spearman per target, raw score correlations, family CV, and a separate predictor trained **only on development models** and evaluated on new model families. Black-box family CV uses training-fold labels after the design is frozen; it measures transfer of a frozen measurement/prediction procedure, **not zero-shot prediction of unseen target labels**. The new-family diagnostic uses a stricter train/development split. Reusing visible feedback across rounds is adaptive development, not hidden validation.

`result.json` reports the aggregate and `eligible` separately. Eligibility requires a complete candidate panel, defined overall, settled accounting, and budgets respected. Do not rank incomplete/ineligible runs using `diagnostic_available_target_mean`. Small panels, heterogeneous target protocols and correlated targets limit interpretation; keep them visible when comparing researchers.

## Artifacts and accounting

Each private run directory contains:

- `state.json`, `result.json`, `provenance.json`, resolved configuration;
- researcher traces, immutable round checkpoints and `freeze.json`;
- per-model job/result files, item evidence and original usage;
- `whitebox/` and `blackbox/` scores with model-level predictions;
- `gateway/ledger.sqlite`: original reservations, known charges and unknown usage.

Prices are USD per million tokens. The ledger conservatively counts cached input at full input price; an optional `price.cache_read_multiplier` produces a separate cache-adjusted estimate. If that discount is unknown and cached input exists, the adjusted estimate is null. These are usage-based estimates, not provider invoices. Researcher fees remain separate from candidate testing fees. Unknown/aborted calls retain their reservations. A provider can report usage above a pre-request bound; overshoot closes the wallet and is reported, never presented as a guaranteed billing cap.

Artifacts, traces, credentials, datasets and real results are ignored by Git. Keep the run directory private. Publish only operator-reviewed outputs through your chosen reporting workflow.

## Tests

```bash
pytest -q
# Include actual namespaces and the complete local mock pipeline:
SEB_TEST_ROOT="$SEB_ROOTFS" SEB_TEST_SCIENCE="$SEB_SCIENCE_PACKAGES" pytest -q
```

Runtime tests use only synthetic fixtures and local fake providers. A collaborator must separately validate their provider's streaming/tool-use compatibility and verified prices before scaling paid experiments.
