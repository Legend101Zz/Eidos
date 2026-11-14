# Study treatments and feature flags

The default is `pilot4-v1`. It preserves the public Pilot 4 configuration, prompt,
component bindings and toolkit from main at `806f939`. Select the experimental
portfolio explicitly before launching a block:

```sh
export EIDOS_STUDY_TREATMENT=v2-portfolio
python -m study.exp.flags show v2-portfolio
python -m study.exp.flags check
```

Selection passes through launcher and lab worker environments. `arm.json`, arm-4
setup and run records, lab import records, and expected/delivered prompt receipts
carry the treatment id, ordered flag ids and canonical resolved SHA-256. A worker
refuses a different selection from its prepared arm record. The hash covers the
resolved investigator, selected prompt bytes, ordered component plans for N01,
N02 and D03 (bundle digests, bindings and upload names), and the complete toolkit
manifest digest. Component publications also bind source bytes. Lifecycle
annotations do not change the resolved hash, so rejection does not break
historical identity. `for_run` refuses missing treatment metadata during live
dispatch; offline replay can still accept legacy metadata. Arm 4 also records the canonical effective
investigator SHA-256 before per-run mutations. Its legacy configuration hash
retains the base-file hash only when the effective dictionary equals the base.

To add a flag, add an entry to `flags.json` and name it in a new treatment under
`treatments/`. Keep the immutable investigator base and Pilot 4 goldens unchanged.
Component `install` and toolkit `add` operations accumulate across flags; binding
and replacement maps merge by destination. Investigator patches and treatment
overrides follow RFC 7396: objects merge,
`null` deletes, arrays and scalars replace. Overrides apply to the investigator
configuration after flag patches. Requirements must be explicitly present in the
treatment; dependencies apply before dependents. Unknown selections, missing
requirements, cycles and invalid lifecycle records refuse resolution.

States are `experimental`, `adopted`, and `rejected`. Experimental flags require
explicit treatment selection. Only a kept experiment can change the default.
After adoption, fold its patch into the default and keep the adopted flag one
cycle as an empty-patch alias. The default refuses every flag (including adopted
aliases) and every override.
An adoption updates the default implementation through a kept experiment, rather
than selecting an alias in its definition.

To reject a flag, set its state to `rejected` and fill `removal` with `decided`,
`experiment`, and `reason`. Keep its patch for replay. New resolutions refuse it;
`resolve(id, allow_rejected=True)` is only for historical replay. Update active
treatments to omit it before `check` will pass. `removal-list` is a dry run: it
reports per-path deletion eligibility, procedures,
blocking flags/treatments/core imports, and treatments that would be retired.
`--assume-rejected floor.v2-slice,toolkit.portfolio` previews a decision without
changing the registry. A nonzero exit means a blocker remains. In that preview,
`floor.selection-margin` still requires the slice and must also be retired.

To remove rejected code, review that inventory, all remaining flag dependencies,
and inline policy branches, then delete only paths marked `removable` and obsolete
wiring,
remove the flag from active treatments, and remove its registry entry. Some
features share modules: the selection-margin flag has no standalone files, so
its inline branches still require review. Entries with `kind: procedure` describe
restoration instead of deletion: rejecting the diagnosis fix restores the pinned
1.0.0 bytes as the unpinned release. Portfolio entries declare `shared_with` in
both owning flags and cannot be deleted while either owner remains. The dry run
resolves every surviving treatment, builds component/toolkit plans for N01, N02
and D03, and repeats resolution and artifact hashing in a disposable tree after
virtual deletions/restoration. Treatment definitions selecting removed flags are
reported for retirement rather than silently rewritten. Dependent flags must be
retired as well before removing registry entries. The portfolio source is used by
both v2 lab and toolkit flags; retain it while either treatment use remains. Re-run the goldens,
registry check, replay tests and the full relevant validation suite.

`toolkit-base.json` is the directory/file inventory main copied. The default maps
its diagnosis destination to the byte-identical pinned 1.0.0 source. Exported
`COVERAGE.md` uses frozen main documentation for the unchanged default tree;
selected toolkit changes use the current coverage document. The diagnosis fix
also retains its pinned 1.0.0 directory in experimental toolkits for explicit
replay. The treatment receipt is outside the toolkit, preserving default tree bytes. Reusing a toolkit directory
under a different treatment is refused.

