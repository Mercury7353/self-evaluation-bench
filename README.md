# Self-Evaluation Bench

Evaluate an **agent researcher's ability to build an inexpensive benchmark**. A YAML experiment selects the researcher, candidate models and model families, visible/hidden target benchmarks, resource access, budgets, and execution policy.

The pipeline runs **researcher → development feedback → frozen submission → independent model panel → target correlations + overall**. This repository contains reusable infrastructure and synthetic fixtures. Supply your own task resources, target scores, provider credentials, and experiment settings.

## Install

Use Linux with working unprivileged user/network namespaces, Bubblewrap (`bwrap`), and Python 3.12. The `claude_code` researcher harness requires a **native Linux Claude Code executable** on `PATH`. The optional `codex` harness requires Node and the pinned SDK installation below. Both execute tools inside an isolated root filesystem. Optional Harbor agent tasks also require Apptainer and the `agent` extra.

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
| `researchers` | Researcher IDs, model/provider, `claude_code` or `codex` harness, effort and optional task-instruction file |
| `models` | Candidate IDs, provider/model, family, development/holdout split, prices and optional effort |
| `benchmarks.whitebox` | Visible target IDs, reference JSON, scale and resource paths |
| `benchmarks.blackbox` | Hidden target IDs and reference JSON; never copied into the researcher workspace |
| `budgets` | Separate researcher, development and acceptance wallets; per-suite and per-item limits |
| `design` | Maximum rounds, per-checkpoint/total development seconds, minimum final item count |
| `evaluation` | Acceptance deadline, optional model preflight, concurrency and candidate output/retry policy |
| `overall` | Aggregate definition, source and completeness rules |

Reference JSON maps candidate IDs to finite numeric target scores, e.g. `{"dev-a": 0.42, "dev-b": 0.61}`. Target scale is the reference's native range (e.g. `1` or `100`). Missing references are frozen exclusions, reported explicitly. Use a consistent scoring direction where higher is better. Actual datasets and labels belong outside Git. Each supplied white-box resource is copied to the researcher's isolated workspace; curate it so it contains only information you intend to expose.

At least three development models and two development families are required. The legacy protocol requires disjoint holdout families. Domain protocol v1 also permits different configurations from a development family. More models/targets are needed for reliable scientific comparisons; a valid run is not a statistical significance claim.

```bash
# Export the API key through your usual secret-management mechanism first.
seb experiment validate --config /path/to/experiment.yaml
seb experiment run --config /path/to/experiment.yaml \
  --researcher researcher-a --output runs/researcher-a-001
seb budget runs/researcher-a-001
```

`validate` resolves local inputs without making model calls. `run` makes paid calls unless `--mock` is explicitly paired with the mock harness. Multiple researcher entries use the same experiment specification; run one selected researcher per new output directory. Budgets are **per invocation**, not a shared cap across concurrent experiments. Use an external scheduler or shell loop for multiple independent runs.

Providers default to the Anthropic Messages protocol at `upstream + /anthropic/v1/messages`, including streaming/tool use for Claude Code. For OpenAI candidate models, set provider `wire_api: openai_responses` and `upstream: https://api.openai.com/v1`; specify each model's frozen `effort`, `native_limits.max_output_tokens`, and `native_limits.max_context_tokens`. The candidate adapter handles text and schema-based function tools through Responses, preserving opaque reasoning and assistant phases across tool turns. It retains original requests, native responses and usage for accounting. Unsupported modalities, context edits and effort changes fail explicitly. OpenAI researchers use the native Codex harness described below.

Each request uses the configured model route and its price. Provider `models` lists and `fallbacks` are rejected before calls or reservations, including on token-counting routes; frozen request parameters cannot replace `model` either. A published model system with fallback requires an explicit system/accounting integration before its scores can be used as a matching reference. Transport success or an echoed model name alone does not verify a deprecated provider alias or its underlying checkpoint.

## Researcher interface and isolation

