This directory is a standalone source export. It requires the supplied Python
environment (torch and numpy); it needs no running lab or server package.

List components: `python toolkit/run_component.py --list`

Run one: `python toolkit/run_component.py eidos.pytorch.train --args '{"config":{"adapter":"my_adapter:ADAPTER","python_path":["/absolute/path/to/my/project"],"builder":"MyModel","steps":10,"seed":11}}' --out train-run`

`--args` is one JSON object with `config`, `inputs`, optional `seed`, `limits`,
`ports` and `llm_grant`. File inputs use
`{"checkpoint":{"file":"train-run/staging/FILE","media_type":"application/octet-stream"}}`.
Text and JSON inputs use `{"text":"...","media_type":"text/csv"}` and `{"json":...}`.
Paths are relative to your working directory; component `config.python_path`
entries are relative to that component's source directory unless absolute.

Each invocation writes inputs, scratch, staging, a typed invocation and the real
worker output manifest. Read the manifest's output filenames and receipt. A
component failure returns a nonzero CLI status. Method cards and authored fixtures
are in `components/COMPONENT/`; they describe interfaces, requirements and limits.
These fixtures are library examples, unrelated to your task's held-out grading.
See COVERAGE.md for capabilities requiring external grants or project adapters.
