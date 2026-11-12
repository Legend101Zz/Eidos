# ADR 0032 · Investigator conclusion scope contracts

**Status** Proposed for a subsequent development build.

In the settled Tier 1 candidate N01-r1 run, the investigator proposed a recipe improvement for independent confirmation. Its optional abstention section set `abstained` to false and misspelled `next_discriminating_test`. The coordinator recorded the conclusion, and the study export preserved the authored section. The pinned structural validator rejected the answer. This is development evidence about the output contract. The original assignment and grade remain immutable.

An operator can set `investigator.conclusion_scope_schema` at investigation creation. The coordinator validates the schema declaration and applies it to supplied scope objects on `conclude` and `stop` before accepting their decisions. The contract uses the existing component configuration validator with a bounded subset of shape keywords. Its only pattern is the non-whitespace string check. The study shape guard is partial and leaves final answer validation to the export validator. The study adapter supplies the schema for its optional `scope.answer.abstention` object. Legacy conclusions without an opt-in contract, scope, answer or abstention section keep their established behavior.

A refusal retains the original proposal and response reference in the ledger. The existing bounded feedback path allows the investigator to author a subsequent proposal within its remaining allowance. The adapter performs no spelling substitution or metadata repair. If the investigator cannot supply a valid conclusion, the existing failure and separately attributed controller-abstention paths apply. The export validator and scientific evaluator still apply to every submitted answer.

The baseline runtime at `257792ab56b6b984e807fe284a22fc5d31c4dd8b` stores the optional investigator setting through its existing open configuration. It does not enforce it. A future paired comparison may use the same controller, settings and study envelope on both builds. Enforcement is a candidate runtime difference and must be recorded as such in the build and evaluation provenance.