The researcher receives the neutral task/contract, SDK, development model IDs, visible development labels, and explicitly supplied resources. It can synthesize tasks, select existing tasks, or combine methods. It can run small pilots, inspect original job evidence, and request white-box feedback. It cannot access the host run directory, provider keys, hidden targets, holdout models, or external network. This is scoped resource isolation for cooperative research agents, not a hardened service for arbitrary hostile tenants.

The submission contains `run.py`, `evaluation.json`, `README.md`, and any required assets. A suite runs once for each candidate under a scoped context, produces scored items with call/agent evidence, and declares its fixed aggregation or adaptive selection rule. See [the executable toy example](examples/arithmetic/) and `seb/research_sdk.py`. An optional `predictor.py` implements `fit(training_rows, target_metadata)` and `predict(fitted, observations)`; it receives anonymous numeric item measurements rather than candidate identities. Optional predictor assets go in `predictor_assets/`.

`evaluation.preflight: true` checks all configured candidate transports through the same metered pipeline before researcher launch. These calls use the development wallet and deadline. Wrong/empty delivered answers do not fail transport readiness. Optional `budgets.judge_usd` reserves a separate, currently inaccessible judge wallet.

Every round shares the original development wallet and total development deadline; `design.checkpoint_seconds` bounds each researcher turn (default at most two hours). The controller snapshots the submission, evaluates development models, and provides visible-target feedback for the next configured round. Exact unchanged snapshots reuse their earliest development job IDs, including wrong and empty answers. The predeclared selection policy is the **last valid saved submission in the last valid round**; acceptance results never select a checkpoint. If the researcher consumes the full development time, controller feedback can be incomplete; the frozen valid submission can still enter acceptance.

While a researcher is running, the host saves changed, structurally valid submissions about every five seconds and at exit before the original deadline. It checks required files, the manifest, entry-point syntax, and copy hashes; execution and measurement quality are verified later. Partial edits remain in the original workspace for audit. Joint/domain mode requires `predictor.py` for a valid snapshot. The archive does not capture every intermediate edit.

A local researcher-wallet reservation denial stops the researcher and preserves the latest valid snapshot. This can occur before actual charges reach the cap: the next request's conservative reservation may not fit. A host timeout or persisted gateway deadline rejection also permits freezing; neither starts another research run. Deadline expiry prevents new development jobs, while existing exact-snapshot job IDs remain readable. Native upstream policy/quota blocks, accounting overshoot closures, and ordinary harness failures stay failures with saved artifacts. Unknown charges are retained and still prevent accounting eligibility. `research-checkpoints/outcome.json` and `freeze.json` identify the reason and evidence separately from acceptance results.

Final acceptance measures all configured candidates afresh under its separate wallet. Submission files, reference/config hashes, and job IDs are persisted. No automatic researcher restart, budget reset, or answer-dependent retry occurs. Infrastructure retries are bounded by the frozen execution policy. Empty/refused/invalid delivered answers score zero; missing execution is unscored and marks coverage incomplete.

## Overall metric

For each eligible black-box target, compute Spearman correlation between its reference scores and frozen-benchmark predictions. **Overall is the unweighted mean across black-box targets**, on `[-1,1]`.

- Default `source: family_cv`: freeze predictor code, then fit it separately while holding out each entire candidate family. A submitted predictor is used if present; otherwise a fixed mean-feature ridge baseline is identified in the result.
- `source: raw_mean`: correlate the suite's aggregate score directly, without fitting a target mapping. Do not compare different overall sources as the same metric.
- Target reference coverage is fixed before running. Targets with fewer than `minimum_models` references are listed as exclusions. Missing predictions for an eligible target make the official overall undefined, instead of silently removing that target/model.
- By default, a constant prediction on a nonconstant reference contributes zero rank information to the aggregate, while its per-target Spearman remains undefined. Set `constant_prediction: undefined` to make the overall undefined too. Constant references remain undefined.

Also retain Pearson/Spearman per target, raw score correlations, family CV, and a separate predictor trained **only on development models** and evaluated on new model families. Black-box family CV uses training-fold labels after the design is frozen; it measures transfer of a frozen measurement/prediction procedure, **not zero-shot prediction of unseen target labels**. The new-family diagnostic uses a stricter train/development split. Reusing visible feedback across rounds is adaptive development, not hidden validation.