`flags check` validates inventory paths and builds every treatment's component and
toolkit plans for N01, N02 and D03. Runtime resolution does not require code
inventory paths (such as tests) to be shipped. Unknown treatment and patch keys
are refused. `floor.v2-slice` recommends `floor.selection-margin`: the v2 prompts
advertise a one-SE margin, and `check` warns if a treatment selects the slice
without that flag. The recommendation is not a dependency; omission still means
strict improvement with no margin.

Provider selection is independent: `EIDOS_STUDY_PROVIDER` defaults to
`minimax-m3.1-flash`. `anthropic-haiku-5-5` and `anthropic-sonnet-5-5` select
Messages through the same audited deadline relay. Profiles patch the investigator
before treatment flags/overrides; `resolve(provider="...")` takes precedence over
the environment. `python -m study.exp.providers check` validates the inventory.
`openai-default` runs `gpt-5-nano` (list price USD 0.05 / 0.40 per million
tokens in / out, USD 0.60 per run limit; see ADR 0053). A profile whose model is
still a `REPLACE_ME` placeholder refuses execution. Claude CLI arms require `arm2`
settings and therefore refuse the OpenAI profile. Arm, run and prompt receipts
bind the provider id, wire and profile file digest. The shared lab restarts on a
provider change; fixture treatments need neither credentials nor a bridge.

## One-command development runs

Start a new session with `bash study/exp/smoke.sh`. It runs baseline stubs on two
public cards, then model-free `autopilot-v2` on N01 and on a configured D card.
It requires a clean committed controller and a buildable dedicated lab. It never
selects a model runtime. Its short autopilot runs carry `smoke: true` and the
separate `eidos-exp-smoke-scorecard/1` schema, which `compare` refuses.

```sh
python -m study.exp.run --treatment stub --cards N01,N03 --out "$RUNS/baseline"
python -m study.exp.run --treatment autopilot-v2 --pool D --card-limit 2 --out "$RUNS/autopilot"
python -m study.exp.run --treatment v2-portfolio --ref HEAD --provider openai-default \
  --pool E1 --replicates 3 --arms 4 --budget budget.json --out "$RUNS/candidate"
python -m study.exp.run --pair 'pilot4-v1@BASE_REF' 'v2-portfolio@HEAD' \
  --pool E1 --budget budget.json --concurrency 2 --out "$RUNS/pair"
python -m study.exp.run --treatment v2-portfolio --pool D --budget budget.json \
  --dry-run --out "$RUNS/plan"
```

Set `RUNS` below `/Volumes/Mrigesh SSD/eidos-v1/improvement/runs`. `--dry-run`
resolves and verifies prerequisites, writes the plan/estimate and starts no lab or
block. Absent builds are reported as requiring a build. A normal run builds
`evals-labs/ip-<sha12>` with the existing builder, or verifies its archive, wheels,
installed packages, interpreter and service ports. Its explicit home is the
build's `home/` subdirectory. Build refs must be ancestors of the clean controller;
both commits and lab receipts are recorded. A standing charter is pinned from
`experiments/CHARTER.md`; its text/path never enters model prompts.

A variant binds treatment, full build commit and provider. `stub` always invokes
`study/stub_agent.py` and exports the supplied baseline, with zero model tokens.
Autopilot uses arm 4 and starts with empty credentials. Other treatments may call
a provider; this is an operator action. Arm 2 requires a Claude-compatible profile.
The default envelope remains 1,800 seconds, eight million processed tokens and one
thread. `--wall-seconds` requires `--smoke`; no shortened run is scientific evidence.

Blocks follow card order, then replicate order. Paired variants for a card and
replicate are adjacent and start together. The concurrency cap applies separately
to each lab; a pair sharing one lab needs concurrency at least two. Whole incident
blocks retry once by default, preserving launcher/grader incident archives. All
labs are stopped in `finally`, unless `--keep-lab-running` is explicit. Exit 0
means all requested blocks were graded and none remains an incident; it does not
mean the treatment improved a task. `manifest.json`, `run.log`, per-block logs,
`scorecard.json` and `scorecard.md` carry the evidence. Pair mode writes
`paired.md` using compare's descriptive statistics, with no keep/reject decision.
The current `compare(aa=True)` requires identical treatments/builds, so the small
runner adapter calls its `paired_stats` helper directly for different variants.

