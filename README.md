# Eidos

**A local research harness for investigating custom PyTorch models with an LLM investigator, under recorded evidence rules.**

Eidos runs on your own machine. A bounded, durable investigator (Anthropic or an OpenAI-compatible model) diagnoses failures, authors and publishes research components, runs trials and checkpoint branches, and exports models. Claims are tested only through frozen, qualified evidence plans and an independent assessor. Every command, job, charge and isolation receipt is in a PostgreSQL ledger. You can drive the lab from the `lab` CLI, from MCP clients (Claude Code, Codex CLI) and from HTML reports.

## Status

**v1.0.0 is the software release:** acceptance areas A1–A15 within the scope in [`docs/release/SUPPORTED.md`](docs/release/SUPPORTED.md).
- **Platforms:** Linux x86_64 and macOS arm64; CPU on both, MPS on macOS.
- **Providers:** Anthropic and OpenAI-compatible endpoints.
- **MCP clients:** Claude Code, Codex CLI and the MCP Python SDK.
- **Sealed (hostile-code) evaluation:** Linux bubblewrap only. macOS uses Seatbelt as the same OS user, with documented limits.

v1.0.0 makes **no scientific claim**: whether Eidos helps an LLM do model research (A16) is a separate, later release (`eval-*` tags).

See [`docs/release/RELEASE_NOTES_v1.0.0.md`](docs/release/RELEASE_NOTES_v1.0.0.md), [`CHANGELOG.md`](CHANGELOG.md) and [`ACCEPTANCE_MATRIX.md`](ACCEPTANCE_MATRIX.md).

## Quick start

```bash
uv build --all-packages -o dist && uv venv lab-venv && uv pip install --python lab-venv/bin/python --no-cache --find-links dist eidos-lab
lab-venv/bin/lab init && lab-venv/bin/lab start && lab-venv/bin/lab doctor
```

Then read [`docs/ops/INSTALL_AND_OPERATE.md`](docs/ops/INSTALL_AND_OPERATE.md) (projects, investigations, MCP, backup/restore, upgrade) and [`docs/ops/TROUBLESHOOTING.md`](docs/ops/TROUBLESHOOTING.md). Component authors: [`docs/author-guide/`](docs/author-guide/).

## Repository

| Path | What |
|---|---|
| `src/eidos` | Lab: ledger, command service, API, Temporal workflows, investigator, supervisor, CLI, MCP, reports |
| `packages/` | `eidos-wire`, `eidos-worker-sdk`, `eidos-pytorch`: standard-library worker packages installed into research environments |
| `components/` | Built-in research components |
| `examples/` | The switch-order and image example projects |
| `docs/design/` | The design (immutable) |
| `research/public/` | Imported public research release (immutable; its own notices; **not** covered by the Apache-2.0 licence) |

## License

Apache License 2.0: see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE). The licence covers everything in this repository except `research/public/`, which keeps its own notices (`research/public/development/NOTICES.md`) and has no general redistribution licence. Third-party licences: [`docs/ops/LICENSES.md`](docs/ops/LICENSES.md).
