# Where the Temporal server comes from

`lab init` needs the pinned Temporal server (`temporal-server`, `temporal-sql-tool`, version 1.32.2). It accepts three sources, in this order of trust. `lab doctor` reports which one was used (`temporal-binary … provenance: …`), and `EIDOS_HOME/services/temporal/provenance.json` records it.

1. **Pinned release archive (default).**
   - Downloaded from GitHub's release CDN. Transient failures are retried with backoff.
   - The archive is **always** checked against the sha256 pinned in `src/eidos/supervisor/services.py`, and a mismatch aborts.
   - If `EIDOS_TEMPORAL_ARCHIVE_CACHE=DIR` is set, a verified copy is kept there and reused. A cached copy is re-verified before use.
   - Provenance: `release-archive`.
2. **The same archive, fetched by you.** Download it on another machine or later. Then either:
   - put it at `$EIDOS_TEMPORAL_ARCHIVE_CACHE/temporal-server-<sha256>.tgz`, which is verified like (1); or
   - extract the two binaries and pass `--temporal-source DIR`.
3. **Built from source, when the release CDN is unavailable.**

   ```bash
   FORCE_SOURCE_BUILD=1 TEMPORAL_SRC_OUT=$SSD/temporal-src bash scripts/ci/temporal_server.sh
   lab init --temporal-source $SSD/temporal-src ...
   ```

   - The script runs `go install go.temporal.io/server/cmd/server@v1.32.2` and the matching `cmd/tools/sql`, through the Go module proxy, with module integrity verified by the Go checksum database.
   - With `GOTOOLCHAIN=auto`, Go fetches the toolchain the module requires (Go 1.26.10).
   - It writes a `provenance.json` (`kind: source-built`, Go version, `go version -m` build info, binary sha256s).
   - `lab init --temporal-source` is the explicit opt-in. The lab records the binaries as `operator-supplied`, with your declared provenance and their sha256s. **They are not the checksum-pinned release**, so treat qualification results obtained with them as such.

**CI:**
- Every lab job restores the verified archive from `actions/cache` (keyed by OS, arch and the pinned table). It falls back to the pinned download, then to the source build.
- The `temporal-source-fallback` job always builds from source and runs the full lab lifecycle on the result.
- uv comes from PyPI at a pinned version when `setup-uv`'s GitHub download fails.

**Proven here, and not:**
- This build sandbox cannot fetch the Go toolchain (`storage.googleapis.com` is blocked). The source build is therefore proven only by the CI job, and the provenance recording is unit-level here.
