# eidos.project.command — method card

**Question.** Runs a researcher's existing training/inference command unchanged ("whole-trainer
escape hatch") so projects that own their trainer need no Eidos hooks.

**How.** Copies the project's small source files (or a candidate `source` bundle) into a private
working directory, places declared input artifacts at the file names given in
`config.files`, runs `config.argv` (an argument array — never a shell string; `python` means the
project interpreter; `{work}`, `{out}`, `{seed}` are substituted), and stages every file the
command writes under `{out}`. A JSON receipt written by the project is surfaced as `metrics`.

**Limits.** The command's own claims (e.g. receipts) are self-reports. Scores used for decisions
come from a separate scorer component. The project directory itself is never modified.

Model export receipts bind the producing file basename, normalized slot, staged SHA-256 and byte size. Preserve the basename when replaying the recipe.