`result.json` reports the aggregate and `eligible` separately. Eligibility requires a complete candidate panel, defined overall, settled accounting, and budgets respected. Do not rank incomplete/ineligible runs using `diagnostic_available_target_mean`. Small panels, heterogeneous target protocols and correlated targets limit interpretation; keep them visible when comparing researchers.

## Domain acceptance protocol

Set `domain_protocol: {version: 1, minimum_models: 8, minimum_families: 4}`
to evaluate two visible targets and two sealed targets separately. Use
`design.rounds: 1`; researcher-controlled trials and revisions remain available
throughout that continuous run. Domain mode gives the researcher the full
`design.seconds` window and defaults its item minimum to one (an interface floor,
not a quality claim); it rejects a shorter controller checkpoint window.

In this mode, final acceptance invokes only `split: holdout` candidates. Each
visible target must have development references, and every target must meet the
configured held-out reference coverage before the runner will spend money.
`predictor.py` is required; its frozen fit/predict procedure receives only final
submission development observations and visible development labels. The code
can load researcher-fitted parameters from `predictor_assets/`. The two sealed
targets both use the suite's frozen aggregate score, with no target-specific fit,
sign change, or post-hoc feature selection. `result.json` reports
`visible_utility` and `sealed_utility` separately. Default legacy family CV and
its historical scores are unchanged.

The operator must verify target/candidate versions and curate resource files;
schema validation does not establish scientific reference comparability.
Native Codex researcher transport and OpenAI text/tool candidate adaptation are
available. Optional equivalent-cost response reuse is configured below. Curated
online research still requires an operator-prepared resource and retrieval boundary.

`seb.campaign.Registry` registers an immutable matrix of researcher/domain/budget
allocations. An atomic claim allows only one supervisor to claim a given episode;
it never releases a claim merely because a process timestamp is old. Existing
handles and artifacts must be inspected on resumption. This registry reserves
allocation envelopes; actual API calls are metered by each run's ledger.

A campaign manifest can set `research_unit: joint` to allocate one episode per
researcher across its entire `domains` list. Its `candidate_suite_usd` is the
whole-program cap. Configure the execution YAML with joint protocol v2 below. Use
`Registry.suspend_launches(reason)` to retire an earlier campaign without
rewriting its manifest, existing claims, supervisor handles, or API ledgers.

## Registered fixed baselines

Use the same joint execution YAML and a prepared, immutable baseline submission:

```bash
seb baseline /path/to/fixed-submission \
  --config /path/to/joint-execution.yaml \
  --campaign /path/to/registered-campaign \
  --episode baseline--fixed--joint--b100 \
  --handle systemd:baseline-fixed.service
```

The submission must supply `run.py`, `evaluation.json`, `README.md`, and
`predictor.py`. This command measures an already prepared random, stratified,
or fixed program; it does not generate or optimize the baseline. It validates
the registered candidate panel, targets, shared budgets, and time allocation,
then atomically claims that one episode. A failed launch retains the original
claim and handle. The output directory comes from the registry.

Baseline execution freezes the program before development measurement and uses
the same frozen program on held-out candidates. The visible predictor is fitted
only on development measurements and visible labels. No researcher credential,
wallet, model route, or agent process is created. Disable `evaluation.preflight`;
transport calibration is separate and must not be repeated by this command.
`--mock` runs candidate transport against the local fixture provider and makes
no quality claim.

When the campaign's `preflight_allocation.episode_id` matches this baseline,
the command requires `reuse_existing_ledger` to name an existing SQLite ledger
with exactly the original development wallet and unchanged cap. It binds that
wallet to one output and links `gateway/ledger.sqlite` to the original file.
All connections resolve the link to use the same SQLite journal. Existing calls,
scopes, charges, and unknown reservations remain in place; another output cannot
claim the same inherited wallet. Missing, closed, differently capped, or mixed
wallet ledgers are rejected rather than replaced.