## Pools and references

The registry accepts both D's `pool_id`/`card_id` manifest and E's `pool`/`id`
manifest. E3 has two cards. `EIDOS_POOLS_ROOT` is a colon-separated list of roots;
the first existing `<root>/<POOL>/seal` wins. References come from
`<pool>/controller/<CARD>/reference.json`. The runner passes a minimal logical
pool identity to the scorecard builder: no private root, provenance, dimensions
or source metadata appears in a scorecard. `EIDOS_EXTRA_POOL_MANIFESTS` is a
controller-only colon-separated list for additional manifests. All seal variables
and the private-root variable are stripped from workers and labs. A separate
`EIDOS_STUDY_POOL_PROFILES` value carries only minimal public rosters, metrics,
bars and commitments, so blind cards still work after location variables are removed.

Legacy references default to the committed `experiments/pools/legacy-references.json`;
`--references` can select another map. Regenerate the seven public reference blocks
with the actual grader's public generators, baseline and practical bars:

```sh
nice -n 10 "$EIDOS_RESEARCH_PYTHON" -m study.exp.cards.reference_legacy \
  --out experiments/pools/legacy-references.json --budget-seconds 480
```

Only training data selects the strong reference recipe. The independent evaluation
uses seeds 11, 23 and 37, and records paired standard error and headroom in the
same reference format as pools. `--autopilot-scorecard` adds the existing builder's
uplift comparison; its build, pool, reference digest and roster must match.

## Live budgets

Stub and fixture runtime need no budget, quota file or credentials. Every live
run requires a JSON budget and an existing JSONL spend ledger, including an empty
file for a new budget. A minimal budget looks like:

```json
{
  "spend_ledger": "spend.jsonl",
  "providers": {
    "openai": {"usd_cap": 20, "stop_fraction": 0.8},
    "anthropic": {"usd_cap": 20, "stop_fraction": 0.8}
  },
  "minimax": {"weekly_share": 0.5, "weekly_floor": 0.15, "interval_floor": 0.2}
}
```

Ledger paths are relative to the budget file unless absolute. Provider family
spend combines all Anthropic/OpenAI profiles. Spend plus worst-case per-run USD
(including every arm, variant, replicate and permitted incident retry) must remain
below the stop cap. Caps cannot exceed USD 20 or an 80% stop fraction. Unknown USD
charges fail closed. Operators must serialize live budget admissions against a
shared ledger; this controller does not reserve budget across independent processes.

MiniMax requires `--quota-file` with `utc`, `weekly_remaining_fraction` and
`interval_remaining_fraction`, each fraction between zero and one. Both the reading
and file must be younger than ten minutes. Remaining quota must exceed the floors,
and consumed weekly share must stay below 50%. `--payg-approved 'who, when, scope'`
bypasses quota admission and is copied verbatim into the scorecard header. No
provider availability or quota is queried automatically. Each live run, including
archived incidents, appends tokens, USD (or null), run id and quota snapshots to
the ledger. USD can be estimated from profile prices when token counts are known.

## Blind H consultations

```sh
python -m study.exp.heldout --pair 'pilot4-v1@B0_REF' 'v2-portfolio@HEAD' \
  --provider openai-default --threshold 0.10 --note 'Prospective consultation rationale' \
  --budget budget.json
```

The private root defaults to `EIDOS_IMP_PRIVATE_ROOT`, or accepts `--private-root`.
All scratch, logs, grading and scorecards remain in `<private-root>/runs/<utc>/`.
An atomic counter permits three consultations; a failed attempt consumes one.
Threshold and rationale are pinned before running. Pass requires mean paired
card VRG delta at least the threshold and one-sided sign-flip p at most 0.10.
Only the coarse receipt is printed and written beside the private root in
`receipts/`: rates and mean delta round to 0.05, with aggregate accounting and
failure counts. Errors print only their exception type, including argument errors.
No card id or private path leaves the runner. Novelty wins are null when the
upstream scorecard does not supply a verified novelty-win observation; absence is
not counted as zero. The threshold uses unrounded values.

Tests fabricate seals and build receipts under temporary roots. The deliberately
explicit `EIDOS_EXP_TEST_ROOT` permits test output there and fabricates a lab
receipt only for the baseline stub; it cannot run a model or fixture lab.
