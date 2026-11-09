# 0012: Provider keys in the OS secret store

Status: accepted (2026-11-09, release 1.1.0).

**Context.** Until 1.1.0 an operator passed provider keys to `lab start` through its environment or
`--credentials-stdin`. The design asks to "keep provider keys in the operator's secret store". Keys must keep
reaching only the investigator worker (the v1.0.0 fix: a scrubbed supervisor environment, keys over a stdin pipe,
kept in memory, added only to the investigator worker's environment).

**Decision.**
1. `lab credentials set|delete|status <provider>` (`anthropic`, `openai`, `openai-compatible`). `set` reads the key
   from stdin when piped, otherwise from a hidden prompt (`getpass`); there is no argument or option for it.
   `status` reports whether a key is stored, never the value.
2. Backends (`eidos.supervisor.secret_store`):
   - macOS: the Keychain through `/usr/bin/security`. The value is written with `security -i`, which reads the
     command from stdin, so the key never appears in any process's argv. `EIDOS_KEYCHAIN` may name a dedicated
     keychain file.
   - Linux: libsecret through `secret-tool` (the value goes to its stdin), which needs a running Secret Service.
   - Neither available: a clear error. **There is no plaintext fallback**; `--credentials-stdin` remains.
3. Items: service `eidos/<lab id>/<provider>`, account the variable name (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`).
   The lab id is derived from the lab's home path, so labs on one machine keep separate keys; a lab restored into
   another directory needs its keys set again.
4. `lab start` without `--credentials-stdin` reads every stored key and hands them to the supervisor exactly as
   before. A variable exported in the shell wins over the stored key. The start output lists names and sources
   (`"os-secret-store"`, `"stdin"`), never values. If no store is available, start proceeds without stored keys.
5. Values never appear in errors, logs, receipts or output: error messages are redacted, `security`'s stderr is
   not passed on for writes (it can quote the command line), and keys are limited to 1-1024 printable ASCII
   characters without whitespace or quotes (the `security -i` command line is ~4 KiB).

**Tests.** `tests/unit/test_secret_store.py`: an in-memory backend, the CLI, `lab start`, leak checks (the planted
value never appears in stdout, stderr, logs or argv), a real round trip against a **temporary keychain file** created
and deleted by the test (never the login keychain), and a real `secret-tool` round trip. CI: `secret-store-macos`
(the real keychain on the macOS runner) and `secret-store-linux` (`dbus-run-session` + an unlocked
`gnome-keyring-daemon`, with `EIDOS_REQUIRE_SECRET_TOOL=1` so a missing Secret Service fails instead of skipping).

**Alternatives.** The `keyring` Python package: an extra server dependency with backend auto-selection that can
silently fall back to plaintext or encrypted-file stores; rejected. A lab-encrypted file: still a key next to the
data it protects; rejected.

**Reversal.** If a platform gains a better native store (e.g. systemd credentials), add a backend; the CLI and the
item naming stay.
