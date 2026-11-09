# 0001 — Repository layout and distributions

Status: accepted (2026-11-07)

## Decision
- One uv workspace. The server is a single distribution `eidos-lab` (`src/eidos/`) whose
  subpackages follow the design's ownership boundaries: `domain`, `application`, `persistence`,
  `orchestration`, `investigator`, `runtime`, `reporting`, `api`, `mcp`, `cli`, `supervisor`,
  and `apps` (thin composition roots / entry points).
- Worker-facing code is published independently and depends only on the standard library:
  `packages/eidos-wire` (`eidos_wire`), `packages/eidos-worker-sdk` (`eidos_worker`),
  `packages/eidos-pytorch` (`eidos_pytorch`, uses the project's already-installed torch, never
  declares it as a dependency).
- Boundaries are enforced with Import Linter contracts in `pyproject.toml` (CI). Runtime
  authority is enforced by PostgreSQL roles/grants, job-scoped API tokens and OS profiles, not
  by imports.

## Why
The design permits consolidating server distributions when imports and credentials stay
separated. A single server wheel cuts release work; separate entry points per role keep
credentials separate (each process receives only its own token/DSN at launch).

## Alternatives
One distribution per design folder (rejected: release overhead with no authority benefit).

## Reversal condition
If a server role must be installed on a different machine/Python than the others, split that
subpackage into its own distribution; import contracts already isolate it.
