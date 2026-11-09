# Contributing to Eidos

Thank you for helping. This page is short on purpose. For the research and testing work, start with the [experiments and testing plan](docs/experiments/README.md).

## Where to start

- **Experiments and study tooling:** read [`docs/experiments/README.md`](docs/experiments/README.md), then pick an open issue in the milestone "R0: study infrastructure". Its [getting-started section](docs/experiments/README.md#getting-started-for-the-teammate) has the exact setup commands.
- **The harness itself:** the [README](README.md) explains what Eidos is and how to install it. Operations: [`docs/ops/INSTALL_AND_OPERATE.md`](docs/ops/INSTALL_AND_OPERATE.md). Writing components: [`docs/author-guide/`](docs/author-guide/). Past decisions: [`docs/decisions/`](docs/decisions/).
- **Do not edit** `research/public/` (the imported public research release) or `docs/design/` (the design). Both are immutable; link to them instead.

## Branches and pull requests

1. Branch from `main`: `git switch -c task/<short-name> origin/main`.
2. Keep each PR to one topic. Add or update tests and docs with the code.
3. Push the branch and open a PR into `main` (`gh pr create --base main`).
4. The owner reviews and merges. Do not merge your own PR, do not tag, and do not force-push a shared branch.

## CI

CI (`.github/workflows/ci.yml`, 10 jobs) runs on every push to `task/**`, `release/**` and `main`. For a PR from a branch in this repository, the PR's own run shows as *skipped*: the push run has already built the same commit, so read that one.

Before pushing, run the fast checks locally:

```bash
uv sync
uv run ruff check src tests && uv run lint-imports
bash scripts/ci/mypy.sh
uv run pytest tests/unit tests/mcp tests/reporting -q
```

What each job checks and how to read a red one: [plan § How we use GitHub](docs/experiments/README.md#how-we-use-github) and [`docs/ops/CI.md`](docs/ops/CI.md). A job that fails while downloading (HTTP 502/504) is the known GitHub CDN flake: re-run it with `gh run rerun <run-id> --failed`.

## Commit identity

Commit as yourself, with your own GitHub identity:

```bash
git config user.name  "<your GitHub name>"
git config user.email "<id>+<login>@users.noreply.github.com"   # or another email on your GitHub account
```

Write commit messages that say what changed and why.

## Licence

By contributing you agree that your contribution is licensed under the [Apache License 2.0](LICENSE), as the rest of the repository (except `research/public/`, which keeps its own notices).
