Work on the research task in `task/`. Read `task/brief.md`, `task/task.json`,
the supplied project and the answer interface in `task/answer_schema.py`.
Use only the supplied training and validation data for exploration and selection.
The evaluator owns test data and performs independent confirmation after you finish.

{{PRACTICAL_BAR}}

Your tools: {{TOOLS_PARAGRAPH}}

Your envelope is 30 minutes of wall time and 8,000,000 total processed model tokens
(input, cache reads, cache writes and output), stopping at the first exhausted limit.
Use CPU only and one computational thread: keep OMP_NUM_THREADS=1 and
MKL_NUM_THREADS=1, and call torch.set_num_threads(1) in task code. Do not launch
parallel training jobs. Stay within the assigned run directory. Do not seek grader
files, reference solutions, unrelated host files, external datasets or paid services.

Submit exactly one locked candidate, or an honest abstention. Write
`out/answer.json` in the eidos-answer/0.2 format defined by `task/answer_schema.py`.
Put the candidate source, model weights, dependency lock, predictions and evidence
files beside it under `out/`; artifact paths in the answer are relative to `out/`.
Include an ordinary runnable project.py with train and infer commands compatible
with the supplied project, and a reproducible training command. Use the candidate
field `predictions_path` for your locked validation predictions. In
`reproduce_command`, use `{inputs}`, `{out}` and `{seed}` as argument placeholders
(for example, `["python", "project.py", "train", "--inputs", "{inputs}", "--out",
"{out}", "--seed", "{seed}"]`). Keep all imported project modules under `out/`.
For every requested seed, `reproduce_command` must write the fresh checkpoint to
`{out}/<basename of candidate.model_path>`. The evaluator passes that file to your
infer command. Use compatible formats for the locked and fresh checkpoints.
The evaluator will provide inference inputs separately. Do not fabricate test
predictions or evidence.
State your claim, its scope and its supporting evidence explicitly in the answer
and in your final message. Record uncertainty, failed attempts, costs you know,
unknown costs, exposures and limitations. A negative or inconclusive result is valid;
an unsuccessful search does not prove that improvement is impossible.
