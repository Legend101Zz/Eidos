# Development pilot 3, addendum 1: arms 3 and 4 could not build the charter for the new cards

Written on 11 November 2026 at about 19:20 IST, after the first block (N03-r1) finished. No arm, prompt, envelope, success rule, card or Eidos source changed.

## What happened
- For the regression cards N02 and N03, the arm-3/arm-4 adapter (`study/eidos_arms/charters.py`) obtained the RMSE bar by importing `study.packaging.p3_data`, which imports NumPy.
- Arms 3 and 4 run their adapters in the lab interpreter, which deliberately has no NumPy. Both crashed during setup, after 1–2 s and with no model call ("ModuleNotFoundError: No module named 'numpy'").
- Block N03-r1 was recorded as an incident ("one or more runs did not record a returned model id").
- The live smoke had used the synthetic task, and the pilot-3 tooling dry run used stubs. Neither exercised this path.

## Fix
`charters.py` now reads the frozen bar directly from `study/packaging/p3_frozen.json` (the same values: N02 8.900388…, N03 0.552139…), with no NumPy import. All four pilot-3 charters were checked under the lab interpreter.

## Charter consequence
- **N03-r1** (an incident) and **N01-r2 and N02-r1** (stopped by the controller mid-run, so the fix could apply before more blocks were wasted) are archived under `pilot-3/runs/aborted-addendum1/`. **All three are re-run whole.** No outcome of these runs is used.
- The fix applies to every block from the restart on.
