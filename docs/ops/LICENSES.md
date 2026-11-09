# Licences

Generated inventory plus the items that need a human decision. The inventory is produced by `scripts/dev/license_inventory.py` (run it with the repo's `.venv` interpreter; it reads `importlib.metadata` of that interpreter) and is a screen, not legal advice: it reports what each package *declares*.

## Eidos's own licence (owner decision, 2026-11-08)

1. **Eidos is licensed under the Apache License 2.0.** The text is in `LICENSE` and the attribution notice in `NOTICE`. All four packages (`eidos-lab`, `eidos-wire`, `eidos-worker-sdk`, `eidos-pytorch`) declare `license = "Apache-2.0"` (PEP 639, wheel metadata `License-Expression: Apache-2.0`). Each wheel ships `LICENSE` and `NOTICE` under `*.dist-info/licenses/`. `scripts/ci/check_wheel_licences.py` checks the built wheels in CI.
2. **Project URLs** (`[project.urls]`) are not set.
3. **`research/public/` is excluded.** It carries "no general redistribution license" (its own `NOTICES.md`), and `NOTICE` says so. It is not part of any wheel.
4. **The worked-example scripts** copied from the owner's handoff archive (see `NOTICES.md`) are the owner's code. They are covered by the project licence.

## Packaging metadata state

All four packages have name, version, description, classifiers, `license = "Apache-2.0"` and `license-files = ["LICENSE", "NOTICE"]` (hatchling ≥ 1.27). None has `[project.urls]`. The worker packages depend on nothing (standard library only); `eidos-lab` depends on the packages in the table.

## Flagged entries in the environment inventory

* **LGPL-3.0-only, runtime: `psycopg` and `psycopg-binary`** (PostgreSQL driver). Used as an unmodified installed library through pip; the binary wheel bundles libpq and OpenSSL. Relevant only if Eidos is redistributed as a bundle containing the wheel; LGPL terms (notice, ability to replace the library) then apply.
* **MPL-2.0 (file-level copyleft), runtime: `certifi`.** Unmodified use is unproblematic; keep its licence notice if redistributed.
* **MPL-2.0, dev only: `pathspec`.** Not shipped.
* **Research environment (`research/public/development/requirements.lock`, checked in the lead's research env, 45 packages):** `tqdm` is `MPL-2.0 AND MIT`; `torch` wheel metadata declares no licence (upstream PyTorch is BSD-3-Clause; confirm from the PyTorch release before redistributing the wheel). numpy declares a multi-licence expression (BSD-3-Clause, 0BSD, MIT, Zlib, CC0-1.0); scikit-learn and pandas are BSD-3-Clause; `datasets` is Apache-2.0. Everything else there is MIT, BSD, Apache-2.0, PSF-2.0.
* **UNKNOWN:** none in the lab environment (every package declares a licence expression, a `License` field or classifiers); `torch` above is the one declaration gap.
* No GPL or AGPL package was found.

## External binaries (not shipped, not modified)

| Program | Licence | How it is obtained | Source of this statement |
|---|---|---|---|
| Temporal Server 1.32.2 (`temporal-server`, `temporal-sql-tool`) | MIT (Temporal Technologies) | pinned release download, sha256-verified, or `--temporal-source DIR` | upstream project; not verified by the build |
| PostgreSQL 14+ | PostgreSQL Licence (BSD-style) | operator-installed (Homebrew, apt) | upstream project; not verified by the build |
| Bubblewrap (`bwrap`, Linux sealed profile) | LGPL-2.0-or-later | operator-installed, optional | upstream project; not verified by the build |
| `sandbox-exec` (macOS Seatbelt) | Apple system component | part of macOS | n/a |

## Research examples under `research/public/`

Not modified. Per `research/public/development/NOTICES.md`: development code newly authored for the task, no general redistribution licence; datasets Iris and Optical Digits are CC BY 4.0 (UCI) through scikit-learn (BSD-3-Clause); the five benchmark repositories inspected for methodology are not copied (AIRS-Bench, MLGym and LLM Speedrunner are CC BY-NC 4.0, AIDE and RE-Bench MIT, with the additional requests the file lists). RE-Bench's requests about protected solutions are respected: nothing was extracted.

## Environment inventory (generated)

Python environment: the repository `.venv` (runtime and dev dependency groups). Scope `runtime` means reachable from `eidos-lab` through non-optional requirements (and the extras it names); `dev` is everything else installed.

| name | version | licence (declared) | scope | flag |
|---|---|---|---|---|
| alembic | 1.20.1 | MIT | runtime |  |
| annotated-doc | 0.0.5 | MIT | runtime |  |
| annotated-types | 0.8.0 | MIT | runtime |  |
| anthropic | 2.6.0 | Apache-2.0 / Apache Software License | runtime |  |
| anyio | 4.16.0 | MIT | runtime |  |
| ast_serialize | 0.13.1 | MIT | dev |  |
| attrs | 26.1.0 | MIT | runtime |  |
| certifi | 2026.10.23 | MPL-2.0 / Mozilla Public License 2.0 (MPL 2.0) | runtime | COPYLEFT? |
| cffi | 2.1.1 | MIT-0 | runtime |  |
| charset-normalizer | 3.5.2 | MIT | runtime |  |
| click | 8.5.1 | BSD-3-Clause | runtime |  |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause | runtime |  |
| docstring_parser | 0.18.0 | MIT / MIT License | runtime |  |
| eidos-lab | 1.0.1 | Apache-2.0 (this project's own package) | runtime |  |
| eidos-pytorch | 1.1.0 | Apache-2.0 (this project's own package) | runtime |  |
| eidos-wire | 1.0.1 | Apache-2.0 (this project's own package) | runtime |  |
| eidos-worker-sdk | 1.1.0 | Apache-2.0 (this project's own package) | runtime |  |
| fastapi | 0.162.2 | MIT | runtime |  |
| genai-prices | 0.2.1 | MIT | runtime |  |
| griffelib | 2.4.1 | ISC | runtime |  |
| grimp | 3.19 | BSD 2-Clause License / BSD License | dev |  |
| h11 | 0.16.0 | MIT / MIT License | runtime |  |
| httpcore | 1.0.9 | BSD-3-Clause | runtime |  |
| httpcore2 | 2.16.0 | BSD-3-Clause | runtime |  |
| httpx | 0.28.1 | BSD-3-Clause / BSD License | runtime |  |
| httpx2 | 2.16.0 | BSD-3-Clause | runtime |  |
| idna | 3.20 | BSD-3-Clause | runtime |  |
| import-linter | 2.16 | BSD 2-Clause License / BSD License | dev |  |
| iniconfig | 2.3.1 | MIT | dev |  |
| jiter | 0.19.0 | MIT | runtime |  |
| jsonschema | 4.26.0 | MIT | runtime |  |
| jsonschema-specifications | 2025.9.1 | MIT | runtime |  |
| librt | 0.16.0 | MIT | dev |  |
| logfire-api | 5.5.0 | MIT | runtime |  |
| Mako | 1.4.3 | MIT | runtime |  |
| markdown-it-py | 4.2.0 | MIT License | runtime |  |
| MarkupSafe | 3.0.4 | BSD-3-Clause | runtime |  |
| mcp | 2.3.0 | MIT / MIT License | runtime |  |
| mcp-types | 2.3.0 | MIT / MIT License | runtime |  |
| mdurl | 0.1.2 | MIT License | runtime |  |
| mypy | 2.4.1 | MIT | dev |  |
| mypy_extensions | 1.2.0 | MIT | dev |  |
| nexus-rpc | 1.4.0 | MIT | runtime |  |
| openai | 3.46.0 | Apache-2.0 | runtime |  |
| opentelemetry-api | 1.45.2 | Apache-2.0 | runtime |  |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | dev |  |
| pathspec | 1.1.1 | Mozilla Public License 2.0 (MPL 2.0) | dev | COPYLEFT? |
| pluggy | 1.7.0 | MIT / MIT License | dev |  |
| protobuf | 7.36.2 | 3-Clause BSD License | runtime |  |
| psycopg | 3.3.8 | LGPL-3.0-only | runtime | COPYLEFT? |
| psycopg-binary | 3.3.8 | LGPL-3.0-only | runtime | COPYLEFT? |
| pycparser | 3.12 | BSD-3-Clause | runtime |  |
| pydantic | 2.14.3 | MIT | runtime |  |
| pydantic-ai-slim | 2.66.0 | MIT | runtime |  |
| pydantic_core | 2.50.4 | MIT | runtime |  |
| pydantic-graph | 2.66.0 | MIT | runtime |  |
| Pygments | 2.21.0 | BSD-2-Clause | runtime |  |
| PyJWT | 2.15.1 | MIT | runtime |  |
| pytest | 9.2.0 | MIT | dev |  |
| pytest-asyncio | 1.4.1.post1 | Apache-2.0 | dev |  |
| python-multipart | 0.0.33 | Apache-2.0 | runtime |  |
| referencing | 0.37.0 | MIT | runtime |  |
| regex | 2026.11.7 | Apache-2.0 AND CNRI-Python | runtime |  |
| requests | 2.34.2 | Apache-2.0 / Apache Software License | runtime |  |
| rich | 15.0.0 | MIT / MIT License | runtime |  |
| rpds-py | 2026.9.1 | MIT | runtime |  |
| ruff | 0.17.4 | MIT | dev |  |
| shellingham | 1.5.4 | ISC License / ISC License (ISCL) | runtime |  |
| sniffio | 1.3.1 | MIT OR Apache-2.0 / MIT License / Apache Software License | runtime |  |
| SQLAlchemy | 2.1.8 | MIT | runtime |  |
| sse-starlette | 3.7.0 | BSD-3-Clause | runtime |  |
| starlette | 1.10.0 | BSD-3-Clause | runtime |  |
| temporalio | 1.35.0 | MIT | runtime |  |
| tiktoken | 0.14.0 | (full licence text in metadata; see the package) | runtime |  |
| truststore | 0.10.5 | MIT | runtime |  |
| typer | 0.28.0 | MIT | runtime |  |
| types-protobuf | 7.35.1.20260906 | Apache-2.0 | runtime |  |
| typing_extensions | 4.16.0 | PSF-2.0 | runtime |  |
| typing-inspection | 0.4.5 | MIT | runtime |  |
| urllib3 | 2.8.0 | MIT | runtime |  |
| uvicorn | 0.54.0 | BSD-3-Clause | runtime |  |

81 distributions (68 runtime, 13 dev) from Python 3.12.8.
