# Standalone scientific component coverage

All 16 top-level public components in `components/` are copied unchanged, with
their method cards and authored fixtures. The three required source packages
(`eidos-wire`, `eidos-worker-sdk`, `eidos-pytorch`) are copied as well. The assessor
and qualification runner also receive their unchanged pure-rule dependency closure
(`eidos.domain.{charter,errors,stats,evidence,qualification}`) under `support/`,
including the repository license. No lab
database, ledger, resolver, or server is supplied. “Callable” means the actual
component entry point executes through the worker SDK, subject to its declared
inputs; it does not imply scientific qualification for a new project.

| Component | Toolkit offer and requirements |
|---|---|
| eidos.pytorch.train | Callable; requires a project training adapter. Verified in sandbox. |
| eidos.pytorch.evaluate | Callable; requires adapter and checkpoint. Verified in sandbox. |
| eidos.pytorch.weight-edit | Callable; requires compatible checkpoint, adapter and edit declaration. |
| eidos.pytorch.behavior-check | Callable; requires compatible adapter/checkpoint and behavior specification. |
| lab.pairs.by_group | Callable analysis; paired prediction table. Verified in sandbox. |
| eidos.assessors.paired-ci | Callable; consumes paired evidence. Verified in sandbox. No lab assessment authority is implied. |
| eidos.templates.improves-under-budget | Callable planner; consumes claim/charter inputs. No lab admission or execution is supplied. |
| eidos.behavior.suite | Callable; behavior predictions and suite input. |
| eidos.task.score-classification | Callable numeric analysis; requires allowed labels/predictions. Hidden evaluator labels are never supplied. |
| eidos.task.score-regression | Callable numeric analysis; same restriction. |
| eidos.qualification-runner | Callable; authored component controls and target source required. Verified in sandbox. |
| eidos.project.command | Callable; runs ordinary project commands in the same outer sandbox. |
| eidos.search.recipe-sweep | Callable; exploratory recipe search over a project command on visible validation (grid or seeded random, seeds/folds, budget, stopping rule), through the same worker SDK as the lab. Verified in sandbox. Project files come from a source bundle or `command.copy`, never implicitly; evaluations get a scrubbed environment. No claim and no test data. Lock the winner before independent confirmation. |
| lab.learning.group-balanced-ema | Included training extension, loaded by a compatible trainer; not a standalone artifact operation. |
| lab.switch.balanced-data | Callable data generator for the library's switch fixture. |
| eidos.synthetic.llm-examples | Entry point included; **not operational without an authorized scoped model grant/broker**. This standalone study grants no additional paid model calls. |

The model-backed generator is an explicit parity gap if arms 3/4 receive a model
grant for it; the controller must give the same grant to this toolkit or disable
that capability for all arms. No substitute generator or fabricated examples are
used. Lab scheduling, catalog discovery, persistence, investigator policy and
assessment authority are infrastructure differences, not exported scientific
algorithms. The nested qualification target is an authored fixture, not a separate
public catalog component.