`ledger-reuse.json` records the inherited call IDs, original charges/reservations,
and a checksum. Accounting includes those costs in the shared limit; inherited
usage is not repriced using the new configuration. Its cache-adjusted estimate
remains unknown when the original price basis has not been supplied. This is
wallet reuse; optional cross-run response caching has a separate configuration below.

## Joint domain acceptance

Set `domain_protocol: {version: 2, domains: [coding, co-work, reasoning]}` and
assign a `domain` to every benchmark. Each domain requires two whitebox and two
blackbox targets. The existing reference-coverage checks run before any paid
request. Use `design.rounds: 1`; the researcher sees all six visible targets in
one workspace. Budget fields apply once to the whole run, and `suite_usd` caps
one complete candidate measurement across all domains.

`evaluation.json` adds a `domain_aggregations` mapping. Each weighted-mean entry
declares `weights: {item_id: positive_weight}`. Different domains may reuse the
same item without repeating its API request. The platform calculates the scores
from validated item results and rejects a conflicting reported mean. A custom
entry declares `kind: custom`, its input `items`, and a frozen textual `method`;
`run.py` returns its value in `domain_scores: {domain_id: score_in_0_to_1}`.
Unresolved selected inputs make the corresponding domain incomplete; a missing
custom score remains missing. Development pilots may cover fewer domains; the
final manifest must declare all three.

The required `predictor.py` fits once on all allowed visible development labels
and shared observations. It outputs six target predictions. Independent
acceptance runs the measurement once per held-out candidate, then compares each
domain score with both of that domain's sealed references without refitting.
Results include per-target and per-domain status, `complete_domains`, and equal
domain-weighted visible/sealed utility. Research, measurement, and accounting
completion remain separate. The operator must still prepare isolated resources,
compatible references and provider configurations before a real campaign.

`python -m seb.provider_probe --config PRIVATE_JSON --output PRIVATE_DIRECTORY`
performs transport checks against an existing scoped allocation, preserving
completed operation IDs and unknown charges. It sends ordinary `READY` requests,
not research tasks or benchmark questions. A provider model listing is weaker
evidence than a successful inference probe, and neither verifies a complete CLI
research session. Never place the private configuration in this repository.

### Pending reference labels

Domain/joint configurations normally require complete reference coverage before
execution. To prepare references alongside research, set
`domain_protocol.allow_pending_references: true`. Every visible and sealed target
must then declare a fixed `holdout_panel: [candidate_ids...]`; a target missing
labels also needs `reference_pending_reason`. Panel membership must already meet
the configured model/family minimum. Visible development labels remain mandatory
(at least three candidates in two families per target). Missing reference files,
invalid scores or unavailable development resources are not waived.

The fixed panel, pending reasons and held-out labels stay in operator artifacts.
Acceptance still measures the configured held-out candidates once. A missing
label within the declared panel makes the column `PENDING_REFERENCE`, even when
the remaining labels exceed the numerical minimum. It cannot silently shrink
to a different subset. Other columns keep their results; incomplete V/S summaries
remain null and `eligible` stays false. `measurement_complete`, `reference_status`
and `failed_target_outputs` separate delivery from reference readiness. A run
waiting only on labels ends with phase `pending_reference`; submission/measurement
failures retain `incomplete`. Neither terminal state authorizes a new researcher.

After the operator verifies newly available labels match the frozen protocol,
score them against saved predictions without model calls or predictor fitting:

```bash
python -m seb.reference_update RUN/domain/reference-scoring-input.json \
  --additions PRIVATE_REFERENCE_ADDITIONS.json --output NEW_REPORT_DIRECTORY
```

The additions JSON has `version: 1`, `scores: {target_id: {heldout_id: score}}`,
and `evidence: {target_id: {heldout_id: {source: "source URI or artifact path",
sha256: "64 lowercase hex characters"}}}`. Only previously absent labels for
existing panel members are accepted. It cannot replace known scores, change
targets/panels, or supply new predictions. Output must be a new directory, and
contains an updated scoring input for further additions. Earlier reports, model
answers and wallets are untouched. Source hashes record provenance; the command
does not independently establish source protocol compatibility or settle bills.

