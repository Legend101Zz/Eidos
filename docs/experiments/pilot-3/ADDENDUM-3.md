# Development pilot 3, addendum 3: quota exhaustion, an arm-3 placeholder model id, and a second re-classification

Written on 11 November 2026 at about 20:05 IST. No arm, prompt, envelope, success rule, card or Eidos source changed.

## 1. MiniMax quota exhausted at about 20:01 IST
- **What happened:** the Token Plan reached its usage limit (error 2056, HTTP 429), as in pilot 2 (pilot-2 addendum 3). Runs ending on that error are incidents under the charter.
- **The first two:** N01-r1 (arm 3's final result was the API error) and N04-r2 (arm 4 stopped at 12 s).
- **What happens next:** the driver finishes the current pass, waits until a probe request succeeds, then re-runs every incident block whole. Records are archived and the model is never substituted.

## 2. Arm 3 counted Claude Code's `<synthetic>` placeholder as a model change
- **The defect:** arm 3's adapter (`study/eidos_arms/arm3.py`) marked a run as an incident whenever more than one model id appeared. It did not exclude `<synthetic>`, the id Claude Code gives its own messages, such as a printed API error. The launcher's version guard already excludes it (pilot 2).
- **Effect on N01-r1:** none. The run is still an incident because its final result was the provider error (`run_arm.py` terminal-error rule).
- **The risk:** a recovered `<synthetic>` message could have made a valid arm-3 run an incident and forced a re-run of the whole block.
- **The fix:** the check now excludes the placeholder, as the launcher does. Any later arm-3 incident labelled "model id changed" with a valid final result will be re-checked under this rule before a re-run.

## 3. N03-r1, arm 4: the same mislabel as N01-r2 (addendum 2)
- **What happened:** the arm-4 process started before addendum 2. Its investigation stopped on refused proposals (`needs_input: refused-proposals`), and its last usage receipt was a successful call.
- **Re-classification under addendum 2's rule:** arm 4 is `crash` and arms 1–3 are `ok`. The block is graded as it stands and arm 4 counts as a failure; there is no re-run. Previous values are kept under `*_before_reclassification`.
