# Notices

## Code copied or adapted from elsewhere

Checked by searching `src/`, `components/`, `packages/`, `examples/`, `scripts/` and `tests/` for "adapted from", "copied from", "derived from" and for the names Pi, OpenCode, Codex, PyGlove, Composer, AiiDA and CheckList, and by reading `docs/decisions/`. Findings:

**No third-party code was copied or adapted into Eidos.** The design documents (`docs/design/`) discuss other systems (agent harnesses, workflow engines, plugin hosts) as prior art for ideas; no source from them is included, and the ADRs record no reuse of upstream code. The only grep hits outside prose were the words "copied from" in comments about internal behaviour (for example "success rules are copied from the charter").

**Copied from the project owner's design handoff archive (`eidos-claude-handoff`, worked example `pytorch-lab-worked-example`):** the researcher's own example scripts, kept with a provenance header in each file. They are the owner's existing code, not third-party code; they are covered by the project's licence, Apache-2.0 (`LICENSE`, `NOTICE`, `docs/ops/LICENSES.md`).
* `examples/switch-order-project/switch_order.py`, `examples/switch-order-project/pair_report.py`
* `examples/image-project/image_reuse.py`, `examples/image-project/pair_report.py`
* `components/eidos.pytorch.{behavior-check,evaluate,train,weight-edit}/fixtures/selftest/switch_order.py` and `pair_report.py` (verbatim copies used as component self-test fixtures)

## Material under `research/public/` (not modified here)

Imported as the public research release `research-public-v0.1.0`; its own `research/public/development/NOTICES.md` is authoritative. In short: the code there was newly authored for the research preparation task; **no general redistribution licence is assigned**; upstream benchmark code (AIRS-Bench, MLGym, LLM Speedrunner, AIDE, RE-Bench) is referenced and inspected, not copied; datasets loaded through scikit-learn are Iris (Fisher 1936, UCI, CC BY 4.0) and Optical Recognition of Handwritten Digits (Alpaydin and Kaynak 1998, UCI, CC BY 4.0), with local resplits and corruption described there.

## External software used as separate programs or installed dependencies

Eidos does not include or modify these; it downloads, locates or depends on them. Licences are listed in `docs/ops/LICENSES.md`.
* Temporal Server 1.32.2 (pinned, sha256-verified download or operator-supplied binaries): MIT.
* PostgreSQL 14 or later (operator-installed): PostgreSQL Licence.
* Python dependencies of the lab and of the worker packages: see the generated table in `docs/ops/LICENSES.md`.