## Grader and simulated-user models

Optional `auxiliary_models` entries configure fixed measurement components. They
use the same `provider`, `model`, `price`, optional `effort` and native Responses
limits as candidate entries, plus `roles: [grader]`, `[simulator]`, or both:

```yaml
auxiliary_models:
  - id: grader-01
    roles: [grader]
    provider: inference
    model: REPLACE_WITH_FROZEN_GRADER_MODEL
    # Placeholder USD per million tokens: verify before running.
    price: {input: 1.0, output: 1.0}
```

Helpers are separate from candidate/researcher panels. Their IDs must be distinct;
a helper backend cannot be a held-out candidate backend. The operator must also
check provider aliases when freezing the configuration. The researcher receives
only helper handles, roles, prices and effort. The provider model and credentials
remain private. Helpers cannot be submitted as suite or candidate-agent targets.

Use helpers inside a budgeted suite/pilot. For a single-response graded item:

```python
def grade(text):
    reply = c.chat(f"{frozen_judge_prompt}\nResponse: {text}",
                   model="grader-01", item_id="item-01")
    # The frozen parser must distinguish an invalid/empty judge response from
    # a candidate's wrong answer. Program defects should raise, not score zero.
    score = parse_judge_response(reply["text"])
    return {"score": score, "answer_status": "answered",
            "evidence": [reply["evidence"]]}

item = c.item("item-01", question, grade)
```

The SDK preserves both evidence IDs. A grader API failure produces an incomplete
item and does not repeat the candidate answer. Other grader exceptions propagate.
For multi-turn simulation, retain every candidate and simulator evidence ID in
the item result. Calls for a declared `budget_group` use that group as `item_id`.
Every completed item must contain evidence from the evaluated candidate. Evidence
from another wallet, execution or item budget is rejected.

Candidate, grader and simulator calls share the original development/evaluation
wallet, item cap and suite cap. There is no extra helper allowance. Unknown usage
remains reserved; model-level accounting includes helper costs and cache estimates.
Helpers inherit the existing 32k–128k output policy and bounded infrastructure
retries. Freeze helper settings before research; this interface alone does not
establish equivalence to any benchmark's official grader or simulator protocol.

## Native Codex researcher

From this checkout, install the pinned official SDK and CLI with
`npm ci --prefix harness/codex --ignore-scripts --no-audit --no-fund`.
Select `harness: codex` and an explicit `effort` on the researcher entry. Set its
provider `upstream` to the Responses API base ending in `/v1`. Supply the exact
model's prices and `native_limits`:

```yaml
harness: codex
effort: xhigh
native_limits:
  max_context_tokens: 1050000
  max_output_tokens: 128000
  timeout_seconds: 600
```

These example limits must match the selected model. The researcher uses the
existing bounded `researcher_usd` wallet and development deadline. Request model
and effort are checked; native prompts, tools, and response bytes are preserved.
Compaction is metered; opaque encrypted history reserves a full model context.
Hosted tools, remote response references, multimodal input, background execution,
and unpriced service tiers are rejected by this text/tool transport.

Only the researcher token may use native routes; candidate tokens cannot invoke
the researcher, and the native token cannot switch to an unvalidated wire route.
The SDK's network namespace has only a scoped bridge to this gateway. Host files,
provider keys, and host networking are not mounted. Retries are disabled on the
native researcher transport; unknown charges retain their reservation. A request
with an explicit operation ID may replay its saved response without another call.
This idempotent replay is separate from cross-run candidate response reuse: a new
operation that hits the shared cache still consumes equivalent test quota.

`gateway/native-wire/` stores original request/response bytes, usage, conservative
charges and separate cache-adjusted estimates. SDK events live in each
`researcher-trace-N/codex.stdout`. Nonfatal SDK item warnings do not turn a completed
turn into a failed run; terminal errors and unfinished zero-exit turns do.

## Artifacts and accounting

Each private run directory contains:

