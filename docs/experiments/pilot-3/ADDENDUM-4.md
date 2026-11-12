# Development pilot 3, addendum 4: both graders confirmed fresh-seed reproductions with a hard-coded `model.pt`

Written on 11 November 2026 at about 20:25 IST, during the pilot. No arm, prompt, envelope, success rule, card or Eidos source changed.

## The defect
- **What the graders do:** for cards that require fresh-initialization reproduction, both the local grader (`study/grade_block.py`) and the GitHub grader (`Legend101Zz/eidos-grading`, `grader/run.py`) retrain the candidate at seeds 23 and 37 with its declared `reproduce_command`. They then run inference with the checkpoint at the **fixed** path `confirmation-<seed>/model.pt`.
- **Why that is wrong:** the task contract (brief, `task.json`, answer schema) never fixes the checkpoint's name or format. The baseline simply happens to write `model.pt`. The answer declares `model_path` (with its hash), and both graders already use it for the locked checkpoint.
- **Who it hit:** a candidate that legitimately switched formats, such as scikit-learn's `model.joblib` or NumPy's `model.npz`, was marked `invalid_output` even though its reproduction ran correctly.

## The fix
Both graders now confirm with the declared checkpoint's file name (`Path(model_path).name`) in the confirmation output directory. Candidates that keep `model.pt` are unaffected (including the six qualification controls). The grader's tests and the study tests pass.

## Effect on pilot 3
- **N03-r1, arms 1 and 3** were `invalid_output` for this reason alone. Re-graded locally, both are verified successes:
  - arm 1: RMSE gain 9.989, reproduced at 23 and 37;
  - arm 3: RMSE gain 9.876, reproduced at 23 and 37.
- **Records:** the old grades are archived under `pilot-3/grading-archive-addendum4/`. Both arms are re-submitted to the fixed GitHub grader.
- **Every later block** is graded with the fixed graders.

## Effect on pilot 2 (erratum: see `docs/experiments/pilot-2/ADDENDUM-4.md`)
The same defect produced the only arm-2 failure in pilot 2 (D03-r2, `model.joblib`). Corrected, arm 2 verifies 12/12, and the pilot-2 primary contrast arm 4 − arm 2 is 0.0, not +0.083.

## Hosted grading infrastructure (added about 21:10 IST)
**Every hosted grade from 13:48 to about 15:30 UTC (19:18–21:00 IST) failed before scoring**, in pilots 2 and 3 alike. GitHub-hosted runners could not resolve Ubuntu's package hosts (`azure.archive.ubuntu.com`, `archive.ubuntu.com` and `security.ubuntu.com`; `Temporary failure resolving`). The runtime install step failed, so every grade became `incident: scorer setup failed`. GitHub's status page reported no incident, and the hosts resolved normally elsewhere. No scoring logic was involved.

Fixes in the grading repository:
- **`9d94e85`:** candidate uploads are serialized with a file lock. Concurrent submissions sharing one store clone had lost a bundle upload (N03-r1 arm 3).
- **`6213e57`:** apt retries, with a fallback to `archive.ubuntu.com`. This was insufficient on its own.
- **`42de7ed`:** a host that does not resolve is looked up over DNS-over-HTTPS (1.1.1.1, reached by IP) and pinned in `/etc/hosts`. The hosts, packages, TLS verification and scoring are unchanged. The run logs show the pins being applied.

**Verification:** N03-r1 arm 1 then graded on GitHub as a verified success, gain 9.98885, matching the local 9.98885.

**Records:** every arm without a valid hosted grade (pilot 2: 8 arms; pilot 3: all graded blocks) is re-submitted. Results go to `github-sweep/` (pilot 2) and `github-sweep/` and `github-retry/` (pilot 3). Failed attempts stay on disk and in the Actions history.
