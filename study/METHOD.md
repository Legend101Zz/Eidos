# Written research method

1. Frame a precise question about the supplied model and population. Distinguish
   a checkpoint repair, a training recipe, an architecture claim, a mechanism and
   transfer; each needs different evidence. Record known exposures and assumptions.
2. Before exploring, fix the baseline, useful gain, metric, guardrails, data splits,
   analysis unit, seed policy and resource budget from the task. Record unresolved
   obligations. Do not lower a success bar after seeing results.
3. Import and verify the working baseline first. Check inputs, training and
   validation separation, model export/reload and basic behavior. Keep baseline
   and parent artifacts immutable; record exact code, configuration and hashes.
4. Form a hypothesis with a discriminating test. Change one thing at a time when
   diagnosing a cause. Use controls for competing explanations; association alone
   does not establish causation. Record failed interventions and actual resources.
5. Select one candidate using allowed training/validation feedback. Match training
   and tuning allowances in comparisons. Freeze the candidate and recipe before
   confirmation; never select from hidden evaluation feedback.
6. Confirm independently on held-out data. Recipe and architecture claims need
   fresh training initializations; an inherited checkpoint repair alone supports
   only a checkpoint-specific statement. Use grouping and uncertainty appropriate
   to the data, rather than treating correlated examples as independent projects.
7. Report the claim, realized evidence, scope, guardrail results, uncertainty,
   costs, deviations and alternative explanations. A reproducible negative or
   inconclusive result is useful. Abstain when obligations remain unmet; absence
   of an improvement in a bounded search does not establish impossibility.

Condensed from the research-method sections of the model research harness design
(question and scope; claims and evidence plans; scientific confirmation). This
document confers no authority to grade your own final claim.