- `state.json`, `result.json`, `provenance.json`, resolved configuration;
- researcher traces, `research-checkpoints/` saved versions and stop evidence, immutable round checkpoints and `freeze.json`;
- per-model job/result files, item evidence and original usage;
- `whitebox/` and `blackbox/` scores with model-level predictions;
- `gateway/ledger.sqlite`: original reservations, known charges and unknown usage.

Optional exact-request response reuse is configured in experiment YAML:

```yaml
response_cache:
  version: 1
  directory: /private/campaign-response-cache
  namespace: frozen-campaign-v1
```

Freeze the namespace, provider/model versions, prices, and policy before the first
run. The directory must be outside all resource mounts and researcher/candidate
workspaces. The public contract exposes reuse semantics, never the host directory,
namespace, other runs' evidence, or a cache listing API. Development and final
acceptance use separate partitions. Researcher calls do not use this cache.

An entry matches the full provider request, upstream, model/effort/limits, wire
protocol, relevant headers, prices, conservative reservation, execution policy,
and sample path. Only a complete, settled first-attempt response is published.
Wrong, empty, refused, and terminal truncated answers are preserved exactly;
partial/error/unknown-cost responses and successes after infrastructure retries
are not published. Corrupt entries fail without issuing a paid fallback request.
Per-request file locks coordinate independent gateway processes through settlement
and publication. A crash before publication may leave no reusable entry; historical
unknown charges remain reserved.

The default is one shared sample for an identical request. Use
`Client.chat(..., sample_id="replicate-2")` or `Client.item(..., sample_id=...)`
for an independent sample, and distinct sample IDs for separate repetitions.
`submit`, `suite`, and `agent` accept `sample_id` too; the server inherits its path
through nested suite/agent tokens, including calls made by an unmodified harness.
The HTTP header is `x-seb-sample-id` (ASCII letters/digits/underscore/hyphen, 1–128
characters). New operation IDs alone do not request independent samples. Reusing
an operation ID with changed input or sample ID is rejected. Replaying the same
completed operation ID remains idempotent, without an additional quota charge.

On a hit the gateway reserves the same conservative amount as a miss, then settles
the original equivalent cost. It assigns a fresh local call ID and scope markers,
saves the original wire response, and reconstructs native Responses tool/reasoning
state in the new token namespace. `cache_replays` records the source evidence in
the private ledger. This avoids a later researcher receiving extra test quota or
borrowing another run's evidence ID. Shared samples are correlated observations,
not independent model answers; cache wait savings can still affect wall-clock use.

Accounting distinguishes:

- `charged` / `charged_usd` / `metered_usd`: quota consumed, including equivalent
  response-replay charges; budget and item/suite caps use this quantity.
- `provider_metered_usd`: known provider usage charges, excluding response replays.
- `cache_adjusted_estimate_usd`: actual-call estimate additionally adjusted for the
  provider's prompt-cache discount; a response replay contributes zero.
- `response_cache_hits`: completed shared-response replays. Scoped
  `usage_by_model` contains actual-call usage; `equivalent_usage_by_model` also
  includes replayed usage. Original unknown reservations remain outstanding.

Prices are USD per million tokens. The ledger conservatively counts cached input at full input price; an optional `price.cache_read_multiplier` produces a separate cache-adjusted estimate. If that discount is unknown and cached input exists, the adjusted estimate is null. These are usage-based estimates, not provider invoices. Researcher fees remain separate from candidate testing fees. Unknown/aborted calls retain their reservations. A provider can report usage above a pre-request bound; overshoot closes the wallet and is reported, never presented as a guaranteed billing cap.

Artifacts, traces, credentials, datasets and real results are ignored by Git. Keep the run directory private. Publish only operator-reviewed outputs through your chosen reporting workflow.

## Tests

```bash
pytest -q
# Include actual namespaces and the complete local mock pipeline:
SEB_TEST_ROOT="$SEB_ROOTFS" SEB_TEST_SCIENCE="$SEB_SCIENCE_PACKAGES" pytest -q
```

Runtime tests use only synthetic fixtures and local fake providers. A collaborator must separately validate their provider's streaming/tool-use compatibility and verified prices before scaling paid experiments.
